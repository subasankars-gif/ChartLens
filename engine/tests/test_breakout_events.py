"""Breakout events (ADR-0022 §19) and the Phase 4 change-bar evidence amendment.

A breakout event is derived from an authoritative source (a pattern's breakout, a level's
role change); the source keeps authority over its own break and reversal; the event
layer adds only the RETEST rule, with a band frozen at the breakout's own ATR; terminal
follow-ups are final; and the event layer never writes to its sources."""

from __future__ import annotations

import ast
from datetime import date
from itertools import pairwise
from pathlib import Path

import pandas as pd
import pytest
from analysis_chain import SEG, SID, Chain, context, manual_swings, random_bars, run_chain, week

from chartlens_core.bars import IS_COMPLETE
from chartlens_core.config import AnalysisConfig, BreakoutsConfig, IndicatorConfig
from chartlens_engine import breakouts
from chartlens_engine.breakouts import TERMINAL_FOLLOW_UPS, BreakoutEventAnalyzer
from chartlens_engine.interfaces import run_analyzer
from chartlens_engine.patterns.model import BROKEN_OUT

HAND = AnalysisConfig(indicators=IndicatorConfig(atr_period=2))
DECLINE = [60.0 - 0.5 * i for i in range(30)]
BOTTOM = [46.0, 47.0, 48.0, 48.5, 48.0, 47.5, 47.0, 46.5, 46.0, 45.8, 46.0]
SWINGS = [
    ("LOW", 12, 13, 45.1),
    ("LOW", 30, 31, 45.0),
    ("HIGH", 35, 36, 49.0),
    ("LOW", 40, 41, 45.3),
]
# weeks 41..: breakout through the 49.0 neckline at week 44, then:
HOLDS = [
    46.0,
    47.0,
    48.4,
    50.0,
    50.5,
    50.8,
    51.0,
    51.2,
    51.0,
    51.3,
    51.5,
    51.4,
    51.6,
    51.8,
    52.0,
    52.1,
]  # a retest at 45 (low 49.5), then nothing until the window ends
FAILS = [46.0, 47.0, 48.4, 50.0, 48.5, 48.0, 47.8, 47.5]  # back below at 45: no retest
RETEST_FAILS = [46.0, 47.0, 48.4, 50.0, 50.5, 48.4, 48.0, 47.6]  # retest at 45, back at 46


def frame(closes: list[float], **extra: list[object]) -> pd.DataFrame:
    rows = []
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


def _hand(tail: list[float], **extra: list[object]) -> Chain:
    bars = frame(DECLINE + BOTTOM + tail, **extra)
    return run_chain(bars, HAND, manual_swings(bars, SWINGS))


def _pattern_event(chain: Chain):  # type: ignore[no-untyped-def]
    (e,) = [e for e in chain.breakouts.pattern_events if e.pattern_type == "DOUBLE_BOTTOM"]
    return e


def _neckline_event(chain: Chain):  # type: ignore[no-untyped-def]
    (e,) = [
        e
        for e in chain.breakouts.level_events
        if e.level_at_break == 49.0 and e.bar_date == week(44)
    ]
    return e


def _kinds(e) -> list[tuple[str, date]]:  # type: ignore[no-untyped-def]
    return [(f.kind, f.effective_date) for f in e.history]


# ---------------------------------------------------------------- hand fixtures


def test_a_held_breakout_is_retested_then_its_window_ends() -> None:
    chain = _hand(HOLDS)
    e = _pattern_event(chain)
    assert (e.direction, e.bar_date, e.level_at_break) == ("BREAKOUT", week(44), 49.0)
    # pattern: max(fail_window 8, retest_window 10) = 10 bars of observation
    assert _kinds(e) == [("RETEST", week(45)), ("WINDOW_ENDED", week(54))]
    retest = e.history[0]
    assert retest.authority == "RETEST_RULE"
    assert retest.measured_values["band"] == pytest.approx(0.5 * e.reference_atr)
    assert retest.measured_values["extreme"] <= 49.0 + retest.measured_values["band"]
    end = e.history[-1]
    assert end.authority == "WINDOW" and end.source_outcome_ref is None  # not "success"


