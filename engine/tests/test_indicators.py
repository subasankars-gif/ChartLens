"""Layer A — indicators (ADR-0020 §A): known values, warm-up, causality, segments, as_of."""

from __future__ import annotations

import math
from datetime import date

import numpy as np
import pandas as pd
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from chartlens_core.asof import AsOfViolation
from chartlens_core.bars import BarFrameError
from chartlens_core.config import IndicatorConfig
from chartlens_core.domain import Timeframe
from chartlens_core.testing import make_bars
from chartlens_engine.indicators import IndicatorAnalyzer, IndicatorResult
from chartlens_engine.indicators import functions as f
from chartlens_engine.interfaces import AnalysisContext, run_analyzer

SID = "SEC-IND"
NAN = float("nan")


def arr(*xs: float) -> np.ndarray:
    return np.array(xs, dtype=np.float64)


def same(a: np.ndarray, b: list[float]) -> bool:
    return len(a) == len(b) and all(
        (math.isnan(x) and math.isnan(y)) or math.isclose(x, y, rel_tol=0, abs_tol=1e-9)
        for x, y in zip(a.tolist(), b, strict=True)
    )


def weekly(periods: int, seed: int = 0, segment: str | None = None) -> pd.DataFrame:
    seg = segment or f"{SID}@2010-01-08"
    return make_bars(date(2010, 1, 4), periods, freq="W-FRI", seed=seed).assign(
        security_id=SID, continuity_segment_id=seg
    )


def analyze(bars: pd.DataFrame, config: IndicatorConfig | None = None) -> IndicatorResult:
    last = pd.Timestamp(bars["bar_date"].iloc[-1]).date()
    ctx = AnalysisContext(
        security_id=SID,
        timeframe=Timeframe.WEEKLY,
        as_of=last,
        methodology_hash="test",
        continuity_segment_id=str(bars["continuity_segment_id"].iloc[0]),
    )
    return run_analyzer(IndicatorAnalyzer(config or IndicatorConfig()), bars, ctx)


# ----------------------------------------------------------------------------- known values


def test_moving_averages_by_hand() -> None:
    assert same(f.sma(arr(1, 2, 3, 4, 5), 3), [NAN, NAN, 2, 3, 4])
    # EMA(3): α = 0.5, seeded with SMA(3) = 4 at t=2.
    assert same(f.ema(arr(2, 4, 6, 8, 4), 3), [NAN, NAN, 4, 6, 5])
    assert same(f.sma(arr(1, 2), 3), [NAN, NAN])  # too short: nothing, never a guess


def test_wilder_rsi_by_hand() -> None:
    # Δ = +1, −1, +2, 0, −3. First averages over Δ1..Δ2: G = L = 0.5 → 50.
    out = f.rsi(arr(10, 11, 10, 12, 12, 9), 2)
    assert same(out, [NAN, NAN, 50.0, 100 - 100 / 6, 100 - 100 / 6, 100 - 100 / 1.2])
    assert same(f.rsi(arr(5, 5, 5, 5), 2), [NAN, NAN, 50.0, 50.0])  # no movement
    assert same(f.rsi(arr(1, 2, 3, 4), 2), [NAN, NAN, 100.0, 100.0])  # no losses


def test_wilder_atr_by_hand() -> None:
    h, lo, c = arr(11, 12, 13, 12.5), arr(9, 10, 11.5, 10), arr(10, 11, 12, 10.5)
    assert same(f.true_range(h, lo, c), [NAN, 2.0, 2.0, 2.5])
    assert same(f.atr(h, lo, c, 2), [NAN, NAN, 2.0, 2.25])  # TR[0] never enters ATR
    assert same(f.atr_percent(arr(NAN, 2.0), arr(10, 8)), [NAN, 25.0])


def test_macd_by_hand() -> None:
    line, signal, hist = f.macd(arr(1, 2, 3, 4, 5, 6), 2, 3, 2)
    # EMA2 = [., 1.5, 2.5, 3.5, 4.5, 5.5]; EMA3 = [., ., 2, 3, 4, 5]
    assert same(line, [NAN, NAN, 0.5, 0.5, 0.5, 0.5])
    assert same(signal, [NAN, NAN, NAN, 0.5, 0.5, 0.5])  # seeded with SMA of 2 line values
    assert same(hist, [NAN, NAN, NAN, 0, 0, 0])


def test_stochastic_by_hand() -> None:
    raw, k, d = f.stochastic(
        arr(10, 12, 11, 13, 13), arr(8, 9, 9, 10, 13), arr(9, 11, 10, 12, 13), 3, 2, 2
    )
    assert same(raw, [NAN, NAN, 50.0, 75.0, 100.0])
    assert same(k, [NAN, NAN, NAN, 62.5, 87.5])
    assert same(d, [NAN, NAN, NAN, NAN, 75.0])
    flat, _, _ = f.stochastic(arr(5, 5, 5), arr(5, 5, 5), arr(5, 5, 5), 3, 1, 1)
    assert same(flat, [NAN, NAN, 50.0])  # HH = LL: 50 by convention


