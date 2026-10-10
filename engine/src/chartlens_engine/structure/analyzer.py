"""Layer C: market structure (ADR-0019, ADR-0020 §C).

Consumes **only the primary confirmed swings** (whatever configuration makes primary) —
it never finds a pivot of its own — and the ATR series. One causal pass over the
segment's complete bars builds:

* **labels** — each swing high HH / LH / EQH against the previous swing high, each swing
  low HL / LL / EQL against the previous swing low, known when the later swing is;
* **events** — BOS and CHoCH, each a complete weekly close beyond a level, with the
  level, its originating swing, the triggering bar, the direction, the prior regime and
  state, and its confirmation and invalidation conditions;
* **trend state** — one of six, with its history of changes.

Order inside one bar ``t``: breaks are judged first, against levels known *before* ``t``
(a level must be knowable before the bar that breaks it); swings confirmed by ``t`` are
added afterwards. The forming week is never evaluated: a BOS seen inside a week exists
only once that week's bar is complete, and is ``known_at`` that bar.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict

from chartlens_core.bars import BAR_DATE, CLOSES_ON_SPECIAL_SESSION, IS_COMPLETE, column
from chartlens_core.config import StructureConfig
from chartlens_engine.indicators import IndicatorResult
from chartlens_engine.interfaces import AnalysisContext, AnalyzerResult
from chartlens_engine.swings import SwingPoint, SwingResult

Direction = Literal["UP", "DOWN"]
Label = Literal["HH", "LH", "EQH", "HL", "LL", "EQL"]
TrendStateName = Literal[
    "STRONG_UPTREND",
    "WEAKENING_UPTREND",
    "RANGE",
    "WEAKENING_DOWNTREND",
    "STRONG_DOWNTREND",
    "TRANSITION",
]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class SwingLabel(_Frozen):
    swing_id: str
    type: Literal["HIGH", "LOW"]
    label: Label
    bar_date: date
    known_at: date
    """The labelled swing's ``known_at``: the comparison needs both swings known."""
    price: float
    previous_swing_id: str
    previous_price: float
    difference_atr: float | None
    """(price − previous price) / ATR at the swing's bar."""


class Condition(_Frozen):
    """A condition as data: what is compared, against what, and the numbers involved."""

    rule: str
    level: float | None
    buffer_atr: float = 0.0
    atr: float | None = None
    threshold: float | None = None
    swing_id: str | None = None
    description: str


class StructureEvent(_Frozen):
    event_id: str
    kind: Literal["BOS", "CHoCH"]
    direction: Direction
    bar_date: date
    """The triggering bar (a complete weekly bar)."""
    known_at: date
    """Equal to ``bar_date``: the break is known when that bar completes, never earlier."""
    close: float
    level: float
    level_swing_id: str
    """The swing whose price is the level broken."""
    prior_regime: Direction | None
    prior_state: TrendStateName
    confirmation: Condition
    invalidation: Condition
    continuity_segment_id: str
    provisional: bool
    """The triggering bar closed on a non-regular session (ADR-0015)."""


class TrendState(_Frozen):
    state: TrendStateName
    since: date
    """The bar from which this state holds (the bar that changed it)."""
    regime: Direction | None
    """Direction of the last confirmed regime (set by a BOS)."""
    pending: Direction | None
    """Direction of an unconfirmed CHoCH (TRANSITION)."""
    latest_high_label: Label | None
    latest_low_label: Label | None
    determining_swing_ids: tuple[str, ...]
    last_event_id: str | None
    provisional: bool


class StructureResult(AnalyzerResult):
    swing_method: str
    swing_sensitivity: str
    labels: list[SwingLabel]
    events: list[StructureEvent]
    trend: TrendState | None
    """The state as of the last complete bar (None when there are no complete bars)."""
    trend_history: list[TrendState]
    """Every change of state, in order; the state as of T is the last entry with
    ``since <= T``."""

    def character_changes(self) -> list[StructureEvent]:
        """The CHoCH events, in order: structure owns its vocabulary, so later layers ask
        for them rather than naming the kind (ADR-0021)."""
        return [e for e in self.events if e.kind == "CHoCH"]

    def state_as_of(self, day: date) -> TrendState | None:
        current = None
        for s in self.trend_history:
            if s.since <= day:
                current = s
        return current


