"""Layers G–H, Phase 5a — pattern candidates, geometry, identity and FORMING patterns
(ADR-0022). Hand fixtures place the swings directly (FRACTAL/MICRO, which is also the
fine sensitivity here) over bars with ATR = 1, so every rule can be checked on paper;
the near-miss cases read the rule that failed from the diagnostics."""

from __future__ import annotations

import pandas as pd
import pytest
from analysis_chain import bars_from_closes, manual_swings, random_bars, run_chain, week
from hypothesis import given, settings
from hypothesis import strategies as st

from chartlens_core.config import AnalysisConfig, IndicatorConfig
from chartlens_engine.patterns import Pattern, PatternResult

HAND = AnalysisConfig(indicators=IndicatorConfig(atr_period=2))
Placed = list[tuple[str, int, int, float]]


def _run(placed: Placed, closes: list[float] | None = None, n: int = 30) -> PatternResult:
    bars = bars_from_closes(closes or [50.0] * n)
    return run_chain(bars, HAND, manual_swings(bars, placed)).patterns


def _of(result: PatternResult, pattern_type: str) -> list[Pattern]:
    return [p for p in result.patterns if p.pattern_type == pattern_type]


def _rules(result: PatternResult, family: str) -> set[str]:
    return {r.rule for r in result.rejections if r.family == family}


def lo(bar: int, price: float) -> tuple[str, int, int, float]:
    return ("LOW", bar, bar + 1, price)


def hi(bar: int, price: float) -> tuple[str, int, int, float]:
    return ("HIGH", bar, bar + 1, price)


# ---------------------------------------------------------------------- 7.1 double

DOUBLE = [lo(5, 45.0), hi(10, 49.0), lo(15, 45.3)]


def test_double_bottom_geometry_identity_and_known_at() -> None:
    (p,) = _of(_run(DOUBLE), "DOUBLE_BOTTOM")
    g = p.geometry
    assert (p.direction, p.known_at, p.status) == ("BULLISH", week(16), "FORMING")
    assert p.pattern_id.endswith(f"PAT:DOUBLE_BOTTOM:{week(5)}:{week(10)}:{week(15)}")
    assert [k.label for k in g.key_points] == ["LOW_1", "NECKLINE", "LOW_2"]
    assert (g.confirmation_level, g.invalidation_level, g.atr_d) == (49.0, 45.0, 1.0)
    assert g.height == pytest.approx(4.0)  # neckline − the lower low
    assert g.measures["extreme_difference_atr"] == pytest.approx(0.3)
    assert g.measures["height_atr"] == pytest.approx(3.7)
    assert p.status_history[0].date == p.known_at
    assert p.depends_on == tuple(k.swing_id for k in g.key_points)


def test_double_top_mirrors() -> None:
    (p,) = _of(_run([hi(5, 55.0), lo(10, 51.0), hi(15, 54.7)]), "DOUBLE_TOP")
    g = p.geometry
    assert (p.direction, g.confirmation_level, g.invalidation_level) == ("BEARISH", 51.0, 55.0)
    assert [k.label for k in g.key_points] == ["HIGH_1", "NECKLINE", "HIGH_2"]


@pytest.mark.parametrize(
    ("placed", "rule"),
    [
        ([lo(5, 45.0), hi(10, 49.0), lo(15, 45.51)], "equal_extremes"),
        ([lo(5, 45.0), hi(7, 49.0), lo(8, 45.3)], "separation"),
        ([lo(5, 45.0), hi(10, 47.2), lo(15, 45.3)], "height"),  # 1.9 ATR above the low
    ],
)
def test_double_bottom_near_misses(placed: Placed, rule: str) -> None:
    result = _run(placed)
    assert _of(result, "DOUBLE_BOTTOM") == []
    assert rule in _rules(result, "double")


# ---------------------------------------------------------------------- 7.2 triple

TRIPLE = [lo(5, 45.0), hi(9, 49.0), lo(13, 45.2), hi(17, 48.8), lo(21, 44.9)]


def test_triple_bottom_and_the_doubles_inside_it_coexist() -> None:
    result = _run(TRIPLE)
    (t,) = _of(result, "TRIPLE_BOTTOM")
    assert (t.known_at, t.geometry.confirmation_level, t.geometry.invalidation_level) == (
        week(22),
        49.0,
        44.9,
    )
    assert t.geometry.height == pytest.approx(4.1)
    assert len(_of(result, "DOUBLE_BOTTOM")) == 2  # existence keeps both; relevance decides
    assert "head_prominence" in _rules(result, "head_shoulders")


def test_triple_near_miss() -> None:
    result = _run([*TRIPLE[:4], lo(21, 44.6)])  # spread 0.6 > 0.5
    assert _of(result, "TRIPLE_BOTTOM") == []
    assert "equal_extremes" in _rules(result, "triple")


# ---------------------------------------------------------------------- 7.3 H&S

HS = [lo(5, 46.0), hi(10, 49.0), lo(15, 44.0), hi(20, 49.2), lo(25, 46.3)]


