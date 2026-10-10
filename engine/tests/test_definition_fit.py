"""Layer H, Phase 5b-C — definition fit (ADR-0022 §15).

Definition fit is how closely a formation satisfies the formal definition and the
applicable evidence. It is not a probability of success: it is a pure function of the
frozen geometry and the known_at context, it never sees an outcome, and its weights are
one declared constant plus structural rules."""

from __future__ import annotations

import ast
import inspect
from datetime import date
from pathlib import Path

import pandas as pd
import pytest
from analysis_chain import manual_swings, random_bars, run_chain
from hypothesis import given
from hypothesis import strategies as st

from chartlens_core.config import AnalysisConfig, IndicatorConfig, PatternsConfig
from chartlens_engine.patterns import fit
from chartlens_engine.patterns.context import (
    DivergenceContext,
    FibonacciContext,
    KeyPointVolume,
    LevelNearby,
    PatternContext,
    PriorMove,
    PriorStructure,
    StructureContext,
    VolatilityContext,
    VolumeContext,
)
from chartlens_engine.patterns.fit import DefinitionFit, balance, closeness, margin, score

CFG = PatternsConfig()
DAY = date(2020, 1, 3)
W = 1 / 18  # a CONFLUENCE or PRIOR_TREND leaf at shape share 2/3; VOLUME is 2W


def make_context(
    labels: tuple[str, ...],
    *,
    state: str | None = "STRONG_UPTREND",
    regime: str | None = None,
    decline: float | None = 2.0,
    rise: float | None = 4.0,
    sma: float | None = 100.0,
    sma_at: dict[str, float | None] | None = None,
    rvol: float | None = 2.0,
    divergence: str = "PRESENT",
    base_role: str | None = "SUPPORT",
    touches: int | None = None,
) -> PatternContext:
    """A hand-made known_at context: every fact is chosen, nothing is computed."""
    smas = {label: sma for label in labels} | (sma_at or {})
    return PatternContext(
        as_of=DAY,
        context_version="3",
        prior_move=PriorMove(
            bars=26,
            window_start=DAY,
            window_end=DAY,
            highest_close=None,
            highest_close_date=None,
            lowest_close=None,
            lowest_close_date=None,
            rise_into_atr=rise,
            decline_into_atr=decline,
            range_atr=None,
        ),
        structure=StructureContext(
            state=None,
            regime=None,
            pending=None,
            since=None,
            last_event_id=None,
            events_in_span=(),
        ),
        levels_near=[],
        levels_near_base=[]
        if base_role is None
        else [
            LevelNearby(
                key_point="BASE",
                level_id="L1:LEVEL",
                source_type="SWING",
                role=base_role,
                price=1.0,
                distance_atr=0.1,
            )
        ],
        base_price=1.0,
        prior_structure=PriorStructure(
            as_of=DAY, state=state, regime=regime, pending=None, since=None
        ),
        volume=VolumeContext(
            volume_sma_first=None,
            volume_sma_last=None,
            relative_volume_last=None,
            volume_state=None,
            volume_trend=None,
            obv_change_ratio=None,
        ),
        volume_at=[
            KeyPointVolume(key_point=x, bar_date=DAY, volume_sma=smas[x], relative_volume=rvol)
            for x in labels
        ],
        boundary_touches_at_known=touches,
        volatility=VolatilityContext(
            atr_percent_first=None,
            atr_percent_last=None,
            atr_percent_known=None,
            contraction_event_ids=(),
        ),
        divergence=DivergenceContext(
            presence=divergence,  # type: ignore[arg-type]
            not_applicable_reason="FINE_SWING_GEOMETRY" if divergence == "NOT_APPLICABLE" else None,
            divergences=[],
        ),
        fibonacci=list[FibonacciContext](),
        evidence_refs=(),
    )