class StructureAnalyzer:
    name = "structure"
    version = "1"

    def __init__(
        self, config: StructureConfig, indicators: IndicatorResult, swings: SwingResult
    ) -> None:
        self.config = config
        self.indicators = indicators
        self.swings = swings

    def analyze(self, bars: pd.DataFrame, context: AnalysisContext) -> StructureResult:
        dates_all = [d.date() for d in pd.DatetimeIndex(column(bars, BAR_DATE))]
        if self.indicators.context != context or self.indicators.bar_dates != dates_all:
            raise ValueError("indicators were computed for other bars or another context")
        if self.swings.context != context:
            raise ValueError("swings were computed for another context")
        complete = (
            column(bars, IS_COMPLETE).to_numpy(dtype=bool)
            if IS_COMPLETE in bars.columns
            else np.ones(len(bars), dtype=bool)
        )
        n = int(complete.sum())
        dates = dates_all[:n]
        close = column(bars, "close").to_numpy(dtype=np.float64)[:n]
        special = (
            column(bars, CLOSES_ON_SPECIAL_SESSION).to_numpy(dtype=bool)[:n]
            if CLOSES_ON_SPECIAL_SESSION in bars.columns
            else np.zeros(n, dtype=bool)
        )
        atr = [None if v is None else float(v) for v in self.indicators.get("atr").data[:n]]
        index_of = {d: i for i, d in enumerate(dates)}

        cfg = self.config
        segment = context.continuity_segment_id or ""
        primary = [s for s in self.swings.primary() if s.known_at in index_of]
        by_known: dict[int, list[SwingPoint]] = {}
        for s in sorted(primary, key=lambda s: (s.known_at, s.bar_date, s.type)):
            assert s.known_at is not None
            by_known.setdefault(index_of[s.known_at], []).append(s)

        known: list[SwingPoint] = []  # in the order they became known
        last_high: SwingPoint | None = None
        last_low: SwingPoint | None = None
        broken: set[str] = set()
        labels: list[SwingLabel] = []
        label_of: dict[str, Label] = {}
        events: list[StructureEvent] = []
        regime: Direction | None = None
        pending: Direction | None = None
        last_event: StructureEvent | None = None
        regular_since_event = True
        history: list[TrendState] = []
        state: TrendState | None = None

        def current_state(t: int) -> TrendState:
            high_label = label_of.get(last_high.swing_id) if last_high else None
            low_label = label_of.get(last_low.swing_id) if last_low else None
            recent = known[-cfg.range_swings :]
            a = atr[t]
            ranging = (
                len(recent) == cfg.range_swings
                and a is not None
                and max(s.price for s in recent) - min(s.price for s in recent)
                <= cfg.range_width_atr * a
            )
            name: TrendStateName
            if regime is None or ranging:
                name = "RANGE"
            elif pending is not None:
                name = "TRANSITION"
            elif regime == "UP":
                strong = high_label == "HH" and low_label == "HL"
                name = "STRONG_UPTREND" if strong else "WEAKENING_UPTREND"
            else:
                strong = high_label == "LH" and low_label == "LL"
                name = "STRONG_DOWNTREND" if strong else "WEAKENING_DOWNTREND"
            determining = tuple(s.swing_id for s in (last_high, last_low) if s is not None)
            return TrendState(
                state=name,
                since=dates[t],
                regime=regime,
                pending=pending,
                latest_high_label=high_label,
                latest_low_label=low_label,
                determining_swing_ids=determining,
                last_event_id=last_event.event_id if last_event else None,
                provisional=bool(last_event and last_event.provisional and not regular_since_event),
            )

        for t in range(n):
            prior_state = state.state if state else "RANGE"
            # 1. Breaks, against levels known before this bar.
            a = atr[t]
            if a is not None:
                candidates: tuple[tuple[Direction, SwingPoint | None, SwingPoint | None], ...] = (
                    ("UP", last_high, last_low),
                    ("DOWN", last_low, last_high),
                )
                for direction, level_swing, opposite in candidates:
                    if level_swing is None or level_swing.swing_id in broken:
                        continue
                    buffer = cfg.break_atr * a
                    sign = 1.0 if direction == "UP" else -1.0
                    threshold = level_swing.price + sign * buffer
                    c = float(close[t])
                    if (direction == "UP" and c <= threshold) or (
                        direction == "DOWN" and c >= threshold
                    ):
                        continue
                    prior_regime = regime
                    kind, regime, pending = _transition(regime, pending, direction)
                    broken.add(level_swing.swing_id)
                    event = StructureEvent(
                        event_id=f"{segment}:{kind}:{direction}:{dates[t]}",
                        kind=kind,
                        direction=direction,
                        bar_date=dates[t],
                        known_at=dates[t],
                        close=c,
                        level=level_swing.price,
                        level_swing_id=level_swing.swing_id,
                        prior_regime=prior_regime,
                        prior_state=prior_state,
                        confirmation=Condition(
                            rule="complete_close_beyond_level",
                            level=level_swing.price,
                            buffer_atr=cfg.break_atr,
                            atr=a,
                            threshold=threshold,
                            swing_id=level_swing.swing_id,
                            description=(
                                f"weekly close {'above' if direction == 'UP' else 'below'} "
                                f"{level_swing.price:.4f} by at least {cfg.break_atr} ATR"
                            ),
                        ),
                        invalidation=_invalidation(direction, opposite),
                        continuity_segment_id=segment,
                        provisional=bool(special[t]),
                    )
                    events.append(event)
                    last_event = event
                    regular_since_event = False
            if last_event is not None and last_event.bar_date != dates[t] and not special[t]:
                regular_since_event = True
            # 2. Swings confirmed by this bar become known (and labelled).
            for s in by_known.get(t, []):
                previous = last_high if s.type == "HIGH" else last_low
                if previous is not None:
                    label = _label(s, previous, cfg.equal_tolerance_atr, atr[s.bar_index])
                    a_bar = atr[s.bar_index]
                    labels.append(
                        SwingLabel(
                            swing_id=s.swing_id,
                            type=s.type,
                            label=label,
                            bar_date=s.bar_date,
                            known_at=dates[t],
                            price=s.price,
                            previous_swing_id=previous.swing_id,
                            previous_price=previous.price,
                            difference_atr=None
                            if a_bar is None or a_bar == 0
                            else (s.price - previous.price) / a_bar,
                        )
                    )
                    label_of[s.swing_id] = label
                if s.type == "HIGH":
                    last_high = s
                else:
                    last_low = s
                known.append(s)
            # 3. The state as of this bar.
            new_state = current_state(t)
            if state is None or _changed(state, new_state):
                history.append(new_state)  # append-only: earlier entries never change
                state = new_state

        return StructureResult(
            analyzer=self.name,
            analyzer_version=self.version,
            context=context,
            swing_method=self.swings.primary_method,
            swing_sensitivity=self.swings.primary_sensitivity,
            labels=labels,
            events=events,
            trend=state,
            trend_history=history,
        )


