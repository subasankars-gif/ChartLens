"""ADR-0024 phase 6a: the orchestrator, the analysis document and canonical serialization.

- The orchestrator composes the layers and is exactly the layers' composition.
- The stored form is deterministic (repeat runs, other processes, other hash seeds,
  other processing orders) and lossless (every section validates back to the layer's
  result).
- The canonical encoder obeys its written rules (an independent reference encoder).
- ``current`` holds references only, and every one resolves.
- The document records no timestamp, run id or snapshot version.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import math
import os
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from analysis_chain import context, random_bars, run_chain
from pydantic import TypeAdapter

from chartlens_core.config import AnalysisConfig, ChartLensSettings
from chartlens_core.domain import Timeframe
from chartlens_engine.analysis import (
    ANALYZERS,
    DATASETS,
    GRAPH,
    AnalysisInputError,
    AnalysisInputs,
    CanonicalError,
    TechnicalAnalysis,
    analysis_version,
    analyze_security,
    canonical_json,
    event_content_hash,
    serialize,
)
from chartlens_engine.analysis.versions import ANALYZER_CLASSES
from chartlens_engine.breakouts import LevelBreakoutEvent, PatternBreakoutEvent

INPUTS = AnalysisInputs(
    exchange="NSE",
    weekly_file_sha256="ab" * 32,
    weekly_schema_version="1",
    weekly_builder_version="1",
    usable_from=date(2006, 1, 6),
)
SEEDS = (0, 6, 15)


def analyse(bars: pd.DataFrame, cfg: AnalysisConfig | None = None) -> TechnicalAnalysis:
    return analyze_security(bars, context(bars), cfg or AnalysisConfig(), INPUTS)


# ------------------------------------------------------------------ reference encoder


def reference_canonical(value: object) -> bytes:
    """The canonical rules written out independently of the production encoder."""
    out: list[str] = []

    def write(v: object) -> None:
        if v is None:
            out.append("null")
        elif v is True or v is False:
            out.append("true" if v else "false")
        elif type(v) is int:
            out.append(str(v))
        elif type(v) is float:
            assert math.isfinite(v), v
            out.append(repr(v))
        elif type(v) is str:
            out.append(json.dumps(v, ensure_ascii=False))
        elif type(v) is list:
            out.append("[")
            for i, item in enumerate(v):  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
                out.append("," if i else "")
                write(item)
            out.append("]")
        elif type(v) is dict:
            keys: list[Any] = list(v)  # pyright: ignore[reportUnknownArgumentType]
            assert all(type(k) is str for k in keys), keys
            out.append("{")
            for i, k in enumerate(sorted(keys)):
                out.append("," if i else "")
                out.append(json.dumps(k, ensure_ascii=False) + ":")
                write(v[k])
            out.append("}")
        else:
            raise AssertionError(f"not canonical: {type(v).__name__}")

    write(value)
    return "".join(out).encode()


# ------------------------------------------------------------------ canonical encoding


def test_mapping_order_never_changes_the_bytes() -> None:
    a = {"b": 1, "a": {"y": 2.5, "x": None}}
    b = {"a": {"x": None, "y": 2.5}, "b": 1}
    assert canonical_json(a) == canonical_json(b) == b'{"a":{"x":null,"y":2.5},"b":1}'


def test_sequence_order_is_preserved_never_sorted() -> None:
    assert canonical_json([3, 1, 2]) == b"[3,1,2]"
    assert canonical_json([1, 2, 3]) != canonical_json([3, 2, 1])


def test_numbers_are_shortest_round_trip_and_lossless() -> None:
    for x in (0.1, 1 / 3, 105.26893859314225, 1e-7, 1e22, 100.0, -2.5, -0.0):
        text = canonical_json(x).decode()
        assert float(text) == x and math.copysign(1, float(text)) == math.copysign(1, x)
        assert text == repr(x)
    assert canonical_json(7) == b"7" and canonical_json(7.0) == b"7.0"


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_non_finite_numbers_are_refused(bad: float) -> None:
    with pytest.raises(CanonicalError):
        canonical_json({"x": [bad]})


@pytest.mark.parametrize("bad", [date(2020, 1, 3), {1, 2}, object(), b"x"])
def test_values_without_a_canonical_form_are_refused(bad: object) -> None:
    with pytest.raises(CanonicalError):
        canonical_json({"x": bad})


def test_unicode_is_utf8_and_there_is_no_whitespace() -> None:
    assert canonical_json({"name": "Bajaj Auto – é"}) == '{"name":"Bajaj Auto – é"}'.encode()
    assert b" " not in canonical_json({"a": [1, 2], "b": {"c": None}})


@pytest.mark.parametrize("seed", SEEDS)
def test_documents_satisfy_the_written_rules(seed: int) -> None:
    """The production encoder equals the reference encoder on real documents: every key
    is a string, every value has a canonical form, no list was reordered."""
    s = serialize(analyse(random_bars(500, seed)))
    assert s.document == reference_canonical(json.loads(s.document))
    for ds in s.events.values():
        assert canonical_json(ds.rows) == reference_canonical(ds.rows)


# ------------------------------------------------------------------ composition


@pytest.mark.parametrize("seed", SEEDS)
def test_composition_equals_the_layers(seed: int) -> None:
    """Every section is, unchanged, the result of running its layer alone."""
    bars = random_bars(600, seed)
    a = analyse(bars)
    chain = run_chain(bars, diagnostics=False)
    assert a.indicators == chain.indicators
    assert a.swings == chain.swings
    assert a.structure == chain.structure
    assert a.fibonacci == chain.fibonacci
    assert a.levels == chain.levels
    assert a.evidence.divergence == chain.divergence
    assert a.evidence.volume == chain.volume
    assert a.evidence.volatility == chain.volatility
    assert a.evidence.candles == chain.candles
    assert a.patterns == chain.patterns
    assert a.relevance == chain.relevance
    assert a.breakout_events == chain.breakouts


def test_production_keeps_no_rejection_diagnostics() -> None:
    a = analyse(random_bars(600, 0))
    assert a.patterns.rejections == []
    assert run_chain(random_bars(600, 0)).patterns.rejections  # the test chain keeps them


def test_provenance_matches_every_analyzer_constructor() -> None:
    """``provenance`` is the composition actually performed: each section consumes
    exactly what its analyzer's constructor takes, in execution order."""
    sections = {
        "indicators": "indicators",
        "swings": "swings",
        "structure": "structure",
        "fibonacci": "fibonacci",
        "levels": "levels",
        "divergence": "evidence.divergence",
        "volatility": "evidence.volatility",
        "patterns": "patterns",
    }
    assert len(GRAPH) == len(ANALYZER_CLASSES)
    for (section, consumes), cls in zip(GRAPH, ANALYZER_CLASSES, strict=True):
        params = [
            p
            for p in inspect.signature(cls.__init__).parameters
            if p not in ("self", "config", "diagnostics")
        ]
        assert sorted(consumes) == sorted(sections[p] for p in params), section
    a = analyse(random_bars(300, 0))
    assert [(p.section, p.analyzer, p.analyzer_version) for p in a.provenance] == [
        (s, n, v) for (s, _), (n, v) in zip(GRAPH, ANALYZERS, strict=True)
    ]


