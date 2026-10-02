"""Layer B: swing points (ADR-0019, ADR-0020 §B).

Runs the four methods at the four sensitivities over the **complete** bars of one
segment and returns one normalized :class:`SwingPoint` contract whatever the method.

* ``bar_date`` is the pivot bar; ``known_at`` is the bar whose completion confirmed it.
  An analysis as of a date before ``known_at`` must not see the swing.
* The forming week (``is_complete`` false) is never scanned, so it cannot confirm,
  extend or create anything.
* A swing confirmed by a bar whose close came from a non-regular session is
  ``provisional`` (ADR-0015).
* The developing leg of each ZigZag-type method is reported apart, ``confirmed = false``.
* Which method and sensitivity are *primary* comes from configuration and is recorded in
  the result; later layers ask :meth:`SwingResult.primary`, never a named method.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict

from chartlens_core.bars import BAR_DATE, CLOSES_ON_SPECIAL_SESSION, IS_COMPLETE, column
from chartlens_core.config import SENSITIVITIES, Sensitivity, SwingConfig, SwingMethod
from chartlens_engine.indicators import IndicatorResult
from chartlens_engine.interfaces import AnalysisContext, AnalyzerResult
from chartlens_engine.swings import methods as m

METHODS: tuple[SwingMethod, ...] = ("FRACTAL", "ATR", "PERCENT", "ZIGZAG")


class SwingPoint(BaseModel):
    """One swing point, the same shape for every method."""

    model_config = ConfigDict(frozen=True)

    swing_id: str
    security_id: str
    timeframe: str
    continuity_segment_id: str
    method: SwingMethod
    sensitivity: Sensitivity
    type: Literal["HIGH", "LOW"]
    confirmed: bool
    """False only for a pending extreme (the leg still developing)."""
    bar_date: date
    """The pivot bar: where the chart anchors the swing."""
    known_at: date | None
    """The bar whose completion confirmed it; ``None`` while pending. Never before
    ``bar_date``."""
    bar_index: int
    """Position of the pivot bar in the segment (0 = the segment's first bar)."""
    price: float
    """The pivot bar's high (or low); the pipeline publishes it as that bar's exact text."""
    bars_from_previous: int | None
    """Bars since the previous swing of this method and sensitivity."""
    price_change: float | None
    """Price minus the previous swing's price."""
    atr_change: float | None
    """``price_change`` in ATR(14) at the pivot bar (null in ATR warm-up)."""
    strength: float | None
    """|atr_change|."""
    provisional: bool = False
    """Confirmed by a bar that closed on a non-regular session (ADR-0015)."""


class SwingResult(AnalyzerResult):
    primary_method: SwingMethod
    primary_sensitivity: Sensitivity
    swings: list[SwingPoint]
    """Confirmed swings, by method, sensitivity, then bar."""
    pending: list[SwingPoint]
    """At most one developing extreme per ZigZag-type method and sensitivity."""

    def of(self, method: SwingMethod, sensitivity: Sensitivity) -> list[SwingPoint]:
        return [s for s in self.swings if (s.method, s.sensitivity) == (method, sensitivity)]

    def primary(self) -> list[SwingPoint]:
        """The configured primary swings — what every later layer builds on."""
        return self.of(self.primary_method, self.primary_sensitivity)

    def known_by(self, day: date) -> list[SwingPoint]:
        """Swings an analysis as of ``day`` may use: ``known_at <= day``."""
        return [s for s in self.swings if s.known_at is not None and s.known_at <= day]


class SwingAnalyzer:
    """Every method at every sensitivity, from one segment's complete bars."""

    name = "swings"
    version = "1"

    def __init__(self, config: SwingConfig, indicators: IndicatorResult) -> None:
        self.config = config
        self.indicators = indicators

    def analyze(self, bars: pd.DataFrame, context: AnalysisContext) -> SwingResult:
        dates_all = [d.date() for d in pd.DatetimeIndex(column(bars, BAR_DATE))]
        if self.indicators.context != context or self.indicators.bar_dates != dates_all:
            raise ValueError("indicators were computed for other bars or another context")
        complete = (
            column(bars, IS_COMPLETE).to_numpy(dtype=bool)
            if IS_COMPLETE in bars.columns
            else np.ones(len(bars), dtype=bool)
        )
        if complete.size and not complete[:-1].all():
            raise ValueError("only the last bar may be incomplete (the forming week)")
        n = int(complete.sum())  # the scan never sees the forming week
        high = column(bars, "high").to_numpy(dtype=np.float64)[:n]
        low = column(bars, "low").to_numpy(dtype=np.float64)[:n]
        close = column(bars, "close").to_numpy(dtype=np.float64)[:n]
        special = (
            column(bars, CLOSES_ON_SPECIAL_SESSION).to_numpy(dtype=bool)[:n]
            if CLOSES_ON_SPECIAL_SESSION in bars.columns
            else np.zeros(n, dtype=bool)
        )
        atr = np.array(
            [np.nan if v is None else float(v) for v in self.indicators.get("atr").data[:n]],
            dtype=np.float64,
        )
        dates = dates_all[:n]
        segment = context.continuity_segment_id or ""
        cfg = self.config
        swings: list[SwingPoint] = []
        pending: list[SwingPoint] = []
        for method in METHODS:
            for sensitivity in SENSITIVITIES:
                if method == "FRACTAL":
                    scan = m.fractal(high, low, int(cfg.fractal_window.at(sensitivity)))
                elif method == "ATR":
                    scan = m.zigzag(
                        high, low, m.atr_threshold(atr, cfg.atr_multiple.at(sensitivity))
                    )
                elif method == "PERCENT":
                    scan = m.zigzag(high, low, m.percent_threshold(cfg.percent.at(sensitivity)))
                else:
                    scan = m.zigzag(
                        close, close, m.percent_threshold(cfg.zigzag_percent.at(sensitivity))
                    )
                swings.extend(
                    self._normalize(scan.pivots, method, sensitivity, dates, atr, special, context)
                )
                if scan.pending is not None:
                    p = scan.pending
                    pending.append(
                        SwingPoint(
                            swing_id=f"{segment}:{method}:{sensitivity}:{p.kind}:pending",
                            security_id=context.security_id,
                            timeframe=str(context.timeframe),
                            continuity_segment_id=segment,
                            method=method,
                            sensitivity=sensitivity,
                            type=p.kind,
                            confirmed=False,
                            bar_date=dates[p.bar],
                            known_at=None,
                            bar_index=p.bar,
                            price=p.price,
                            bars_from_previous=None,
                            price_change=None,
                            atr_change=None,
                            strength=None,
                        )
                    )
        return SwingResult(
            analyzer=self.name,
            analyzer_version=self.version,
            context=context,
            primary_method=cfg.primary_method,
            primary_sensitivity=cfg.primary_sensitivity,
            swings=swings,
            pending=pending,
        )

    @staticmethod
    def _normalize(
        pivots: tuple[m.Pivot, ...],
        method: SwingMethod,
        sensitivity: Sensitivity,
        dates: list[date],
        atr: np.ndarray,
        special: np.ndarray,
        context: AnalysisContext,
    ) -> list[SwingPoint]:
        segment = context.continuity_segment_id or ""
        ordered = sorted(pivots, key=lambda p: (p.bar, p.kind))
        out: list[SwingPoint] = []
        previous: m.Pivot | None = None
        for p in ordered:
            if p.known < p.bar:  # pragma: no cover - guarded by construction
                raise AssertionError("a swing cannot be known before its bar")
            change = None if previous is None else p.price - previous.price
            a = float(atr[p.bar])
            atr_change = None if change is None or np.isnan(a) or a == 0 else change / a
            out.append(
                SwingPoint(
                    swing_id=f"{segment}:{method}:{sensitivity}:{p.kind}:{dates[p.bar]}",
                    security_id=context.security_id,
                    timeframe=str(context.timeframe),
                    continuity_segment_id=segment,
                    method=method,
                    sensitivity=sensitivity,
                    type=p.kind,
                    confirmed=True,
                    bar_date=dates[p.bar],
                    known_at=dates[p.known],
                    bar_index=p.bar,
                    price=p.price,
                    bars_from_previous=None if previous is None else p.bar - previous.bar,
                    price_change=change,
                    atr_change=atr_change,
                    strength=None if atr_change is None else abs(atr_change),
                    provisional=bool(special[p.known]),
                )
            )
            previous = p
        return out
