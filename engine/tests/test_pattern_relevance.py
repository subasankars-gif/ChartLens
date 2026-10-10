"""Layer H, Phase 5b-D — pattern relevance (ADR-0022 §18).

Relevance is an attention annotation with named reasons, never a score. Central
invariant: changing relevance rules never changes pattern identity, geometry, lifecycle,
definition fit or any underlying object. Every entry is known at its own bar, appended,
never rewritten, and reproducible by a run that stops there."""

from __future__ import annotations

import ast
from collections import Counter
from datetime import date
from itertools import pairwise
from pathlib import Path

import pandas as pd
import pytest
from analysis_chain import SEG, SID, manual_swings, random_bars, run_chain, week

from chartlens_core.bars import CLOSES_ON_SPECIAL_SESSION, IS_COMPLETE
from chartlens_core.config import AnalysisConfig, IndicatorConfig, PatternsConfig, RelevanceConfig
from chartlens_engine.patterns import RelevanceAnalyzer, relevance
from chartlens_engine.patterns import fit as fit_module
from chartlens_engine.patterns.families import REVERSAL_FAMILIES
from chartlens_engine.patterns.relevance import FORMING_STAGE, PatternRelevance

HAND = AnalysisConfig(indicators=IndicatorConfig(atr_period=2))
Row = tuple[float, float, float, float]


def frame(closes: list[float], **extra: list[object]) -> pd.DataFrame:
    rows: list[Row] = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i else c
        rows.append((o, max(o, c) + 0.5, min(o, c) - 0.5, c))
    o, h, lo, c = (list(x) for x in zip(*rows, strict=True))
    return pd.DataFrame(
        {
            "bar_date": pd.date_range(date(2010, 1, 8), periods=len(rows), freq="W-FRI"),
            "open": o,
            "high": h,
            "low": lo,
            "close": c,
            "volume": 1000.0,
            "security_id": SID,
            "continuity_segment_id": SEG,
            **extra,
        }
    )


# A double bottom: lows at weeks 30 (45.0) and 40 (45.3, known at week 41), neckline 49.0
# at week 35, an older support at 45.1 (week 12).
DECLINE = [60.0 - 0.5 * i for i in range(30)]
BOTTOM = [46.0, 47.0, 48.0, 48.5, 48.0, 47.5, 47.0, 46.5, 46.0, 45.8, 46.0]
SWINGS = [
    ("LOW", 12, 13, 45.1),
    ("LOW", 30, 31, 45.0),
    ("HIGH", 35, 36, 49.0),
    ("LOW", 40, 41, 45.3),
]
# weeks 41..: near the neckline at 43, breakout at 44, holds, then reaches the zone
WALK = [46.0, 47.0, 48.4, 50.0, 50.5, 50.0, 50.5, 50.2, 50.6, 51.5, 52.5, 53.5, 54.0]
QUIET = [46.0, 46.2, 46.0, 46.1, 46.0, 46.2, 46.1]


def _double(tail: list[float], **extra: list[object]) -> tuple[PatternRelevance, object]:
    bars = frame(DECLINE + BOTTOM + tail, **extra)
    chain = run_chain(bars, HAND, manual_swings(bars, SWINGS))
    (p,) = [p for p in chain.patterns.patterns if p.pattern_type == "DOUBLE_BOTTOM"]
    (r,) = [r for r in chain.relevance.relevance if r.pattern_id == p.pattern_id]
    return r, p


def _reasons(r: PatternRelevance) -> list[tuple[date, str]]:
    """Reason changes, ignoring entries that only refresh tags."""
    out: list[tuple[date, str]] = []
    for e in r.history:
        if not out or out[-1][1] != e.reason:
            out.append((e.effective_date, e.reason))
    return out


# ---------------------------------------------------------------- reasons and windows


def test_new_pattern_window_is_four_weekly_bars() -> None:
    """NEWLY_RECOGNISED while k ≤ t < k + 4 (weekly bars, not calendar days)."""
    r, p = _double(QUIET)
    assert r.pattern_known_at == p.known_at == week(41)  # type: ignore[attr-defined]
    assert _reasons(r) == [(week(41), "NEWLY_RECOGNISED"), (week(45), "FORMING")]
    assert all(e.included for e in r.history)