# One pattern per family whose every shape criterion scores exactly 1, and a fixed
# evidence context (prior structure STRONG_UPTREND; decline into 2 ATR → 0.5, rise into
# 4 ATR → 1; equal volume everywhere → 0, except V's RVOL 2 ≥ 1.5 → 1; divergence
# PRESENT; a SUPPORT level at the base). The expected values are worked by hand in the
# comments: shape weight × 1 + Σ participating leaf weight × score.
GOLDEN: list[
    tuple[
        str,
        str,
        str,
        dict[str, float],
        tuple[str, ...],
        int | None,
        tuple[float, float] | None,
        int,
    ]
] = [
    # bullish reversal: ps 0 (W), pm 0.5 (W), vol 0 (2W), div 1 (W), lvl 1 (W) → 2/3 + 2.5W
    (
        "double",
        "DOUBLE_BOTTOM",
        "BULLISH",
        {"extreme_difference_atr": 0.0, "height_atr": 4.0},
        ("LOW_1", "NECKLINE", "LOW_2"),
        None,
        None,
        81,
    ),
    # bearish reversal: ps 1, pm 1, vol 0, div 1, lvl 0 (needs RESISTANCE) → 2/3 + 3W
    (
        "triple",
        "TRIPLE_TOP",
        "BEARISH",
        {"extreme_spread_atr": 0.0, "height_atr": 4.0, "peak_difference_atr": 0.0},
        ("HIGH_1", "PEAK_1", "HIGH_2", "PEAK_2", "HIGH_3"),
        None,
        None,
        83,
    ),
    (
        "head_shoulders",
        "INVERSE_HEAD_SHOULDERS",
        "BULLISH",
        {
            "head_prominence_atr": 2.0,
            "shoulder_difference_atr": 0.0,
            "time_ratio": 1.0,
            "neckline_slope_atr": 0.0,
        },
        ("LEFT_SHOULDER", "NECK_1", "HEAD", "NECK_2", "RIGHT_SHOULDER"),
        None,
        None,
        81,
    ),
    # pm N/A (the bowl is shape), div not in definition: shape 14/18; + lvl W → 15/18
    (
        "rounding",
        "ROUNDING_BOTTOM",
        "BULLISH",
        {"r2": 1.0, "rim_difference_atr": 0.0, "depth_atr": 6.0, "vertex_fraction": 0.5},
        ("RIM_1", "RIM_2", "VERTEX"),
        None,
        None,
        83,
    ),
    # pm N/A (the drop is shape): shape 14/18 + vol 2W + lvl W → 17/18
    (
        "v",
        "V_BOTTOM",
        "BULLISH",
        {"drop_atr": 8.0, "drop_bars": 0.0},
        ("START", "LOW"),
        None,
        None,
        94,
    ),
    # neutral: only volume (0) participates → shape 8/9
    (
        "rectangle",
        "RECTANGLE",
        "NEUTRAL",
        {"upper_difference_atr": 0.0, "lower_difference_atr": 0.0, "height_atr": 4.0},
        ("HIGH_1", "LOW_2", "HIGH_3", "LOW_4"),
        6,
        None,
        89,
    ),
    # bullish continuation: ps 1, pm 1, vol 0 → shape 14/18 + 2W → 16/18
    (
        "triangle",
        "TRIANGLE_ASCENDING",
        "BULLISH",
        {
            "upper_slope_atr": 0.0,
            "lower_slope_atr": 0.1,
            "width_start_atr": 4.0,
            "width_end_atr": 2.0,
        },
        ("HIGH_1", "LOW_2", "HIGH_3", "LOW_4"),
        6,
        None,
        89,
    ),
    # bullish reversal, no level in definition: shape 13/18 + pm 0.5W + div W → 14.5/18
    (
        "wedge",
        "WEDGE_FALLING",
        "BULLISH",
        {
            "upper_slope_atr": -0.2,
            "lower_slope_atr": -0.1,
            "width_start_atr": 4.0,
            "width_end_atr": 2.0,
        },
        ("HIGH_1", "LOW_2", "HIGH_3", "LOW_4"),
        6,
        None,
        81,
    ),
    # pm N/A (pole): shape 15/18 + ps W → 16/18
    (
        "flag",
        "BULL_FLAG",
        "BULLISH",
        {"pole_atr": 6.0, "upper_slope_atr": -0.1, "lower_slope_atr": -0.1, "retrace": 0.0},
        ("POLE_START", "POLE_END", "FLAG_1", "FLAG_2", "FLAG_3"),
        None,
        None,
        89,
    ),
    # bearish continuation wants a prior downtrend: ps 0 → 15/18
    (
        "pennant",
        "BEAR_PENNANT",
        "BEARISH",
        {"pole_atr": 6.0, "retrace": 0.0},
        ("POLE_START", "POLE_END", "FLAG_1", "FLAG_2", "FLAG_3"),
        None,
        (4.0, 2.0),
        83,
    ),
    # bullish continuation: ps 1, pm 1, vol 0 → 14/18 + 2W → 16/18
    (
        "cup_handle",
        "CUP_HANDLE",
        "BULLISH",
        {"r2": 1.0, "rim_difference_atr": 0.0, "depth_atr": 6.0, "handle_ratio": 0.0},
        ("RIM_1", "CUP_LOW", "RIM_2", "HANDLE"),
        None,
        None,
        89,
    ),
]


