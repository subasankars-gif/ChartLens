"""Layer F — divergence, volume, volatility and candle evidence (ADR-0021 §F): one fixture
per rule with its near misses, plus rule-by-rule checks on random series."""

from __future__ import annotations

import numpy as np
import pytest
from analysis_chain import (
    bars_from_closes,
    bars_from_ohlc,
    manual_swings,
    random_bars,
    run_chain,
    week,
)

from chartlens_core.config import (
    AnalysisConfig,
    CandleConfig,
    DivergenceConfig,
    IndicatorConfig,
    SwingConfig,
    VolatilityConfig,
)
from chartlens_engine.evidence.divergence import _RULES  # pyright: ignore[reportPrivateUsage]

HAND = AnalysisConfig(
    indicators=IndicatorConfig(atr_period=2),
    swings=SwingConfig(primary_method="FRACTAL", primary_sensitivity="MICRO"),
)


def _with(**sections: object) -> AnalysisConfig:
    return HAND.model_copy(update=sections)


# ------------------------------------------------------------------------ divergence

# A long drift down, a sharp fall to a low (bar 33), a rally to a high (bar 37), then a
# slow drift to a lower low (bar 48) and a turn. FRACTAL/MICRO confirms each one bar later.
BASE = [
    *[100.0 - 0.2 * i for i in range(30)],
    90.0, 84.0, 78.0, 72.0,
    80.0, 86.0, 90.0, 92.0, 91.0,
    89.0, 87.0, 85.0, 83.0, 81.0, 79.0, 77.0, 75.0, 73.0, 71.0,
    74.0, 78.0,
]  # fmt: skip


def _rsi_divergence(closes: list[float], cfg: AnalysisConfig = HAND):
    chain = run_chain(bars_from_closes(closes), cfg)
    rsi = [d for d in chain.divergence.divergences if d.indicator == "rsi"]
    return chain, rsi


def test_regular_bullish_divergence_by_hand() -> None:
    chain, (div,) = _rsi_divergence(BASE)
    rsi = chain.indicators.get("rsi").data
    assert (div.type, div.price_label) == ("REGULAR_BULLISH", "LL")
    assert (div.date_start, div.date_end, div.known_at) == (week(33), week(48), week(49))
    assert (div.price_1, div.price_2) == (71.5, 70.5)
    assert (div.indicator_1, div.indicator_2) == (rsi[33], rsi[48])
    assert div.strength == pytest.approx((div.indicator_2 - div.indicator_1) / 2.0)
    assert div.confirmation.level == 92.5  # the intervening swing high
    assert div.invalidation.level == 70.5
    assert [(e.status, e.date) for e in div.status_history] == [("FORMING", week(49))]
    assert div.as_of(week(48)) is None


def test_the_indicator_threshold_is_strict() -> None:
    _, (div,) = _rsi_divergence(BASE)
    change = div.indicator_2 - div.indicator_1
    at = _with(divergence=DivergenceConfig(rsi_min_delta=change))
    below = _with(divergence=DivergenceConfig(rsi_min_delta=change - 1e-9))
    assert _rsi_divergence(BASE, at)[1] == []
    assert len(_rsi_divergence(BASE, below)[1]) == 1


@pytest.mark.parametrize(("lo", "hi", "found"), [(15, 52, True), (16, 52, False), (4, 14, False)])
def test_swing_distance_window(lo: int, hi: int, found: bool) -> None:
    cfg = _with(divergence=DivergenceConfig(min_bars=lo, max_bars=hi))
    assert bool(_rsi_divergence(BASE, cfg)[1]) is found  # the lows are 15 bars apart


@pytest.mark.parametrize(
    ("tail", "status", "on"),
    [
        ([85.0, 93.0], "CONFIRMED", 52),  # close above the intervening high (92.5)
        ([72.0, 70.0], "INVALIDATED", 52),  # close below the second low (70.5)
    ],
)
def test_divergence_confirmation_and_invalidation(tail: list[float], status: str, on: int) -> None:
    _, (div,) = _rsi_divergence([*BASE, *tail])
    assert [(e.status, e.date) for e in div.status_history] == [
        ("FORMING", week(49)),
        (status, week(on)),
    ]


def test_divergence_expires() -> None:
    cfg = _with(divergence=DivergenceConfig(expiry_weeks=2))
    _, (div,) = _rsi_divergence([*BASE, 79.0, 80.0], cfg)
    assert [(e.status, e.date) for e in div.status_history] == [
        ("FORMING", week(49)),
        ("EXPIRED", week(51)),
    ]


