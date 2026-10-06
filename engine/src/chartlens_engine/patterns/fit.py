"""Layer H, Phase 5b-C: definition fit (ADR-0022 §15).

**Definition fit** is how closely an observed formation satisfies ChartLens's formal
pattern definition (the §7 rule table) and the applicable technical evidence. It is
**not** a probability of breakout or success, it never reads an outcome, and it is never
tuned to outcomes.

``score`` is a pure function of three frozen inputs: the pattern's geometry (fixed at
``known_at``), its context (the ``known_at`` snapshot) and configuration. It receives no
bars, no layers and no status, so the score is fixed at ``known_at`` by construction.
This module must not import the lifecycle or read a status history (a boundary test
enforces it).

Aggregation (§15.2), with one declared constant:

- **Shape** carries ``shape_share`` (2/3) when everything applies: the mean of the
  family's §7 shape criteria.
- **Evidence** shares the rest equally among three aspects (1/9 each at 2/3):
  PRIOR_TREND (``prior_structure``, ``prior_move``), VOLUME (``volume_behaviour``) and
  CONFLUENCE (``divergence``, ``level_alignment``); leaves split their aspect equally.
  These are fit aspects, not prerequisites: the pattern already exists.
- A leaf that does not participate gives its weight **to shape, never to other
  evidence**; an applicable leaf always carries exactly its nominal weight.
- ``NOT_IN_DEFINITION``: §7 does not list it for the family (nominal 0, actual 0).
  ``NOT_APPLICABLE``: listed, but not observable for this pattern (nominal w, actual 0).
  ``UNAVAILABLE``: applicable and observable in principle, but the history is
  insufficient: scores 0 and keeps its weight.
- **One fact, one component.** Every input is registered with the underlying fact it
  measures; two participating components may never share a fact (checked on every
  score). ``prior_move`` is NOT_APPLICABLE exactly when the move §7 names for it is a
  fact the shape already scores (V drop, flag/pennant pole, rounding bowl).
"""

from __future__ import annotations

import math
from typing import Literal

from chartlens_core.config import PatternsConfig, PatternSection
from chartlens_engine.causal import Frozen
from chartlens_engine.patterns.context import PatternContext

FIT_VERSION = "1"

Status = Literal["APPLICABLE", "NOT_APPLICABLE", "NOT_IN_DEFINITION", "UNAVAILABLE"]
Aspect = Literal["SHAPE", "PRIOR_TREND", "VOLUME", "CONFLUENCE"]
Value = float | int | str | None

PARTICIPATING: frozenset[str] = frozenset({"APPLICABLE", "UNAVAILABLE"})

# ---------------------------------------------------------------- fact registry

FACTS: dict[str, str] = {
    # geometry measures (frozen at known_at)
    "geometry.extreme_difference_atr": "shape.extreme_equality",
    "geometry.extreme_spread_atr": "shape.extreme_equality",
    "geometry.height_atr": "shape.height",
    "geometry.peak_difference_atr": "shape.peak_equality",
    "geometry.head_prominence_atr": "shape.head_prominence",
    "geometry.shoulder_difference_atr": "shape.shoulder_equality",
    "geometry.time_ratio": "shape.time_balance",
    "geometry.neckline_slope_atr": "shape.neckline_slope",
    "geometry.r2": "shape.curve_fit",
    "geometry.rim_difference_atr": "shape.rim_equality",
    "geometry.vertex_fraction": "shape.vertex_position",
    "geometry.depth_atr": "move.inside_pattern",
    "geometry.drop_atr": "move.inside_pattern",
    "geometry.drop_bars": "shape.move_speed",
    "geometry.upper_difference_atr": "shape.upper_band",
    "geometry.lower_difference_atr": "shape.lower_band",
    "geometry.upper_slope_atr": "shape.upper_slope",
    "geometry.lower_slope_atr": "shape.lower_slope",
    "geometry.width_start_atr": "shape.convergence",
    "geometry.width_end_atr": "shape.convergence",
    "geometry.line_widths": "shape.convergence",
    "geometry.pole_atr": "move.pole",
    "geometry.retrace": "move.retrace_inside_pattern",
    "geometry.handle_ratio": "move.handle_retrace",
    "context.boundary_touches_at_known": "shape.touches",
    # context facts (the known_at snapshot)
    "context.prior_move.decline_into_atr": "move.before_pattern",
    "context.prior_move.rise_into_atr": "move.before_pattern",
    "context.prior_structure": "structure.before_pattern",
    "context.volume_at": "volume.at_key_points",
    "context.divergence": "divergence.at_defining_swing",
    "context.levels_near_base": "level.near_base",
}
"""Every input a component may read, mapped to the underlying fact it measures. Aliases
of one observation map to one fact. A new input must be registered here, honestly: an
unregistered key is an error."""