def test_a_pattern_walks_through_its_lifecycle_one_dated_entry_at_a_time() -> None:
    r, p = _double(WALK)
    events = {e.status: e.effective_date for e in p.status_history}  # type: ignore[attr-defined]
    b, done = events["CONFIRMED"], events["COMPLETED"]
    assert b == week(44)
    assert _reasons(r) == [
        (week(41), "NEWLY_RECOGNISED"),
        (week(43), "APPROACHING_CONFIRMATION"),  # within 1 ATR, and still "new": precedence
        (week(44), "BREAKOUT_CONFIRMED"),
        (week(48), "BREAKOUT_OPEN"),  # b ≤ t < b + 4, then open
        (done, "TERMINAL_COMPLETED"),
    ]
    approach = next(e for e in r.history if e.reason == "APPROACHING_CONFIRMATION")
    v = approach.measured_values
    assert v["level"] == 49.0 and abs(v["close"] - v["level"]) <= v["atr"]
    assert v["distance_atr"] == pytest.approx(abs(v["close"] - v["level"]) / v["atr"])
    # terminal is permanent: nothing after it
    assert r.history[-1].reason == "TERMINAL_COMPLETED" and not r.history[-1].included
    assert [e.included for e in r.history[:-1]] == [True] * (len(r.history) - 1)


def test_precedence_is_fixed_and_only_the_first_reason_is_emitted() -> None:
    """Week 43 is both inside the new-pattern window and within 1 ATR of the neckline;
    APPROACHING_CONFIRMATION is emitted, by declared precedence."""
    r, _ = _double(WALK)
    e = r.as_of(week(43))
    assert e is not None and e.reason == "APPROACHING_CONFIRMATION"
    assert week(43) < week(41 + RelevanceConfig().new_pattern_bars)


def test_a_special_session_close_makes_the_entry_provisional() -> None:
    n = len(DECLINE + BOTTOM + WALK)
    special = [False] * n
    special[43] = True
    r, _ = _double(WALK, **{CLOSES_ON_SPECIAL_SESSION: special})
    e = r.as_of(week(43))
    assert e is not None and e.effective_date == week(43) and e.provisional
    assert not r.as_of(week(42)).provisional  # type: ignore[union-attr]


def test_the_forming_week_never_creates_an_entry() -> None:
    complete = DECLINE + BOTTOM + WALK[:3]
    done, _ = _double(WALK[:3])
    forming, _ = _double([*WALK[:3], 50.0], **{IS_COMPLETE: [True] * len(complete) + [False]})
    assert forming.history == done.history  # the close at 50 in a forming week is ignored


# ---------------------------------------------------------------- containment and cap


def test_a_double_bottom_inside_a_triple_bottom_is_contained() -> None:
    closes = (
        DECLINE
        + BOTTOM
        + [46.0, 47.0, 48.0, 48.6, 48.0, 47.0, 46.5, 46.0, 45.8, 45.6, 46.0, 46.2, 46.0, 46.3]
    )
    swings = [*SWINGS[1:3], ("LOW", 40, 41, 45.2), ("HIGH", 45, 46, 49.2), ("LOW", 50, 51, 45.1)]
    bars = frame(closes)
    chain = run_chain(bars, HAND, manual_swings(bars, swings))
    by_type = {p.pattern_type: p for p in chain.patterns.patterns}
    triple = by_type["TRIPLE_BOTTOM"]
    rel = {r.pattern_id: r for r in chain.relevance.relevance}
    doubles = [p for p in chain.patterns.patterns if p.pattern_type == "DOUBLE_BOTTOM"]
    assert doubles and triple.known_at == week(51)
    for d in doubles:
        e = rel[d.pattern_id].as_of(week(51))
        assert e is not None and not e.included and e.reason == "CONTAINED"
        assert e.container_pattern_id == triple.pattern_id
        assert triple.pattern_id in e.evidence_refs
    assert rel[triple.pattern_id].as_of(week(51)).included  # type: ignore[union-attr]
    first = rel[doubles[0].pattern_id]
    assert first.as_of(week(50)).included  # type: ignore[union-attr]  # before the triple


@pytest.mark.parametrize("seed", [0, 6, 15])
def test_the_cap_keeps_the_most_recently_known_and_records_why(seed: int) -> None:
    cfg = AnalysisConfig()
    cfg = cfg.model_copy(
        update={
            "patterns": cfg.patterns.model_copy(
                update={"relevance": RelevanceConfig(max_forming_per_type=1)}
            )
        }
    )
    bars = random_bars(900, seed)
    chain = run_chain(bars, cfg)
    pats = {p.pattern_id: p for p in chain.patterns.patterns}
    capped = 0
    for day in (pd.Timestamp(d).date() for d in bars["bar_date"]):
        entries = {r.pattern_id: r.as_of(day) for r in chain.relevance.relevance}
        live: Counter[str] = Counter()
        for pid, e in entries.items():
            if e is None:
                continue
            if e.included and e.reason in FORMING_STAGE:
                live[pats[pid].pattern_type] += 1
            if e.reason == "FORMING_CAP":
                capped += 1
                assert not e.included and len(e.kept_pattern_ids) == 1
                (kept,) = e.kept_pattern_ids
                k = entries[kept]
                assert k is not None and k.included and k.reason in FORMING_STAGE
                assert pats[kept].pattern_type == pats[pid].pattern_type
                kp, cp = pats[kept], pats[pid]
                assert kp.known_at > cp.known_at or (kp.known_at == cp.known_at and kept < pid)
        assert all(v <= 1 for v in live.values()), day
    assert capped > 0