@pytest.mark.parametrize("seed", [11, 12, 13])
def test_every_divergence_follows_the_table_and_none_is_missed(seed: int) -> None:
    chain = run_chain(random_bars(700, seed))
    cfg = DivergenceConfig()
    by_id = {s.swing_id: s for s in chain.swings.primary()}
    atr = chain.indicators.get("atr").data
    vsma = chain.indicators.get("volume_sma").data
    expected = set()
    for label in chain.structure.labels:
        if label.label not in _RULES:
            continue
        a, b = by_id[label.previous_swing_id], by_id[label.swing_id]
        if not cfg.min_bars <= b.bar_index - a.bar_index <= cfg.max_bars:
            continue
        kind, sign = _RULES[label.label]
        for name in cfg.indicators:
            data = chain.indicators.get(name).data
            i1, i2 = data[a.bar_index], data[b.bar_index]
            scale = {"rsi": 1.0, "macd": atr[b.bar_index], "obv": vsma[b.bar_index]}[name]
            if i1 is None or i2 is None or scale is None:
                continue
            delta = {
                "rsi": cfg.rsi_min_delta,
                "macd": cfg.macd_min_delta_atr * float(scale),
                "obv": cfg.obv_min_delta_volume * float(scale),
            }[name]
            if sign * (float(i2) - float(i1)) > delta:
                expected.add((name, kind, a.swing_id, b.swing_id))
    got = {
        (d.indicator, d.type, d.price_swing_1, d.price_swing_2)
        for d in chain.divergence.divergences
    }
    assert got == expected
    assert {k for _, k, _, _ in got} >= {"REGULAR_BULLISH", "REGULAR_BEARISH"}


# ---------------------------------------------------------------------------- volume

FLAT = [50.0] * 30


@pytest.mark.parametrize(
    ("close", "volume", "climax"),
    [
        (53.0, 3000.0, True),
        (53.0, 2490.0, False),  # relative volume 2.49 < 2.5
        (51.5, 3000.0, True),  # true range 2.0 = 2 x ATR 1.0
        (51.49, 3000.0, False),
    ],
)
def test_volume_climax(close: float, volume: float, climax: bool) -> None:
    closes, volumes = list(FLAT), [1000.0] * 30
    closes[25], volumes[25] = close, volume
    events = run_chain(bars_from_closes(closes, volume=volumes), HAND).volume.events
    found = [e for e in events if e.kind == "VOLUME_CLIMAX"]
    assert [e.bar_date for e in found] == ([week(25)] if climax else [])


@pytest.mark.parametrize(
    ("volume", "kind"),
    [
        (2000.0, "BREAKOUT_VOLUME_CONFIRMATION"),
        (500.0, "BREAKOUT_VOLUME_CONTRADICTION"),
        (1000.0, None),
    ],
)
def test_breakout_volume_on_a_structure_break(volume: float, kind: str | None) -> None:
    closes = [49.0] * 25 + [50.0] * 5
    volumes = [1000.0] * 30
    volumes[25] = volume
    bars = bars_from_closes(closes, volume=volumes)
    chain = run_chain(bars, HAND, manual_swings(bars, [("HIGH", 22, 23, 49.5)]))
    (bos,) = chain.structure.events
    assert bos.bar_date == week(25)
    found = [e for e in chain.volume.events if e.kind.startswith("BREAKOUT")]
    if kind is None:
        assert found == []
    else:
        (event,) = found
        assert (event.kind, event.known_at, event.depends_on) == (kind, week(25), (bos.event_id,))


def test_obv_trend_state() -> None:
    rising = [50 + 0.5 * i for i in range(40)]
    state = run_chain(bars_from_closes(rising), HAND).volume.state
    assert state is not None
    assert (state.obv_trend, state.obv_change_ratio) == ("RISING", 1.0)
    falling = run_chain(bars_from_closes(rising[::-1]), HAND).volume.state
    assert falling is not None and falling.obv_trend == "FALLING"


