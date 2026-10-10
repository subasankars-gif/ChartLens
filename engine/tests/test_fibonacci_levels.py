"""Layers D and E — Fibonacci, support/resistance zones and trendlines (ADR-0021 §D, §E),
by hand. Hand fixtures place the primary swings directly (FRACTAL/MICRO) and use ATR(2)
over bars whose true range is 1, so every number can be checked on paper."""

from __future__ import annotations

import math

import pytest
from analysis_chain import (
    Chain,
    bars_from_closes,
    manual_swings,
    random_bars,
    run_chain,
    week,
)

from chartlens_core.config import AnalysisConfig, FibonacciConfig, IndicatorConfig, LevelsConfig
from chartlens_engine.levels import LevelsResult

HAND = AnalysisConfig(indicators=IndicatorConfig(atr_period=2))
UP = [10 + 0.5 * i for i in range(21)]  # bars 0..20: 10 → 20
DOWN = [19.5 - 0.5 * i for i in range(25)]  # bars 21..45: 19.5 → 7.5


def _cfg(min_leg_atr: float) -> AnalysisConfig:
    return HAND.model_copy(update={"fibonacci": FibonacciConfig(min_leg_atr=min_leg_atr)})


# ------------------------------------------------------------------------- Fibonacci


def test_fibonacci_levels_by_hand() -> None:
    bars = bars_from_closes(UP + DOWN)
    swings = manual_swings(bars, [("LOW", 0, 2, 9.5), ("HIGH", 20, 22, 20.5)])
    (fib,) = run_chain(bars, HAND, swings).fibonacci.structures
    assert (fib.direction, fib.sensitivity, fib.known_at) == ("UP", "MICRO", week(22))
    assert fib.leg_atr == pytest.approx(11.0)  # 11 points, ATR 1 at the high's bar
    got = {(lv.kind, lv.ratio): round(lv.price, 4) for lv in fib.levels}
    assert got == {
        ("RETRACEMENT", 0.236): 17.904,
        ("RETRACEMENT", 0.382): 16.298,
        ("RETRACEMENT", 0.5): 15.0,
        ("RETRACEMENT", 0.618): 13.702,
        ("RETRACEMENT", 0.786): 11.854,
        ("EXTENSION", 1.272): 23.492,
        ("EXTENSION", 1.618): 27.298,
        ("EXTENSION", 2.618): 38.298,
    }
    # Close 9.0 at bar 42 is the first beyond the anchor at 9.5.
    assert [(e.status, e.date) for e in fib.status_history] == [
        ("ACTIVE", week(22)),
        ("BROKEN", week(42)),
    ]
    assert fib.as_of(week(41)).status == "ACTIVE"  # type: ignore[union-attr]
    assert fib.as_of(week(21)) is None  # not knowable before the high is confirmed
    assert fib.depends_on == (fib.anchor_swing_id, fib.counter_swing_id)


def test_fibonacci_extended_and_terminal() -> None:
    closes = [*UP, 19.5, 19.0, 19.5, 20.0, 20.5, 21.0, 15.0, 9.0]
    bars = bars_from_closes(closes)
    swings = manual_swings(bars, [("LOW", 0, 2, 9.5), ("HIGH", 20, 22, 20.5)])
    (fib,) = run_chain(bars, HAND, swings).fibonacci.structures
    assert [(e.status, e.date) for e in fib.status_history] == [
        ("ACTIVE", week(22)),
        ("EXTENDED", week(26)),  # close 21.0 above the high; the later fall changes nothing
    ]


def test_fibonacci_is_known_when_its_later_swing_is() -> None:
    bars = bars_from_closes(UP + DOWN)
    swings = manual_swings(bars, [("LOW", 0, 30, 9.5), ("HIGH", 20, 22, 20.5)])
    (fib,) = run_chain(bars, HAND, swings).fibonacci.structures
    assert fib.known_at == week(30)  # the anchor was confirmed last


def test_a_pending_extreme_is_never_an_anchor() -> None:
    bars = bars_from_closes(UP + DOWN)
    swings = manual_swings(
        bars, [("LOW", 0, 2, 9.5), ("HIGH", 20, 22, 20.5)], pending=[("LOW", 45, 7.0)]
    )
    structures = run_chain(bars, HAND, swings).fibonacci.structures
    assert len(structures) == 1  # no HIGH → pending-LOW leg
    assert all(":pending" not in d for f in structures for d in f.depends_on)