@pytest.mark.parametrize(
    ("family", "ptype", "direction", "measures", "labels", "touches", "widths", "expected"),
    GOLDEN,
    ids=[g[1] for g in GOLDEN],
)
def test_golden_value_per_family(
    family: str,
    ptype: str,
    direction: str,
    measures: dict[str, float],
    labels: tuple[str, ...],
    touches: int | None,
    widths: tuple[float, float] | None,
    expected: int,
) -> None:
    ctx = make_context(labels, touches=touches)
    f = score(family, ptype, direction, measures, widths, ctx, CFG)
    assert f.shape_score == pytest.approx(1.0)
    assert f.value == expected
    _check_invariants(f)


def _check_invariants(f: DefinitionFit) -> None:
    assert sum(c.actual_weight for c in f.components) == pytest.approx(1.0)
    assert 0 <= f.exact <= 1 and 0 <= f.value <= 100
    shape = [c for c in f.components if c.aspect == "SHAPE"]
    assert sum(c.actual_weight for c in shape) == pytest.approx(f.shape_weight)
    assert sum(c.nominal_weight for c in shape) == pytest.approx(f.shape_share)
    for c in f.components:
        if c.aspect == "SHAPE" and c.status == "APPLICABLE":
            assert c.score is not None and 0 <= c.score <= 1
            assert c.actual_weight >= c.nominal_weight
        elif c.aspect == "SHAPE":
            assert (c.status, c.reason) == ("NOT_APPLICABLE", "TOUCHES_AFTER_KNOWN_AT")
            assert c.score is None and c.actual_weight == 0 == c.nominal_weight
        elif c.status == "APPLICABLE":
            assert c.score is not None and 0 <= c.score <= 1
            assert c.actual_weight == pytest.approx(c.nominal_weight)
        elif c.status == "UNAVAILABLE":
            assert c.score == 0 and c.nominal_weight > 0
            assert c.actual_weight == pytest.approx(c.nominal_weight)
        elif c.status == "NOT_APPLICABLE":
            assert c.score is None and c.actual_weight == 0 and c.nominal_weight > 0
        else:
            assert c.status == "NOT_IN_DEFINITION"
            assert c.score is None and c.actual_weight == 0 == c.nominal_weight


# ---------------------------------------------------------------- ADR worked examples


DOUBLE = ("double", "DOUBLE_BOTTOM", "BULLISH")
DB_LABELS = ("LOW_1", "NECKLINE", "LOW_2")


def test_adr_example_double_bottom_81() -> None:
    """Shape 0.80, prior structure STRONG_DOWNTREND (1), prior move 3.6 ATR (0.9), volume
    lower at L2 (1), divergence ABSENT (0), a support level near the lows (1)."""
    ctx = make_context(
        DB_LABELS,
        state="STRONG_DOWNTREND",
        decline=3.6,
        divergence="ABSENT",
        sma_at={"LOW_2": 80.0},
    )
    m = {"extreme_difference_atr": 0.2, "height_atr": 4.0}  # closeness 0.6, margin 1
    f = score(*DOUBLE, m, None, ctx, CFG)
    assert f.shape_score == pytest.approx(0.8)
    assert f.exact == pytest.approx(2 / 3 * 0.8 + W * 1 + W * 0.9 + 2 * W + 0 + W)
    assert f.value == 81


