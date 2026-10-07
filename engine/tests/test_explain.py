"""ADR-0028: explanations of real engine documents.

- Every claim validates against the exact document it is bound to (the shared checker).
- The document is unchanged by explaining it; the output is deterministic.
- Claims follow the engine's order and lists; no divergence claim; conditions only as
  stored (a level, else the named boundary line, else nothing).
- A template change changes ``explain_version`` and nothing analytical.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from analysis_chain import context, random_bars

from chartlens_core import claims as core_claims
from chartlens_core.canonical import canonical_json
from chartlens_core.claims import explanation_bytes, validate
from chartlens_core.config import AnalysisConfig
from chartlens_engine.analysis import AnalysisInputs, analysis_version, analyze_security, serialize
from chartlens_engine.explain import explain, explain_version

INPUTS = AnalysisInputs(
    exchange="NSE",
    weekly_schema_version="1",
    weekly_builder_version="1",
    usable_from=date(2006, 1, 6),
)
SEEDS = (0, 13, 18)
"""0: two included patterns (one with a stored confirmation level), an active trendline,
measured moves; 13: a stored confirmation boundary line; 18: two included patterns."""


@pytest.fixture(scope="module")
def documents() -> dict[int, tuple[dict[str, Any], str, bytes]]:
    out: dict[int, tuple[dict[str, Any], str, bytes]] = {}
    for seed in SEEDS:
        bars = random_bars(600, seed)
        stored = serialize(analyze_security(bars, context(bars), AnalysisConfig(), INPUTS))
        out[seed] = (json.loads(stored.document), stored.document_sha256, stored.document)
    return out


@pytest.mark.parametrize("seed", SEEDS)
def test_every_claim_validates_against_its_document(documents: Any, seed: int) -> None:
    doc, sha, _ = documents[seed]
    ex = explain(doc, sha)
    assert ex.claims
    assert validate(ex.model_dump(mode="json"), doc, sha) == []
    assert ex.document_sha256 == sha and ex.explain_version == explain_version()


@pytest.mark.parametrize("seed", SEEDS)
def test_explaining_changes_nothing_and_is_deterministic(documents: Any, seed: int) -> None:
    doc, sha, stored = documents[seed]
    before = copy.deepcopy(doc)
    first = explanation_bytes(explain(doc, sha))
    assert doc == before and canonical_json(doc) == stored
    assert explanation_bytes(explain(json.loads(stored), sha)) == first


def test_claims_follow_the_engine_lists_in_order(documents: Any) -> None:
    doc, sha, _ = documents[0]
    claims = explain(doc, sha).claims
    types = [c.claim_type for c in claims]
    assert types[0] == "DATA_CONTEXT" and types[1] == "TREND_STATE"
    zone_ids = [c.claim_id.split(":", 1)[1] for c in claims if c.claim_type == "ZONE"]
    assert zone_ids == doc["current"]["zone_ids"]
    pattern_ids = [c.claim_id.split(":", 1)[1] for c in claims if c.claim_type == "PATTERN"]
    assert pattern_ids == doc["current"]["included_pattern_ids"]
    lines = [c.claim_id.split(":", 1)[1] for c in claims if c.claim_type == "ACTIVE_TRENDLINE"]
    assert lines == doc["current"]["active_trendline_ids"]
    assert not any("DIVERGENCE" in t for t in types)  # clarification 3


def test_conditions_are_quoted_only_as_stored(documents: Any) -> None:
    for seed in SEEDS:
        doc, sha, _ = documents[seed]
        by_id = {p["pattern_id"]: p for p in doc["patterns"]["patterns"]}
        claims = {c.claim_id: c for c in explain(doc, sha).claims}
        for pid in doc["current"]["included_pattern_ids"]:
            g = by_id[pid]["geometry"]
            c = claims.get(f"PATTERN_CONFIRMATION:{pid}")
            if g["confirmation_level"] is not None:
                assert c is not None and c.template_id == "PATTERN_CONFIRMATION_LEVEL"
            elif g["confirmation_line"] is not None:
                assert c is not None and c.template_id == "PATTERN_CONFIRMATION_LINE"
                assert {q.name for q in c.quoted_values} >= {"start_value", "end_value"}
            else:
                assert c is None  # nothing stored, nothing said
    seen = {c.template_id for s in SEEDS for c in explain(documents[s][0], documents[s][1]).claims}
    assert {"PATTERN_CONFIRMATION_LEVEL", "PATTERN_CONFIRMATION_LINE"} <= seen
    assert {"ACTIVE_TRENDLINE", "PATTERN_MEASURED_MOVE", "PATTERN_FIT"} <= seen


def test_the_forming_week_is_said_and_nothing_is_confirmed_by_it(documents: Any) -> None:
    doc, sha, _ = documents[0]
    forming = copy.deepcopy(doc)
    forming["identity"]["forming_week_present"] = True
    claims = explain(forming, sha).claims
    assert claims[1].claim_type == "FORMING_WEEK"
    assert "nothing is confirmed by it" in claims[1].rendered_text
    assert validate(explain(forming, sha).model_dump(mode="json"), forming, sha) == []


def test_every_number_in_the_text_is_a_quoted_value(documents: Any) -> None:
    """Strip each quoted value's rendering from the text: no digit may remain."""
    for seed in SEEDS:
        doc, sha, _ = documents[seed]
        for c in explain(doc, sha).claims:
            text = c.rendered_text
            for q in sorted(c.quoted_values, key=lambda q: -len(str(q.value))):
                if q.kind in core_claims.RENDERED_KINDS:
                    text = text.replace(core_claims.render_value(q.kind, q.value), "")
            assert not any(ch.isdigit() for ch in text), (c.claim_id, text)


