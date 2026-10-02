"""Layer D: support/resistance levels, zones and trendlines (ADR-0021 §D, Phase 4 rules).

Levels **consume** the layers before them and never redefine them: swing prices come
from the primary confirmed swings, structural levels from structure's BOS/CHoCH events,
Fibonacci levels from the Fibonacci layer and moving averages from the indicators. No
pivot, label or break of structure is computed here.

Existence is kept apart from relevance (ADR-0021):

* **Levels** (existence) are events: every primary swing price and every BOS/CHoCH level,
  known from its source's ``known_at``, with an append-only **role history**. A support
  closed below by ``level_break_atr`` × ATR becomes resistance on that bar, and the
  reverse, so a broken level never goes on looking like an untouched one.
* **Zones** (relevance) are current state, as of the last complete bar (``state_date``):
  sources known by then are clustered greedily in price order, and the nearest
  ``max_zones_per_side`` on each side are reported. Which zones are relevant is part of
  the methodology (configured and hashed), never a serving decision. A zone is
  ``known_at`` its latest source's ``known_at``. ``strength`` is a sum of measured
  components reported next to it: evidence of how much the level has mattered, not a
  probability or a score of the security.
* **Trendlines** are events. A line through two primary swings of one type (rising for
  support, falling for resistance) is *validated* by a third touch; it is ``known_at``
  that touch's ``known_at``, and BROKEN by the first complete close beyond it.
"""

from __future__ import annotations

import hashlib
import math
from datetime import date
from typing import Literal

import numpy as np
import pandas as pd

from chartlens_core.config import LevelsConfig
from chartlens_engine.causal import (
    Array,
    CompleteBars,
    Frozen,
    StatusEntry,
    complete_bars,
    history_as_of,
    numeric,
    state,
)
from chartlens_engine.fibonacci import FibonacciResult
from chartlens_engine.indicators import IndicatorResult
from chartlens_engine.interfaces import AnalysisContext, AnalyzerResult
from chartlens_engine.structure import StructureResult
from chartlens_engine.swings import SwingPoint, SwingResult

SourceType = Literal["SWING", "STRUCTURE", "DYNAMIC", "FIBONACCI"]
Side = Literal["SUPPORT", "RESISTANCE"]


class RoleChange(Frozen):
    role: Side
    date: date
    """The complete bar from which the level plays this role."""
    threshold: float | None
    """The close that had to be crossed (level ± buffer); None for the original role."""
    provisional: bool = False


class Level(Frozen):
    """A horizontal level and its role over time (existence, ADR-0021)."""

    level_id: str
    source_type: Literal["SWING", "STRUCTURE"]
    ref_id: str
    """The swing or structure event the price comes from."""
    continuity_segment_id: str
    price: float
    bar_date: date
    known_at: date
    original_role: Side
    """A swing low or a level broken upwards starts as support; the reverse as resistance."""
    role_history: list[RoleChange]
    high_volume: bool = False
    depends_on: tuple[str, ...]

    @property
    def role(self) -> Side:
        return self.role_history[-1].role

    def as_of(self, day: date) -> Level | None:
        if self.known_at > day:
            return None
        return self.model_copy(
            update={"role_history": [r for r in self.role_history if r.date <= day]}
        )


class ZoneSource(Frozen):
    source_type: SourceType
    ref_id: str
    """The swing, structure event, indicator or Fibonacci level this price comes from."""
    price: float
    bar_date: date | None
    """The bar the price belongs to (None for a moving average's current value)."""
    known_at: date
    high_volume: bool = False
    """A swing whose pivot bar had volume expansion (the VOLUME source type)."""
    level_id: str | None = None
    """The :class:`Level` behind a swing or structure source."""
    original_role: Side | None = None
    role: Side | None = None
    """The level's role at the state date."""
    role_since: date | None = None