def test_the_pattern_lifecycle_decides_the_false_breakout() -> None:
    chain = _hand(FAILS)
    (p,) = [p for p in chain.patterns.patterns if p.pattern_type == "DOUBLE_BOTTOM"]
    failed = next(x for x in p.status_history if x.status == "FAILED")
    e = _pattern_event(chain)
    assert _kinds(e) == [("FALSE_BREAKOUT", failed.effective_date)]
    f = e.history[0]
    assert f.authority == "PATTERN_LIFECYCLE"
    assert f.source_outcome_ref == f"{p.pattern_id}#FAILED@{failed.effective_date}"


def test_a_reversal_after_a_retest_is_a_failed_retest() -> None:
    e = _pattern_event(_hand(RETEST_FAILS))
    assert _kinds(e) == [("RETEST", week(45)), ("FAILED_RETEST", week(46))]
    assert e.status == "FAILED_RETEST"


def test_level_events_come_from_role_changes_and_are_not_merged_with_patterns() -> None:
    """The neckline's level flips to SUPPORT on the same bar the pattern confirms: two
    events from two sources, each with its own rule and evidence."""
    chain = _hand(HOLDS)
    pe, le = _pattern_event(chain), _neckline_event(chain)
    assert pe.event_id != le.event_id and pe.bar_date == le.bar_date
    level = next(lv for lv in chain.levels.levels if lv.level_id == le.source_id)
    rc = next(r for r in level.role_history if r.date == week(44))
    assert rc.role == "SUPPORT" and le.direction == "BREAKOUT"
    assert le.reference_atr == rc.change_bar.atr  # type: ignore[union-attr]  # ATR[t], Phase 4
    assert pe.reference_atr == pe.source_measured_values["atr_pre"]  # ATR_pre, 5b-A
    assert le.bar_volume == rc.change_bar_volume


def test_a_level_flipping_back_within_three_bars_is_a_false_breakout() -> None:
    chain = _hand(FAILS)
    le = _neckline_event(chain)
    level = next(lv for lv in chain.levels.levels if lv.level_id == le.source_id)
    back = next(r for r in level.role_history if r.date > week(44))
    assert _kinds(le) == [("FALSE_BREAKOUT", back.date)]
    assert le.history[0].authority == "LEVEL_ROLE_CHANGE"
    assert le.history[0].source_outcome_ref == f"{level.level_id}#ROLE_CHANGE@{back.date}"


def test_the_forming_week_never_adds_a_follow_up() -> None:
    n = len(DECLINE + BOTTOM) + 4  # through the breakout week
    done = _pattern_event(_hand(HOLDS[:4]))
    forming = _pattern_event(_hand([*HOLDS[:4], 49.0], **{IS_COMPLETE: [True] * n + [False]}))
    assert done.history == forming.history == []


# ---------------------------------------------------------------- properties


@pytest.mark.parametrize("seed", [3, 9, 21])
def test_follow_up_invariants(seed: int) -> None:
    chain = run_chain(random_bars(700, seed))
    events = [*chain.breakouts.pattern_events, *chain.breakouts.level_events]
    assert chain.breakouts.pattern_events and chain.breakouts.level_events
    for e in events:
        kinds = [f.kind for f in e.history]
        terminal = [k for k in kinds if k in TERMINAL_FOLLOW_UPS]
        assert len(terminal) <= 1 and (not terminal or kinds[-1] == terminal[0])
        assert kinds.count("RETEST") <= 1 and (not kinds or "RETEST" not in kinds[1:])
        for a, b in pairwise(e.history):
            # a retest on the last window bar shares it with WINDOW_ENDED, in that order
            assert a.effective_date < b.effective_date or (
                a.effective_date == b.effective_date
                and (a.kind, b.kind) == ("RETEST", "WINDOW_ENDED")
            )
        for f in e.history:
            assert f.known_at == f.effective_date > e.bar_date
            if f.kind == "RETEST":
                assert f.measured_values["band"] == pytest.approx(
                    BreakoutsConfig().retest_tol_atr * e.reference_atr  # type: ignore[operator]
                )
                assert f.measured_values["reference_atr"] == e.reference_atr
        if e.history and e.history[-1].kind == "WINDOW_ENDED":
            idx = [pd.Timestamp(d).date() for d in random_bars(700, seed)["bar_date"]]
            assert idx.index(e.history[-1].effective_date) - idx.index(e.bar_date) == (
                e.observation_bars
            )