@pytest.mark.parametrize(("minimum", "exists"), [(11.0, True), (11.0001, False)])
def test_only_meaningful_legs(minimum: float, exists: bool) -> None:
    bars = bars_from_closes(UP + DOWN)
    swings = manual_swings(bars, [("LOW", 0, 2, 9.5), ("HIGH", 20, 22, 20.5)])
    fibs = run_chain(bars, _cfg(min_leg_atr=minimum), swings).fibonacci.structures
    assert bool(fibs) is exists


def test_current_is_the_latest_leg_per_sensitivity() -> None:
    bars = bars_from_closes(UP + DOWN)
    swings = manual_swings(bars, [("LOW", 0, 2, 9.5), ("HIGH", 20, 22, 20.5), ("LOW", 41, 43, 9.0)])
    result = run_chain(bars, HAND, swings).fibonacci
    assert [f.direction for f in result.structures] == ["UP", "DOWN"]
    assert [f.direction for f in result.current(week(30))] == ["UP"]
    assert [f.direction for f in result.current()] == ["DOWN"]
    assert result.sensitivities == ("MICRO", "MAJOR")


def test_fibonacci_on_real_swings_uses_confirmed_primary_legs_only() -> None:
    chain = run_chain(random_bars(600, seed=4))
    confirmed = {s.swing_id for s in chain.swings.swings}
    assert chain.fibonacci.structures
    for f in chain.fibonacci.structures:
        assert {f.anchor_swing_id, f.counter_swing_id} <= confirmed
        assert f.method == chain.swings.primary_method
        assert f.leg_atr >= AnalysisConfig().fibonacci.min_leg_atr
    assert {f.sensitivity for f in chain.fibonacci.current()} <= {"INTERMEDIATE", "MAJOR"}


# ----------------------------------------------------------------------------- zones

FLAT = [50.0] * 30  # true range 1 every bar: ATR 1.0


NO_FIB = _cfg(min_leg_atr=1000)  # zone fixtures: swing and structure sources only


def _zones(
    lows: tuple[float, ...] = (),
    highs: tuple[float, ...] = (),
    closes: list[float] = FLAT,
) -> Chain:
    bars = bars_from_closes(closes)
    placed = [("LOW", 3 + 2 * i, 4 + 2 * i, p) for i, p in enumerate(lows)]
    placed += [("HIGH", 3 + 2 * i, 4 + 2 * i, p) for i, p in enumerate(highs)]
    return run_chain(bars, NO_FIB, manual_swings(bars, placed))


@pytest.mark.parametrize(("second", "zones"), [(40.5, 1), (40.5001, 2)])
def test_a_source_joins_within_the_tolerance(second: float, zones: int) -> None:
    levels = _zones(lows=(40.0, second)).levels
    assert levels.atr == 1.0
    assert len(levels.zones) == zones


def test_a_source_joins_on_the_zones_mean_not_its_last_member() -> None:
    zones = _zones(lows=(40.0, 40.5, 41.0)).levels.zones
    # mean of 40 and 40.5 is 40.25; 41 is 0.75 away
    assert sorted((z.price_low, z.price_high) for z in zones) == [(40.0, 40.5), (40.875, 41.125)]


def test_sides_minimum_width_and_the_nearest_cap() -> None:
    levels = _zones(lows=(40, 42, 44, 46, 48), highs=(52, 54, 56, 58, 60)).levels
    support = [z for z in levels.zones if z.type == "SUPPORT"]
    resistance = [z for z in levels.zones if z.type == "RESISTANCE"]
    assert [(z.price_low + z.price_high) / 2 for z in support] == [48, 46, 44, 42]
    assert [(z.price_low + z.price_high) / 2 for z in resistance] == [52, 54, 56, 58]
    assert support[0].price_high - support[0].price_low == pytest.approx(0.25)
    assert support[0].distance_atr == pytest.approx(1.875)


def test_touches_strength_and_its_components() -> None:
    closes = list(FLAT)
    for i in (1, 10, 11, 20):  # bar 1 is before the level is known; 10–11 is one touch
        closes[i] = 49.5
    (zone,) = _zones(lows=(49.0,), closes=closes).levels.zones
    assert zone.type == "SUPPORT"
    assert zone.touches == [week(10), week(20)]
    assert zone.last_tested == week(20)
    recency = math.exp(-9 / 26)
    assert zone.strength_components == {
        "touches": 2.0,
        "source_types": 1.0,
        "touch_rvol": 1.0,  # bar 20 is the first with a 20-week baseline
        "recency": pytest.approx(recency),
    }
    assert zone.strength == pytest.approx(2 + 1 + 0.5 * 1.0 + 2 * recency)