PRIOR_MOVE_FACT: dict[str, str] = {
    "v": "move.inside_pattern",  # §7.5: "the drop itself is the move"
    "rounding": "move.inside_pattern",  # §7.4: decline from H_a into the vertex = depth
    "flag": "move.pole",  # §7.10: "prior_move = the pole"
    "pennant": "move.pole",
}
"""The move §7 names as a family's prior move, when it is not the move before the
pattern. If shape already scores that fact, ``prior_move`` is NOT_APPLICABLE."""

# ---------------------------------------------------------------- definitions (§7)

REVERSAL = {"double", "triple", "head_shoulders", "rounding", "v", "wedge"}
CONTINUATION_TYPES = {
    "TRIANGLE_ASCENDING",
    "TRIANGLE_DESCENDING",
    "BULL_FLAG",
    "BEAR_FLAG",
    "BULL_PENNANT",
    "BEAR_PENNANT",
    "CUP_HANDLE",
    "INVERSE_CUP_HANDLE",
}
LEVEL_FAMILIES = {"double", "triple", "head_shoulders", "rounding", "v"}
DIVERGENCE_FAMILIES = {"double", "triple", "head_shoulders", "wedge"}
BOUNDARY_FAMILIES = {"rectangle", "triangle", "wedge"}

TREND_UP = {"STRONG_UPTREND", "WEAKENING_UPTREND"}
TREND_DOWN = {"STRONG_DOWNTREND", "WEAKENING_DOWNTREND"}


class FitComponent(Frozen):
    component: str
    aspect: Aspect
    status: Status
    reason: str | None
    score: float | None
    """In [0, 1]; None unless APPLICABLE; 0 when UNAVAILABLE."""
    nominal_weight: float
    actual_weight: float
    inputs: dict[str, Value]
    """Registered input key → the value read."""
    facts: tuple[str, ...]
    """The underlying facts behind ``inputs`` (``FACTS``)."""
    evidence_refs: tuple[str, ...]
    methodology_version: str


class DefinitionFit(Frozen):
    """Not a probability of breakout or success."""

    value: int
    """0–100: round half up of 100 × ``exact``."""
    exact: float
    """Σ actual weight × score, in [0, 1] (for ranking without rounding ties)."""
    fit_version: str
    shape_share: float
    shape_score: float
    shape_weight: float
    """Shape's actual weight: ``shape_share`` plus the weight of every non-participating
    evidence leaf."""
    components: list[FitComponent]


# ---------------------------------------------------------------- component shapes (§5)


def _clip(x: float) -> float:
    return 0.0 if math.isnan(x) else min(1.0, max(0.0, x))


def closeness(d: float, tol: float) -> float:
    """1 when exact, 0 at the tolerance."""
    if tol <= 0:
        return 1.0 if d == 0 else 0.0
    return _clip(1 - abs(d) / tol)


def margin(x: float, minimum: float) -> float:
    """0.5 at the minimum, 1 at twice it."""
    return _clip(x / (2 * minimum))


def balance(r: float, lo: float, hi: float) -> float:
    """1 when r = 1, 0 at the bound on r's side."""
    if r <= 0:
        return 0.0
    bound = hi if r >= 1 else lo
    if bound == 1:
        return 1.0 if r == 1 else 0.0
    return _clip(1 - math.log(r) / math.log(bound))


def rescaled(x: float, lo: float) -> float:
    """``lo`` → 0, 1 → 1."""
    return _clip((x - lo) / (1 - lo)) if lo < 1 else 1.0


# ---------------------------------------------------------------- scorer


class _Leaf:
    def __init__(
        self,
        component: str,
        aspect: Aspect,
        status: Status,
        reason: str | None = None,
        score: float | None = None,
        inputs: dict[str, Value] | None = None,
        refs: tuple[str, ...] = (),
    ) -> None:
        self.component = component
        self.aspect: Aspect = aspect
        self.status: Status = status
        self.reason = reason
        self.score = 0.0 if status == "UNAVAILABLE" else score
        self.inputs = inputs or {}
        self.refs = refs