@pytest.mark.parametrize("seed", [21, 22])
def test_volume_divergence_follows_structure_labels(seed: int) -> None:
    chain = run_chain(random_bars(700, seed))
    by_id = {s.swing_id: s for s in chain.swings.primary()}
    vsma = chain.indicators.get("volume_sma").data
    expected = set()
    for label in chain.structure.labels:
        if label.label in ("HH", "LL"):
            a, b = by_id[label.previous_swing_id], by_id[label.swing_id]
            v1, v2 = vsma[a.bar_index], vsma[b.bar_index]
            if v1 is not None and v2 is not None and (float(v1) - float(v2)) / float(v1) > 0.10:
                expected.add(label.known_at)
    got = {e.known_at for e in chain.volume.events if e.kind == "VOLUME_DIVERGENCE"}
    assert got == expected and got


# ------------------------------------------------------------------------ volatility


def _flat_ranges(ranges: list[float]) -> list[tuple[float, float, float, float]]:
    return [(50.0, 50 + r / 2, 50 - r / 2, 50.0) for r in ranges]


def test_nr7_is_strictly_the_narrowest_and_one_event_per_run() -> None:
    chain = run_chain(bars_from_ohlc(_flat_ranges([5, 5, 4, 3, 2.5, 2, 1.5, 1, 0.8])), HAND)
    narrow = [e for e in chain.volatility.events if e.kind == "RANGE_CONTRACTION"]
    assert [e.bar_date for e in narrow] == [week(7)]  # bars 7 and 8 are one episode
    assert chain.volatility.state is not None and chain.volatility.state.range_contracted
    tie = run_chain(bars_from_ohlc(_flat_ranges([5, 5, 4, 3, 2.5, 2, 1.5, 1.5])), HAND)
    assert not [e for e in tie.volatility.events if e.kind == "RANGE_CONTRACTION"]


@pytest.mark.parametrize(("factor", "found"), [(1.5, True), (1.49, False)])
def test_expansion_after_contraction(factor: float, found: bool) -> None:
    ranges = [5, 5, 4, 3, 2.5, 2, 1.5, 1, 1.2]
    first = run_chain(bars_from_ohlc(_flat_ranges(ranges)), HAND)
    atr_before = first.indicators.get("atr").data[-1]
    assert atr_before is not None
    chain = run_chain(bars_from_ohlc(_flat_ranges([*ranges, factor * float(atr_before)])), HAND)
    expansions = [e for e in chain.volatility.events if e.kind == "EXPANSION_AFTER_CONTRACTION"]
    if not found:
        assert expansions == []
        return
    (event,) = expansions
    nr7 = next(e for e in chain.volatility.events if e.kind == "RANGE_CONTRACTION")
    assert event.bar_date == week(9) and nr7.event_id in event.depends_on


@pytest.mark.parametrize("seed", [31, 32])
def test_compression_is_judged_against_previous_bars_only(seed: int) -> None:
    cfg = AnalysisConfig()
    chain = run_chain(random_bars(600, seed), cfg)
    atr_pct = np.array(
        [np.nan if v is None else v for v in chain.indicators.get("atr_percent").data]
    )
    lookback, pct = cfg.volatility.lookback, cfg.volatility.compression_percentile
    index = {d: i for i, d in enumerate(chain.indicators.bar_dates)}
    events = [e for e in chain.volatility.events if e.kind == "ATR_COMPRESSION"]
    assert events
    for e in events:
        t = index[e.bar_date]
        threshold = np.percentile(atr_pct[t - lookback : t], pct)
        assert e.values["threshold"] == pytest.approx(threshold)
        assert atr_pct[t] <= threshold
        previous = atr_pct[t - 1 - lookback : t - 1]
        if t > lookback and not np.isnan(previous).any():
            # an episode starts where the previous bar was not compressed
            assert atr_pct[t - 1] > np.percentile(previous, pct)


def test_volatility_needs_a_full_lookback() -> None:
    cfg = HAND.model_copy(update={"volatility": VolatilityConfig(lookback=60)})
    chain = run_chain(random_bars(55, seed=1), cfg)
    kinds = {e.kind for e in chain.volatility.events}
    assert "ATR_COMPRESSION" not in kinds and "BOLLINGER_CONTRACTION" not in kinds


# --------------------------------------------------------------------------- candles

DECLINE = [(c + 0.5, c + 0.7, c - 0.2, c) for c in (60.0, 59, 58, 57, 56, 55, 54)]
ADVANCE = [(c - 0.5, c + 0.2, c - 0.7, c) for c in (40.0, 41, 42, 43, 44, 45, 46)]


def _candles(rows: list[tuple[float, float, float, float]], cfg: AnalysisConfig = HAND):
    chain = run_chain(bars_from_ohlc(rows), cfg)
    last = week(len(rows) - 1)
    return {e.type for e in chain.candles.events if e.bar_date == last}, chain