@pytest.mark.parametrize("seed", [3, 9])
def test_events_copy_their_sources_evidence(seed: int) -> None:
    chain = run_chain(random_bars(700, seed))
    pats = {p.pattern_id: p for p in chain.patterns.patterns}
    for e in chain.breakouts.pattern_events:
        src = next(x for x in pats[e.source_id].status_history if x.status in BROKEN_OUT)
        assert e.source_measured_values == src.measured_values
        assert e.bar_volume == src.breakout_bar_volume and e.bar_volume is not None
        assert e.reference_atr == src.measured_values["atr_pre"]
    levels = {lv.level_id: lv for lv in chain.levels.levels}
    for e in chain.breakouts.level_events:
        rc = next(r for r in levels[e.source_id].role_history[1:] if r.date == e.bar_date)
        assert rc.change_bar is not None and e.bar_volume == rc.change_bar_volume
        assert e.reference_atr == rc.change_bar.atr
        assert e.direction == ("BREAKOUT" if rc.role == "SUPPORT" else "BREAKDOWN")


@pytest.mark.parametrize("seed", [4, 17])
def test_level_role_changes_record_their_change_bar(seed: int) -> None:
    bars = random_bars(700, seed)
    chain = run_chain(bars)
    dates = [pd.Timestamp(d).date() for d in bars["bar_date"]]
    atr = chain.indicators.get("atr").data
    for lv in chain.levels.levels:
        first, *changes = lv.role_history
        assert first.change_bar is None and first.change_bar_volume is None
        for rc in changes:
            bar, vol = rc.change_bar, rc.change_bar_volume
            assert bar is not None and vol is not None
            t = dates.index(rc.date)
            assert (bar.open, bar.close) == (bars["open"].iloc[t], bars["close"].iloc[t])
            assert bar.level == lv.price and bar.threshold == rc.threshold
            assert bar.atr == atr[t] and vol.bar_date == rc.date
            if bar.atr is not None:
                assert bar.buffer == pytest.approx(bar.level_break_atr * bar.atr)
            assert bar.direction == ("BREAKOUT" if rc.role == "SUPPORT" else "BREAKDOWN")


@pytest.mark.parametrize("seed", [3, 9])
def test_breakout_events_replay_at_any_date(seed: int) -> None:
    bars = random_bars(600, seed)
    full = run_chain(bars).breakouts
    by_id = {e.event_id: e for e in [*full.pattern_events, *full.level_events]}
    for cut in (260, 420):
        end = pd.Timestamp(bars["bar_date"].iloc[cut]).date()
        part = run_chain(bars.iloc[: cut + 1].copy()).breakouts
        for e in [*part.pattern_events, *part.level_events]:
            assert e == by_id[e.event_id].as_of(end), e.event_id
        known = {i for i, e in by_id.items() if e.known_at <= end}
        assert known == {e.event_id for e in [*part.pattern_events, *part.level_events]}


def test_the_event_layer_never_writes_to_its_sources() -> None:
    bars = random_bars(700, seed=9)
    chain = run_chain(bars)
    before = (chain.levels.model_dump(), chain.patterns.model_dump())
    other = AnalysisConfig(
        breakouts=BreakoutsConfig(retest_window=3, retest_tol_atr=2.0, level_false_window=1)
    )
    result = run_analyzer(
        BreakoutEventAnalyzer(other, chain.indicators, chain.levels, chain.patterns),
        bars,
        context(bars),
    )
    assert result != chain.breakouts
    assert (chain.levels.model_dump(), chain.patterns.model_dump()) == before


def test_the_event_layer_reads_no_indicators_for_atr_or_volume() -> None:
    for py in Path(breakouts.__file__).parent.glob("*.py"):
        tree = ast.parse(py.read_text())
        names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | {
            n.id for n in ast.walk(tree) if isinstance(n, ast.Name)
        }
        assert not names & {"numeric", "relative_volume", "volume_state"}, py.name
        text = py.read_text()
        assert "indicators.get" not in text and "indicators.series" not in text, py.name