def test_inverse_head_and_shoulders() -> None:
    (p,) = _of(_run(HS), "INVERSE_HEAD_SHOULDERS")
    g = p.geometry
    (neck,) = g.lines
    assert (g.confirmation_line, g.invalidation_level, p.known_at) == ("NECKLINE", 44.0, week(26))
    assert neck.value_at(15) == pytest.approx(49.1)
    assert g.height == pytest.approx(5.1)
    assert [k.label for k in g.key_points][2] == "HEAD"


@pytest.mark.parametrize(
    ("placed", "rule"),
    [
        ([*HS[:2], lo(15, 45.1), *HS[3:]], "head_prominence"),  # 0.9 ATR below a shoulder
        ([*HS[:4], lo(25, 47.6)], "shoulder_symmetry"),  # shoulders 1.6 ATR apart
        ([*HS[:3], hi(20, 52.0), HS[4]], "neckline_slope"),  # 0.3 ATR per bar
        ([lo(1, 46.0), hi(3, 49.0), lo(4, 44.0), *HS[3:]], "time_balance"),  # 3 vs 21 bars
    ],
)
def test_head_and_shoulders_near_misses(placed: Placed, rule: str) -> None:
    result = _run(placed)
    assert _of(result, "INVERSE_HEAD_SHOULDERS") == []
    assert rule in _rules(result, "head_shoulders")


# ---------------------------------------------------------------------- 7.5 V


@pytest.mark.parametrize(("low_bar", "found"), [(14, True), (19, False)])
def test_v_bottom_and_speed(low_bar: int, found: bool) -> None:
    result = _run([hi(10, 55.0), lo(low_bar, 50.5)])
    vs = _of(result, "V_BOTTOM")
    if found:
        (v,) = vs
        assert v.geometry.confirmation_level == pytest.approx(50.5 + 0.618 * 4.5)
        assert v.geometry.invalidation_level == 50.5
    else:
        assert vs == [] and "drop_speed" in _rules(result, "v")


# ---------------------------------------------------------------------- 7.6 rectangle

RECT = [hi(4, 52.0), lo(8, 48.0), hi(12, 52.3), lo(16, 47.8)]


def test_rectangle_touches_and_same_formation() -> None:
    result = _run([*RECT, hi(20, 52.1)])
    (r,) = _of(result, "RECTANGLE")
    g = r.geometry
    assert (r.direction, r.known_at) == ("NEUTRAL", week(17))
    assert [(ln.label, ln.anchor_value) for ln in g.lines] == [("UPPER", 52.3), ("LOWER", 47.8)]
    assert g.confirmation_level is None and g.confirmation_line is None  # either side
    assert [(t.bar_date, t.target) for t in r.touches] == [(week(20), "UPPER")]
    # The window L8–H12–L16–H20 is the same formation: recorded as a touch, not a new one.
    assert result.candidates["rectangle"].same_formation == 1
    assert r.as_of(week(20)).touches == []  # type: ignore[union-attr]  # known at 21


def test_rectangle_rejects_a_close_outside_before_it_is_known() -> None:
    closes = [50.0] * 30
    closes[10] = 53.0  # above 52.3 + 0.25 × ATR of the bar before
    result = _run(RECT, closes)
    assert _of(result, "RECTANGLE") == []
    assert "close_outside" in _rules(result, "rectangle")


# ------------------------------------------------------------ 7.7–7.8 triangles, wedges


def test_ascending_triangle() -> None:
    result = _run([hi(4, 52.0), lo(8, 46.0), hi(12, 52.05), lo(16, 48.0)])
    (t,) = _of(result, "TRIANGLE_ASCENDING")
    g = t.geometry
    assert (t.direction, g.confirmation_line, g.invalidation_line) == ("BULLISH", "UPPER", "LOWER")
    assert g.height == pytest.approx(7.0)  # width at the first defining bar
    assert g.measures["apex_bars_after_last"] == pytest.approx(7 / 0.24375 - 12)
    assert "lower_band" in _rules(result, "rectangle")  # the same window is no rectangle


def test_symmetrical_triangle_is_neutral() -> None:
    (t,) = _of(_run([hi(4, 54.0), lo(8, 46.0), hi(12, 52.0), lo(16, 48.0)]), "TRIANGLE_SYMMETRICAL")
    assert t.direction == "NEUTRAL" and t.geometry.confirmation_line is None


def test_rising_wedge() -> None:
    def mid(b: int) -> float:
        return (50 + 0.25 * (b - 4) + 44 + 0.5 * (b - 4)) / 2

    closes = [mid(b) for b in range(30)]
    result = _run([hi(4, 50.0), lo(8, 46.0), hi(12, 52.0), lo(16, 50.0)], closes)
    (w,) = _of(result, "WEDGE_RISING")
    assert (w.direction, w.geometry.confirmation_line) == ("BEARISH", "LOWER")