def test_a_zone_is_known_when_its_latest_source_is() -> None:
    bars = bars_from_closes(FLAT)
    swings = manual_swings(bars, [("LOW", 3, 4, 40.0), ("LOW", 6, 20, 40.5)])
    (zone,) = run_chain(bars, HAND, swings).levels.zones
    assert (zone.first_seen, zone.known_at) == (week(4), week(20))


def test_high_volume_swings_and_structure_levels_are_source_types() -> None:
    volume = [1000.0] * 30
    volume[25] = 3000.0  # relative volume 3 at the swing's pivot bar
    bars = bars_from_closes(FLAT, volume=volume)
    swings = manual_swings(bars, [("LOW", 25, 26, 45.0), ("HIGH", 3, 4, 49.6)])
    chain = run_chain(bars, HAND, swings)
    by_price = {round(z.sources[0].price, 1): z for z in chain.levels.zones}
    assert by_price[45.0].source_types == ("SWING", "VOLUME")
    bos = chain.structure.events[0]  # close 50 above 49.6 at bar 5 (known at 4)
    assert by_price[49.6].source_types == ("STRUCTURE", "SWING")
    structural = next(s for s in by_price[49.6].sources if s.source_type == "STRUCTURE")
    assert (structural.ref_id, structural.known_at) == (bos.event_id, week(5))


def test_no_zones_while_atr_warms_up() -> None:
    levels = run_chain(bars_from_closes([50.0, 50.0]), HAND).levels
    assert levels.atr is None and levels.zones == []


@pytest.mark.parametrize("seed", [2, 9])
def test_fibonacci_and_dynamic_sources_come_from_their_layers(seed: int) -> None:
    chain = run_chain(random_bars(700, seed))
    current = {f.fib_id: f for f in chain.fibonacci.current()}
    day = chain.levels.state_date
    kinds = set()
    for zone in chain.levels.zones:
        for s in zone.sources:
            kinds.add(s.source_type)
            if s.source_type == "FIBONACCI":
                fib_id, ratio = s.ref_id.rsplit("@", 1)
                assert current[fib_id].status == "ACTIVE"
                assert s.price == current[fib_id].level(float(ratio))
            if s.source_type == "DYNAMIC":
                name = s.ref_id.split("@")[0]
                assert s.known_at == day
                assert s.price == chain.indicators.get(name).data[-1]
    assert {"SWING", "STRUCTURE"} <= kinds
    cfg = LevelsConfig()
    assert sum(z.type == "SUPPORT" for z in chain.levels.zones) <= cfg.max_zones_per_side


# ------------------------------------------------------------------------ trendlines

ON_LINE = [12 + 0.5 * (b - 2) for b in range(30)]  # closes 2 above the line 10 + 0.5(b − 2)
SUPPORT = [("LOW", 2, 3, 10.0), ("LOW", 8, 9, 13.0), ("LOW", 14, 15, 16.0)]


def _lines(
    swings: list[tuple[str, int, int, float]], closes: list[float] = ON_LINE
) -> LevelsResult:
    bars = bars_from_closes(closes)
    return run_chain(bars, HAND, manual_swings(bars, swings)).levels


def test_a_trendline_is_validated_by_its_third_touch() -> None:
    levels = _lines(SUPPORT)
    (line,) = levels.trendlines
    assert (line.type, line.known_at, line.status) == ("SUPPORT", week(15), "ACTIVE")
    assert line.slope_per_bar == 0.5
    assert [t.bar_date for t in line.touches] == [week(2), week(8), week(14)]
    assert line.as_of(week(14)) is None  # two touches are not yet a line
    (active,) = levels.active_trendlines
    assert active.value == pytest.approx(10 + 0.5 * 27)


@pytest.mark.parametrize(("price", "validated"), [(13.5, True), (13.51, False)])
def test_touch_tolerance_near_miss(price: float, validated: bool) -> None:
    # The line through the outer swings is at 13 on bar 8: the middle swing touches it
    # within 0.5 ATR (ATR 1), or the three swings are not one line.
    levels = _lines([SUPPORT[0], ("LOW", 8, 9, price), SUPPORT[2]])
    assert bool(levels.trendlines) is validated


def test_a_trendline_breaks_on_a_complete_close_beyond_it() -> None:
    closes = list(ON_LINE)
    closes[22] = 18.0  # the line is at 20; ATR at bar 22 is 2.5, so the buffer is 0.25
    (line,) = _lines(SUPPORT, closes).trendlines
    assert [(e.status, e.date) for e in line.status_history] == [
        ("ACTIVE", week(15)),
        ("BROKEN", week(22)),
    ]
    assert line.as_of(week(21)).status == "ACTIVE"  # type: ignore[union-attr]