def _facts(inputs: dict[str, Value]) -> tuple[str, ...]:
    missing = [k for k in inputs if k not in FACTS]
    if missing:
        raise KeyError(f"unregistered fit inputs (add them to FACTS): {missing}")
    return tuple(dict.fromkeys(FACTS[k] for k in inputs))


def score(
    family: str,
    pattern_type: str,
    direction: str,
    geometry_measures: dict[str, float],
    geometry_widths: tuple[float, float] | None,
    context: PatternContext,
    config: PatternsConfig,
    shape_share: float | None = None,
) -> DefinitionFit:
    """The definition fit of one pattern from frozen inputs only.

    ``geometry_widths``: the distance between the pattern's two lines at its first and
    last defining bars (pennants), read from the frozen geometry by the caller.
    ``shape_share`` overrides the configured constant (diagnostic sensitivity runs
    only)."""
    g = config.shape_share if shape_share is None else shape_share
    section: PatternSection = getattr(config, family)
    shape = _shape(family, pattern_type, geometry_measures, geometry_widths, context, section)
    shape_facts = {f for leaf in shape for f in _facts(leaf.inputs)}
    evidence = _evidence(family, pattern_type, direction, context, config, shape_facts)

    # Nominal leaf weights: (1 - g) split equally among the three aspects, then equally
    # among each aspect's leaves. Fixed, whatever else applies.
    aspect_leaves: dict[str, int] = {}
    for leaf in evidence:
        aspect_leaves[leaf.aspect] = aspect_leaves.get(leaf.aspect, 0) + 1
    evidence_components: list[FitComponent] = []
    for leaf in evidence:
        w = (1 - g) / 3 / aspect_leaves[leaf.aspect]
        nominal = 0.0 if leaf.status == "NOT_IN_DEFINITION" else w
        actual = nominal if leaf.status in PARTICIPATING else 0.0
        evidence_components.append(_component(leaf, nominal, actual))
    # Every non-participating leaf's weight returns to shape, never to other evidence.
    shape_weight = 1 - sum(c.actual_weight for c in evidence_components)
    n = len(shape)
    shape_score = sum(leaf.score or 0.0 for leaf in shape) / n
    components = [_component(leaf, g / n, shape_weight / n) for leaf in shape]
    components += evidence_components

    used: dict[str, str] = {}
    for c in components:
        if c.status not in PARTICIPATING:
            continue
        for fact in c.facts:
            if fact in used:
                raise ValueError(f"double counting: {fact} feeds {used[fact]} and {c.component}")
            used[fact] = c.component

    exact = sum(c.actual_weight * (c.score or 0.0) for c in components)
    return DefinitionFit(
        value=math.floor(100 * exact + 0.5),
        exact=exact,
        fit_version=FIT_VERSION,
        shape_share=g,
        shape_score=shape_score,
        shape_weight=shape_weight,
        components=components,
    )


def _component(leaf: _Leaf, nominal: float, actual: float) -> FitComponent:
    return FitComponent(
        component=leaf.component,
        aspect=leaf.aspect,
        status=leaf.status,
        reason=leaf.reason,
        score=leaf.score if leaf.status in PARTICIPATING else None,
        nominal_weight=nominal,
        actual_weight=actual,
        inputs=leaf.inputs,
        facts=_facts(leaf.inputs),
        evidence_refs=leaf.refs,
        methodology_version=f"fit-{FIT_VERSION}",
    )


# ---------------------------------------------------------------- shape (§7)