def test_adr_example_bull_flag_64() -> None:
    """Shape 0.70; prior structure uptrend (1); volume not drying up (0); prior move
    (pole), divergence and level do not participate, so shape weighs 5/6."""
    ctx = make_context(("POLE_START", "POLE_END", "FLAG_1", "FLAG_2", "FLAG_3"))
    m = {"pole_atr": 6.0, "upper_slope_atr": -0.1, "lower_slope_atr": -0.075, "retrace": 0.2}
    f = score("flag", "BULL_FLAG", "BULLISH", m, None, ctx, CFG)
    assert f.shape_score == pytest.approx(0.7)  # (1 + 0.5 + 0.6) / 3
    assert f.shape_weight == pytest.approx(5 / 6)
    assert f.value == 64


def test_rectangle_volume_moves_it_by_at_most_eleven_points() -> None:
    m = {"upper_difference_atr": 0.0, "lower_difference_atr": 0.0, "height_atr": 4.0}
    labels = ("HIGH_1", "LOW_2", "HIGH_3", "LOW_4")

    def value(**kw: object) -> int:
        ctx = make_context(labels, touches=6, **kw)  # type: ignore[arg-type]
        return score("rectangle", "RECTANGLE", "NEUTRAL", m, None, ctx, CFG).value

    dried = value(sma_at={"LOW_4": 50.0})
    flat = value()
    assert (dried, flat) == (100, 89)  # shape 8/9 + volume 1/9 × {1, 0}


# ---------------------------------------------------------------- statuses and weights


def _component(f: DefinitionFit, name: str) -> fit.FitComponent:
    return next(c for c in f.components if c.component == name)


def test_unavailable_scores_zero_and_keeps_its_weight() -> None:
    """Warm-up: no structure state at the first swing, no volume SMA yet. Both keep
    their weight with score 0, so short history never raises the fit."""
    m = {"extreme_difference_atr": 0.0, "height_atr": 4.0}
    full = score(*DOUBLE, m, None, make_context(DB_LABELS, state="STRONG_DOWNTREND"), CFG)
    young = score(
        *DOUBLE, m, None, make_context(DB_LABELS, state=None, sma=None, decline=None), CFG
    )
    for name in ("prior_structure", "prior_move", "volume_behaviour"):
        c = _component(young, name)
        assert (c.status, c.reason, c.score) == ("UNAVAILABLE", "INSUFFICIENT_HISTORY", 0.0)
        assert c.actual_weight == c.nominal_weight == _component(full, name).nominal_weight
    assert young.shape_weight == full.shape_weight == pytest.approx(2 / 3)
    assert young.value < full.value
    _check_invariants(young)


def test_not_applicable_weight_goes_to_shape_never_to_other_evidence() -> None:
    m = {"extreme_difference_atr": 0.0, "height_atr": 4.0}
    absent = score(*DOUBLE, m, None, make_context(DB_LABELS, divergence="ABSENT"), CFG)
    na = score(*DOUBLE, m, None, make_context(DB_LABELS, divergence="NOT_APPLICABLE"), CFG)
    d = _component(na, "divergence")
    assert (d.status, d.reason, d.score, d.actual_weight) == (
        "NOT_APPLICABLE",
        "FINE_SWING_GEOMETRY",
        None,
        0.0,
    )
    assert d.nominal_weight == pytest.approx(W)
    assert na.shape_weight == pytest.approx(absent.shape_weight + W)
    for name in ("prior_structure", "prior_move", "volume_behaviour", "level_alignment"):
        assert _component(na, name).actual_weight == _component(absent, name).actual_weight


def test_not_in_definition_has_no_weight_at_all() -> None:
    ctx = make_context(("START", "LOW"))
    f = score("v", "V_BOTTOM", "BULLISH", {"drop_atr": 8.0, "drop_bars": 0.0}, None, ctx, CFG)
    d = _component(f, "divergence")
    assert (d.status, d.nominal_weight, d.actual_weight, d.score) == (
        "NOT_IN_DEFINITION",
        0.0,
        0.0,
        None,
    )