class Zone(Frozen):
    zone_id: str
    type: Side
    price_low: float
    price_high: float
    continuity_segment_id: str
    sources: list[ZoneSource]
    source_types: tuple[str, ...]
    touches: list[date]
    """First bar of each run of touching complete bars since ``tested_since``: tests in
    the zone's current role."""
    tested_since: date
    """The bar after the later of the first source becoming known and the last role
    change of any of its levels."""
    role_changes: list[date]
    """Every role change of the zone's levels (breaks), oldest first."""
    role_reversed: bool
    """At least one of its levels now plays the opposite of its original role."""
    first_seen: date
    known_at: date
    """The latest source's ``known_at``: the zone, as built, exists from then."""
    last_tested: date | None
    strength: float
    strength_components: dict[str, float]
    """Each measured component, before weighting: touches, source_types, touch_rvol,
    recency."""
    distance_atr: float
    """From the last complete close to the zone's nearest edge, in ATR (0 inside it)."""
    depends_on: tuple[str, ...]

    @property
    def touch_count(self) -> int:
        return len(self.touches)


class TrendlineTouch(Frozen):
    swing_id: str
    bar_date: date
    known_at: date
    price: float
    line_value: float
    distance_atr: float | None


class Trendline(Frozen):
    trendline_id: str
    type: Side
    continuity_segment_id: str
    anchor_1_swing_id: str
    anchor_1_bar_date: date
    anchor_1_bar_index: int
    anchor_1_price: float
    anchor_2_swing_id: str
    anchor_2_bar_date: date
    slope_per_bar: float
    """Price change per weekly bar of the segment (bars, not calendar weeks)."""
    touches: list[TrendlineTouch]
    """Every swing on the line, each with its own ``known_at``; the anchors included."""
    known_at: date
    """The validating (third) touch's ``known_at``."""
    depends_on: tuple[str, ...]
    """The three swings that validated it."""
    status_history: list[StatusEntry]

    @property
    def status(self) -> str:
        return self.status_history[-1].status

    def value_at(self, bar_index: int) -> float:
        return self.anchor_1_price + self.slope_per_bar * (bar_index - self.anchor_1_bar_index)

    def as_of(self, day: date) -> Trendline | None:
        if self.known_at > day:
            return None
        return self.model_copy(
            update={
                "touches": [t for t in self.touches if t.known_at <= day],
                "status_history": history_as_of(self.status_history, day),
            }
        )


class ActiveTrendline(Frozen):
    trendline_id: str
    type: Side
    value: float
    """The line's value at the state date."""
    distance_atr: float


class LevelsResult(AnalyzerResult):
    state_date: date | None
    atr: float | None
    """ATR at the state date: the unit of every tolerance here."""
    last_close: float | None
    levels: list[Level]
    """Existence: every horizontal level, in the order it became known, with its role
    history."""
    zones: list[Zone]
    """Relevance: current zones, nearest first on each side (at most
    ``max_zones_per_side`` each)."""
    trendlines: list[Trendline]
    """Every validated trendline, in the order it became known."""
    active_trendlines: list[ActiveTrendline]