# ---------------------------------------------------------------- invariants


def test_relevance_rules_never_change_the_patterns() -> None:
    """The central invariant: different relevance settings, identical patterns."""
    bars = random_bars(700, seed=8)
    a = AnalysisConfig()
    b = a.model_copy(
        update={
            "patterns": a.patterns.model_copy(
                update={
                    "relevance": RelevanceConfig(
                        new_pattern_bars=9,
                        recent_breakout_bars=1,
                        approaching_confirmation_atr=3.0,
                        max_forming_per_type=1,
                    )
                }
            )
        }
    )
    ca, cb = run_chain(bars, a), run_chain(bars, b)
    assert ca.patterns.patterns == cb.patterns.patterns
    assert ca.relevance.relevance != cb.relevance.relevance


@pytest.mark.parametrize("seed", [3, 9])
def test_relevance_is_known_at_its_bar_and_never_rewritten(seed: int) -> None:
    """A run that stops at T reproduces the full run's history up to T, entry for entry:
    no pattern becomes relevant in retrospect."""
    bars = random_bars(600, seed)
    full = {r.pattern_id: r for r in run_chain(bars).relevance.relevance}
    for cut in (250, 380, 520):
        end = pd.Timestamp(bars["bar_date"].iloc[cut]).date()
        part = run_chain(bars.iloc[: cut + 1].copy()).relevance.relevance
        assert part
        for r in part:
            prefix = [e for e in full[r.pattern_id].history if e.relevance_known_at <= end]
            assert r.history == prefix, (r.pattern_id, cut)
            assert r.current == full[r.pattern_id].as_of(end)


@pytest.mark.parametrize("seed", [4, 17])
def test_history_shape(seed: int) -> None:
    chain = run_chain(random_bars(900, seed))
    pats = {p.pattern_id: p for p in chain.patterns.patterns}
    assert [r.pattern_id for r in chain.relevance.relevance] == list(pats)
    for r in chain.relevance.relevance:
        h = r.history
        assert h and h[0].effective_date == r.pattern_known_at == pats[r.pattern_id].known_at
        for a, b in pairwise(h):
            assert a.effective_date < b.effective_date
            assert not a.reason.startswith("TERMINAL_") and a.reason != "AGED_OUT"
        for e in h:
            assert e.relevance_known_at == e.effective_date
            assert r.pattern_id in e.evidence_refs
            assert e.included == (
                e.reason not in {"AGED_OUT", "CONTAINED", "FORMING_CAP"}
                and not e.reason.startswith("TERMINAL_")
            )
            if e.reason.startswith("TERMINAL_"):
                assert pats[r.pattern_id].status_history[-1].status == e.reason[9:]


def test_tags_never_decide_anything(monkeypatch: pytest.MonkeyPatch) -> None:
    bars = random_bars(900, seed=6)
    with_tags = run_chain(bars).relevance.relevance
    monkeypatch.setattr(RelevanceAnalyzer, "_tags", lambda *_a, **_k: [])
    without = run_chain(bars).relevance.relevance
    days = [pd.Timestamp(d).date() for d in bars["bar_date"]]
    tagged = 0
    for a, b in zip(with_tags, without, strict=True):
        tagged += sum(bool(e.tags) for e in a.history)
        for day in days:
            ea, eb = a.as_of(day), b.as_of(day)
            if ea is None or eb is None:
                assert ea is eb
                continue
            assert (ea.included, ea.reason, ea.container_pattern_id, ea.kept_pattern_ids) == (
                eb.included,
                eb.reason,
                eb.container_pattern_id,
                eb.kept_pattern_ids,
            )
    assert tagged > 0


def test_relevance_never_reads_definition_fit_or_ranks() -> None:
    tree = ast.parse(Path(relevance.__file__).read_text())
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
    }
    modules = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert "definition_fit" not in names and "exact" not in names
    assert "chartlens_engine.patterns.fit" not in modules
    assert not any("score" in x for x in names), [x for x in names if "score" in x]


def test_reversal_families_agree_with_the_fit_definitions() -> None:
    assert frozenset(fit_module.REVERSAL) == REVERSAL_FAMILIES


def test_relevance_defaults_are_the_declared_ones() -> None:
    assert PatternsConfig().relevance == RelevanceConfig(
        new_pattern_bars=4,
        recent_breakout_bars=4,
        approaching_confirmation_atr=1.0,
        max_forming_per_type=2,
    )