@pytest.mark.parametrize("touches", [4, 5, 9])
def test_boundary_touches_are_recorded_but_never_scored(touches: int) -> None:
    """Touches arrive after recognition, so at known_at the count cannot be observed:
    the criterion is NOT_APPLICABLE, carries no weight, and the shape score is the mean
    of the observable criteria whatever the count (5b-C review)."""
    m = {"upper_difference_atr": 0.0, "lower_difference_atr": 0.0, "height_atr": 4.0}
    ctx = make_context(("HIGH_1", "LOW_2", "HIGH_3", "LOW_4"), touches=touches)
    f = score("rectangle", "RECTANGLE", "NEUTRAL", m, None, ctx, CFG)
    t = _component(f, "touches")
    assert (t.status, t.reason, t.score, t.nominal_weight, t.actual_weight) == (
        "NOT_APPLICABLE",
        "TOUCHES_AFTER_KNOWN_AT",
        None,
        0.0,
        0.0,
    )
    assert t.inputs == {"context.boundary_touches_at_known": touches}
    assert f.shape_score == pytest.approx(1.0)
    _check_invariants(f)


def test_neutral_patterns_have_no_prior_trend_components() -> None:
    m = {"upper_difference_atr": 0.0, "lower_difference_atr": 0.0, "height_atr": 4.0}
    ctx = make_context(("HIGH_1", "LOW_2", "HIGH_3", "LOW_4"), touches=4)
    f = score("rectangle", "RECTANGLE", "NEUTRAL", m, None, ctx, CFG)
    for name in ("prior_structure", "prior_move"):
        assert (_component(f, name).status, _component(f, name).reason) == (
            "NOT_IN_DEFINITION",
            "NEUTRAL_PATTERN",
        )


@pytest.mark.parametrize(
    ("state", "regime", "expected"),
    [
        ("STRONG_DOWNTREND", None, 1.0),
        ("WEAKENING_DOWNTREND", None, 1.0),
        ("TRANSITION", "DOWN", 2 / 3),
        ("RANGE", None, 1 / 3),
        ("TRANSITION", "UP", 1 / 3),
        ("STRONG_UPTREND", None, 0.0),
        ("WEAKENING_UPTREND", None, 0.0),
    ],
)
def test_prior_structure_is_scored_by_evenly_spaced_ranks(
    state: str, regime: str | None, expected: float
) -> None:
    """A bullish reversal needs a prior downtrend; the ranks encode order only."""
    m = {"extreme_difference_atr": 0.0, "height_atr": 4.0}
    f = score(*DOUBLE, m, None, make_context(DB_LABELS, state=state, regime=regime), CFG)
    assert _component(f, "prior_structure").score == pytest.approx(expected)


def test_prior_move_cap_is_four_atr() -> None:
    m = {"extreme_difference_atr": 0.0, "height_atr": 4.0}
    for decline, expected in ((0.0, 0.0), (2.0, 0.5), (4.0, 1.0), (9.0, 1.0), (-1.0, 0.0)):
        f = score(*DOUBLE, m, None, make_context(DB_LABELS, decline=decline), CFG)
        assert _component(f, "prior_move").score == pytest.approx(expected)


# ---------------------------------------------------------------- double counting


def test_prior_move_is_not_applicable_exactly_when_its_move_is_shape() -> None:
    cases = {
        ("v", "V_BOTTOM"): ({"drop_atr": 8.0, "drop_bars": 0.0}, ("START", "LOW"), None),
        ("flag", "BULL_FLAG"): (
            {"pole_atr": 6.0, "upper_slope_atr": 0.0, "lower_slope_atr": 0.0, "retrace": 0.1},
            ("POLE_START", "POLE_END", "FLAG_1", "FLAG_2", "FLAG_3"),
            None,
        ),
        ("rounding", "ROUNDING_BOTTOM"): (
            {"r2": 0.9, "rim_difference_atr": 0.0, "depth_atr": 6.0, "vertex_fraction": 0.5},
            ("RIM_1", "RIM_2", "VERTEX"),
            None,
        ),
    }
    for (family, ptype), (m, labels, widths) in cases.items():
        f = score(family, ptype, "BULLISH", m, widths, make_context(labels), CFG)
        c = _component(f, "prior_move")
        assert (c.status, c.reason) == ("NOT_APPLICABLE", "MOVE_IS_SHAPE"), family


