"""Layer H, Phase 5b-B: pattern context (ADR-0022 §5, §12).

Context is a **snapshot of facts available at the pattern's ``known_at``**: the bars up to
that bar, and every other layer's objects as they stood then (``as_of(known_at)``). It
is never a description derived from what happened afterwards. A run as of ``known_at``
produces exactly the same context as the full run (a replay test pins this).

Context records measured facts only. Mapping them to [0, 1] components and a confidence
is a separate, later step (5b-C), so a change to scoring can never be mistaken for a
change to what was observed. Context never creates, deletes or reshapes a pattern.

**Object-level known_at contract (5b-B review).** An object is eligible for a pattern's
context only if the object's *own* ``known_at`` is on or before the pattern's
``known_at``. Old observations are not enough: a swing, level, divergence, Fibonacci
structure, contraction episode or structure event whose bars all precede ``known_at`` but
which only became knowable later is excluded. Bar-indexed facts (indicators, volume and
volatility readings) are read at bars ≤ ``known_at`` only. Every lookup below goes through
``_known`` or an ``as_of(day)`` projection, never through bar dates alone.
"""

from __future__ import annotations

import math
from datetime import date
from typing import TYPE_CHECKING, Literal

import numpy as np

from chartlens_core.config import PatternsConfig, PatternSection
from chartlens_engine.causal import Array, CompleteBars, Frozen
from chartlens_engine.evidence import DivergenceResult, VolatilityResult
from chartlens_engine.fibonacci import FibonacciResult
from chartlens_engine.indicators import IndicatorResult
from chartlens_engine.levels import LevelsResult
from chartlens_engine.structure import StructureResult
from chartlens_engine.swings import SwingPoint

if TYPE_CHECKING:  # candidates imports model, which imports this module
    from chartlens_engine.patterns.candidates import Spec

CONTEXT_VERSION = "3"
"""2: divergence PRESENT / ABSENT / NOT_APPLICABLE with statuses as of ``known_at``;
Fibonacci structures carry their swings and ``known_at`` (5b-B review).
3: the structure before the pattern (at the first defining swing's bar), volume at every
key point, levels near the pattern's base, boundary touches known by ``known_at``, and a
rounding pattern's base at its fitted extreme (5b-C, ADR-0022 §15)."""


def _known(object_known_at: date | None, day: date) -> bool:
    """The object-level contract: the object itself was knowable by ``day``."""
    return object_known_at is not None and object_known_at <= day


class PriorMove(Frozen):
    """The ``context_lookback`` complete bars before the first defining swing."""

    bars: int
    """Bars actually available (fewer near the start of the segment)."""
    window_start: date | None
    window_end: date | None
    highest_close: float | None
    highest_close_date: date | None
    lowest_close: float | None
    lowest_close_date: date | None
    rise_into_atr: float | None
    """(price of the first defining swing − lowest close) / ATR at that swing's bar."""
    decline_into_atr: float | None
    """(highest close − price of the first defining swing) / ATR at that swing's bar."""
    range_atr: float | None
    """(highest high − lowest low) of the window / ATR: how tight the preceding
    consolidation was."""


class StructureContext(Frozen):
    """Market structure **as of ``known_at``**: what the structure layer said on the day
    the pattern became known. This is not the trend that preceded the pattern: by
    ``known_at`` the pattern's own defining move may already have changed the state (the
    swing that completes a double bottom can itself be a CHoCH). For the move into the
    pattern use ``PriorMove``."""

    state: str | None
    regime: str | None
    pending: str | None
    since: date | None
    last_event_id: str | None
    events_in_span: tuple[str, ...]
    """BOS/CHoCH events between the first defining bar and ``known_at``."""


class PriorStructure(Frozen):
    """Market structure **before the pattern**: the structure layer's state as of the
    first defining swing's bar. That swing is not yet confirmed then, so the state comes
    only from swings known before the formation began (ADR-0022 §15.1). Its own
    ``as_of`` is ≤ the pattern's ``known_at``."""

    as_of: date
    """The first defining swing's bar date."""
    state: str | None
    """None when the structure layer had no state yet (warm-up)."""
    regime: str | None
    pending: str | None
    since: date | None


