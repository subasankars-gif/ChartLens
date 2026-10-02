"""Layer B — swing points (ADR-0020 §B): hand-calculated pivots, ``known_at``, and
causality proved three ways — prefix stability, sufficiency (the bars up to ``known_at``
establish the swing) and future independence (later bars cannot change it)."""

from __future__ import annotations

from datetime import date
from itertools import pairwise

import numpy as np
import pandas as pd
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from chartlens_core.bars import BarFrameError
from chartlens_core.config import SENSITIVITIES, IndicatorConfig, SwingConfig
from chartlens_core.domain import Timeframe
from chartlens_core.testing import make_bars
from chartlens_engine.indicators import IndicatorAnalyzer
from chartlens_engine.interfaces import AnalysisContext, run_analyzer
from chartlens_engine.swings import METHODS, SwingAnalyzer, SwingPoint, SwingResult
from chartlens_engine.swings import methods as m

SID = "SEC-SW"
SEG = f"{SID}@2010-01-08"


def arr(*xs: float) -> np.ndarray:
    return np.array(xs, dtype=np.float64)


def bars_from(
    highs: list[float], lows: list[float], start: date = date(2010, 1, 8), **extra: object
) -> pd.DataFrame:
    closes = [(h + lo) / 2 for h, lo in zip(highs, lows, strict=True)]
    frame = pd.DataFrame(
        {
            "bar_date": pd.date_range(start, periods=len(highs), freq="W-FRI"),
            "open": closes,
            "high": [float(h) for h in highs],
            "low": [float(lo) for lo in lows],
            "close": closes,
            "volume": [1000.0] * len(highs),
            "security_id": SID,
            "continuity_segment_id": SEG,
        }
    )
    return frame.assign(**extra) if extra else frame


def random_bars(periods: int, seed: int) -> pd.DataFrame:
    return make_bars(date(2010, 1, 4), periods, freq="W-FRI", seed=seed).assign(
        security_id=SID, continuity_segment_id=SEG
    )


def analyze(bars: pd.DataFrame, config: SwingConfig | None = None) -> SwingResult:
    ctx = AnalysisContext(
        security_id=SID,
        timeframe=Timeframe.WEEKLY,
        as_of=pd.Timestamp(bars["bar_date"].iloc[-1]).date(),
        methodology_hash="test",
        continuity_segment_id=str(bars["continuity_segment_id"].iloc[0]),
    )
    indicators = run_analyzer(IndicatorAnalyzer(IndicatorConfig()), bars, ctx)
    return run_analyzer(SwingAnalyzer(config or SwingConfig(), indicators), bars, ctx)


def key(s: SwingPoint) -> tuple[object, ...]:
    return (s.method, s.sensitivity, s.type, s.bar_date)


# ----------------------------------------------------------------------------- by hand


def test_fractal_by_hand_with_ties_to_the_earliest_bar() -> None:
    scan = m.fractal(arr(1, 3, 2, 5, 4, 4, 6), arr(0.5, 2, 1, 4, 3, 3.5, 5), 1)
    assert [(p.kind, p.bar, p.known) for p in scan.pivots] == [
        ("HIGH", 1, 2),
        ("LOW", 2, 3),
        ("HIGH", 3, 4),
        ("LOW", 4, 5),
    ]
    ties = m.fractal(arr(1, 3, 3, 2), arr(1, 1, 1, 1), 1)
    assert [(p.kind, p.bar) for p in ties.pivots] == [("HIGH", 1)]
    assert scan.pending is None  # fractals have no developing leg


def test_zigzag_by_hand() -> None:
    highs = arr(100, 110, 112, 105, 104, 107)
    lows = arr(95, 105, 108, 99, 97, 101)
    scan = m.zigzag(highs, lows, m.percent_threshold(10))
    assert [(p.kind, p.bar, p.known, p.price) for p in scan.pivots] == [
        ("LOW", 0, 1, 95.0),
        ("HIGH", 2, 3, 112.0),
        ("LOW", 4, 5, 97.0),
    ]
    assert scan.pending == m.Pending("HIGH", 5, 107.0)


def test_a_bar_is_checked_for_reversal_before_it_can_extend_the_leg() -> None:
    # Up-leg high 112 at bar 2; bar 3 trades 120 but its low (100) already reverses 10 %
    # from 112: the swing high stays at bar 2, and the down-leg starts from bar 3's low.
    scan = m.zigzag(arr(100, 110, 112, 120), arr(95, 105, 108, 100), m.percent_threshold(10))
    assert [(p.kind, p.bar, p.known) for p in scan.pivots] == [("LOW", 0, 1), ("HIGH", 2, 3)]
    assert scan.pending == m.Pending("LOW", 3, 100.0)


def test_atr_swings_wait_for_atr_at_the_pivot_bar() -> None:
    highs, lows = arr(100, 120, 90, 130), arr(90, 110, 80, 120)
    none = m.zigzag(highs, lows, m.atr_threshold(arr(np.nan, np.nan, np.nan, np.nan), 1.0))
    assert none.pivots == ()
    some = m.zigzag(highs, lows, m.atr_threshold(arr(5, 5, 5, 5), 1.0))
    assert [(p.kind, p.bar) for p in some.pivots] == [("LOW", 0), ("HIGH", 1), ("LOW", 2)]


