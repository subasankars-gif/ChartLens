"""Layer H, Phase 5b-A — confirmation and lifecycle (ADR-0022 §3, §5).

Hand fixtures place the defining swings directly and build bars whose open is the
previous close, so a breakout bar has a real body. The double bottom used throughout:
lows 45.0 (week 5) and 45.3 (week 15), neckline 49.0 (week 10); known at week 16."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest
from analysis_chain import SEG, SID, manual_swings, random_bars, run_chain, week

from chartlens_core.config import AnalysisConfig, IndicatorConfig
from chartlens_engine.patterns import Pattern, PatternResult
from chartlens_engine.patterns.model import BROKEN_OUT, TERMINAL
from chartlens_engine.swings import SwingResult

HAND = AnalysisConfig(indicators=IndicatorConfig(atr_period=2))
Row = tuple[float, float, float, float]
DOUBLE = [("LOW", 5, 6, 45.0), ("HIGH", 10, 11, 49.0), ("LOW", 15, 16, 45.3)]


def rows_from_closes(closes: list[float]) -> list[Row]:
    """open = previous close; high/low 0.5 beyond the body."""
    out: list[Row] = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i else c
        out.append((o, max(o, c) + 0.5, min(o, c) - 0.5, c))
    return out


def frame(rows: list[Row]) -> pd.DataFrame:
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
        }
    ).astype({"open": "float64", "high": "float64", "low": "float64", "close": "float64"})


def run(rows: list[Row], placed: list[tuple[str, int, int, float]] = DOUBLE) -> PatternResult:
    bars = frame(rows)
    return run_chain(bars, HAND, manual_swings(bars, placed)).patterns


def one(result: PatternResult, pattern_type: str = "DOUBLE_BOTTOM") -> Pattern:
    (p,) = [p for p in result.patterns if p.pattern_type == pattern_type]
    return p


def steps(p: Pattern) -> list[tuple[str, date, str]]:
    return [(e.status, e.effective_date, e.reason) for e in p.status_history]


BELOW = [47.0] * 17 + [48.0, 48.5]  # bars 0–18 under the neckline


# ------------------------------------------------------------------ confirmation


def test_prospective_confirmation_then_completion() -> None:
    closes = [*BELOW, 50.0, 51.0, 52.0, 53.0, 54.0, 55.0]
    rows = rows_from_closes(closes)
    p = one(run(rows))
    assert steps(p)[:2] == [
        ("FORMING", week(16), "PATTERN_KNOWN"),
        ("CONFIRMED", week(19), "BREAKOUT_UP"),
    ]
    confirmed = p.status_history[1]
    assert confirmed.known_at == confirmed.effective_date == week(19)
    assert confirmed.measured_values["previous_close"] == 48.5
    mm = p.measured_move
    assert mm is not None and mm.target_calculated_at == week(19)
    assert mm.target_method == "LEVEL_PLUS_HEIGHT"
    assert (mm.target_inputs["level"], mm.target_inputs["height"]) == (49.0, 4.0)
    half = 0.5 * mm.target_inputs["atr_pre"]
    assert (mm.target_low, mm.target_high) == pytest.approx((53.0 - half, 53.0 + half))
    # COMPLETED on the first later bar whose high reaches the zone.
    first = next(t for t in range(20, len(rows)) if rows[t][1] >= mm.target_low)
    assert steps(p)[2] == ("COMPLETED", week(first), "MEASURED_MOVE_REACHED")
    assert len(p.status_history) == 3


def test_already_beyond_when_known_is_recognised_never_confirmed() -> None:
    p = one(run(rows_from_closes([50.0] * 25)))
    assert steps(p)[:2] == [
        ("FORMING", week(16), "PATTERN_KNOWN"),
        ("RECOGNISED_AFTER_BREAKOUT", week(16), "ALREADY_BEYOND_LEVEL"),
    ]
    assert all(e.status != "CONFIRMED" for e in p.status_history)
    assert p.breakout is not None and p.breakout.effective_date >= p.known_at


def test_the_threshold_uses_the_atr_of_the_week_before() -> None:
    # A huge breakout bar: its own range would push ATR (period 2) to ~10 and the
    # threshold to ~2.6 above the neckline. ATR_pre (the bar before) is ~1.
    rows = [*rows_from_closes(BELOW), (48.5, 60.0, 40.0, 49.6), *rows_from_closes([49.6] * 4)[1:]]
    p = one(run(rows))
    assert steps(p)[1] == ("CONFIRMED", week(19), "BREAKOUT_UP")
    values = p.status_history[1].measured_values
    assert values["atr_pre"] < 2.0 and values["close"] - values["level"] >= values["buffer"]


def test_a_bearish_body_does_not_confirm_and_the_next_bar_is_not_fresh() -> None:
    rows = [*rows_from_closes(BELOW), (51.0, 51.5, 49.5, 50.0), (50.0, 51.0, 49.5, 50.5)]
    rows += rows_from_closes([50.5] * 5)[1:]
    p = one(run(rows))
    # Week 19 closes beyond but with a bearish body; week 20 is beyond again, but the
    # previous close was already beyond: no confirmation (ADR-0022 common rule).
    assert p.status == "FORMING"


# ------------------------------------------------------------------ terminal states


def test_invalidation_before_confirmation() -> None:
    p = one(run(rows_from_closes([*BELOW, 47.0, 44.9, 47.0])))
    assert steps(p)[1:] == [("INVALIDATED", week(20), "CLOSE_BEYOND_INVALIDATION")]
    assert p.status_history[1].measured_values == {"close": 44.9, "level": 45.0}


def test_expiry_is_not_invalidation() -> None:
    p = one(run(rows_from_closes([47.0] * (16 + 26 + 3))))
    assert steps(p)[1:] == [("EXPIRED", week(42), "WAIT_WINDOW")]  # 26 bars after week 16


def test_failure_after_confirmation() -> None:
    p = one(run(rows_from_closes([*BELOW, 50.0, 47.5, 50.0])))
    assert [s for s, _, _ in steps(p)] == ["FORMING", "CONFIRMED", "FAILED"]
    assert steps(p)[2] == ("FAILED", week(20), "CLOSE_BACK_THROUGH_LEVEL")


def test_failure_and_completion_on_one_bar_is_failed() -> None:
    rows = [*rows_from_closes([*BELOW, 50.0]), (50.0, 60.0, 47.0, 47.5)]
    p = one(run(rows))
    assert p.status == "FAILED"  # the close is the bar's final state


def test_a_breakout_that_neither_fails_nor_completes_stays_confirmed() -> None:
    p = one(run(rows_from_closes([*BELOW, 50.0] + [50.5] * 60)))
    assert p.status == "CONFIRMED"  # a historical fact; recency is relevance


# ---------------------------------------------------- lines, neutral patterns, V


def _triangle_mid(t: int) -> float:
    return ((52 + 0.00625 * (t - 4)) + (46 + 0.25 * (t - 8))) / 2


TRIANGLE = [
    ("HIGH", 4, 5, 52.0),
    ("LOW", 8, 9, 46.0),
    ("HIGH", 12, 13, 52.05),
    ("LOW", 16, 17, 48.0),
]


def test_a_triangle_expires_at_its_apex() -> None:
    p = one(
        run(rows_from_closes([_triangle_mid(t) for t in range(45)]), TRIANGLE), "TRIANGLE_ASCENDING"
    )
    assert steps(p)[1:] == [("EXPIRED", week(33), "APEX_REACHED")]  # apex at bar 32.7


def test_a_triangle_confirms_on_its_line() -> None:
    closes = [_triangle_mid(t) for t in range(20)] + [
        53.5,
        54.0,
        55.0,
        56.0,
        57.0,
        58.0,
        59.0,
        60.0,
    ]
    p = one(run(rows_from_closes(closes), TRIANGLE), "TRIANGLE_ASCENDING")
    confirmed = p.status_history[1]
    assert (confirmed.status, confirmed.effective_date) == ("CONFIRMED", week(20))
    assert confirmed.measured_values["level"] == pytest.approx(52 + 0.00625 * 16)  # line at bar 20
    assert p.measured_move is not None
    assert p.measured_move.target_inputs["height"] == pytest.approx(7.0)


def test_a_neutral_rectangle_breaks_down_without_changing_its_geometry() -> None:
    rect = [
        ("HIGH", 4, 5, 52.0),
        ("LOW", 8, 9, 48.0),
        ("HIGH", 12, 13, 52.3),
        ("LOW", 16, 17, 47.8),
    ]
    p = one(run(rows_from_closes([50.0] * 20 + [47.0, 46.0]), rect), "RECTANGLE")
    assert steps(p)[1] == ("CONFIRMED", week(20), "BREAKOUT_DOWN")
    assert p.direction == "NEUTRAL"  # geometry and identity are untouched by the breakout
    mm = p.measured_move
    assert mm is not None and mm.target_inputs["direction"] == -1.0
    assert (mm.target_low + mm.target_high) / 2 == pytest.approx(47.8 - 4.5)


def test_v_measured_move_is_a_full_retrace() -> None:
    closes = [53.0] * 15 + [51.0, 52.0, 53.5, 54.0, 55.5, 56.0]
    p = one(
        run(rows_from_closes(closes), [("HIGH", 10, 11, 55.0), ("LOW", 14, 15, 50.5)]), "V_BOTTOM"
    )
    assert p.breakout is not None
    mm = p.measured_move
    assert mm is not None and mm.target_method == "FULL_RETRACE"
    assert (mm.target_low + mm.target_high) / 2 == pytest.approx(55.0)


# ------------------------------------------------------------- historical replay


@pytest.mark.parametrize("seed", [2, 5, 11])
def test_historical_replay_of_every_breakout(seed: int) -> None:
    """run(as_of = breakout week) reproduces the full run's identity, geometry and
    breakout event exactly; run(as_of = the week before) has no breakout event."""
    bars = random_bars(600, seed)
    full = run_chain(bars).patterns
    dates = [pd.Timestamp(d).date() for d in bars["bar_date"]]
    broke = [p for p in full.patterns if p.breakout is not None]
    assert broke
    for p in broke[:: max(1, len(broke) // 6)]:
        assert p.breakout is not None
        c = dates.index(p.breakout.effective_date)
        at = {q.pattern_id: q for q in run_chain(bars.iloc[: c + 1].copy()).patterns.patterns}
        replayed = at[p.pattern_id]
        assert replayed.geometry == p.geometry
        assert replayed.breakout == p.breakout  # level, date, reason, measured move
        assert replayed == p.as_of(dates[c])
        before = {q.pattern_id: q for q in run_chain(bars.iloc[:c].copy()).patterns.patterns}
        if p.breakout.status == "CONFIRMED":
            assert before[p.pattern_id].status == "FORMING"
        else:  # recognised on its known bar: the week before, it does not exist yet
            assert p.pattern_id not in before


@pytest.mark.parametrize("seed", [1, 7])
def test_lifecycle_invariants(seed: int) -> None:
    for p in run_chain(random_bars(700, seed)).patterns.patterns:
        h = p.status_history
        assert h[0].status == "FORMING" and h[0].effective_date == p.known_at
        assert all(e.known_at == e.effective_date for e in h)
        assert [e.effective_date for e in h] == sorted(e.effective_date for e in h)
        assert sum(e.status in BROKEN_OUT for e in h) <= 1
        assert sum(e.status in TERMINAL for e in h) <= 1
        assert all(e.status not in TERMINAL for e in h[:-1])  # terminal is last
        for e in h:
            if e.status == "RECOGNISED_AFTER_BREAKOUT":
                assert e.effective_date == p.known_at
            if e.status == "CONFIRMED":
                assert e.effective_date > p.known_at
            if e.status in ("FAILED", "COMPLETED"):
                assert p.breakout is not None and e.effective_date > p.breakout.effective_date
            if e.status in ("INVALIDATED", "EXPIRED"):
                assert p.breakout is None
        mm = p.measured_move
        if mm is not None:
            assert p.breakout is not None and mm.target_calculated_at == p.breakout.effective_date


# ------------------------------------------------ cup & handle: geometry version 2


def _two_sensitivities(
    bars: pd.DataFrame,
    primary: list[tuple[str, int, int, float]],
    fine: list[tuple[str, int, int, float]],
) -> SwingResult:
    """Primary swings at MICRO and the pattern layer's fine swings at MINOR (configured
    as ``fine_sensitivity``), so a right rim can exist at the fine level only."""
    p = manual_swings(bars, primary, sensitivity="MICRO")
    f = manual_swings(bars, fine, sensitivity="MINOR")
    return p.model_copy(update={"swings": [*p.swings, *f.swings]})


CUP_CFG = HAND.model_copy(
    update={"patterns": HAND.patterns.model_copy(update={"fine_sensitivity": "MINOR"})}
)
BOWL = [60.0] * 5 + [50 + 0.1 * (b - 15) ** 2 for b in range(5, 26)]  # rims 60, low 50
CUP_PRIMARY = [("HIGH", 5, 6, 60.0), ("LOW", 15, 16, 49.5)]  # left rim and cup low


def _cup(
    tail: list[float], fine: list[tuple[str, int, int, float]]
) -> tuple[PatternResult, pd.DataFrame]:
    bars = frame(rows_from_closes(BOWL + tail))
    swings = _two_sensitivities(bars, CUP_PRIMARY, fine)
    return run_chain(bars, CUP_CFG, swings).patterns, bars


SHALLOW_HANDLE = [("HIGH", 25, 26, 60.3), ("LOW", 28, 29, 58.0)]  # handle 2.3 of a 10 cup


def test_a_cup_whose_right_rim_is_only_a_fine_swing_is_known_with_its_handle() -> None:
    """The handle (2.3) is far shallower than the ~3-ATR reversal a primary right rim
    would need: with a fine right rim the pattern is known once the handle low is, and it
    is FORMING then — not invalidated on recognition."""
    tail = [60.3, 59.5, 58.5, 59.0, 59.5, 60.0, 61.5, 62.5, 63.0, 64.0]
    result, bars = _cup(tail, SHALLOW_HANDLE)
    (c,) = [p for p in result.patterns if p.pattern_type == "CUP_HANDLE"]
    assert c.known_at == week(29)
    assert c.as_of(week(29)) is not None and c.as_of(week(29)).status == "FORMING"  # type: ignore[union-attr]
    assert c.geometry.measures["handle_ratio"] < 0.5
    assert [k.label for k in c.geometry.key_points] == ["RIM_1", "CUP_LOW", "RIM_2", "HANDLE"]
    # As of the right rim's known date the handle low does not exist: no pattern yet.
    early = run_chain(
        bars.iloc[:27].copy(),
        CUP_CFG,
        _two_sensitivities(bars.iloc[:27].copy(), CUP_PRIMARY, SHALLOW_HANDLE[:1]),
    ).patterns
    assert not [p for p in early.patterns if p.pattern_type == "CUP_HANDLE"]
    assert c.breakout is not None and c.breakout.status == "CONFIRMED"  # a later, fresh breakout


def test_a_handle_deeper_than_half_the_cup_is_rejected() -> None:
    result, _ = _cup(
        [60.3, 58.0, 55.0, 54.5, 56.0], [("HIGH", 25, 26, 60.3), ("LOW", 28, 29, 54.0)]
    )
    assert not [p for p in result.patterns if p.pattern_type == "CUP_HANDLE"]
    assert "handle_depth" in {r.rule for r in result.rejections if r.family == "cup_handle"}


def test_a_handle_that_breaks_its_low_after_recognition_is_invalidated() -> None:
    result, _ = _cup([60.3, 59.5, 58.5, 59.0, 57.5], SHALLOW_HANDLE)
    (c,) = [p for p in result.patterns if p.pattern_type == "CUP_HANDLE"]
    assert steps(c)[1:] == [("INVALIDATED", week(30), "CLOSE_BEYOND_INVALIDATION")]


# --------------------------------------------------- V: an intrinsic recognition limit


def test_v_recognition_after_the_breakout_is_methodology_not_a_defect() -> None:
    """A V low is a primary swing: it is confirmed only after a reversal (3 ATR by
    default), while the V's confirmation level is 0.618 of a ≥ 4-ATR drop. So a V is
    often already beyond its level when it becomes knowable, and is then
    RECOGNISED_AFTER_BREAKOUT. Do not "fix" this by weakening swing confirmation to raise
    the share of prospective confirmations (Suba, 5b-A review)."""
    closes = [60.0] * 11 + [56.0, 52.0, 50.0, 50.0, 57.0, 57.5]  # V low known at week 15
    rows = rows_from_closes(closes)
    p = one(run(rows, [("HIGH", 10, 11, 60.5), ("LOW", 14, 15, 49.5)]), "V_BOTTOM")
    level = p.geometry.confirmation_level
    assert level == pytest.approx(49.5 + 0.618 * 11)  # 56.3; week 15 already closes at 57
    assert steps(p)[1] == ("RECOGNISED_AFTER_BREAKOUT", week(15), "ALREADY_BEYOND_LEVEL")


# -------------------------------------------- a terminal decision never changes later


def test_a_same_week_tie_stays_failed_in_every_later_replay() -> None:
    """FAILED and COMPLETED both true on week 20: FAILED. Runs as of week 20, week 21 and
    the full history (where price later reaches the zone again) all keep FAILED at week
    20; no later bar upgrades it."""
    rows = [*rows_from_closes([*BELOW, 50.0]), (50.0, 60.0, 47.0, 47.5)]
    rows += rows_from_closes([47.5, 52.0, 55.0, 58.0, 60.0])[1:]
    bars = frame(rows)
    terminal = []
    for end in (21, 22, len(rows)):
        part = bars.iloc[:end].copy()
        p = one(run_chain(part, HAND, manual_swings(part, DOUBLE)).patterns)
        terminal.append((p.status, p.status_history[-1].effective_date, p.status_history[-1]))
    assert {t[:2] for t in terminal} == {("FAILED", week(20))}
    assert terminal[0][2] == terminal[1][2] == terminal[2][2]