class KeyPointVolume(Frozen):
    key_point: str
    """A defining swing's label, or VERTEX for a rounding pattern's fitted extreme."""
    bar_date: date
    volume_sma: float | None
    relative_volume: float | None


class LevelNearby(Frozen):
    key_point: str
    level_id: str
    source_type: str
    role: str
    """The level's role as of ``known_at``."""
    price: float
    distance_atr: float


class VolumeContext(Frozen):
    volume_sma_first: float | None
    """Volume SMA at the first defining swing's bar."""
    volume_sma_last: float | None
    """At the last defining swing's bar."""
    relative_volume_last: float | None
    volume_state: str | None
    """At ``known_at``."""
    volume_trend: str | None
    obv_change_ratio: float | None
    """OBV change from the first to the last defining bar / (bars × volume SMA there)."""


class VolatilityContext(Frozen):
    atr_percent_first: float | None
    atr_percent_last: float | None
    atr_percent_known: float | None
    contraction_event_ids: tuple[str, ...]
    """Contraction episodes starting inside the span, known by ``known_at``."""


class FibonacciContext(Frozen):
    """Where the pattern's base sits in a Fibonacci structure current at ``known_at``.
    The structure's identity is kept so later analysis can judge whether that
    retracement is relevant to the pattern; its mere existence carries no information."""

    fib_id: str
    sensitivity: str
    anchor_swing_id: str
    counter_swing_id: str
    fib_known_at: date
    """The structure's own ``known_at`` (≤ the pattern's)."""
    status: str
    """As of ``known_at``."""
    leg_direction: str
    base_price: float
    """The pattern's base (see ``_base``)."""
    base_retracement: float
    """Where the base sits in the leg: 0 = at the counter swing, 1 = at the anchor."""
    nearest_ratio: float
    nearest_distance_atr: float


DivergencePresence = Literal["PRESENT", "ABSENT", "NOT_APPLICABLE"]


class DivergenceRef(Frozen):
    divergence_id: str
    type: str
    indicator: str
    status: str
    """As of ``known_at``."""


class DivergenceContext(Frozen):
    """Divergence ending at one of the pattern's defining swings, in its direction.

    Divergence is computed on the primary swings only. ``NOT_APPLICABLE`` says the
    methodology cannot observe it here, which is different from ``ABSENT``:

    - ``NEUTRAL_DIRECTION``: the pattern has no direction to agree with;
    - ``FINE_SWING_GEOMETRY``: the defining swings of the needed type (lows for a bullish
      pattern, highs for a bearish one) are all fine swings (flags, pennants);
    - ``NO_DEFINING_SWING_OF_TYPE``: no defining swing of that type at all (a rounding
      bottom's bowl is a curve, not a swing).

    ``PRESENT``: at least one such divergence known by ``known_at`` is FORMING or
    CONFIRMED as of that day. ``ABSENT``: none is (any listed are INVALIDATED or EXPIRED
    by then)."""

    presence: DivergencePresence
    not_applicable_reason: str | None
    divergences: list[DivergenceRef]


class PatternContext(Frozen):
    as_of: date
    """Equal to the pattern's ``known_at``: the context is that day's snapshot."""
    context_version: str
    prior_move: PriorMove
    structure: StructureContext
    levels_near: list[LevelNearby]
    """Levels known by ``known_at`` within ``level_tol_atr`` × ATR_D of a key point,
    excluding levels built from the pattern's own defining swings."""
    levels_near_base: list[LevelNearby]
    """Levels known by ``known_at`` within ``level_tol_atr`` × ATR_D of the pattern's base
    (``key_point`` = BASE), with their role as of ``known_at``; own swings excluded."""
    base_price: float
    prior_structure: PriorStructure
    volume: VolumeContext
    volume_at: list[KeyPointVolume]
    """Volume SMA and RVOL at every key point's bar (all ≤ ``known_at``)."""
    boundary_touches_at_known: int | None
    """Boundary patterns: defining swings plus touches known by ``known_at``; None
    otherwise."""
    volatility: VolatilityContext
    divergence: DivergenceContext
    fibonacci: list[FibonacciContext]
    evidence_refs: tuple[str, ...]
    """Every object id this context cites."""


