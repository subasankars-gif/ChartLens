"""Layer H, Phase 5b-B: pattern context (ADR-0022 §5, §12).

Context is a **snapshot of facts available at the pattern's ``known_at``**: the bars up to
that bar, and every other layer's objects as they stood then (``as_of(known_at)``). It
is never a description derived from what happened afterwards. A run as of ``known_at``
produces exactly the same context as the full run (a replay test pins this).

Context records measured facts only. Mapping them to [0, 1] components and a confidence
is a separate, later step (5b-C), so a change to scoring can never be mistaken for a
change to what was observed. Context never creates, deletes or reshapes a pattern.
"""

from __future__ import annotations

import math
from datetime import date
from typing import TYPE_CHECKING

import numpy as np

from chartlens_core.config import PatternsConfig, PatternSection
from chartlens_engine.causal import Array, CompleteBars, Frozen
from chartlens_engine.evidence import DivergenceResult, VolatilityResult
from chartlens_engine.fibonacci import FibonacciResult
from chartlens_engine.indicators import IndicatorResult
from chartlens_engine.levels import LevelsResult
from chartlens_engine.structure import StructureResult

if TYPE_CHECKING:  # candidates imports model, which imports this module
    from chartlens_engine.patterns.candidates import Spec

CONTEXT_VERSION = "1"


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
    state: str | None
    regime: str | None
    pending: str | None
    since: date | None
    last_event_id: str | None
    events_in_span: tuple[str, ...]
    """BOS/CHoCH events between the first defining bar and ``known_at``."""


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
    fib_id: str
    sensitivity: str
    status: str
    """As of ``known_at``."""
    leg_direction: str
    base_price: float
    """The pattern's base (see ``_base``)."""
    base_retracement: float
    """Where the base sits in the leg: 0 = at the counter swing, 1 = at the anchor."""
    nearest_ratio: float
    nearest_distance_atr: float


class PatternContext(Frozen):
    as_of: date
    """Equal to the pattern's ``known_at``: the context is that day's snapshot."""
    context_version: str
    prior_move: PriorMove
    structure: StructureContext
    levels_near: list[LevelNearby]
    """Levels known by ``known_at`` within ``level_tol_atr`` × ATR_D of a key point,
    excluding levels built from the pattern's own defining swings."""
    volume: VolumeContext
    volatility: VolatilityContext
    divergence_ids: tuple[str, ...]
    """Divergences in the pattern's direction, known by ``known_at``, whose second swing
    is a defining swing (empty for neutral patterns)."""
    fibonacci: list[FibonacciContext]
    evidence_refs: tuple[str, ...]
    """Every object id this context cites."""


def _base(spec: Spec) -> float:
    """The pattern's base: its lowest defining low (bullish) or highest defining high
    (bearish); its invalidation level when no defining swing is of that type (a rounding
    pattern's rims); its last defining swing when neutral."""
    if spec.direction == "NEUTRAL":
        return spec.defining[-1].price
    kind = "LOW" if spec.direction == "BULLISH" else "HIGH"
    prices = [s.price for s in spec.defining if s.type == kind]
    if prices:
        return min(prices) if kind == "LOW" else max(prices)
    if spec.invalidation_level is not None:
        return spec.invalidation_level
    return spec.defining[-1].price


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
    ) -> None:
        self.config = config
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

    def build(self, spec: Spec) -> PatternContext:
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
            and e.known_at <= day
            and first.bar_date <= e.bar_date <= last.bar_date
        )
        refs += list(contractions)
        volatility = VolatilityContext(
            atr_percent_first=_value(self.atr_pct, first.bar_index),
            atr_percent_last=_value(self.atr_pct, last.bar_index),
            atr_percent_known=_value(self.atr_pct, k),
            contraction_event_ids=contractions,
        )
        divergences = self._divergences(spec, day)
        refs += list(divergences)
        fibonacci = self._fibonacci(spec, day)
        refs += [f.fib_id for f in fibonacci]
        return PatternContext(
            as_of=day,
            context_version=CONTEXT_VERSION,
            prior_move=prior,
            structure=structure,
            levels_near=levels,
            volume=volume,
            volatility=volatility,
            divergence_ids=divergences,
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
        known = [e for e in self.structure.events if e.known_at <= day]
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
        tol = self.config.common(self.config_section(spec), "level_tol_atr") * spec.atr_d
        own = {s.swing_id for s in spec.defining}
        out: list[LevelNearby] = []
        for lv in self.levels.levels:
            snap = lv.as_of(day)
            if snap is None or lv.ref_id in own or self.level_swing.get(lv.ref_id) in own:
                continue
            for label, s in zip(spec.labels, spec.defining, strict=True):
                d = abs(s.price - lv.price)
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
        out.sort(key=lambda x: (x.key_point, x.distance_atr, x.level_id))
        return out

    def _divergences(self, spec: Spec, day: date) -> tuple[str, ...]:
        if spec.direction == "NEUTRAL":
            return ()
        want = "BULLISH" if spec.direction == "BULLISH" else "BEARISH"
        own = {s.swing_id for s in spec.defining}
        return tuple(
            d.divergence_id
            for d in self.divergence.divergences
            if d.known_at <= day and d.type.endswith(want) and d.price_swing_2 in own
        )

    def _fibonacci(self, spec: Spec, day: date) -> list[FibonacciContext]:
        base = _base(spec)
        out: list[FibonacciContext] = []
        for fib in self.fibonacci.current(day):
            leg = fib.counter_price - fib.anchor_price
            if leg == 0:
                continue
            nearest = min(fib.levels, key=lambda lv: (abs(lv.price - base), lv.ratio))
            out.append(
                FibonacciContext(
                    fib_id=fib.fib_id,
                    sensitivity=fib.sensitivity,
                    status=fib.status,
                    leg_direction=fib.direction,
                    base_price=base,
                    base_retracement=(fib.counter_price - base) / leg,
                    nearest_ratio=nearest.ratio,
                    nearest_distance_atr=abs(nearest.price - base) / spec.atr_d,
                )
            )
        return out