def test_one_fact_may_not_feed_two_components(monkeypatch: pytest.MonkeyPatch) -> None:
    """If someone later makes the prior move read the pole (here: registers the prior
    move as an alias of the pole and stops treating the flag's prior move as shape), the
    scorer refuses to count it twice."""
    monkeypatch.setitem(fit.FACTS, "context.prior_move.rise_into_atr", "move.pole")
    monkeypatch.setattr(fit, "PRIOR_MOVE_FACT", {})
    m = {"pole_atr": 6.0, "upper_slope_atr": 0.0, "lower_slope_atr": 0.0, "retrace": 0.1}
    ctx = make_context(("POLE_START", "POLE_END", "FLAG_1", "FLAG_2", "FLAG_3"))
    with pytest.raises(ValueError, match=r"double counting: move\.pole"):
        score("flag", "BULL_FLAG", "BULLISH", m, None, ctx, CFG)


def test_an_unregistered_input_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delitem(fit.FACTS, "geometry.height_atr")
    with pytest.raises(KeyError, match="unregistered"):
        score(
            *DOUBLE,
            {"extreme_difference_atr": 0.0, "height_atr": 4.0},
            None,
            make_context(DB_LABELS),
            CFG,
        )


def test_aliases_of_one_observation_share_one_fact() -> None:
    assert fit.FACTS["geometry.drop_atr"] == fit.FACTS["geometry.depth_atr"]
    assert (
        fit.FACTS["context.prior_move.rise_into_atr"]
        == fit.FACTS["context.prior_move.decline_into_atr"]
    )
    assert fit.FACTS["geometry.width_start_atr"] == fit.FACTS["geometry.line_widths"]


# ---------------------------------------------------------------- component shapes


@given(st.floats(0, 10), st.floats(0.01, 10))
def test_closeness_is_monotone_and_bounded(d: float, tol: float) -> None:
    assert 0 <= closeness(d, tol) <= 1
    assert closeness(d, tol) >= closeness(d + 0.1, tol)


@given(st.floats(0, 20), st.floats(0.01, 10))
def test_margin_is_monotone_and_bounded(x: float, minimum: float) -> None:
    assert 0 <= margin(x, minimum) <= 1
    assert margin(x, minimum) <= margin(x + 0.1, minimum)
    assert margin(minimum, minimum) == pytest.approx(0.5)


@given(st.floats(0.05, 20))
def test_balance_peaks_at_one(r: float) -> None:
    assert 0 <= balance(r, 0.4, 2.5) <= balance(1.0, 0.4, 2.5) == 1


def test_a_closer_shape_scores_higher_and_nothing_else_moves() -> None:
    ctx = make_context(DB_LABELS)
    fits = [
        score(*DOUBLE, {"extreme_difference_atr": d, "height_atr": 4.0}, None, ctx, CFG)
        for d in (0.0, 0.1, 0.3, 0.5)
    ]
    assert [f.exact for f in fits] == sorted((f.exact for f in fits), reverse=True)
    evidence = [[c for c in f.components if c.aspect != "SHAPE"] for f in fits]
    assert all(e == evidence[0] for e in evidence)


# ---------------------------------------------------------------- frozen inputs only


SOURCE = Path(fit.__file__)


def test_the_scorer_imports_nothing_that_could_carry_an_outcome() -> None:
    tree = ast.parse(SOURCE.read_text())
    modules = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module} | {
        a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
    }
    assert modules <= {
        "__future__",
        "math",
        "typing",
        "chartlens_core.config",
        "chartlens_engine.causal",
        "chartlens_engine.patterns.context",
    }, modules
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
    }
    forbidden = {
        "status_history",
        "breakout",
        "measured_move",
        "Lifecycle",
        "PatternEvent",
        "touches",
        "close",
        "bars",
        "historical_stats",
    }
    assert not names & forbidden, names & forbidden


def test_score_takes_no_bars_layers_or_status() -> None:
    params = list(inspect.signature(score).parameters)
    assert params == [
        "family",
        "pattern_type",
        "direction",
        "geometry_measures",
        "geometry_widths",
        "context",
        "config",
        "shape_share",
    ]