def _shape(
    family: str,
    pattern_type: str,
    m: dict[str, float],
    widths: tuple[float, float] | None,
    context: PatternContext,
    cfg: PatternSection,
) -> list[_Leaf]:
    c: object = cfg

    def leaf(name: str, value: float, *keys: str) -> _Leaf:
        inputs: dict[str, Value] = {f"geometry.{k}": m[k] for k in keys}
        return _Leaf(name, "SHAPE", "APPLICABLE", score=value, inputs=inputs)

    def num(attr: str) -> float:
        return float(getattr(c, attr))

    out: list[_Leaf] = []
    if family == "double":
        out += [
            leaf(
                "lows_equal",
                closeness(m["extreme_difference_atr"], num("eq_tol_atr")),
                "extreme_difference_atr",
            ),
            leaf("height", margin(m["height_atr"], num("min_height_atr")), "height_atr"),
        ]
    elif family == "triple":
        out += [
            leaf(
                "lows_equal",
                closeness(m["extreme_spread_atr"], num("eq_tol_atr")),
                "extreme_spread_atr",
            ),
            leaf("height", margin(m["height_atr"], num("min_height_atr")), "height_atr"),
            leaf(
                "peaks_similar",
                closeness(m["peak_difference_atr"], 2 * num("eq_tol_atr")),
                "peak_difference_atr",
            ),
        ]
    elif family == "head_shoulders":
        out += [
            leaf(
                "head_prominence",
                margin(m["head_prominence_atr"], num("head_prominence_atr")),
                "head_prominence_atr",
            ),
            leaf(
                "shoulders_equal",
                closeness(m["shoulder_difference_atr"], num("shoulder_tol_atr")),
                "shoulder_difference_atr",
            ),
            leaf(
                "time_balance",
                balance(m["time_ratio"], num("time_balance_min"), num("time_balance_max")),
                "time_ratio",
            ),
            leaf(
                "neckline_flat",
                closeness(abs(m["neckline_slope_atr"]), num("neckline_max_slope_atr")),
                "neckline_slope_atr",
            ),
        ]
    elif family in ("rounding", "cup_handle"):
        out += [
            leaf("curve_fit", rescaled(m["r2"], num("min_r2")), "r2"),
            leaf(
                "rims_equal",
                closeness(m["rim_difference_atr"], num("rim_tol_atr")),
                "rim_difference_atr",
            ),
            leaf("depth", margin(m["depth_atr"], num("min_depth_atr")), "depth_atr"),
        ]
        if family == "rounding":
            out.append(
                leaf(
                    "vertex_central",
                    balance(
                        m["vertex_fraction"] / 0.5, num("vertex_min") / 0.5, num("vertex_max") / 0.5
                    ),
                    "vertex_fraction",
                )
            )
        else:
            out.append(
                leaf(
                    "handle_shallow",
                    closeness(m["handle_ratio"], num("handle_max_ratio")),
                    "handle_ratio",
                )
            )
    elif family == "v":
        out += [
            leaf("move_size", margin(m["drop_atr"], num("v_move_atr")), "drop_atr"),
            leaf("move_speed", _clip(1 - m["drop_bars"] / (num("max_drop_bars") + 1)), "drop_bars"),
        ]
    elif family == "rectangle":
        out += [
            leaf(
                "upper_band",
                closeness(m["upper_difference_atr"], num("band_tol_atr")),
                "upper_difference_atr",
            ),
            leaf(
                "lower_band",
                closeness(m["lower_difference_atr"], num("band_tol_atr")),
                "lower_difference_atr",
            ),
            leaf("height", margin(m["height_atr"], num("min_height_atr")), "height_atr"),
        ]
    elif family in ("triangle", "wedge"):
        su, sl = m["upper_slope_atr"], m["lower_slope_atr"]
        flat = num("flat_slope_atr")
        if pattern_type == "TRIANGLE_ASCENDING":
            out.append(leaf("upper_flat", closeness(abs(su), flat), "upper_slope_atr"))
        elif pattern_type == "TRIANGLE_DESCENDING":
            out.append(leaf("lower_flat", closeness(abs(sl), flat), "lower_slope_atr"))
        elif pattern_type == "TRIANGLE_SYMMETRICAL":
            r = num("symmetry_ratio")
            ratio = abs(su) / abs(sl) if sl else math.inf
            out.append(
                leaf(
                    "slopes_symmetrical",
                    balance(ratio, 1 / r, r),
                    "upper_slope_atr",
                    "lower_slope_atr",
                )
            )
        else:
            # §7.8 "slope agreement" (ADR-0022 §16): how clearly the shallower line also
            # slopes the wedge's way, in the §5 margin shape against the flat threshold.
            out.append(
                leaf(
                    "slopes_agree",
                    margin(min(abs(su), abs(sl)), flat),
                    "upper_slope_atr",
                    "lower_slope_atr",
                )
            )
        w0, w1 = m["width_start_atr"], m["width_end_atr"]
        out.append(
            leaf(
                "convergence",
                margin(1 - w1 / w0 if w0 > 0 else 0.0, 1 - num("converge_ratio")),
                "width_start_atr",
                "width_end_atr",
            )
        )
    elif family in ("flag", "pennant"):
        out.append(leaf("pole", margin(m["pole_atr"], num("pole_atr")), "pole_atr"))
        if family == "flag":
            out.append(
                leaf(
                    "parallel",
                    closeness(
                        abs(m["upper_slope_atr"] - m["lower_slope_atr"]), num("parallel_tol_atr")
                    ),
                    "upper_slope_atr",
                    "lower_slope_atr",
                )
            )
        else:
            assert widths is not None
            w0, w1 = widths
            conv = margin(1 - w1 / w0 if w0 > 0 else 0.0, 1 - num("converge_ratio"))
            out.append(
                _Leaf(
                    "convergence",
                    "SHAPE",
                    "APPLICABLE",
                    score=conv,
                    inputs={"geometry.line_widths": f"{w0:.6g}/{w1:.6g}"},
                )
            )
        out.append(leaf("retrace", closeness(m["retrace"], num("max_retrace")), "retrace"))
    else:
        raise ValueError(f"no shape criteria for family {family}")
    if family in BOUNDARY_FAMILIES:
        n = context.boundary_touches_at_known
        assert n is not None
        out.append(
            _Leaf(
                "touches",
                "SHAPE",
                "APPLICABLE",
                score=_clip((n - 3) / 3),
                inputs={"context.boundary_touches_at_known": n},
            )
        )
    return out


