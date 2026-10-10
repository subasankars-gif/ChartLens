"""Explanations (ADR-0028): the claims a published analysis document supports, about its
current state, in the engine's own order.

> An explanation restates published analytical facts in words. It never adds a fact,
> a number, a judgement or an order that the analysis does not already contain.

:func:`explain` reads one analysis document (the JSON of its verified canonical bytes)
and **selects**: which stored objects (the ``current`` lists, the trend, the data
context) and which of their stored fields. Every value it uses is quoted verbatim with
its location; the words come from the closed template set in
:mod:`chartlens_core.claims`, which also renders and validates. There is no arithmetic
(no durations, distances, counts or line values at later weeks), no ordering of its own
and no judgement. Divergences are not explained until the engine publishes a current
list (clarification 3); conditions are quoted only where the engine stored them
(clarification 4).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from chartlens_core.canonical import content_hash
from chartlens_core.claims import (
    CLAIM_SCHEMA_VERSION,
    TEMPLATES,
    Claim,
    ClaimError,
    Explanation,
    Kind,
    QuotedValue,
    Reference,
    explanation_key,
    object_id,
    render,
    resolve,
    template_set_hash,
)

SELECTION_RULES_VERSION: Final = "1"
"""What is explained and from which fields (§3). Changing it changes ``explain_version``."""


def explain_version() -> str:
    """The explanation methodology: claim schema, selection rules and the full template
    set. A template change cannot ship without a new version (ADR-0028 §4)."""
    digest = content_hash(
        {
            "claim_schema_version": CLAIM_SCHEMA_VERSION,
            "selection_rules_version": SELECTION_RULES_VERSION,
            "templates": template_set_hash(TEMPLATES),
        }
    )
    return f"explain-{digest[:12]}"


class _Builder:
    def __init__(self, doc: Mapping[str, Any], claim_type: str, subject: str) -> None:
        self.doc = doc
        self.claim_type = claim_type
        self.subject = subject
        self.refs: dict[str, Reference] = {}
        self.quoted: list[QuotedValue] = []
        self.ref(subject)

    def ref(self, pointer: str) -> str:
        if pointer not in self.refs:
            obj = resolve(self.doc, pointer)
            self.refs[pointer] = Reference(id=object_id(obj, pointer), pointer=pointer)
        return pointer

    def quote(self, name: str, field: str, kind: Kind, *, at: str | None = None) -> Any:
        pointer = self.ref(at or self.subject)
        value = resolve(self.doc, pointer + field)
        self.quoted.append(QuotedValue(name=name, ref=pointer, field=field, kind=kind, value=value))
        return value

    def build(self, template_id: str, claim_id: str) -> Claim:
        by_name = {q.name: q.value for q in self.quoted}
        return Claim(
            claim_id=claim_id,
            claim_type=self.claim_type,
            template_id=template_id,
            subject=self.subject,
            references=list(self.refs.values()),
            quoted_values=list(self.quoted),
            rendered_text=render(template_id, self.quoted),
            known_at=by_name.get("known_at"),
            provisional=bool(by_name.get("provisional", False)),
        )


def _last(items: list[Any]) -> int:
    """The position of a stored list's last entry (the only arithmetic here: a position,
    never a value; the static test allows it in this function alone)."""
    return len(items) - 1


def _index(items: list[dict[str, Any]], key: str, value: str, where: str) -> int:
    for i, item in enumerate(items):
        if item.get(key) == value:
            return i
    raise ClaimError(f"{where}: {value} is listed as current but not stored")


def _data_context(doc: Mapping[str, Any]) -> list[Claim]:
    b = _Builder(doc, "DATA_CONTEXT", "/identity")
    b.quote("as_of", "/as_of", "date")
    claims: list[Claim] = []
    if resolve(doc, "/inputs/usable_from") is not None:
        b.quote("usable_from", "/usable_from", "date", at="/inputs")
        claims.append(b.build("DATA_CONTEXT", "DATA_CONTEXT"))
    else:
        claims.append(b.build("DATA_CONTEXT_AS_OF", "DATA_CONTEXT"))
    if resolve(doc, "/identity/forming_week_present") is True:
        f = _Builder(doc, "FORMING_WEEK", "/identity")
        f.quote("forming_week_present", "/forming_week_present", "bool")
        claims.append(f.build("FORMING_WEEK", "FORMING_WEEK"))
    return claims


def _trend(doc: Mapping[str, Any]) -> list[Claim]:
    trend = resolve(doc, "/structure/trend")
    if not isinstance(trend, dict):
        return []
    b = _Builder(doc, "TREND_STATE", "/structure/trend")
    b.quote("state", "/state", "enum")
    b.quote("since", "/since", "date")
    b.quote("known_at", "/since", "date")
    b.quote("provisional", "/provisional", "bool")
    event_id = trend.get("last_event_id")
    if event_id is None:
        return [b.build("TREND_STATE_NO_EVENT", "TREND_STATE")]
    i = _index(doc["structure"]["events"], "event_id", event_id, "structure.events")
    event = f"/structure/events/{i}"
    b.quote("kind", "/kind", "code", at=event)
    b.quote("direction", "/direction", "enum", at=event)
    b.quote("event_date", "/bar_date", "date", at=event)
    b.quote("level", "/level", "price", at=event)
    return [b.build("TREND_STATE", "TREND_STATE")]


def _zones(doc: Mapping[str, Any]) -> list[Claim]:
    claims: list[Claim] = []
    zones = doc["levels"]["zones"]
    for zone_id in doc["current"]["zone_ids"]:
        z = f"/levels/zones/{_index(zones, 'zone_id', zone_id, 'levels.zones')}"
        b = _Builder(doc, "ZONE", z)
        b.quote("side", "/type", "enum")
        b.quote("price_low", "/price_low", "value")
        b.quote("price_high", "/price_high", "value")
        b.quote("first_seen", "/first_seen", "date")
        b.quote("known_at", "/known_at", "date")
        if resolve(doc, z + "/last_tested") is not None:
            b.quote("last_tested", "/last_tested", "date")
            claims.append(b.build("ZONE", f"ZONE:{zone_id}"))
        else:
            claims.append(b.build("ZONE_UNTESTED", f"ZONE:{zone_id}"))
        if resolve(doc, z + "/role_reversed") is True:
            r = _Builder(doc, "ZONE_ROLE_REVERSED", z)
            r.quote("role_reversed", "/role_reversed", "bool")
            r.quote("price_low", "/price_low", "value")
            r.quote("price_high", "/price_high", "value")
            r.quote("known_at", "/known_at", "date")
            claims.append(r.build("ZONE_ROLE_REVERSED", f"ZONE_ROLE_REVERSED:{zone_id}"))
    return claims


def _trendlines(doc: Mapping[str, Any]) -> list[Claim]:
    claims: list[Claim] = []
    levels = doc["levels"]
    for line_id in doc["current"]["active_trendline_ids"]:
        i = _index(levels["trendlines"], "trendline_id", line_id, "levels.trendlines")
        j = _index(levels["active_trendlines"], "trendline_id", line_id, "active_trendlines")
        t = f"/levels/trendlines/{i}"
        a = f"/levels/active_trendlines/{j}"
        touches = resolve(doc, t + "/touches")
        history = resolve(doc, t + "/status_history")
        if not touches or not history:
            raise ClaimError(f"{line_id}: an active trendline without touches or status")
        last = _last(touches)
        b = _Builder(doc, "ACTIVE_TRENDLINE", t)
        b.quote("side", "/type", "enum")
        b.quote("known_at", "/known_at", "date")
        b.quote("first_date", "/touches/0/bar_date", "date")
        b.quote("first_value", "/touches/0/line_value", "value")
        b.quote("last_date", f"/touches/{last}/bar_date", "date")
        b.quote("last_value", f"/touches/{last}/line_value", "value")
        b.quote("provisional", f"/status_history/{_last(history)}/provisional", "bool")
        b.quote("value", "/value", "value", at=a)
        b.quote("state_date", "/state_date", "date", at="/levels")
        claims.append(b.build("ACTIVE_TRENDLINE", f"ACTIVE_TRENDLINE:{line_id}"))
    return claims


def _fibonacci(doc: Mapping[str, Any]) -> list[Claim]:
    claims: list[Claim] = []
    structures = doc["fibonacci"]["structures"]
    for fib_id in doc["current"]["fibonacci_ids"]:
        f = f"/fibonacci/structures/{_index(structures, 'fib_id', fib_id, 'fibonacci.structures')}"
        history = resolve(doc, f + "/status_history")
        last = _last(history)
        b = _Builder(doc, "FIBONACCI", f)
        b.quote("direction", "/direction", "enum")
        b.quote("method", "/method", "code")
        b.quote("sensitivity", "/sensitivity", "enum")
        b.quote("anchor_price", "/anchor_price", "price")
        b.quote("anchor_date", "/anchor_bar_date", "date")
        b.quote("counter_price", "/counter_price", "price")
        b.quote("counter_date", "/counter_bar_date", "date")
        b.quote("known_at", "/known_at", "date")
        b.quote("status", f"/status_history/{last}/status", "enum")
        b.quote("status_date", f"/status_history/{last}/date", "date")
        b.quote("provisional", f"/status_history/{last}/provisional", "bool")
        claims.append(b.build("FIBONACCI", f"FIBONACCI:{fib_id}"))
        for i, _ in enumerate(resolve(doc, f + "/levels")):
            lv = _Builder(doc, "FIBONACCI_LEVEL", f)
            lv.quote("ratio", f"/levels/{i}/ratio", "ratio")
            lv.quote("price", f"/levels/{i}/price", "value")
            lv.quote("level_kind", f"/levels/{i}/kind", "enum")
            lv.quote("known_at", "/known_at", "date")
            claims.append(lv.build("FIBONACCI_LEVEL", f"FIBONACCI_LEVEL:{fib_id}:{i}"))
    return claims


def _condition(doc: Mapping[str, Any], p: str, pid: str, side: str) -> list[Claim]:
    """A stored level when there is one, otherwise the stored boundary line it names;
    nothing when neither is stored. Never reconstructed (clarification 4)."""
    upper = side.upper()
    claim_type = f"PATTERN_{upper}"
    if resolve(doc, f"{p}/geometry/{side}_level") is not None:
        b = _Builder(doc, claim_type, p)
        b.quote("level", f"/geometry/{side}_level", "value")
        b.quote("known_at", "/known_at", "date")
        return [b.build(f"PATTERN_{upper}_LEVEL", f"{claim_type}:{pid}")]
    name = resolve(doc, f"{p}/geometry/{side}_line")
    if name is None:
        return []
    lines = resolve(doc, f"{p}/geometry/lines")
    j = _index(lines, "label", name, f"{pid} geometry.lines")
    b = _Builder(doc, claim_type, p)
    b.quote("line_name", f"/geometry/{side}_line", "code")
    b.quote("line", f"/geometry/lines/{j}/label", "enum")
    b.quote("start_value", f"/geometry/lines/{j}/start_value", "value")
    b.quote("start_date", f"/geometry/lines/{j}/start_date", "date")
    b.quote("end_value", f"/geometry/lines/{j}/end_value", "value")
    b.quote("end_date", f"/geometry/lines/{j}/end_date", "date")
    b.quote("known_at", "/known_at", "date")
    return [b.build(f"PATTERN_{upper}_LINE", f"{claim_type}:{pid}")]


def _patterns(doc: Mapping[str, Any]) -> list[Claim]:
    claims: list[Claim] = []
    patterns = doc["patterns"]["patterns"]
    relevance = doc["relevance"]["relevance"]
    for pid in doc["current"]["included_pattern_ids"]:
        p = f"/patterns/patterns/{_index(patterns, 'pattern_id', pid, 'patterns.patterns')}"
        history = resolve(doc, p + "/status_history")
        last = _last(history)
        b = _Builder(doc, "PATTERN", p)
        b.quote("pattern_type", "/pattern_type", "enum")
        b.quote("start_date", "/start_date", "date")
        b.quote("end_date", "/end_date", "date")
        b.quote("known_at", "/known_at", "date")
        b.quote("status", f"/status_history/{last}/status", "enum")
        b.quote("status_date", f"/status_history/{last}/effective_date", "date")
        b.quote("provisional", f"/status_history/{last}/provisional", "bool")
        claims.append(b.build("PATTERN", f"PATTERN:{pid}"))
        claims.extend(_condition(doc, p, pid, "confirmation"))
        claims.extend(_condition(doc, p, pid, "invalidation"))
        with_move = [k for k, entry in enumerate(history) if entry.get("measured_move")]
        if with_move:
            k = with_move[-1]
            m = _Builder(doc, "PATTERN_MEASURED_MOVE", p)
            m.quote("target_low", f"/status_history/{k}/measured_move/target_low", "value")
            m.quote("target_high", f"/status_history/{k}/measured_move/target_high", "value")
            m.quote(
                "calculated_at", f"/status_history/{k}/measured_move/target_calculated_at", "date"
            )
            m.quote("known_at", f"/status_history/{k}/known_at", "date")
            m.quote("provisional", f"/status_history/{k}/provisional", "bool")
            claims.append(m.build("PATTERN_MEASURED_MOVE", f"PATTERN_MEASURED_MOVE:{pid}"))
        fit = _Builder(doc, "PATTERN_FIT", p)
        fit.quote("fit", "/definition_fit/value", "int")
        fit.quote("known_at", "/known_at", "date")
        claims.append(fit.build("PATTERN_FIT", f"PATTERN_FIT:{pid}"))
        r = f"/relevance/relevance/{_index(relevance, 'pattern_id', pid, 'relevance.relevance')}"
        entries = resolve(doc, r + "/history")
        h = _last(entries)
        for t, _ in enumerate(entries[h]["tags"]):
            tag = _Builder(doc, "PATTERN_TAG", r)
            tag.ref(p)
            tag.quote("tag", f"/history/{h}/tags/{t}/tag", "enum")
            tag.quote("known_at", f"/history/{h}/relevance_known_at", "date")
            claims.append(tag.build("PATTERN_TAG", f"PATTERN_TAG:{pid}:{t}"))
    return claims


def explain(document: Mapping[str, Any], document_sha256: str) -> Explanation:
    """The explanation of one published analysis document, bound to its address. Claims
    follow the engine's order: the data context, the trend, then the ``current`` lists in
    their stored order (zones, active trendlines, Fibonacci, included patterns)."""
    identity = document["identity"]
    version = explain_version()
    claims = [
        *_data_context(document),
        *_trend(document),
        *_zones(document),
        *_trendlines(document),
        *_fibonacci(document),
        *_patterns(document),
    ]
    return Explanation(
        explain_version=version,
        explanation_key=explanation_key(identity["security_id"], document_sha256, version),
        security_id=identity["security_id"],
        continuity_segment_id=identity["continuity_segment_id"],
        document_sha256=document_sha256,
        claims=claims,
    )


__all__ = ["SELECTION_RULES_VERSION", "explain", "explain_version"]