def test_diverging_lines_are_neither() -> None:
    result = _run([hi(4, 52.0), lo(8, 48.0), hi(12, 53.0), lo(16, 47.0)])
    assert not [p for p in result.patterns if p.family in ("triangle", "wedge")]
    assert "slopes" in _rules(result, "triangle") and "slopes" in _rules(result, "wedge")


# ---------------------------------------------------------------- 7.10–7.11 flag, pennant


def test_bull_flag() -> None:
    result = _run([lo(2, 40.0), hi(5, 46.0), lo(7, 44.0), hi(9, 45.6), lo(11, 43.7)], [45.0] * 30)
    (f,) = _of(result, "BULL_FLAG")
    g = f.geometry
    assert g.height == pytest.approx(6.0)  # the pole
    assert g.measures["retrace"] == pytest.approx(2.3 / 6)
    assert g.invalidation_level == pytest.approx(43.0)  # pole top − 0.5 × pole
    assert "slopes" in _rules(result, "pennant")


def test_bull_pennant() -> None:
    result = _run([lo(2, 40.0), hi(5, 46.0), lo(7, 43.0), hi(9, 45.5), lo(11, 43.8)], [44.5] * 30)
    (p,) = _of(result, "BULL_PENNANT")
    assert p.geometry.confirmation_line == "UPPER"
    assert "parallel" in _rules(result, "flag")


def test_flag_pole_must_be_fast() -> None:
    result = _run([lo(0, 40.0), hi(7, 46.0), lo(9, 44.0), hi(11, 45.6), lo(13, 43.7)], [45.0] * 30)
    assert not _of(result, "BULL_FLAG") and "pole_speed" in _rules(result, "flag")


# ---------------------------------------------------------------- 7.4 / 7.12 cup, rounding


def test_cup_and_handle_and_the_rounding_bottom_on_the_same_rims() -> None:
    cup = [50 + 0.1 * (b - 15) ** 2 for b in range(26)]  # 60 at the rims, 50 at bar 15
    closes = [60.0] * 5 + cup[5:] + [60.3, 59.5, 58.5, 59.0, 59.5, 60.0]
    result = _run([hi(5, 60.0), hi(25, 60.3), lo(28, 58.0)], closes, n=len(closes))
    (c,) = _of(result, "CUP_HANDLE")
    g = c.geometry
    assert (g.confirmation_level, g.invalidation_level) == (60.3, 58.0)
    assert g.measures["r2"] == pytest.approx(1.0)
    assert c.known_at == week(29)  # the handle low is known last
    assert len(_of(result, "ROUNDING_BOTTOM")) == 1  # coexists: relevance prefers the cup


# ------------------------------------------------------- identity, immutability, causality


@pytest.mark.parametrize("seed", [3, 8])
def test_geometry_is_fixed_when_known_and_identity_is_stable(seed: int) -> None:
    bars = random_bars(600, seed)
    full = run_chain(bars).patterns
    assert full.patterns
    dates = [pd.Timestamp(d).date() for d in bars["bar_date"]]
    for p in full.patterns[:: max(1, len(full.patterns) // 8)]:
        k = dates.index(p.known_at)
        for end in (k, min(k + 7, len(dates) - 1)):  # at known_at, and as touches arrive
            part = run_chain(bars.iloc[: end + 1].copy()).patterns
            (same,) = [q for q in part.patterns if q.pattern_id == p.pattern_id]
            assert same.geometry == p.geometry
            assert same == p.as_of(dates[end])
        before = run_chain(bars.iloc[:k].copy()).patterns
        assert p.pattern_id not in {q.pattern_id for q in before.patterns}


@settings(max_examples=12, deadline=None)
@given(
    periods=st.integers(min_value=60, max_value=500),
    seed=st.integers(min_value=0, max_value=10_000),
    cut=st.floats(min_value=0.1, max_value=1.0),
)
def test_prefix_stability(periods: int, seed: int, cut: float) -> None:
    bars = random_bars(periods, seed)
    full = run_chain(bars).patterns
    k = max(1, round(cut * periods))
    part = run_chain(bars.iloc[:k].copy()).patterns
    end = pd.Timestamp(bars["bar_date"].iloc[k - 1]).date()
    assert part.patterns == [x for p in full.patterns if (x := p.as_of(end))]


@pytest.mark.parametrize("seed", [1, 4])
def test_every_pattern_is_known_with_its_last_defining_swing(seed: int) -> None:
    chain = run_chain(random_bars(700, seed))
    swings = {s.swing_id: s for s in chain.swings.swings}
    for p in chain.patterns.patterns:
        known = [swings[i].known_at for i in p.depends_on]
        assert p.known_at == max(k for k in known if k is not None)
        assert all(t.bar_date <= t.known_at and t.bar_date > p.end_date for t in p.touches)
        assert p.status_history[0].status == "FORMING"
    for c in chain.patterns.candidates.values():
        assert c.same_formation <= c.valid <= c.generated
        assert c.valid + sum(c.rejected.values()) == c.generated
