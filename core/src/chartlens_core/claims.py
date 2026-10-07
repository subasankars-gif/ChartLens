"""Explanation claims (ADR-0028): the model, the template set, the renderer and the
validator, shared by the engine's ``explain`` (which selects), the ANALYSIS job stage
(which writes), the publisher (which verifies before anything goes live) and the tests.
One algorithm, so their interpretations cannot drift apart.

> An explanation restates published analytical facts in words. It never adds a fact,
> a number, a judgement or an order that the analysis does not already contain.

A claim is auditable on its own:

- ``references``: the objects it relies on, each a JSON pointer into the **one** analysis
  document the explanation is bound to (its ``document_sha256``), with the object's id.
  An object carrying an id field must carry exactly that id;
- ``quoted_values``: every value it uses, each the stored value **verbatim**, located by
  its reference and a field pointer;
- ``rendered_text`` = :func:`render` (template, quoted values), nothing else.

:func:`validate` re-resolves and re-renders every claim against the bound document.
Generic: no analytical rule lives here, only JSON values, pointers and templates.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from chartlens_core.canonical import canonical_json, content_hash

CLAIM_SCHEMA_VERSION: Final = "1"
EXPLANATION_SCHEMA_VERSION: Final = "1"
EXPLANATION_KEY_VERSION: Final = "1"

Kind = Literal["date", "price", "value", "ratio", "int", "enum", "code", "bool", "id"]
"""How a quoted value is rendered: ``date`` ISO text; ``price`` a stored price's exact
canonical text; ``value`` a derived value to 4 decimals (ADR-0026 K7); ``ratio`` its
canonical text; ``int`` decimal; ``enum`` a stored code as lower-case words; ``code``
verbatim. ``bool`` and ``id`` are quoted to justify a claim and never rendered."""

RENDERED_KINDS: Final = frozenset({"date", "price", "value", "ratio", "int", "enum", "code"})

ID_FIELDS: Final = (
    "swing_id",
    "zone_id",
    "trendline_id",
    "fib_id",
    "pattern_id",
    "event_id",
    "divergence_id",
    "level_id",
)
"""Fields that name an object: a reference to an object carrying one must use it."""

FORBIDDEN_WORDS: Final = (
    "buy",
    "sell",
    "hold",
    "guarantee",
    "guaranteed",
    "will",
    "must",
    "should",
    "likely",
    "probable",
    "probability",
    "target",
    "recommend",
    "recommendation",
    "best",
    "strongest",
    "signal",
    "confidence",
    "expect",
    "expected",
)
"""The last guardrail (ADR-0028 clarification 8), not the semantic control."""

TEMPLATES: Final[Mapping[str, str]] = {
    "DATA_CONTEXT": "Weekly analysis as of {as_of}, using the history usable from {usable_from}.",
    "DATA_CONTEXT_AS_OF": "Weekly analysis as of {as_of}.",
    "FORMING_WEEK": "The last week is still forming, and nothing is confirmed by it.",
    "TREND_STATE": (
        "Market structure: {state} since {since}; the last structure event is a {kind} "
        "{direction} on {event_date} at the level {level}."
    ),
    "TREND_STATE_NO_EVENT": "Market structure: {state} since {since}.",
    "ZONE": (
        "{side} zone from {price_low} to {price_high}, first seen {first_seen}, known since "
        "{known_at}, last tested {last_tested}."
    ),
    "ZONE_UNTESTED": (
        "{side} zone from {price_low} to {price_high}, first seen {first_seen}, known since "
        "{known_at}; no test is stored."
    ),
    "ZONE_ROLE_REVERSED": "The zone from {price_low} to {price_high} has reversed its role.",
    "ACTIVE_TRENDLINE": (
        "{side} trendline known since {known_at}, stored from {first_value} ({first_date}) "
        "to {last_value} ({last_date}); its stored value on {state_date} is {value}."
    ),
    "FIBONACCI": (
        "Fibonacci {direction} leg ({method}, {sensitivity}) from {anchor_price} ({anchor_date}) "
        "to {counter_price} ({counter_date}), known since {known_at}; stored status {status} "
        "since {status_date}."
    ),
    "FIBONACCI_LEVEL": "Fibonacci level {ratio}: {price} ({level_kind}).",
    "PATTERN": (
        "{pattern_type} formed from {start_date} to {end_date} and recognised on {known_at}; "
        "stored status {status} since {status_date}."
    ),
    "PATTERN_CONFIRMATION_LEVEL": "Stored confirmation level: {level}.",
    "PATTERN_CONFIRMATION_LINE": (
        "Stored confirmation boundary: the {line} line, from {start_value} ({start_date}) "
        "to {end_value} ({end_date})."
    ),
    "PATTERN_INVALIDATION_LEVEL": "Stored invalidation level: {level}.",
    "PATTERN_INVALIDATION_LINE": (
        "Stored invalidation boundary: the {line} line, from {start_value} ({start_date}) "
        "to {end_value} ({end_date})."
    ),
    "PATTERN_MEASURED_MOVE": (
        "Stored measured-move zone: {target_low} to {target_high}, calculated on {calculated_at}."
    ),
    "PATTERN_FIT": (
        "Definition fit {fit}: how closely the stored geometry meets the pattern's "
        "definition, not a likelihood."
    ),
    "PATTERN_TAG": "Relevance tag: {tag}.",
}

_SLOT = re.compile(r"\{([a-z_]+)\}")
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ClaimError(ValueError):
    """A claim cannot be rendered or does not hold against its document."""


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Reference(_Model):
    id: str
    """The object's id field, or the pointer itself for an object without one."""
    pointer: str
    """RFC 6901 pointer from the document root."""