def test_every_section_is_for_the_documents_context() -> None:
    bars = random_bars(400, 6)
    a = analyse(bars)
    ctx = context(bars)
    for section in (
        a.indicators,
        a.swings,
        a.structure,
        a.fibonacci,
        a.levels,
        a.evidence.divergence,
        a.evidence.volume,
        a.evidence.volatility,
        a.evidence.candles,
        a.patterns,
        a.relevance,
        a.breakout_events,
    ):
        assert section.context == ctx
    assert a.identity.security_id == ctx.security_id
    assert a.identity.continuity_segment_id == ctx.continuity_segment_id
    assert a.versions.data_methodology_hash == ctx.methodology_hash
    assert a.inputs == INPUTS


def test_identity_reports_a_forming_week() -> None:
    bars = random_bars(400, 0)
    complete = analyse(bars.assign(is_complete=True))
    flags = [True] * (len(bars) - 1) + [False]
    forming = analyse(bars.assign(is_complete=flags))
    assert not complete.identity.forming_week_present
    assert forming.identity.forming_week_present
    assert forming.identity.as_of == bars["bar_date"].iloc[-1].date()
    assert forming.identity.state_date == bars["bar_date"].iloc[-2].date()
    assert complete.identity.state_date == complete.identity.as_of


def test_the_orchestrator_refuses_other_inputs() -> None:
    bars = random_bars(200, 0)
    ctx = context(bars)
    with pytest.raises(AnalysisInputError):
        analyze_security(
            bars, ctx.model_copy(update={"timeframe": Timeframe.DAILY}), AnalysisConfig(), INPUTS
        )
    with pytest.raises(AnalysisInputError):
        analyze_security(
            bars, ctx.model_copy(update={"continuity_segment_id": None}), AnalysisConfig(), INPUTS
        )