def _base(spec: Spec) -> tuple[float, int]:
    """The pattern's base and its bar: its lowest defining low (bullish) or highest
    defining high (bearish); a rounding pattern's fitted extreme (the bowl's vertex, where
    §7 places its level alignment and volume); its last defining swing when neutral."""
    if spec.direction == "NEUTRAL":
        return spec.defining[-1].price, spec.defining[-1].bar_index
    if spec.family == "rounding":
        m = spec.measures
        a, b, c = m["fit_a"], m["fit_b"], m["fit_c"]  # price space (s · q-space)
        return c - b * b / (4 * a), round(m["fit_origin_index"] - b / (2 * a))
    kind = "LOW" if spec.direction == "BULLISH" else "HIGH"
    swings = [s for s in spec.defining if s.type == kind]
    if swings:
        pick = min if kind == "LOW" else max
        best = pick(swings, key=lambda s: (s.price, s.bar_index))
        return best.price, best.bar_index
    if spec.invalidation_level is not None:
        return spec.invalidation_level, spec.defining[-1].bar_index
    return spec.defining[-1].price, spec.defining[-1].bar_index


def _value(x: Array, i: int) -> float | None:
    if i < 0 or i >= len(x):
        return None
    v = float(x[i])
    return None if math.isnan(v) else v