class QuotedValue(_Model):
    name: str
    ref: str
    """The pointer of one of the claim's references."""
    field: str
    """RFC 6901 pointer relative to that object (``""`` for the object itself)."""
    kind: Kind
    value: Any
    """The stored value, verbatim."""


class Claim(_Model):
    claim_id: str
    claim_type: str
    template_id: str
    subject: str
    """The pointer of the reference the claim is about."""
    references: list[Reference]
    quoted_values: list[QuotedValue]
    rendered_text: str
    known_at: str | None
    """Equal to the quoted value named ``known_at`` (None when there is none)."""
    provisional: bool
    """Equal to the quoted value named ``provisional`` (False when there is none)."""


class Explanation(_Model):
    explanation_schema_version: str = EXPLANATION_SCHEMA_VERSION
    explain_version: str
    explanation_key: str
    security_id: str
    continuity_segment_id: str
    document_sha256: str
    claims: list[Claim]


# ----------------------------------------------------------------------------- rendering


def render_value(kind: Kind, value: Any) -> str:
    """One display rule per kind; anything else is refused."""
    if kind == "date" and isinstance(value, str) and _ISO.match(value):
        return value
    if kind in ("price", "ratio") and _is_number(value):
        return canonical_json(value).decode()
    if kind == "value" and _is_number(value):
        return f"{float(value):.4f}"
    if kind == "int" and isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if kind == "enum" and isinstance(value, str):
        return value.replace("_", " ").lower()
    if kind == "code" and isinstance(value, str):
        return value
    raise ClaimError(f"a {kind} slot cannot render {value!r}")


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def render(template_id: str, quoted: Sequence[QuotedValue]) -> str:
    """The text of a claim: its template with each slot filled by the quoted value of
    that name, rendered by its kind. The first character is upper-cased."""
    template = TEMPLATES.get(template_id)
    if template is None:
        raise ClaimError(f"unknown template {template_id}")
    by_name = {q.name: q for q in quoted}

    def fill(match: re.Match[str]) -> str:
        q = by_name.get(match.group(1))
        if q is None:
            raise ClaimError(f"{template_id}: no quoted value for {{{match.group(1)}}}")
        if q.kind not in RENDERED_KINDS:
            raise ClaimError(f"{template_id}: {q.name} is a {q.kind}, not rendered")
        return render_value(q.kind, q.value)

    text = _SLOT.sub(fill, template)
    return text[:1].upper() + text[1:]


def template_problems(templates: Mapping[str, str] = TEMPLATES) -> list[str]:
    """Static rules: no digits (every number in text comes from a quoted value) and no
    forbidden word in any template."""
    problems: list[str] = []
    for tid, text in templates.items():
        fixed = _SLOT.sub("", text)
        if any(ch.isdigit() for ch in fixed):
            problems.append(f"{tid}: a template contains a digit")
        problems += [f"{tid}: {w}" for w in forbidden_words(fixed)]
    return problems


def forbidden_words(text: str) -> list[str]:
    lowered = text.lower()
    return [w for w in FORBIDDEN_WORDS if re.search(rf"\b{w}\b", lowered)]


def template_set_hash(templates: Mapping[str, str] = TEMPLATES) -> str:
    return content_hash(dict(templates))


def explanation_key(security_id: str, document_sha256: str, explain_version: str) -> str:
    """The complete dependency fingerprint of an explanation (ADR-0028 §4)."""
    return content_hash(
        {
            "key_version": EXPLANATION_KEY_VERSION,
            "security_id": security_id,
            "document_sha256": document_sha256,
            "explain_version": explain_version,
        }
    )


# ----------------------------------------------------------------------------- resolving


class _Missing:
    pass


MISSING: Final = _Missing()