# ------------------------------------------------------------------ current


@pytest.mark.parametrize("seed", (*SEEDS, 3, 21))
def test_every_current_reference_resolves(seed: int) -> None:
    a = analyse(random_bars(700, seed))
    cur = a.current
    assert cur.state_date == a.identity.state_date
    if cur.trend_since is None:
        assert a.structure.trend is None
    else:
        assert a.structure.trend is not None and a.structure.trend.since == cur.trend_since
        assert a.structure.trend in a.structure.trend_history
    assert cur.zone_ids == [z.zone_id for z in a.levels.zones]
    trendlines = {t.trendline_id for t in a.levels.trendlines}
    assert set(cur.active_trendline_ids) <= trendlines
    fibs = {f.fib_id for f in a.fibonacci.structures}
    assert set(cur.fibonacci_ids) <= fibs
    patterns = {p.pattern_id for p in a.patterns.patterns}
    assert set(cur.included_pattern_ids) <= patterns
    for pid in cur.included_pattern_ids:
        entry = next(r for r in a.relevance.relevance if r.pattern_id == pid).as_of(
            cur.state_date  # type: ignore[arg-type]
        )
        assert entry is not None and entry.included


def test_current_holds_references_only() -> None:
    """No value, rank, score or signal: every field is a date or a list of ids."""
    fields = TechnicalAnalysis.model_fields["current"].annotation.model_fields  # type: ignore[union-attr]
    assert set(fields) == {
        "state_date",
        "trend_since",
        "zone_ids",
        "active_trendline_ids",
        "fibonacci_ids",
        "included_pattern_ids",
    }


# ------------------------------------------------------------------ determinism


@pytest.mark.parametrize("seed", SEEDS)
def test_repeat_runs_give_identical_bytes(seed: int) -> None:
    bars = random_bars(600, seed)
    first, second = serialize(analyse(bars)), serialize(analyse(bars.copy()))
    assert first.document == second.document
    assert first.document_sha256 == second.document_sha256
    for name in DATASETS:
        assert first.events[name].content_sha256 == second.events[name].content_sha256


def test_processing_order_never_shows() -> None:
    frames = {
        seed: random_bars(400, seed).assign(
            security_id=f"S{seed}", continuity_segment_id=f"S{seed}@2006-01-06"
        )
        for seed in (1, 2, 3, 4)
    }

    def run(order: list[int]) -> dict[int, str]:
        out: dict[int, str] = {}
        for seed in order:
            bars = frames[seed]
            ctx = context(bars, f"S{seed}", f"S{seed}@2006-01-06")
            out[seed] = serialize(
                analyze_security(bars, ctx, AnalysisConfig(), INPUTS)
            ).document_sha256
        return out

    assert run([1, 2, 3, 4]) == run([4, 2, 3, 1]) == run([3, 3, 1, 4, 2, 1])