# ---------------------------------------------------------------- evidence


def _evidence(
    family: str,
    pattern_type: str,
    direction: str,
    ctx: PatternContext,
    config: PatternsConfig,
    shape_facts: set[str],
) -> list[_Leaf]:
    neutral = direction == "NEUTRAL"
    reversal = family in REVERSAL
    assert neutral or reversal or pattern_type in CONTINUATION_TYPES, pattern_type
    bullish = direction == "BULLISH"
    # The trend the formation needs before it: a reversal reverses it, a continuation
    # continues it.
    want_down = (reversal and bullish) or (not reversal and not bullish)
    out = [
        _prior_structure(ctx, neutral, want_down),
        _prior_move(family, ctx, config, neutral, want_down, shape_facts),
        _volume(family, ctx, config),
        _divergence(family, ctx),
        _level_alignment(family, ctx, bullish),
    ]
    return out


def _prior_structure(ctx: PatternContext, neutral: bool, want_down: bool) -> _Leaf:
    name, aspect = "prior_structure", "PRIOR_TREND"
    if neutral:
        return _Leaf(name, aspect, "NOT_IN_DEFINITION", "NEUTRAL_PATTERN")
    ps = ctx.prior_structure
    inputs: dict[str, Value] = {"context.prior_structure": ps.state}
    if ps.state is None:
        return _Leaf(name, aspect, "UNAVAILABLE", "INSUFFICIENT_HISTORY", inputs=inputs)
    wanted, opposite = (TREND_DOWN, TREND_UP) if want_down else (TREND_UP, TREND_DOWN)
    regime = "DOWN" if want_down else "UP"
    # Ordinal: evenly spaced ranks encode order only (§15.2 rule 6).
    if ps.state in wanted:
        value = 1.0
    elif ps.state == "TRANSITION" and ps.regime == regime:
        value = 2 / 3
    elif ps.state in opposite:
        value = 0.0
    else:  # RANGE, or TRANSITION out of the opposite regime
        value = 1 / 3
    return _Leaf(name, aspect, "APPLICABLE", score=value, inputs=inputs)


def _prior_move(
    family: str,
    ctx: PatternContext,
    config: PatternsConfig,
    neutral: bool,
    want_down: bool,
    shape_facts: set[str],
) -> _Leaf:
    name, aspect = "prior_move", "PRIOR_TREND"
    if neutral:
        return _Leaf(name, aspect, "NOT_IN_DEFINITION", "NEUTRAL_PATTERN")
    if PRIOR_MOVE_FACT.get(family, "move.before_pattern") in shape_facts:
        return _Leaf(name, aspect, "NOT_APPLICABLE", "MOVE_IS_SHAPE")
    key = "decline_into_atr" if want_down else "rise_into_atr"
    move = getattr(ctx.prior_move, key)
    inputs: dict[str, Value] = {f"context.prior_move.{key}": move}
    if move is None:
        return _Leaf(name, aspect, "UNAVAILABLE", "INSUFFICIENT_HISTORY", inputs=inputs)
    cap = 2 * config.common(getattr(config, family), "context_move_atr")
    return _Leaf(name, aspect, "APPLICABLE", score=_clip(move / cap), inputs=inputs)


