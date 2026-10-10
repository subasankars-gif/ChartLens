"""Layer E: Fibonacci structures (ADR-0021 §E and its Phase 4 rules).

Built only from **confirmed** swing legs of the primary method — at the primary
sensitivity and at the configured extra sensitivities. A pending (developing) extreme is
never an anchor. A leg is two consecutive confirmed swings of opposite type spanning at
least ``min_leg_atr`` ATR.

Each structure is an event: it exists from ``known_at`` = the later ``known_at`` of its
two swings, and its status (ACTIVE / BROKEN / EXTENDED) has an append-only history. The
first status is judged on the bar that makes it known; later ones on each complete bar
after. BROKEN and EXTENDED are terminal. "Current" Fibonacci is the latest structure per
sensitivity known by the state date.
"""

from __future__ import annotations

from datetime import date
from itertools import pairwise
from typing import Literal

import numpy as np
import pandas as pd

from chartlens_core.config import FibonacciConfig, Sensitivity
from chartlens_engine.causal import (
    CompleteBars,
    Frozen,
    StatusEntry,
    complete_bars,
    history_as_of,
    numeric,
)
from chartlens_engine.indicators import IndicatorResult
from chartlens_engine.interfaces import AnalysisContext, AnalyzerResult
from chartlens_engine.swings import SwingPoint, SwingResult

FibStatus = Literal["ACTIVE", "BROKEN", "EXTENDED"]


class FibLevel(Frozen):
    kind: Literal["RETRACEMENT", "EXTENSION"]
    ratio: float
    price: float
    """Derived from the two swing prices (published rounded to 4 decimals, K7)."""


class FibonacciStructure(Frozen):
    fib_id: str
    continuity_segment_id: str
    method: str
    sensitivity: Sensitivity
    direction: Literal["UP", "DOWN"]
    """The leg's direction: UP from a swing low to a swing high."""
    anchor_swing_id: str
    anchor_bar_date: date
    anchor_price: float
    counter_swing_id: str
    counter_bar_date: date
    counter_price: float
    leg_atr: float
    """|counter − anchor| in ATR at the counter swing's bar."""
    known_at: date
    """The later ``known_at`` of the two swings: never earlier."""
    depends_on: tuple[str, ...]
    levels: list[FibLevel]
    status_history: list[StatusEntry]

    @property
    def status(self) -> str:
        return self.status_history[-1].status

    def level(self, ratio: float) -> float:
        return next(lv.price for lv in self.levels if lv.ratio == ratio)

    def as_of(self, day: date) -> FibonacciStructure | None:
        if self.known_at > day:
            return None
        return self.model_copy(update={"status_history": history_as_of(self.status_history, day)})


class FibonacciResult(AnalyzerResult):
    state_date: date | None
    sensitivities: tuple[Sensitivity, ...]
    structures: list[FibonacciStructure]
    """Every meaningful leg, in the order it became known."""

    def current(self, day: date | None = None) -> list[FibonacciStructure]:
        """The latest structure per sensitivity known by ``day`` (default: the state date),
        with its status as of that day."""
        day = day or self.state_date
        if day is None:
            return []
        out: list[FibonacciStructure] = []
        for s in self.sensitivities:
            known = [f for f in self.structures if f.sensitivity == s and f.known_at <= day]
            if known:
                latest = known[-1].as_of(day)
                assert latest is not None
                out.append(latest)
        return out


class FibonacciAnalyzer:
    name = "fibonacci"
    version = "1"

    def __init__(
        self, config: FibonacciConfig, indicators: IndicatorResult, swings: SwingResult
    ) -> None:
        self.config = config
        self.indicators = indicators
        self.swings = swings

    def sensitivities(self) -> tuple[Sensitivity, ...]:
        out: list[Sensitivity] = [self.swings.primary_sensitivity]
        for s in self.config.extra_sensitivities:
            if s not in out:
                out.append(s)
        return tuple(out)

    def analyze(self, bars: pd.DataFrame, context: AnalysisContext) -> FibonacciResult:
        if self.swings.context != context:
            raise ValueError("swings were computed for another context")
        cb = complete_bars(bars, context, self.indicators)
        atr = numeric(self.indicators, "atr", cb.n)
        index_of = cb.index()
        structures: list[FibonacciStructure] = []
        sensitivities = self.sensitivities()
        for sensitivity in sensitivities:
            swings = sorted(
                (
                    s
                    for s in self.swings.of(self.swings.primary_method, sensitivity)
                    if s.confirmed and s.known_at in index_of
                ),
                key=lambda s: (s.bar_index, s.type),
            )
            for anchor, counter in pairwise(swings):
                if anchor.type == counter.type:
                    continue
                fib = self._structure(anchor, counter, cb, atr, index_of)
                if fib is not None:
                    structures.append(fib)
        structures.sort(key=lambda f: (f.known_at, f.counter_bar_date, f.sensitivity))
        return FibonacciResult(
            analyzer=self.name,
            analyzer_version=self.version,
            context=context,
            state_date=cb.state_date,
            sensitivities=sensitivities,
            structures=structures,
        )

    def _structure(
        self,
        anchor: SwingPoint,
        counter: SwingPoint,
        cb: CompleteBars,
        atr: np.ndarray,
        index_of: dict[date, int],
    ) -> FibonacciStructure | None:
        a = float(atr[counter.bar_index])
        height = counter.price - anchor.price
        if np.isnan(a) or a <= 0 or abs(height) < self.config.min_leg_atr * a:
            return None  # not measurable, or not a meaningful leg
        assert anchor.known_at is not None and counter.known_at is not None
        known_at = max(anchor.known_at, counter.known_at)
        k = index_of[known_at]
        up = height > 0
        levels = [
            FibLevel(kind="RETRACEMENT", ratio=r, price=counter.price - r * height)
            for r in self.config.retracements
        ] + [
            FibLevel(kind="EXTENSION", ratio=e, price=anchor.price + e * height)
            for e in self.config.extensions
        ]
        # Status: judged from the bar that makes it known; BROKEN/EXTENDED are terminal.
        lo, hi = (anchor.price, counter.price) if up else (counter.price, anchor.price)
        close = cb.close[k:]
        beyond = np.flatnonzero((close < lo) | (close > hi))
        history = [StatusEntry(status="ACTIVE", date=cb.dates[k], provisional=bool(cb.special[k]))]
        if beyond.size:
            t = k + int(beyond[0])
            below = cb.close[t] < lo
            status: FibStatus = (
                ("BROKEN" if below else "EXTENDED") if up else ("EXTENDED" if below else "BROKEN")
            )
            entry = StatusEntry(status=status, date=cb.dates[t], provisional=bool(cb.special[t]))
            history = [entry] if t == k else [*history, entry]
        sensitivity = counter.sensitivity
        return FibonacciStructure(
            fib_id=f"{cb.segment}:FIB:{counter.method}:{sensitivity}:{anchor.bar_date}:{counter.bar_date}",
            continuity_segment_id=cb.segment,
            method=counter.method,
            sensitivity=sensitivity,
            direction="UP" if up else "DOWN",
            anchor_swing_id=anchor.swing_id,
            anchor_bar_date=anchor.bar_date,
            anchor_price=anchor.price,
            counter_swing_id=counter.swing_id,
            counter_bar_date=counter.bar_date,
            counter_price=counter.price,
            leg_atr=abs(height) / a,
            known_at=known_at,
            depends_on=(anchor.swing_id, counter.swing_id),
            levels=levels,
            status_history=history,
        )