# ----------------------------------------------------------------------------- known_at


def test_known_at_is_the_confirming_bar_not_the_pivot_bar() -> None:
    """The ADR example: a pivot on 20 Jun 2025 confirmed on 4 Jul 2025."""
    bars = bars_from([1, 2, 5, 3, 2], [0.5, 1, 4, 2, 1], start=date(2025, 6, 6))
    swing = next(s for s in analyze(bars).of("FRACTAL", "MINOR") if s.bar_date == date(2025, 6, 20))
    assert (swing.bar_date, swing.known_at) == (date(2025, 6, 20), date(2025, 7, 4))
    # As of 25 Jun it is invisible, although its bar is earlier.
    assert swing not in analyze(bars).known_by(date(2025, 6, 25))
    upto_27_jun = analyze(bars.iloc[:4].copy())
    assert all(s.bar_date != date(2025, 6, 20) for s in upto_27_jun.of("FRACTAL", "MINOR"))


def test_every_swing_is_known_on_or_after_its_bar() -> None:
    result = analyze(random_bars(400, seed=11))
    assert result.swings
    assert all(s.known_at is not None and s.bar_date <= s.known_at for s in result.swings)


# ----------------------------------------------------------------------------- causality


@settings(max_examples=20, deadline=None)
@given(
    periods=st.integers(min_value=20, max_value=300),
    seed=st.integers(min_value=0, max_value=10_000),
    cuts=st.lists(st.floats(min_value=0.05, max_value=1.0), min_size=1, max_size=4),
)
def test_prefix_stability(periods: int, seed: int, cuts: list[float]) -> None:
    """A run on the bars up to T equals the full run's swings with known_at <= T, field
    for field; and a prefix's pending leg is never retroactively a swing."""
    bars = random_bars(periods, seed)
    full = analyze(bars)
    for cut in cuts:
        k = max(1, round(cut * periods))
        part = analyze(bars.iloc[:k].copy())
        end = pd.Timestamp(bars["bar_date"].iloc[k - 1]).date()
        assert part.swings == full.known_by(end)
        for p in part.pending:
            later = [s for s in full.swings if key(s) == key(p)]
            assert all(s.known_at is not None and s.known_at > end for s in later)


def test_the_bars_up_to_known_at_establish_every_swing() -> None:
    """Sufficiency: the data up to a swing's known_at — and nothing later — produce it."""
    bars = random_bars(220, seed=5)
    full = analyze(bars)
    dates = [pd.Timestamp(d).date() for d in bars["bar_date"]]
    for s in full.primary() + full.of("FRACTAL", "MAJOR"):
        assert s.known_at is not None
        upto = analyze(bars.iloc[: dates.index(s.known_at) + 1].copy())
        assert s in upto.swings


def test_later_bars_cannot_change_a_known_swing() -> None:
    """Future independence: replace everything after a cut-off with other data; every
    swing known by the cut-off is unchanged."""
    bars = random_bars(260, seed=21)
    cutoff_index = 150
    other = random_bars(260, seed=999)
    scale = bars["close"].iloc[cutoff_index] / other["close"].iloc[cutoff_index]
    altered = bars.copy()
    for col in ("open", "high", "low", "close"):
        altered.loc[cutoff_index + 1 :, col] = other[col].iloc[cutoff_index + 1 :] * scale
    cutoff = pd.Timestamp(bars["bar_date"].iloc[cutoff_index]).date()
    assert analyze(bars).known_by(cutoff) == analyze(altered).known_by(cutoff)


# ----------------------------------------------------------------------------- rules


def test_deterministic_serialization() -> None:
    bars = random_bars(300, seed=3)
    assert analyze(bars).model_dump_json() == analyze(bars).model_dump_json()


def test_segment_isolation_and_local_warm_up() -> None:
    a = random_bars(80, seed=1)
    b = make_bars(date(2012, 1, 2), 90, freq="W-FRI", seed=2).assign(
        security_id=SID, continuity_segment_id=f"{SID}@2012-01-06"
    )
    with pytest.raises(BarFrameError, match="continuity"):
        analyze(pd.concat([a, b], ignore_index=True))
    result = analyze(b)
    first = pd.Timestamp(b["bar_date"].iloc[0]).date()
    assert all(s.bar_date >= first for s in result.swings + result.pending)
    assert all(s.continuity_segment_id == f"{SID}@2012-01-06" for s in result.swings)
    atr_warmup = IndicatorConfig().atr_period
    assert all(s.bar_index >= atr_warmup for s in result.swings if s.method == "ATR")
    cfg = SwingConfig()
    for s in result.of("FRACTAL", "MAJOR"):
        assert s.bar_index >= cfg.fractal_window.MAJOR