@pytest.mark.parametrize(
    ("prefix", "row", "expected", "absent"),
    [
        (DECLINE, (50, 51, 49, 50.19), "DOJI", None),  # body 0.19 <= 0.1 x range 2
        (DECLINE, (50, 51, 49, 50.21), None, "DOJI"),
        (DECLINE, (50, 50.6, 48.5, 50.5), "HAMMER", "HANGING_MAN"),
        (ADVANCE, (50, 50.6, 48.5, 50.5), "HANGING_MAN", "HAMMER"),
        (DECLINE, (50, 50.6, 49.01, 50.5), None, "HAMMER"),  # lower shadow 0.99 < 2 bodies
        (DECLINE, (50, 52, 49.9, 50.5), "INVERTED_HAMMER", "SHOOTING_STAR"),
        (ADVANCE, (50, 52, 49.9, 50.5), "SHOOTING_STAR", "INVERTED_HAMMER"),
    ],
)
def test_single_bar_candles(prefix, row, expected, absent) -> None:  # type: ignore[no-untyped-def]
    types, chain = _candles([*prefix, row])
    if expected:
        assert expected in types
        event = next(e for e in chain.candles.events if e.type == expected)
        direction = "DOWN" if prefix is DECLINE else "UP"
        assert event.context.prior_direction == direction
        assert event.known_at == event.bar_date == event.start_date
    if absent:
        assert absent not in types


@pytest.mark.parametrize(("close", "found"), [(51.2, True), (50.9, False)])
def test_bullish_engulfing(close: float, found: bool) -> None:
    types, _ = _candles([*DECLINE, (51, 51.2, 49.9, 50), (49.8, close + 0.1, 49.7, close)])
    assert ("BULLISH_ENGULFING" in types) is found


def test_bearish_engulfing() -> None:
    types, _ = _candles([*ADVANCE, (50, 51.1, 49.9, 51), (51.2, 51.3, 49.7, 49.8)])
    assert "BEARISH_ENGULFING" in types


@pytest.mark.parametrize(("close", "found"), [(54.0, True), (52.9, False)])
def test_morning_star(close: float, found: bool) -> None:
    rows = [
        *DECLINE,
        (56, 56.2, 49.8, 50),
        (49.5, 49.8, 49, 49.3),
        (49.5, close + 0.1, 49.4, close),
    ]
    types, chain = _candles(rows)
    assert ("MORNING_STAR" in types) is found
    if found:
        star = next(e for e in chain.candles.events if e.type == "MORNING_STAR")
        assert (star.start_date, star.bar_date) == (week(7), week(9))


def test_evening_star() -> None:
    rows = [*ADVANCE, (50, 56.2, 49.8, 56), (56.5, 57, 56.2, 56.7), (56.5, 56.6, 52, 52.5)]
    types, _ = _candles(rows)
    assert "EVENING_STAR" in types


def test_inside_and_outside_bars() -> None:
    inside, _ = _candles([*DECLINE, (54, 55, 53, 54), (54, 55, 53, 54.5)])  # equal: inside
    assert "INSIDE_BAR" in inside and "OUTSIDE_BAR" not in inside
    outside, _ = _candles([*DECLINE, (54, 55, 53, 54), (54, 55, 52.9, 54.5)])
    assert "OUTSIDE_BAR" in outside and "INSIDE_BAR" not in outside


@pytest.mark.parametrize(("close", "doji"), [(50.25, True), (50.2501, False)])
def test_doji_boundary_is_inclusive(close: float, doji: bool) -> None:
    cfg = HAND.model_copy(update={"candles": CandleConfig(doji_body=0.125)})
    types, _ = _candles([*DECLINE, (50, 51, 49, close)], cfg)  # limit 0.125 x 2 = 0.25
    assert ("DOJI" in types) is doji


def test_candle_context_and_configuration() -> None:
    row = (50, 50.6, 48.5, 50.5)
    _, chain = _candles([*DECLINE, row])
    hammer = next(e for e in chain.candles.events if e.type == "HAMMER")
    assert hammer.context.trend_state is not None
    assert hammer.context.prior_change_atr is not None and hammer.context.prior_change_atr < 0
    strict = HAND.model_copy(update={"candles": CandleConfig(shadow_body=3.1)})
    assert "HAMMER" not in _candles([*DECLINE, row], strict)[0]  # 1.5 < 3.1 x 0.5