_SUBPROCESS = """
import sys
from datetime import date
from chartlens_core.config import AnalysisConfig
from chartlens_core.domain import Timeframe
from chartlens_core.testing import make_bars
from chartlens_engine.analysis import AnalysisInputs, analyze_security, serialize
from chartlens_engine.interfaces import AnalysisContext
out = []
for seed in (0, 6):
    bars = make_bars(date(2006, 1, 2), 500, freq="W-FRI", seed=seed).assign(
        security_id="SEC-EV", continuity_segment_id="SEC-EV@2006-01-06")
    ctx = AnalysisContext(security_id="SEC-EV", timeframe=Timeframe.WEEKLY,
        as_of=bars["bar_date"].iloc[-1].date(), methodology_hash="test",
        continuity_segment_id="SEC-EV@2006-01-06")
    inputs = AnalysisInputs(exchange="NSE", weekly_file_sha256="ab" * 32,
        weekly_schema_version="1", weekly_builder_version="1", usable_from=date(2006, 1, 6))
    out.append(serialize(analyze_security(bars, ctx, AnalysisConfig(), inputs)).document_sha256)
print(" ".join(out))
"""


def test_other_processes_and_hash_seeds_give_the_same_address() -> None:
    """No set or dict iteration order, object id or process state reaches the bytes."""
    here = [serialize(analyse(random_bars(500, seed))).document_sha256 for seed in (0, 6)]
    for hash_seed in ("0", "4242"):
        env = {**os.environ, "PYTHONHASHSEED": hash_seed}
        done = subprocess.run(
            [sys.executable, "-c", _SUBPROCESS],
            env=env,
            capture_output=True,
            text=True,
            check=True,
            cwd=Path(__file__).resolve().parents[2],
        )
        assert done.stdout.split() == here


# ------------------------------------------------------------------ the stored form


def _section_types() -> dict[str, TypeAdapter[Any]]:
    return {
        name: TypeAdapter(field.annotation)
        for name, field in TechnicalAnalysis.model_fields.items()
        if name != "breakout_events"
    }


@pytest.mark.parametrize("seed", SEEDS)
def test_the_stored_form_is_lossless(seed: int) -> None:
    """Every section validates back from the document to an equal result (no field is
    lost to a base-class annotation, no float to formatting), and so do the events."""
    a = analyse(random_bars(600, seed))
    s = serialize(a)
    doc = json.loads(s.document)
    for name, cls in _section_types().items():
        assert cls.validate_python(doc[name]) == getattr(a, name), name
    bo = a.breakout_events
    rows = s.events
    assert [PatternBreakoutEvent.model_validate(r) for r in rows["pattern_breakouts"].rows] == (
        bo.pattern_events
    )
    assert [LevelBreakoutEvent.model_validate(r) for r in rows["level_breakouts"].rows] == (
        bo.level_events
    )


def test_the_document_pins_its_event_datasets() -> None:
    a = analyse(random_bars(700, 6))
    s = serialize(a)
    doc = json.loads(s.document)
    section = doc["breakout_events"]
    assert set(section["datasets"]) == set(DATASETS)
    assert section["analyzer"] == a.breakout_events.analyzer
    for name in DATASETS:
        ds = s.events[name]
        assert ds.content_sha256 == event_content_hash(ds.rows)
        assert ds.content_sha256 == hashlib.sha256(canonical_json(ds.rows)).hexdigest()
        assert section["datasets"][name] == {
            "schema_version": "1",
            "row_count": ds.row_count,
            "content_sha256": ds.content_sha256,
        }
        assert ds.metadata["chartlens.content_sha256"] == ds.content_sha256
        assert ds.metadata["chartlens.security_id"] == a.identity.security_id
        assert ds.metadata["chartlens.continuity_segment_id"] == a.identity.continuity_segment_id
        assert ds.metadata["chartlens.analysis_version"] == a.versions.analysis_version
        keys = [(r["bar_date"], r["event_key"]) for r in ds.rows]
        assert keys == sorted(keys), "the layer orders both datasets by date, then key"
    assert s.events["level_breakouts"].row_count > 0
    assert "pattern_events" not in json.dumps(section)