def _transition(
    regime: Direction | None, pending: Direction | None, d: Direction
) -> tuple[Literal["BOS", "CHoCH"], Direction | None, Direction | None]:
    """BOS: the first break, or a break with the regime (cancelling an opposite pending
    CHoCH), or the break that confirms a pending CHoCH. CHoCH: a break against the regime."""
    if regime is None or regime == d:
        return "BOS", d, None
    if pending == d:
        return "BOS", d, None
    return "CHoCH", regime, d


def _label(s: SwingPoint, previous: SwingPoint, tolerance: float, a: float | None) -> Label:
    band = 0.0 if a is None else tolerance * a
    diff = s.price - previous.price
    if s.type == "HIGH":
        return "HH" if diff > band else "LH" if diff < -band else "EQH"
    return "HL" if diff > band else "LL" if diff < -band else "EQL"


def _invalidation(direction: Direction, opposite: SwingPoint | None) -> Condition:
    if opposite is None:
        return Condition(
            rule="none_defined",
            level=None,
            description="no opposite swing yet: the break has no structural invalidation level",
        )
    side = "below" if direction == "UP" else "above"
    return Condition(
        rule="complete_close_beyond_swing",
        level=opposite.price,
        swing_id=opposite.swing_id,
        description=f"weekly close {side} the swing at {opposite.price:.4f} ({opposite.bar_date})",
    )


def _changed(a: TrendState, b: TrendState) -> bool:
    """Anything but ``since`` differs (a provisional state that a regular week then holds
    is a new entry, so the history stays append-only and prefix-stable)."""
    return a.model_dump(exclude={"since"}) != b.model_dump(exclude={"since"})