def test_roc_bollinger_volume_by_hand() -> None:
    assert same(f.roc(arr(10, 20, 15, 30), 2), [NAN, NAN, 50.0, 50.0])
    assert same(f.roc(arr(0, 1, 2), 2), [NAN, NAN, NAN])  # base 0: no value
    mid, upper, lower, width = f.bollinger(arr(1, 3, 5), 2, 1.0)
    assert same(mid, [NAN, 2, 4]) and same(upper, [NAN, 3, 5]) and same(lower, [NAN, 1, 3])
    assert same(width, [NAN, 1.0, 0.5])
    # RVOL never includes the current week in its own baseline.
    assert same(f.relative_volume(arr(10, 20, 30, 60), 2), [NAN, NAN, 2.0, 2.4])
    assert same(f.obv(arr(10, 11, 10, 10, 12), arr(5, 6, 7, 8, 9)), [0, 6, -1, -1, 8])
    assert f.volume_state(arr(NAN, 1.6, 0.5, 1.0), 1.5, 0.67) == [
        None,
        "EXPANSION",
        "CONTRACTION",
        "NORMAL",
    ]
    assert f.volume_trend(arr(1, 1, 1, 4, 4, 4), 2, 4, 0.1)[-3:] == ["RISING", "RISING", "RISING"]


def test_windows_match_an_independent_implementation() -> None:
    close = make_bars(date(2010, 1, 4), 300, freq="W-FRI", seed=3)["close"].to_numpy()
    s = pd.Series(close)
    for n in (10, 20, 50, 200):
        assert np.allclose(f.sma(close, n), s.rolling(n).mean().to_numpy(), equal_nan=True)
    _, upper, _, _ = f.bollinger(close, 20, 2.0)
    expected = s.rolling(20).mean() + 2 * s.rolling(20).std(ddof=0)
    assert np.allclose(upper, expected.to_numpy(), equal_nan=True)


# ----------------------------------------------------------------------------- the analyzer


def test_warm_up_is_exactly_the_documented_one() -> None:
    result = analyze(weekly(260))
    first = {s.name: s.warmup_bars for s in result.series}
    expected = {
        "sma_10": 9,
        "sma_200": 199,
        "ema_10": 9,
        "ema_200": 199,
        "rsi": 14,
        "macd": 25,
        "macd_signal": 33,
        "macd_histogram": 33,
        "stochastic_k": 15,
        "stochastic_d": 17,
        "roc": 12,
        "atr": 14,
        "atr_percent": 14,
        "bollinger_upper": 19,
        "bollinger_bandwidth": 19,
        "volume_sma": 19,
        "relative_volume": 20,
        "obv": 0,
        "volume_trend": 19,
        "volume_state": 20,
    }
    assert {k: first[k] for k in expected} == expected
    for s in result.series:  # null before warm-up, a value from then on
        assert all(v is None for v in s.data[: s.warmup_bars])
        assert all(v is not None for v in s.data[s.warmup_bars :])


def test_a_short_segment_has_no_long_averages() -> None:
    """RELIANCE's valid segment has ~168 weeks: no 200-week SMA. Not missing data."""
    result = analyze(weekly(168))
    assert result.get("200W").data == [None] * 168
    assert result.get("sma_200").warmup_bars == 168
    assert result.get("100W").data[99] is not None


def test_aliases_are_the_same_series() -> None:
    result = analyze(weekly(60))
    assert result.get("20W") is result.get("sma_20")
    assert len([s for s in result.series if s.name == "sma_20"]) == 1


def test_the_forming_week_is_provisional() -> None:
    bars = weekly(30).assign(is_complete=True)
    bars.loc[bars.index[-1], "is_complete"] = False
    result = analyze(bars)
    assert result.provisional == [False] * 29 + [True]


def test_deterministic_and_refuses_guessing() -> None:
    bars = weekly(120, seed=9)
    assert analyze(bars).model_dump_json() == analyze(bars).model_dump_json()
    broken = bars.copy()
    broken.loc[broken.index[5], "close"] = NAN
    with pytest.raises(BarFrameError, match="NaN"):
        analyze(broken)


def test_segments_never_share_warm_up() -> None:
    """Warm-up starts again after a continuity break: segment B alone is what is analysed,
    and nothing from segment A reaches it (ADR-0019)."""
    a = weekly(80, seed=1, segment=f"{SID}@2010-01-08")
    b = make_bars(date(2012, 1, 2), 60, freq="W-FRI", seed=2).assign(
        security_id=SID, continuity_segment_id=f"{SID}@2012-01-06"
    )
    with pytest.raises(BarFrameError, match="continuity"):
        analyze(pd.concat([a, b], ignore_index=True))
    result = analyze(b)
    assert result.get("sma_50").warmup_bars == 49  # counted inside segment B only


def test_bars_after_as_of_are_refused() -> None:
    bars = weekly(40)
    ctx = AnalysisContext(
        security_id=SID,
        timeframe=Timeframe.WEEKLY,
        as_of=pd.Timestamp(bars["bar_date"].iloc[20]).date(),
        methodology_hash="test",
        continuity_segment_id=str(bars["continuity_segment_id"].iloc[0]),
    )
    with pytest.raises(AsOfViolation):
        run_analyzer(IndicatorAnalyzer(IndicatorConfig()), bars, ctx)


# ----------------------------------------------------------------------------- causality


@settings(max_examples=25, deadline=None)
@given(
    periods=st.integers(min_value=1, max_value=260),
    seed=st.integers(min_value=0, max_value=10_000),
    cuts=st.lists(st.floats(min_value=0.0, max_value=1.0), min_size=1, max_size=6),
)
def test_every_prefix_gives_identical_values(periods: int, seed: int, cuts: list[float]) -> None:
    """analysis(as_of = T) equals the full analysis cut at T, bit for bit: no value ever
    depends on a later bar (ADR-0019 prefix stability)."""
    bars = weekly(periods, seed=seed)
    full = analyze(bars)
    for cut in cuts:
        k = max(1, round(cut * periods))
        part = analyze(bars.iloc[:k].copy())
        assert part.bar_dates == full.bar_dates[:k]
        for p, s in zip(part.series, full.series, strict=True):
            assert p.name == s.name
            assert p.data == s.data[:k], p.name