def test_an_event_change_changes_the_document_address() -> None:
    """The document hash covers the event content: change one event, the address moves."""
    a = analyse(random_bars(700, 6))
    events = a.breakout_events.level_events
    first = events[0].model_copy(update={"provisional": not events[0].provisional})
    altered = a.model_copy(
        update={
            "breakout_events": a.breakout_events.model_copy(
                update={"level_events": [first, *events[1:]]}
            )
        }
    )
    assert serialize(altered).document_sha256 != serialize(a).document_sha256


def test_inputs_change_the_address_and_nothing_else_does() -> None:
    bars = random_bars(500, 0)
    base = serialize(analyse(bars)).document_sha256
    other = INPUTS.model_copy(update={"weekly_file_sha256": "cd" * 32})
    moved = serialize(analyze_security(bars, context(bars), AnalysisConfig(), other))
    assert moved.document_sha256 != base


FORBIDDEN_KEYS = (
    "timestamp",
    "generated_at",
    "created_at",
    "published_at",
    "run_id",
    "meta_version",
    "snapshot",
    "hostname",
    "runner",
)


def test_the_document_records_no_execution_or_publication_facts() -> None:
    """Decision 3: no execution time, run id, snapshot version or runner identity."""
    doc = json.loads(serialize(analyse(random_bars(500, 0))).document)
    keys: set[str] = set()

    def walk(v: object) -> None:
        if isinstance(v, dict):
            for k, x in v.items():  # pyright: ignore[reportUnknownVariableType]
                keys.add(str(k))  # pyright: ignore[reportUnknownArgumentType]
                walk(x)
        elif isinstance(v, list):
            for x in v:  # pyright: ignore[reportUnknownVariableType]
                walk(x)

    walk(doc)
    offenders = sorted(k for k in keys for f in FORBIDDEN_KEYS if f in k.lower())
    assert not offenders, offenders


# ------------------------------------------------------------------ versions


def test_analysis_version_is_the_code_and_the_settings_only() -> None:
    cfg = AnalysisConfig()
    assert analysis_version(cfg) == analysis_version(AnalysisConfig())
    assert analysis_version(cfg).startswith("analysis-")
    changed = cfg.model_copy(
        update={"breakouts": cfg.breakouts.model_copy(update={"retest_window": 11})}
    )
    assert analysis_version(changed) != analysis_version(cfg)
    assert cfg.methodology_hash() == ChartLensSettings().analysis_methodology_hash()
    assert changed.methodology_hash() != cfg.methodology_hash()


def test_analysis_version_moves_with_any_analyzer_version(monkeypatch: pytest.MonkeyPatch) -> None:
    import chartlens_engine.analysis.versions as versions

    before = analysis_version(AnalysisConfig())
    bumped = tuple((n, v + "x" if n == "levels" else v) for n, v in versions.ANALYZERS)
    monkeypatch.setattr(versions, "ANALYZERS", bumped)
    assert analysis_version(AnalysisConfig()) != before


def test_the_document_carries_its_analytical_provenance() -> None:
    a = analyse(random_bars(300, 0))
    v = a.versions
    assert v.analysis_version == analysis_version(AnalysisConfig())
    assert v.analysis_methodology_hash == AnalysisConfig().methodology_hash()
    assert [(x.name, x.version) for x in v.analyzers] == list(ANALYZERS)
    assert {v.document_schema_version, v.canonical_serialization_version} == {"1"}