def test_a_line_broken_before_its_third_touch_is_known_never_exists() -> None:
    closes = list(ON_LINE)
    closes[12] = 12.0  # line at 15: broken at bar 12, the third touch is known at 15
    assert _lines(SUPPORT, closes).trendlines == []


def test_a_swing_beyond_the_line_between_its_anchors_rejects_it() -> None:
    assert _lines([*SUPPORT, ("LOW", 5, 6, 10.4)]).trendlines == []


def test_later_touches_and_causal_deduplication() -> None:
    levels = _lines([*SUPPORT, ("LOW", 20, 21, 19.0)])
    (line,) = levels.trendlines  # (2,8), (2,14), (8,14), ... are one line
    assert len(line.touches) == 4
    assert len(line.as_of(week(20)).touches) == 3  # type: ignore[union-attr]
    assert line.depends_on == tuple(t.swing_id for t in line.touches[:3])


def test_resistance_lines_mirror_support() -> None:
    closes = [28 - 0.5 * (b - 2) for b in range(30)]  # 2 below the line 30 − 0.5(b − 2)
    resistance = [("HIGH", 2, 3, 30.0), ("HIGH", 8, 9, 27.0), ("HIGH", 14, 15, 24.0)]
    (line,) = _lines(resistance, closes).trendlines
    assert (line.type, line.slope_per_bar, line.known_at) == ("RESISTANCE", -0.5, week(15))


# ------------------------------------------------- levels: existence and role changes


def _role_fixture(n: int) -> Chain:
    closes = [50.0] * 10 + [48.5] * 5 + [50.0] * 10  # broken at bar 10, regained at 15
    return _zones(lows=(49.0,), closes=closes[:n])


def test_a_broken_level_changes_role_explicitly() -> None:
    chain = _role_fixture(25)
    level, bos = chain.levels.levels  # the swing low, and structure's BOS down through it
    assert (bos.source_type, bos.original_role, bos.known_at) == (
        "STRUCTURE",
        "RESISTANCE",
        week(10),
    )
    assert [(r.role, r.date) for r in bos.role_history] == [
        ("RESISTANCE", week(10)),
        ("SUPPORT", week(15)),
    ]
    assert (level.source_type, level.original_role, level.known_at) == ("SWING", "SUPPORT", week(4))
    assert [(r.role, r.date) for r in level.role_history] == [
        ("SUPPORT", week(4)),
        ("RESISTANCE", week(10)),  # close 48.5 below 49 − 0.1 × ATR 1.5
        ("SUPPORT", week(15)),  # close 50 back above 49 + 0.1 × ATR
    ]
    assert level.role_history[1].threshold == pytest.approx(48.85)
    assert level.as_of(week(12)).role == "RESISTANCE"  # type: ignore[union-attr]
    assert level.as_of(week(3)) is None


def test_zones_count_tests_in_their_current_role_only() -> None:
    (regained,) = _role_fixture(25).levels.zones
    assert regained.type == "SUPPORT"
    assert regained.role_reversed  # the BOS level was born resistance and is support again
    assert regained.role_changes == [week(10), week(15)]
    assert regained.tested_since == week(16)
    assert regained.touches == []  # nothing since it was regained

    (flipped,) = _role_fixture(15).levels.zones  # as of bar 14: still below
    assert flipped.type == "RESISTANCE" and flipped.role_reversed
    assert flipped.role_changes == [week(10)]
    assert flipped.tested_since == week(11)
    assert flipped.touches == [week(11)]  # tests from below, as resistance
    source = next(s for s in flipped.sources if s.source_type == "SWING")
    assert (source.original_role, source.role, source.role_since) == (
        "SUPPORT",
        "RESISTANCE",
        week(10),
    )


def test_a_level_broken_upwards_starts_as_support() -> None:
    bars = bars_from_closes(FLAT)
    chain = run_chain(bars, NO_FIB, manual_swings(bars, [("HIGH", 3, 4, 49.6)]))
    structural, swing = sorted(chain.levels.levels, key=lambda lv: lv.source_type)
    assert [(r.role, r.date) for r in swing.role_history] == [
        ("RESISTANCE", week(4)),
        ("SUPPORT", week(5)),  # the same close that is structure's BOS
    ]
    assert (structural.original_role, structural.known_at) == ("SUPPORT", week(5))
    assert structural.role_history == structural.role_history[:1]