def resolve(document: Any, pointer: str) -> Any:
    """RFC 6901 (``~0`` and ``~1`` escapes); :data:`MISSING` if absent."""
    if pointer == "":
        return document
    if not pointer.startswith("/"):
        return MISSING
    node = document
    for raw in pointer[1:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict):
            if token not in node:
                return MISSING
            node = node[token]
        elif isinstance(node, list):
            if not token.isdigit() or int(token) >= len(node):
                return MISSING
            node = node[int(token)]
        else:
            return MISSING
    return node


def object_id(obj: Any, pointer: str) -> str:
    """The id a reference to ``obj`` must carry."""
    if isinstance(obj, dict):
        for name in ID_FIELDS:
            if name in obj and isinstance(obj[name], str):
                return obj[name]
    return pointer


def _kind_holds(kind: Kind, value: Any) -> bool:
    if kind == "date":
        return isinstance(value, str) and bool(_ISO.match(value))
    if kind in ("price", "value", "ratio"):
        return _is_number(value)
    if kind == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "bool":
        return isinstance(value, bool)
    return isinstance(value, str)


# ----------------------------------------------------------------------------- validating


def claim_problems(claim: Claim, document: Any) -> list[str]:
    """Every way a claim fails to hold against its bound document."""
    out: list[str] = []
    cid = claim.claim_id
    pointers = [r.pointer for r in claim.references]
    if len(set(pointers)) != len(pointers):
        out.append(f"{cid}: a reference appears twice")
    for r in claim.references:
        obj = resolve(document, r.pointer)
        if obj is MISSING:
            out.append(f"{cid}: reference {r.pointer} does not resolve")
        elif object_id(obj, r.pointer) != r.id:
            out.append(f"{cid}: {r.pointer} is {object_id(obj, r.pointer)}, not {r.id}")
    if claim.subject not in pointers:
        out.append(f"{cid}: the subject is not a reference")
    names = [q.name for q in claim.quoted_values]
    if len(set(names)) != len(names):
        out.append(f"{cid}: a quoted value name appears twice")
    for q in claim.quoted_values:
        if q.ref not in pointers:
            out.append(f"{cid}: {q.name} quotes an object it does not reference")
            continue
        stored = resolve(document, q.ref + q.field)
        if stored is MISSING:
            out.append(f"{cid}: {q.name} ({q.ref}{q.field}) does not resolve")
        elif canonical_json(stored) != canonical_json(q.value):
            out.append(f"{cid}: {q.name} is not the stored value at {q.ref}{q.field}")
        elif not _kind_holds(q.kind, q.value):
            out.append(f"{cid}: {q.name} is not a {q.kind}")
    try:
        if render(claim.template_id, claim.quoted_values) != claim.rendered_text:
            out.append(f"{cid}: the text is not its template rendered with its quoted values")
    except ClaimError as exc:
        out.append(f"{cid}: {exc}")
    by_name = {q.name: q.value for q in claim.quoted_values}
    if claim.known_at != by_name.get("known_at"):
        out.append(f"{cid}: known_at is not the quoted known_at")
    if claim.provisional != bool(by_name.get("provisional", False)):
        out.append(f"{cid}: provisional is not the quoted provisional flag")
    out += [f"{cid}: forbidden word {w!r}" for w in forbidden_words(claim.rendered_text)]
    return out


def validate(
    explanation: Mapping[str, Any] | Explanation,
    document: Mapping[str, Any],
    document_sha256: str,
) -> list[str]:
    """Every problem with an explanation against the exact document it must be bound to
    (ADR-0028 §6 provenance invariant). Empty when it holds."""
    try:
        ex = (
            explanation
            if isinstance(explanation, Explanation)
            else Explanation.model_validate(explanation)
        )
    except ValidationError as exc:
        return [f"not an explanation ({exc.error_count()} errors)"]
    out: list[str] = []
    if ex.explanation_schema_version != EXPLANATION_SCHEMA_VERSION:
        out.append(f"explanation schema {ex.explanation_schema_version} is not supported")
    if ex.document_sha256 != document_sha256:
        out.append("bound to another analysis document")
    identity = document.get("identity", {})
    if ex.security_id != identity.get("security_id"):
        out.append("names another security than its document")
    if ex.continuity_segment_id != identity.get("continuity_segment_id"):
        out.append("names another segment than its document")
    if ex.explanation_key != explanation_key(
        ex.security_id, ex.document_sha256, ex.explain_version
    ):
        out.append("its explanation_key is not its dependency fingerprint")
    ids = [c.claim_id for c in ex.claims]
    if len(set(ids)) != len(ids):
        out.append("a claim id appears twice")
    for claim in ex.claims:
        out += claim_problems(claim, document)
    return out


def explanation_bytes(explanation: Explanation) -> bytes:
    return canonical_json(explanation.model_dump(mode="json"))