@pytest.mark.parametrize("seed", [3, 9])
def test_fit_is_identical_when_run_as_of_known_at(seed: int) -> None:
    bars = random_bars(600, seed)
    full = run_chain(bars).patterns.patterns
    assert full and all(p.definition_fit is not None for p in full)
    dates = [pd.Timestamp(d).date() for d in bars["bar_date"]]
    for p in full[:: max(1, len(full) // 12)]:
        k = dates.index(p.known_at)
        part = {q.pattern_id: q for q in run_chain(bars.iloc[: k + 1].copy()).patterns.patterns}
        assert part[p.pattern_id].definition_fit == p.definition_fit


def test_later_bars_cannot_change_a_fit() -> None:
    bars = random_bars(500, seed=21)
    cut = 330
    other = random_bars(500, seed=2121)
    scale = bars["close"].iloc[cut] / other["close"].iloc[cut]
    altered = bars.copy()
    for col in ("open", "high", "low", "close"):
        altered.loc[cut + 1 :, col] = other[col].iloc[cut + 1 :] * scale
    altered.loc[cut + 1 :, "volume"] = other["volume"].iloc[cut + 1 :]
    end = pd.Timestamp(bars["bar_date"].iloc[cut]).date()

    def fits(frame: pd.DataFrame) -> dict[str, object]:
        return {
            p.pattern_id: p.definition_fit
            for p in run_chain(frame).patterns.patterns
            if p.known_at <= end
        }

    a, b = fits(bars), fits(altered)
    assert a and a == b


# A double bottom (lows at weeks 30 and 40, neckline 49 at week 35) known at week 41;
# the same history then either completes or fails. The fit is the same.
HAND = AnalysisConfig(indicators=IndicatorConfig(atr_period=2))
DECLINE = [60.0 - 0.5 * i for i in range(30)]
BOTTOM = [46.0, 47.0, 48.0, 48.5, 48.0, 47.5, 47.0, 46.5, 46.0, 45.8, 46.0]
SWINGS = [
    ("LOW", 12, 13, 45.1),
    ("LOW", 30, 31, 45.0),
    ("HIGH", 35, 36, 49.0),
    ("LOW", 40, 41, 45.3),
]


def _hand_double(tail: list[float]) -> object:
    closes = DECLINE + BOTTOM + tail
    rows = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i else c
        rows.append((o, max(o, c) + 0.5, min(o, c) - 0.5, c))
    o, h, lo, c = (list(x) for x in zip(*rows, strict=True))
    bars = pd.DataFrame(
        {
            "bar_date": pd.date_range(date(2010, 1, 8), periods=len(rows), freq="W-FRI"),
            "open": o,
            "high": h,
            "low": lo,
            "close": c,
            "volume": 1000.0,
            "security_id": "SEC-EV",
            "continuity_segment_id": "SEC-EV@2006-01-06",
        }
    )
    chain = run_chain(bars, HAND, manual_swings(bars, SWINGS))
    (p,) = [p for p in chain.patterns.patterns if p.pattern_type == "DOUBLE_BOTTOM"]
    return p


def test_the_outcome_never_changes_the_fit() -> None:
    win = _hand_double([47.0, 48.5, 50.0, 51.5, 53.0, 54.5, 56.0])
    loss = _hand_double([47.0, 44.0, 43.0, 42.0, 41.0])
    assert win.status_history[-1].status != loss.status_history[-1].status  # type: ignore[attr-defined]
    assert win.definition_fit == loss.definition_fit  # type: ignore[attr-defined]
    assert win.definition_fit is not None  # type: ignore[attr-defined]


@pytest.mark.parametrize("seed", [4, 17, 33])
def test_every_real_fit_keeps_the_invariants(seed: int) -> None:
    for p in run_chain(random_bars(900, seed)).patterns.patterns:
        f = p.definition_fit
        assert f is not None and f.shape_share == pytest.approx(2 / 3)
        _check_invariants(f)
        used = [x for c in f.components if c.status in fit.PARTICIPATING for x in c.facts]
        assert len(used) == len(set(used)), p.pattern_id