def _volume(family: str, ctx: PatternContext, config: PatternsConfig) -> _Leaf:
    name, aspect = "volume_behaviour", "VOLUME"
    at = {v.key_point: v for v in ctx.volume_at}
    labels = list(at)

    def sma(label: str) -> float | None:
        return at[label].volume_sma if label in at else None

    if family == "v":
        low = labels[1]
        rvol = at[low].relative_volume if low in at else None
        inputs: dict[str, Value] = {"context.volume_at": f"rvol@{low}={rvol}"}
        if rvol is None:
            return _Leaf(name, aspect, "UNAVAILABLE", "INSUFFICIENT_HISTORY", inputs=inputs)
        ok = rvol >= config.v.capitulation_rvol
        return _Leaf(name, aspect, "APPLICABLE", score=float(ok), inputs=inputs)
    # (later, earlier...): volume at `later` must be below volume at each `earlier`.
    rule: dict[str, tuple[int, tuple[int, ...]]] = {
        "double": (2, (0,)),
        "triple": (4, (0,)),
        "head_shoulders": (4, (2,)),
        "flag": (4, (1,)),
        "pennant": (4, (1,)),
        "cup_handle": (1, (0,)),
        "rectangle": (-1, (0,)),
        "triangle": (-1, (0,)),
        "wedge": (-1, (0,)),
    }
    if family == "rounding":
        later, earlier = "VERTEX", ["RIM_1", "RIM_2"]
    else:
        i, js = rule[family]
        defining = [x for x in labels if x != "VERTEX"]
        later, earlier = defining[i], [defining[j] for j in js]
    values = {x: sma(x) for x in (later, *earlier)}
    inputs = {"context.volume_at": ", ".join(f"sma@{k}={v}" for k, v in values.items())}
    if any(v is None for v in values.values()):
        return _Leaf(name, aspect, "UNAVAILABLE", "INSUFFICIENT_HISTORY", inputs=inputs)
    lv = values[later]
    assert lv is not None
    ok = all(lv < (values[e] or 0.0) for e in earlier)
    return _Leaf(name, aspect, "APPLICABLE", score=float(ok), inputs=inputs)


def _divergence(family: str, ctx: PatternContext) -> _Leaf:
    name, aspect = "divergence", "CONFLUENCE"
    if family not in DIVERGENCE_FAMILIES:
        return _Leaf(name, aspect, "NOT_IN_DEFINITION", "NOT_IN_FAMILY_DEFINITION")
    d = ctx.divergence
    if d.presence == "NOT_APPLICABLE":
        return _Leaf(name, aspect, "NOT_APPLICABLE", d.not_applicable_reason)
    refs = tuple(x.divergence_id for x in d.divergences)
    inputs: dict[str, Value] = {"context.divergence": d.presence}
    present = d.presence == "PRESENT"
    return _Leaf(name, aspect, "APPLICABLE", score=float(present), inputs=inputs, refs=refs)


def _level_alignment(family: str, ctx: PatternContext, bullish: bool) -> _Leaf:
    name, aspect = "level_alignment", "CONFLUENCE"
    if family not in LEVEL_FAMILIES:
        return _Leaf(name, aspect, "NOT_IN_DEFINITION", "NOT_IN_FAMILY_DEFINITION")
    role = "SUPPORT" if bullish else "RESISTANCE"
    hits = [lv for lv in ctx.levels_near_base if lv.role == role]
    inputs: dict[str, Value] = {
        "context.levels_near_base": f"{len(hits)} {role.lower()} of {len(ctx.levels_near_base)}"
    }
    return _Leaf(
        name,
        aspect,
        "APPLICABLE",
        score=float(bool(hits)),
        inputs=inputs,
        refs=tuple(lv.level_id for lv in hits),
    )