class ContextBuilder:
    def __init__(
        self,
        config: PatternsConfig,
        cb: CompleteBars,
        indicators: IndicatorResult,
        structure: StructureResult,
        levels: LevelsResult,
        fibonacci: FibonacciResult,
        divergence: DivergenceResult,
        volatility: VolatilityResult,
        primary: tuple[str, str],
    ) -> None:
        """``primary``: the (method, sensitivity) of the primary swings, on which
        divergence is computed."""
        self.config = config
        self.primary = primary
        self.cb = cb
        self.structure = structure
        self.levels = levels
        self.fibonacci = fibonacci
        self.divergence = divergence
        self.volatility = volatility
        n = cb.n

        def series(name: str) -> Array:
            return np.array(
                [np.nan if v is None else float(v) for v in indicators.get(name).data[:n]],
                dtype=np.float64,
            )

        self.atr = series("atr")
        self.atr_pct = series("atr_percent")
        self.vsma = series("volume_sma")
        self.rvol = series("relative_volume")
        self.obv = series("obv")
        self.volume_state = indicators.get("volume_state").data[:n]
        self.volume_trend = indicators.get("volume_trend").data[:n]
        self.level_swing = {e.event_id: e.level_swing_id for e in structure.events}

    def build(self, spec: Spec, boundary_touches: int | None = None) -> PatternContext:
        """``boundary_touches``: touches of a boundary pattern known by ``known_at``
        (counted by the analyzer with that cut-off, never from the lifecycle)."""
        cb = self.cb
        k = spec.known_index
        day = cb.dates[k]
        first, last = spec.defining[0], spec.defining[-1]
        refs: list[str] = []

        prior = self._prior_move(spec)
        structure = self._structure(spec, day)
        refs += list(structure.events_in_span)
        if structure.last_event_id:
            refs.append(structure.last_event_id)
        levels = self._levels(spec, day)
        refs += [lv.level_id for lv in levels]
        base_price, base_bar = _base(spec)
        near_base = self._levels_near(spec, day, "BASE", base_price)
        refs += [lv.level_id for lv in near_base]
        prior_structure = self._prior_structure(spec)
        points = [
            (label, sw.bar_index) for label, sw in zip(spec.labels, spec.defining, strict=True)
        ]
        if spec.family == "rounding":
            points.append(("VERTEX", base_bar))
        volume_at = [
            KeyPointVolume(
                key_point=label,
                bar_date=cb.dates[b],
                volume_sma=_value(self.vsma, b),
                relative_volume=_value(self.rvol, b),
            )
            for label, b in points
            if 0 <= b <= k
        ]
        span = max(1, last.bar_index - first.bar_index)
        o1, o2 = _value(self.obv, first.bar_index), _value(self.obv, last.bar_index)
        v_last = _value(self.vsma, last.bar_index)
        volume = VolumeContext(
            volume_sma_first=_value(self.vsma, first.bar_index),
            volume_sma_last=v_last,
            relative_volume_last=_value(self.rvol, last.bar_index),
            volume_state=None if self.volume_state[k] is None else str(self.volume_state[k]),
            volume_trend=None if self.volume_trend[k] is None else str(self.volume_trend[k]),
            obv_change_ratio=None
            if o1 is None or o2 is None or not v_last
            else (o2 - o1) / (span * v_last),
        )
        contractions = tuple(
            e.event_id
            for e in self.volatility.events
            if e.kind != "EXPANSION_AFTER_CONTRACTION"
            and _known(e.known_at, day)
            and first.bar_date <= e.bar_date <= last.bar_date
        )
        refs += list(contractions)
        volatility = VolatilityContext(
            atr_percent_first=_value(self.atr_pct, first.bar_index),
            atr_percent_last=_value(self.atr_pct, last.bar_index),
            atr_percent_known=_value(self.atr_pct, k),
            contraction_event_ids=contractions,
        )
        divergence = self._divergence(spec, day)
        refs += [d.divergence_id for d in divergence.divergences]
        fibonacci = self._fibonacci(spec, day)
        refs += [f.fib_id for f in fibonacci]
        return PatternContext(
            as_of=day,
            context_version=CONTEXT_VERSION,
            prior_move=prior,
            structure=structure,
            levels_near=levels,
            levels_near_base=near_base,
            base_price=base_price,
            prior_structure=prior_structure,
            volume=volume,
            volume_at=volume_at,
            boundary_touches_at_known=None
            if spec.touch_tol_atr is None
            else len(spec.defining) + (boundary_touches or 0),
            volatility=volatility,
            divergence=divergence,
            fibonacci=fibonacci,
            evidence_refs=tuple(dict.fromkeys(refs)),
        )

    # ------------------------------------------------------------------ parts

    def _prior_move(self, spec: Spec) -> PriorMove:
        cb = self.cb
        first = spec.defining[0]
        lookback = int(self.config.common(self.config_section(spec), "context_lookback"))
        start, end = max(0, first.bar_index - lookback), first.bar_index  # [start, end)
        a = _value(self.atr, first.bar_index)
        if end <= start:
            return PriorMove(
                bars=0,
                window_start=None,
                window_end=None,
                highest_close=None,
                highest_close_date=None,
                lowest_close=None,
                lowest_close_date=None,
                rise_into_atr=None,
                decline_into_atr=None,
                range_atr=None,
            )
        close = cb.close[start:end]
        hi_i, lo_i = start + int(np.argmax(close)), start + int(np.argmin(close))
        hi_c, lo_c = float(cb.close[hi_i]), float(cb.close[lo_i])
        rng = float(cb.high[start:end].max() - cb.low[start:end].min())
        return PriorMove(
            bars=end - start,
            window_start=cb.dates[start],
            window_end=cb.dates[end - 1],
            highest_close=hi_c,
            highest_close_date=cb.dates[hi_i],
            lowest_close=lo_c,
            lowest_close_date=cb.dates[lo_i],
            rise_into_atr=None if not a else (first.price - lo_c) / a,
            decline_into_atr=None if not a else (hi_c - first.price) / a,
            range_atr=None if not a else rng / a,
        )

    def config_section(self, spec: Spec) -> PatternSection:
        section: PatternSection = getattr(self.config, spec.family)
        return section

    def _structure(self, spec: Spec, day: date) -> StructureContext:
        state = self.structure.state_as_of(day)
        known = [e for e in self.structure.events if _known(e.known_at, day)]
        first = spec.defining[0].bar_date
        return StructureContext(
            state=state.state if state else None,
            regime=state.regime if state else None,
            pending=state.pending if state else None,
            since=state.since if state else None,
            last_event_id=known[-1].event_id if known else None,
            events_in_span=tuple(e.event_id for e in known if e.bar_date >= first),
        )

    def _levels(self, spec: Spec, day: date) -> list[LevelNearby]:
        out: list[LevelNearby] = []
        for label, s in zip(spec.labels, spec.defining, strict=True):
            out += self._levels_near(spec, day, label, s.price)
        out.sort(key=lambda x: (x.key_point, x.distance_atr, x.level_id))
        return out

    def _levels_near(self, spec: Spec, day: date, label: str, price: float) -> list[LevelNearby]:
        """Levels known by ``day`` (``as_of``: the object-level contract) within
        ``level_tol_atr`` × ATR_D of ``price``, with their role that day. Levels built from
        the pattern's own defining swings are never counted."""
        tol = self.config.common(self.config_section(spec), "level_tol_atr") * spec.atr_d
        own = {s.swing_id for s in spec.defining}
        out: list[LevelNearby] = []
        for lv in self.levels.levels:
            snap = lv.as_of(day)
            if snap is None or lv.ref_id in own or self.level_swing.get(lv.ref_id) in own:
                continue
            d = abs(price - lv.price)
            if d <= tol:
                out.append(
                    LevelNearby(
                        key_point=label,
                        level_id=lv.level_id,
                        source_type=lv.source_type,
                        role=snap.role,
                        price=lv.price,
                        distance_atr=d / spec.atr_d,
                    )
                )
        out.sort(key=lambda x: (x.distance_atr, x.level_id))
        return out

    def _prior_structure(self, spec: Spec) -> PriorStructure:
        first = spec.defining[0].bar_date
        state = self.structure.state_as_of(first)
        return PriorStructure(
            as_of=first,
            state=state.state if state else None,
            regime=state.regime if state else None,
            pending=state.pending if state else None,
            since=state.since if state else None,
        )

    def _divergence(self, spec: Spec, day: date) -> DivergenceContext:
        reason = self._divergence_not_applicable(spec)
        if reason is not None:
            return DivergenceContext(
                presence="NOT_APPLICABLE", not_applicable_reason=reason, divergences=[]
            )
        want = "BULLISH" if spec.direction == "BULLISH" else "BEARISH"
        own = {s.swing_id for s in spec.defining}
        refs: list[DivergenceRef] = []
        for d in self.divergence.divergences:
            if not (_known(d.known_at, day) and d.type.endswith(want) and d.price_swing_2 in own):
                continue
            snap = d.as_of(day)
            assert snap is not None
            refs.append(
                DivergenceRef(
                    divergence_id=d.divergence_id,
                    type=d.type,
                    indicator=d.indicator,
                    status=snap.status,
                )
            )
        active = any(r.status in ("FORMING", "CONFIRMED") for r in refs)
        return DivergenceContext(
            presence="PRESENT" if active else "ABSENT",
            not_applicable_reason=None,
            divergences=refs,
        )

    def _divergence_not_applicable(self, spec: Spec) -> str | None:
        if spec.direction == "NEUTRAL":
            return "NEUTRAL_DIRECTION"
        kind = "LOW" if spec.direction == "BULLISH" else "HIGH"
        of_type: list[SwingPoint] = [s for s in spec.defining if s.type == kind]
        if not of_type:
            return "NO_DEFINING_SWING_OF_TYPE"
        if not any((s.method, s.sensitivity) == self.primary for s in of_type):
            return "FINE_SWING_GEOMETRY"
        return None

    def _fibonacci(self, spec: Spec, day: date) -> list[FibonacciContext]:
        base, _ = _base(spec)
        out: list[FibonacciContext] = []
        for fib in self.fibonacci.current(day):  # as_of(day) projections, known_at ≤ day
            leg = fib.counter_price - fib.anchor_price
            if leg == 0:
                continue
            nearest = min(fib.levels, key=lambda lv: (abs(lv.price - base), lv.ratio))
            out.append(
                FibonacciContext(
                    fib_id=fib.fib_id,
                    sensitivity=fib.sensitivity,
                    anchor_swing_id=fib.anchor_swing_id,
                    counter_swing_id=fib.counter_swing_id,
                    fib_known_at=fib.known_at,
                    status=fib.status,
                    leg_direction=fib.direction,
                    base_price=base,
                    base_retracement=(fib.counter_price - base) / leg,
                    nearest_ratio=nearest.ratio,
                    nearest_distance_atr=abs(nearest.price - base) / spec.atr_d,
                )
            )
        return out