def test_a_template_change_changes_explain_version_and_nothing_analytical(
    documents: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    doc, sha, stored = documents[0]
    version, av = explain_version(), analysis_version(AnalysisConfig())
    changed = dict(core_claims.TEMPLATES)
    changed["PATTERN_TAG"] = "Relevance tag recorded: {tag}."
    monkeypatch.setattr("chartlens_engine.explain.TEMPLATES", changed)
    assert explain_version() != version
    assert analysis_version(AnalysisConfig()) == av
    assert canonical_json(doc) == stored and hashlib.sha256(stored).hexdigest() == sha


def test_explain_does_no_arithmetic_and_no_ordering() -> None:
    """Static: no numeric library, no sorting or aggregation, no ordering comparison, and
    arithmetic only in ``_last`` (a list position, never a value)."""
    path = Path(__file__).parents[1] / "src/chartlens_engine/explain/__init__.py"
    tree = ast.parse(path.read_text())
    imports = {
        (n.module or "").split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
    } | {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not imports & {"numpy", "pandas", "math", "statistics", "decimal"}
    calls = {
        n.func.id
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert not calls & {"sorted", "min", "max", "sum", "round", "abs"}
    last = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_last")
    inside = {id(x) for x in ast.walk(last)}
    arithmetic = [
        n
        for n in ast.walk(tree)
        if (
            isinstance(n, ast.AugAssign)
            or (isinstance(n, ast.BinOp) and not isinstance(n.op, ast.BitOr))
        )
        and id(n) not in inside
    ]
    # String concatenation of pointers is not arithmetic on values.
    assert all(
        isinstance(n, ast.BinOp) and isinstance(n.op, ast.Add) and _is_pointer_concat(n)
        for n in arithmetic
    ), [ast.dump(n) for n in arithmetic]
    ordering = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Compare)
        and any(isinstance(op, ast.Lt | ast.LtE | ast.Gt | ast.GtE) for op in n.ops)
    ]
    assert not ordering


def _is_pointer_concat(node: ast.BinOp) -> bool:
    def stringy(x: ast.expr) -> bool:
        return isinstance(x, ast.JoinedStr) or (
            isinstance(x, ast.Constant) and isinstance(x.value, str)
        )

    return stringy(node.left) or stringy(node.right) or isinstance(node.left, ast.Name)
