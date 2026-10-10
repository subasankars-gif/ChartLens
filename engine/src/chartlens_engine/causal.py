"""Shared plumbing for the layers after structure (ADR-0019 causal model, ADR-0021).

* :class:`CompleteBars` — the complete bars of one segment as arrays. Layers D–F never
  read the forming week: their current state is as of the last complete bar
  (``state_date``), and every event is dated by a complete bar.
* :class:`StatusEntry` — one dated entry of an append-only ``status_history``.
* **Causal composition** (ADR-0021 Phase 4 rules): every derived object records
  ``depends_on`` — the ids of the objects it was built from — and its ``known_at`` is
  never before the ``known_at`` of any of them. A generic test checks this for every
  layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd
from numpy.typing import NDArray
from pydantic import BaseModel, ConfigDict

from chartlens_core.bars import BAR_DATE, CLOSES_ON_SPECIAL_SESSION, IS_COMPLETE, column
from chartlens_engine.indicators import IndicatorResult
from chartlens_engine.interfaces import AnalysisContext

Array = NDArray[np.float64]


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class StatusEntry(Frozen):
    status: str
    date: date
    """The complete bar that caused this status."""
    provisional: bool = False
    """That bar closed on a non-regular session (ADR-0015)."""


def history_as_of(history: list[StatusEntry], day: date) -> list[StatusEntry]:
    return [e for e in history if e.date <= day]


@dataclass(frozen=True)
class CompleteBars:
    """The complete bars of one segment, oldest first (the forming week is dropped)."""

    dates: list[date]
    open: Array
    high: Array
    low: Array
    close: Array
    volume: Array
    special: NDArray[np.bool_]
    segment: str

    @property
    def n(self) -> int:
        return len(self.dates)

    @property
    def state_date(self) -> date | None:
        """The date current state is computed as of: the last complete bar."""
        return self.dates[-1] if self.dates else None

    def index(self) -> dict[date, int]:
        return {d: i for i, d in enumerate(self.dates)}


def complete_bars(
    bars: pd.DataFrame, context: AnalysisContext, indicators: IndicatorResult
) -> CompleteBars:
    """Extract the complete bars, refusing indicators computed for other bars."""
    dates_all = [d.date() for d in pd.DatetimeIndex(column(bars, BAR_DATE))]
    if indicators.context != context or indicators.bar_dates != dates_all:
        raise ValueError("indicators were computed for other bars or another context")
    complete = (
        column(bars, IS_COMPLETE).to_numpy(dtype=bool)
        if IS_COMPLETE in bars.columns
        else np.ones(len(bars), dtype=bool)
    )
    if complete.size and not complete[:-1].all():
        raise ValueError("only the last bar may be incomplete (the forming week)")
    n = int(complete.sum())

    def col(name: str) -> Array:
        return column(bars, name).to_numpy(dtype=np.float64)[:n]

    special = (
        column(bars, CLOSES_ON_SPECIAL_SESSION).to_numpy(dtype=bool)[:n]
        if CLOSES_ON_SPECIAL_SESSION in bars.columns
        else np.zeros(n, dtype=bool)
    )
    return CompleteBars(
        dates=dates_all[:n],
        open=col("open"),
        high=col("high"),
        low=col("low"),
        close=col("close"),
        volume=col("volume"),
        special=special,
        segment=context.continuity_segment_id or "",
    )


def numeric(indicators: IndicatorResult, name: str, n: int) -> Array:
    """An indicator series over the first ``n`` bars, warm-up as NaN."""
    return np.array(
        [np.nan if v is None else float(v) for v in indicators.get(name).data[:n]],
        dtype=np.float64,
    )


def state(indicators: IndicatorResult, name: str, n: int) -> list[str | None]:
    return [None if v is None else str(v) for v in indicators.get(name).data[:n]]


def value(x: Array, i: int) -> float | None:
    """``x[i]`` or ``None`` in warm-up."""
    v = float(x[i])
    return None if np.isnan(v) else v