class LevelsAnalyzer:
    name = "levels"
    version = "1"

    def __init__(
        self,
        config: LevelsConfig,
        indicators: IndicatorResult,
        swings: SwingResult,
        structure: StructureResult,
        fibonacci: FibonacciResult,
    ) -> None:
        self.config = config
        self.indicators = indicators
        self.swings = swings
        self.structure = structure
        self.fibonacci = fibonacci

    def analyze(self, bars: pd.DataFrame, context: AnalysisContext) -> LevelsResult:
        for layer in (self.swings, self.structure, self.fibonacci):
            if layer.context != context:
                raise ValueError(f"{layer.analyzer} was computed for another context")
        cb = complete_bars(bars, context, self.indicators)
        atr = numeric(self.indicators, "atr", cb.n)
        index_of = cb.index()
        primary = [s for s in self.swings.primary() if s.known_at in index_of]
        trendlines = self._trendlines(cb, atr, primary, index_of)
        levels = self._levels(cb, atr, primary, index_of)
        if cb.n == 0 or np.isnan(atr[cb.n - 1]):
            # No complete bar, or ATR still warming up: no unit to measure zones in.
            return self._result(context, cb, None, levels, [], trendlines, [])
        a = float(atr[cb.n - 1])
        sources = self._sources(cb, levels)
        zones = self._zones(cb, a, sources, levels, index_of)
        active = self._active_trendlines(cb, a, trendlines)
        return self._result(context, cb, a, levels, zones, trendlines, active)

    def _result(
        self,
        context: AnalysisContext,
        cb: CompleteBars,
        a: float | None,
        levels: list[Level],
        zones: list[Zone],
        trendlines: list[Trendline],
        active: list[ActiveTrendline],
    ) -> LevelsResult:
        return LevelsResult(
            analyzer=self.name,
            analyzer_version=self.version,
            context=context,
            state_date=cb.state_date,
            atr=a,
            last_close=float(cb.close[-1]) if cb.n else None,
            levels=levels,
            zones=zones,
            trendlines=trendlines,
            active_trendlines=active,
        )

    # ------------------------------------------------------------------ zones

    # ------------------------------------------------------------------ levels

    def _levels(
        self,
        cb: CompleteBars,
        atr: Array,
        primary: list[SwingPoint],
        index_of: dict[date, int],
    ) -> list[Level]:
        volume_state = state(self.indicators, "volume_state", cb.n)
        origins: list[tuple[Literal["SWING", "STRUCTURE"], str, float, date, date, Side, bool]] = []
        for s in primary:
            assert s.known_at is not None
            role: Side = "SUPPORT" if s.type == "LOW" else "RESISTANCE"
            high_volume = volume_state[s.bar_index] == "EXPANSION"
            origins.append(
                ("SWING", s.swing_id, s.price, s.bar_date, s.known_at, role, high_volume)
            )
        for e in self.structure.events:
            if e.known_at in index_of:
                role = "SUPPORT" if e.direction == "UP" else "RESISTANCE"
                origins.append(
                    ("STRUCTURE", e.event_id, e.level, e.bar_date, e.known_at, role, False)
                )
        buffer = np.nan_to_num(atr * self.config.level_break_atr, nan=0.0)
        levels: list[Level] = []
        for source_type, ref, price, bar_date, known_at, role, high_volume in origins:
            k = index_of[known_at]
            history = [RoleChange(role=role, date=known_at, threshold=None)]
            current, t = role, k + 1  # never broken by the bar that makes it known
            while t < cb.n:
                below = cb.close[t:] < price - buffer[t:]
                above = cb.close[t:] > price + buffer[t:]
                hits = np.flatnonzero(below if current == "SUPPORT" else above)
                if not hits.size:
                    break
                t += int(hits[0])
                current = "RESISTANCE" if current == "SUPPORT" else "SUPPORT"
                sign = -1.0 if current == "RESISTANCE" else 1.0
                history.append(
                    RoleChange(
                        role=current,
                        date=cb.dates[t],
                        threshold=price + sign * float(buffer[t]),
                        provisional=bool(cb.special[t]),
                    )
                )
                t += 1
            levels.append(
                Level(
                    level_id=f"{ref}:LEVEL",
                    source_type=source_type,
                    ref_id=ref,
                    continuity_segment_id=cb.segment,
                    price=price,
                    bar_date=bar_date,
                    known_at=known_at,
                    original_role=role,
                    role_history=history,
                    high_volume=high_volume,
                    depends_on=(ref,),
                )
            )
        levels.sort(key=lambda lv: (lv.known_at, lv.bar_date, lv.level_id))
        return levels

    # ------------------------------------------------------------------ zones

    def _sources(self, cb: CompleteBars, levels: list[Level]) -> list[ZoneSource]:
        cfg = self.config
        day = cb.dates[-1]
        out = [
            ZoneSource(
                source_type=lv.source_type,
                ref_id=lv.ref_id,
                price=lv.price,
                bar_date=lv.bar_date,
                known_at=lv.known_at,
                high_volume=lv.high_volume,
                level_id=lv.level_id,
                original_role=lv.original_role,
                role=lv.role,
                role_since=lv.role_history[-1].date,
            )
            for lv in levels
        ]
        for name in cfg.dynamic_sources:
            v = self.indicators.get(name).data[cb.n - 1]
            if v is not None:
                out.append(
                    ZoneSource(
                        source_type="DYNAMIC",
                        ref_id=f"{name}@{day}",
                        price=float(v),
                        bar_date=None,
                        known_at=day,
                    )
                )
        for fib in self.fibonacci.current(day):
            if fib.status != "ACTIVE":
                continue
            for ratio in cfg.fibonacci_ratios:
                level = next((lv for lv in fib.levels if lv.ratio == ratio), None)
                if level is not None:
                    out.append(
                        ZoneSource(
                            source_type="FIBONACCI",
                            ref_id=f"{fib.fib_id}@{ratio}",
                            price=level.price,
                            bar_date=fib.counter_bar_date,
                            known_at=fib.known_at,
                        )
                    )
        return out

    def _zones(
        self,
        cb: CompleteBars,
        a: float,
        sources: list[ZoneSource],
        levels: list[Level],
        index_of: dict[date, int],
    ) -> list[Zone]:
        cfg = self.config
        tolerance = cfg.zone_tolerance_atr * a
        clusters: list[list[ZoneSource]] = []
        for src in sorted(sources, key=lambda s: (s.price, s.known_at, s.ref_id)):
            if clusters:
                current = clusters[-1]
                mean = sum(s.price for s in current) / len(current)
                if abs(src.price - mean) <= tolerance:
                    current.append(src)
                    continue
            clusters.append([src])
        close = float(cb.close[-1])
        candidates: list[tuple[Side, float, float, list[ZoneSource]]] = []
        for cluster in clusters:
            low = min(s.price for s in cluster)
            high = max(s.price for s in cluster)
            width = cfg.zone_min_width_atr * a
            if high - low < width:
                mid = (low + high) / 2
                low, high = mid - width / 2, mid + width / 2
            side: Side = "SUPPORT" if (low + high) / 2 <= close else "RESISTANCE"
            candidates.append((side, low, high, cluster))

        def distance(c: tuple[Side, float, float, list[ZoneSource]]) -> float:
            _, low, high, _ = c
            return max(0.0, low - close, close - high)

        chosen: list[tuple[Side, float, float, list[ZoneSource]]] = []
        for side in ("SUPPORT", "RESISTANCE"):
            same = sorted(
                (c for c in candidates if c[0] == side), key=lambda c: (distance(c), c[1])
            )
            chosen += same[: cfg.max_zones_per_side]
        by_id = {lv.level_id: lv for lv in levels}
        return [
            self._zone(
                cb, a, side, low, high, srcs, by_id, index_of, distance((side, low, high, srcs))
            )
            for side, low, high, srcs in chosen
        ]

    def _zone(
        self,
        cb: CompleteBars,
        a: float,
        side: Side,
        low: float,
        high: float,
        sources: list[ZoneSource],
        by_id: dict[str, Level],
        index_of: dict[date, int],
        dist: float,
    ) -> Zone:
        cfg = self.config
        first_seen = min(s.known_at for s in sources)
        known_at = max(s.known_at for s in sources)
        zone_levels = [by_id[s.level_id] for s in sources if s.level_id is not None]
        role_changes = sorted({r.date for lv in zone_levels for r in lv.role_history[1:]})
        # Tests count in the zone's current role: after its first source was known and
        # after its last break (a test needs the level known before the bar).
        start = max([index_of[first_seen], *(index_of[d] for d in role_changes)]) + 1
        if side == "SUPPORT":
            hit = (cb.low[start:] <= high) & (cb.close[start:] >= low)
        else:
            hit = (cb.high[start:] >= low) & (cb.close[start:] <= high)
        runs = np.flatnonzero(hit & ~np.concatenate(([False], hit[:-1])))
        touch_idx = [start + int(i) for i in runs]
        rvol = numeric(self.indicators, "relative_volume", cb.n)[start:][hit]
        rvol = rvol[~np.isnan(rvol)]
        touch_rvol = float(rvol.max()) if rvol.size else 0.0
        if touch_idx:
            since = cb.n - 1 - touch_idx[-1]
        else:  # untested in this role: recency of its latest source bar or break
            since = (
                cb.n
                - 1
                - max(
                    [
                        *(_bar_index(s, index_of) for s in sources),
                        *(index_of[d] for d in role_changes),
                    ]
                )
            )
        recency = math.exp(-since / cfg.recency_halflife_weeks)
        types: set[str] = {s.source_type for s in sources}
        if any(s.high_volume for s in sources):
            types.add("VOLUME")
        components = {
            "touches": float(len(touch_idx)),
            "source_types": float(len(types)),
            "touch_rvol": touch_rvol,
            "recency": recency,
        }
        strength = (
            cfg.w_touch * components["touches"]
            + cfg.w_sources * components["source_types"]
            + cfg.w_volume * components["touch_rvol"]
            + cfg.w_recency * components["recency"]
        )
        refs = tuple(s.ref_id for s in sources)
        level_ids = tuple(s.level_id for s in sources if s.level_id is not None)
        digest = hashlib.sha256("|".join(sorted(refs)).encode()).hexdigest()[:12]
        return Zone(
            zone_id=f"{cb.segment}:ZONE:{digest}",
            type=side,
            price_low=low,
            price_high=high,
            continuity_segment_id=cb.segment,
            sources=sources,
            source_types=tuple(sorted(types)),
            touches=[cb.dates[i] for i in touch_idx],
            tested_since=cb.dates[min(start, cb.n - 1)],
            role_changes=role_changes,
            role_reversed=any(lv.role != lv.original_role for lv in zone_levels),
            first_seen=first_seen,
            known_at=known_at,
            last_tested=cb.dates[touch_idx[-1]] if touch_idx else None,
            strength=strength,
            strength_components=components,
            distance_atr=dist / a,
            depends_on=refs + level_ids,
        )

    # ------------------------------------------------------------------ trendlines

    def _trendlines(
        self,
        cb: CompleteBars,
        atr: Array,
        primary: list[SwingPoint],
        index_of: dict[date, int],
    ) -> list[Trendline]:
        cfg = self.config
        found: list[tuple[int, Trendline, int | None]] = []  # (validated index, line, break index)
        sides: tuple[tuple[str, Side], ...] = (("LOW", "SUPPORT"), ("HIGH", "RESISTANCE"))
        for kind, side in sides:
            sw = sorted((s for s in primary if s.type == kind), key=lambda s: s.bar_index)
            for i, first in enumerate(sw):
                for j in range(i + 1, len(sw)):
                    second = sw[j]
                    span = second.bar_index - first.bar_index
                    if span > cfg.trendline_max_bars:
                        break
                    if span < cfg.trendline_min_bars:
                        continue
                    rising = second.price > first.price
                    if (side == "SUPPORT") != rising or second.price == first.price:
                        continue
                    line = self._line(cb, atr, sw, i, j, side, index_of)
                    if line is not None:
                        found.append(line)
        # Causal de-duplication: a line validated later that shares two touches (known by
        # then) with a line already accepted and still unbroken is the same line.
        found.sort(key=lambda f: (f[0], f[1].anchor_1_bar_index, f[1].anchor_2_bar_date))
        accepted: list[tuple[int, Trendline, int | None]] = []
        for validated, line, broken in found:
            ids = {t.swing_id for t in line.touches if index_of[t.known_at] <= validated}
            duplicate = any(
                other.type == line.type
                and (other_broken is None or other_broken > validated)
                and len(
                    ids & {t.swing_id for t in other.touches if index_of[t.known_at] <= validated}
                )
                >= 2
                for _, other, other_broken in accepted
            )
            if not duplicate:
                accepted.append((validated, line, broken))
        return [line for _, line, _ in accepted]

    def _line(
        self,
        cb: CompleteBars,
        atr: Array,
        sw: list[SwingPoint],
        i: int,
        j: int,
        side: Side,
        index_of: dict[date, int],
    ) -> tuple[int, Trendline, int | None] | None:
        cfg = self.config
        first, second = sw[i], sw[j]

        def known(s: SwingPoint) -> int:
            assert s.known_at is not None
            return index_of[s.known_at]

        b1 = first.bar_index
        slope = (second.price - first.price) / (second.bar_index - b1)
        made = max(known(first), known(second))  # the line exists from here

        def at(b: int) -> float:
            return first.price + slope * (b - b1)

        def distance(s: SwingPoint) -> float | None:
            a = float(atr[s.bar_index])
            return None if np.isnan(a) or a <= 0 else (s.price - at(s.bar_index)) / a

        tolerance = cfg.trendline_touch_atr
        touches: list[SwingPoint] = []
        for s in sw[i + 1 :]:
            if s is second:
                continue
            d = distance(s)
            if d is None:
                continue
            # A swing between the anchors may touch the line but never lie beyond it.
            beyond = (d < -tolerance) if side == "SUPPORT" else (d > tolerance)
            if s.bar_index < second.bar_index and known(s) <= made and beyond:
                return None
            if abs(d) <= tolerance:
                touches.append(s)
        if not any(known(s) - b1 <= cfg.trendline_max_bars for s in touches):
            return None  # no third touch in time: never validated
        # The first complete close beyond the line, by trendline_break_atr x ATR at that bar.
        line = first.price + slope * (np.arange(b1, cb.n) - b1)
        buffer = np.nan_to_num(atr[b1:] * cfg.trendline_break_atr, nan=0.0)
        close = cb.close[b1:]
        hits = np.flatnonzero(close < line - buffer if side == "SUPPORT" else close > line + buffer)
        broken = b1 + int(hits[0]) if hits.size else None
        if broken is not None and broken <= made:
            return None  # broken before it was a line
        # A touch counts only if known before the bar that breaks the line.
        live = [s for s in touches if broken is None or known(s) < broken]
        in_time = [s for s in live if known(s) - b1 <= cfg.trendline_max_bars]
        if not in_time:
            return None
        validating = min(in_time, key=lambda s: (known(s), s.bar_index))
        validated = max(made, known(validating))
        history = [
            StatusEntry(
                status="ACTIVE", date=cb.dates[validated], provisional=bool(cb.special[validated])
            )
        ]
        if broken is not None:
            history.append(
                StatusEntry(
                    status="BROKEN", date=cb.dates[broken], provisional=bool(cb.special[broken])
                )
            )
        trendline = Trendline(
            trendline_id=f"{cb.segment}:TL:{side}:{first.bar_date}:{second.bar_date}",
            type=side,
            continuity_segment_id=cb.segment,
            anchor_1_swing_id=first.swing_id,
            anchor_1_bar_date=first.bar_date,
            anchor_1_bar_index=b1,
            anchor_1_price=first.price,
            anchor_2_swing_id=second.swing_id,
            anchor_2_bar_date=second.bar_date,
            slope_per_bar=slope,
            touches=[
                TrendlineTouch(
                    swing_id=s.swing_id,
                    bar_date=s.bar_date,
                    known_at=cb.dates[known(s)],
                    price=s.price,
                    line_value=at(s.bar_index),
                    distance_atr=distance(s),
                )
                for s in sorted([first, second, *live], key=lambda s: s.bar_index)
            ],
            known_at=cb.dates[validated],
            depends_on=(first.swing_id, second.swing_id, validating.swing_id),
            status_history=history,
        )
        return validated, trendline, broken

    def _active_trendlines(
        self, cb: CompleteBars, a: float, trendlines: list[Trendline]
    ) -> list[ActiveTrendline]:
        last = cb.n - 1
        close = float(cb.close[last])
        out: list[ActiveTrendline] = []
        for side in ("SUPPORT", "RESISTANCE"):
            live = [t for t in trendlines if t.type == side and t.status == "ACTIVE"]
            live.sort(key=lambda t: (max(x.known_at for x in t.touches), t.known_at), reverse=True)
            for t in live[: self.config.max_trendlines_per_side]:
                v = t.value_at(last)
                out.append(
                    ActiveTrendline(
                        trendline_id=t.trendline_id,
                        type=t.type,
                        value=v,
                        distance_atr=abs(close - v) / a,
                    )
                )
        return out


def _bar_index(source: ZoneSource, index_of: dict[date, int]) -> int:
    """The bar a source belongs to, or the bar it became known for one without a bar."""
    if source.bar_date is not None and source.bar_date in index_of:
        return index_of[source.bar_date]
    return index_of[source.known_at]
