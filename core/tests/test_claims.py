"""The shared claim checker (ADR-0028): templates, rendering, resolving and validation.
Each tampering a reviewer could worry about is refused by the same validator the job
stage, the publisher and the tests use."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from chartlens_core.canonical import canonical_json
from chartlens_core.claims import (
    MISSING,
    TEMPLATES,
    Claim,
    ClaimError,
    Explanation,
    QuotedValue,
    Reference,
    explanation_key,
    forbidden_words,
    render,
    render_value,
    resolve,
    template_problems,
    validate,
)

DOC: dict[str, Any] = {
    "identity": {
        "security_id": "SEC-1",
        "continuity_segment_id": "SEC-1@2010-01-04",
        "as_of": "2024-05-31",
    },
    "levels": {
        "state_date": "2024-05-31",
        "zones": [
            {
                "zone_id": "Z1",
                "type": "SUPPORT",
                "price_low": 98.123456,
                "price_high": 99.5,
                "first_seen": "2023-01-06",
                "known_at": "2023-02-10",
                "last_tested": None,
            }
        ],
    },
    "odd/key": {"x~y": 7},
}
SHA = "a" * 64


def zone_claim(**over: Any) -> Claim:
    ref = Reference(id="Z1", pointer="/levels/zones/0")
    quoted = [
        QuotedValue(name="side", ref=ref.pointer, field="/type", kind="enum", value="SUPPORT"),
        QuotedValue(
            name="price_low", ref=ref.pointer, field="/price_low", kind="value", value=98.123456
        ),
        QuotedValue(
            name="price_high", ref=ref.pointer, field="/price_high", kind="value", value=99.5
        ),
        QuotedValue(
            name="first_seen", ref=ref.pointer, field="/first_seen", kind="date", value="2023-01-06"
        ),
        QuotedValue(
            name="known_at", ref=ref.pointer, field="/known_at", kind="date", value="2023-02-10"
        ),
    ]
    fields: dict[str, Any] = {
        "claim_id": "ZONE:Z1",
        "claim_type": "ZONE",
        "template_id": "ZONE_UNTESTED",
        "subject": ref.pointer,
        "references": [ref],
        "quoted_values": quoted,
        "rendered_text": render("ZONE_UNTESTED", quoted),
        "known_at": "2023-02-10",
        "provisional": False,
    }
    fields.update(over)
    return Claim(**fields)


def explanation(*claims: Claim, **over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "explain_version": "explain-test",
        "explanation_key": explanation_key("SEC-1", SHA, "explain-test"),
        "security_id": "SEC-1",
        "continuity_segment_id": "SEC-1@2010-01-04",
        "document_sha256": SHA,
        "claims": [c.model_dump(mode="json") for c in claims],
    }
    body.update(over)
    return Explanation.model_validate(body).model_dump(mode="json")


# ----------------------------------------------------------------------------- templates


def test_templates_hold_no_number_and_no_forbidden_word() -> None:
    assert template_problems() == []
    assert template_problems({"BAD": "Buy above 100."}) == [
        "BAD: a template contains a digit",
        "BAD: buy",
    ]


def test_rendering_is_one_rule_per_kind() -> None:
    assert render_value("value", 98.123456) == "98.1235"
    assert render_value("price", 98.123456) == "98.123456"  # a stored price: exact text
    assert render_value("ratio", 0.236) == "0.236"
    assert render_value("enum", "STRONG_DOWNTREND") == "strong downtrend"
    assert render_value("code", "CHoCH") == "CHoCH"
    assert render_value("date", "2024-05-31") == "2024-05-31"
    assert render_value("int", 72) == "72"
    for kind, value in (("date", "31/05/2024"), ("int", True), ("value", "1.0"), ("bool", True)):
        with pytest.raises(ClaimError):
            render_value(kind, value)  # type: ignore[arg-type]


def test_render_fills_slots_from_quoted_values_only() -> None:
    claim = zone_claim()
    assert claim.rendered_text == (
        "Support zone from 98.1235 to 99.5000, first seen 2023-01-06, known since "
        "2023-02-10; no test is stored."
    )
    with pytest.raises(ClaimError, match="no quoted value"):
        render("ZONE", claim.quoted_values)  # needs last_tested
    with pytest.raises(ClaimError, match="unknown template"):
        render("NOPE", [])


def test_resolve_follows_rfc6901() -> None:
    assert resolve(DOC, "/levels/zones/0/zone_id") == "Z1"
    assert resolve(DOC, "/odd~1key/x~0y") == 7
    assert resolve(DOC, "") is DOC
    for missing in ("/levels/zones/1", "/levels/zones/x", "levels", "/nope"):
        assert resolve(DOC, missing) is MISSING


def test_forbidden_words_match_whole_words() -> None:
    assert forbidden_words("not a likelihood") == []
    assert forbidden_words("a likely move; Hold") == ["hold", "likely"]


# ----------------------------------------------------------------------------- validation


def test_a_true_claim_validates() -> None:
    assert validate(explanation(zone_claim()), DOC, SHA) == []


@pytest.mark.parametrize(
    ("tamper", "message"),
    [
        (
            lambda c: c.model_copy(update={"rendered_text": c.rendered_text + " Buy."}),
            "not its template",
        ),
        (
            lambda c: c.model_copy(
                update={
                    "quoted_values": [
                        q.model_copy(update={"value": 98.2}) if q.name == "price_low" else q
                        for q in c.quoted_values
                    ]
                }
            ),
            "not the stored value",
        ),
        (
            lambda c: c.model_copy(
                update={"references": [Reference(id="Z9", pointer="/levels/zones/0")]}
            ),
            "is Z1, not Z9",
        ),
        (
            lambda c: c.model_copy(
                update={"references": [Reference(id="Z1", pointer="/levels/zones/5")]}
            ),
            "does not resolve",
        ),
        (lambda c: c.model_copy(update={"known_at": "2023-01-06"}), "known_at"),
        (lambda c: c.model_copy(update={"provisional": True}), "provisional"),
        (lambda c: c.model_copy(update={"subject": "/levels"}), "subject"),
    ],
)
def test_every_tampering_is_refused(tamper: Any, message: str) -> None:
    problems = validate(explanation(tamper(zone_claim())), DOC, SHA)
    assert any(message in p for p in problems), problems


def test_an_arithmetic_value_cannot_be_quoted() -> None:
    """A value is quoted only from a stored field: a computed width resolves to nothing."""
    claim = zone_claim()
    width = QuotedValue(
        name="width", ref="/levels/zones/0", field="/width", kind="value", value=1.376544
    )
    problems = validate(
        explanation(claim.model_copy(update={"quoted_values": [*claim.quoted_values, width]})),
        DOC,
        SHA,
    )
    assert any("width" in p and "does not resolve" in p for p in problems), problems


def test_the_binding_to_one_document_is_checked() -> None:
    good = explanation(zone_claim())
    assert "bound to another analysis document" in validate(good, DOC, "b" * 64)
    other = copy.deepcopy(DOC)
    other["identity"]["security_id"] = "SEC-2"
    assert "names another security than its document" in validate(good, other, SHA)
    forged = {**good, "explanation_key": "0" * 64}
    assert "its explanation_key is not its dependency fingerprint" in validate(forged, DOC, SHA)
    assert validate({"claims": "x"}, DOC, SHA)[0].startswith("not an explanation")


def test_a_stored_enum_carrying_a_forbidden_word_is_flagged() -> None:
    doc = copy.deepcopy(DOC)
    doc["levels"]["zones"][0]["type"] = "BUY_ZONE"
    claim = zone_claim()
    quoted = [
        q.model_copy(update={"value": "BUY_ZONE"}) if q.name == "side" else q
        for q in claim.quoted_values
    ]
    claim = claim.model_copy(
        update={"quoted_values": quoted, "rendered_text": render("ZONE_UNTESTED", quoted)}
    )
    assert any("forbidden word 'buy'" in p for p in validate(explanation(claim), doc, SHA))


def test_explanation_key_depends_on_exactly_its_inputs() -> None:
    base = explanation_key("S", SHA, "v1")
    assert base == explanation_key("S", SHA, "v1")
    assert (
        len(
            {
                base,
                explanation_key("T", SHA, "v1"),
                explanation_key("S", "b" * 64, "v1"),
                explanation_key("S", SHA, "v2"),
            }
        )
        == 4
    )


def test_template_ids_are_stable_names() -> None:
    assert all(t.isupper() for t in TEMPLATES)
    assert canonical_json(dict(TEMPLATES))  # canonical: hashable into explain_version