def test_the_forming_week_cannot_confirm_or_create_a_swing() -> None:
    bars = random_bars(120, seed=8).assign(is_complete=True)
    settled = analyze(bars.iloc[:-1].copy())
    # The forming week crashes far below every leg: it would reverse every up-leg.
    bars.loc[bars.index[-1], ["low", "close", "open"]] = 1.0
    bars.loc[bars.index[-1], "is_complete"] = False
    forming = analyze(bars)
    assert forming.swings == settled.swings
    last = pd.Timestamp(bars["bar_date"].iloc[-1]).date()
    assert all(s.bar_date != last for s in forming.swings + forming.pending)


def test_a_special_session_close_makes_the_confirmation_provisional() -> None:
    bars = bars_from([1, 2, 5, 3, 2, 2.5], [0.5, 1, 4, 2, 1, 1.5], closes_on_special_session=False)
    bars.loc[4, "closes_on_special_session"] = True  # the bar confirming the bar-2 pivot
    swing = next(s for s in analyze(bars).of("FRACTAL", "MINOR") if s.bar_index == 2)
    assert swing.provisional is True
    # Still subject to completeness: incomplete + special confirms nothing.
    bars = bars.iloc[:5].assign(is_complete=[True, True, True, True, False])
    assert all(s.bar_index != 2 for s in analyze(bars).of("FRACTAL", "MINOR"))


def test_sensitivities_and_methods_are_independent_and_ordered() -> None:
    bars = random_bars(500, seed=13)
    result = analyze(bars)
    high, low = bars["high"].to_numpy(), bars["low"].to_numpy()
    close = bars["close"].to_numpy()
    cfg = SwingConfig()
    atr = np.array([np.nan if v is None else v for v in result_indicators(bars).get("atr").data])
    for s in SENSITIVITIES:
        direct = {
            "FRACTAL": m.fractal(high, low, int(cfg.fractal_window.at(s))),
            "ATR": m.zigzag(high, low, m.atr_threshold(atr, cfg.atr_multiple.at(s))),
            "PERCENT": m.zigzag(high, low, m.percent_threshold(cfg.percent.at(s))),
            "ZIGZAG": m.zigzag(close, close, m.percent_threshold(cfg.zigzag_percent.at(s))),
        }
        for method in METHODS:  # no hidden state: same as running the method alone
            got = [(x.bar_index, x.type) for x in result.of(method, s)]
            assert got == sorted((p.bar, p.kind) for p in direct[method].pivots), (method, s)
    # FRACTAL: a MAJOR pivot is also a MAJOR-to-MICRO pivot (nested).
    sets = [{(x.type, x.bar_index) for x in result.of("FRACTAL", s)} for s in SENSITIVITIES]
    assert sets[3] <= sets[2] <= sets[1] <= sets[0]
    # ZigZag-type swings alternate HIGH, LOW, HIGH...
    for method in ("ATR", "PERCENT", "ZIGZAG"):
        types = [x.type for x in result.of(method, "INTERMEDIATE")]
        assert all(a != b for a, b in pairwise(types))


def result_indicators(bars: pd.DataFrame):  # type: ignore[no-untyped-def]
    ctx = AnalysisContext(
        security_id=SID,
        timeframe=Timeframe.WEEKLY,
        as_of=pd.Timestamp(bars["bar_date"].iloc[-1]).date(),
        methodology_hash="test",
        continuity_segment_id=SEG,
    )
    return run_analyzer(IndicatorAnalyzer(IndicatorConfig()), bars, ctx)


def test_the_primary_swings_come_from_configuration() -> None:
    bars = random_bars(200, seed=4)
    default = analyze(bars)
    assert (default.primary_method, default.primary_sensitivity) == ("ATR", "INTERMEDIATE")
    assert default.primary() == default.of("ATR", "INTERMEDIATE")
    other = analyze(bars, SwingConfig(primary_method="FRACTAL", primary_sensitivity="MINOR"))
    assert other.primary() == other.of("FRACTAL", "MINOR")


def test_pending_legs_are_never_confirmed_swings() -> None:
    result = analyze(random_bars(300, seed=17))
    assert result.pending
    assert all(not p.confirmed and p.known_at is None for p in result.pending)
    assert all(s.confirmed for s in result.swings)
    assert {p.method for p in result.pending} <= {"ATR", "PERCENT", "ZIGZAG"}


def test_indicators_for_other_bars_are_refused() -> None:
    bars = random_bars(60, seed=1)
    ctx = AnalysisContext(
        security_id=SID,
        timeframe=Timeframe.WEEKLY,
        as_of=pd.Timestamp(bars["bar_date"].iloc[-1]).date(),
        methodology_hash="test",
        continuity_segment_id=SEG,
    )
    shorter = bars.iloc[:50].copy()
    ind = run_analyzer(
        IndicatorAnalyzer(IndicatorConfig()),
        shorter,
        ctx.model_copy(update={"as_of": pd.Timestamp(shorter["bar_date"].iloc[-1]).date()}),
    )
    with pytest.raises(ValueError, match="other bars"):
        run_analyzer(SwingAnalyzer(SwingConfig(), ind), bars, ctx)
