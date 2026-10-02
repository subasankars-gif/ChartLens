"""Volatility evidence (ADR-0021 §F and its Phase 4 rules).

Contraction is judged against the security's own recent past: ATR% (or Bollinger
bandwidth) at ``t`` at or below the ``compression_percentile`` of the previous
``lookback`` complete bars — the bar itself excluded, so it is causal and there is no
value until the lookback is full. NR7: the true range at ``t`` is strictly the smallest
of the last ``nr_window`` bars.

Each contraction is an **episode**: one event on the first bar of a run of qualifying
bars. Expansion after contraction: a true range ≥ ``expansion_range_atr`` × the ATR of
the bar before, within ``expansion_window`` bars after a contraction bar — again one
event per run.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view
from numpy.typing import NDArray

from chartlens_core.config import VolatilityConfig
from chartlens_engine.causal import Array, Frozen, complete_bars, numeric, value
from chartlens_engine.indicators import IndicatorResult
from chartlens_engine.indicators import functions as f
from chartlens_engine.interfaces import AnalysisContext, AnalyzerResult

ContractionKind = Literal["ATR_COMPRESSION", "BOLLINGER_CONTRACTION", "RANGE_CONTRACTION"]
VolatilityKind = Literal[
    "ATR_COMPRESSION", "BOLLINGER_CONTRACTION", "RANGE_CONTRACTION", "EXPANSION_AFTER_CONTRACTION"
]


class VolatilityEvent(Frozen):
    event_id: str
    kind: VolatilityKind
    continuity_segment_id: str
    bar_date: date
    """The first bar of the episode."""
    known_at: date
    values: dict[str, float]
    depends_on: tuple[str, ...]
    provisional: bool


class VolatilityState(Frozen):
    date: date
    atr: float | None
    atr_percent: float | None
    atr_percent_threshold: float | None
    """The ``compression_percentile`` of ATR% over the previous ``lookback`` bars."""
    bandwidth: float | None
    bandwidth_threshold: float | None
    true_range: float | None
    atr_compressed: bool
    bollinger_contracted: bool
    range_contracted: bool
    expanding: bool
    """The state date is in an expansion-after-contraction run."""


class VolatilityResult(AnalyzerResult):
    state_date: date | None
    state: VolatilityState | None
    events: list[VolatilityEvent]


def _threshold(x: Array, lookback: int, pct: float) -> Array:
    """The ``pct`` percentile of ``x[t-lookback .. t-1]`` at each ``t`` (NaN until full)."""
    out = np.full(len(x), np.nan)
    if len(x) > lookback:
        windows = sliding_window_view(x[:-1], lookback)
        full = ~np.isnan(windows).any(axis=1)
        values = np.full(len(windows), np.nan)
        if full.any():
            values[full] = np.percentile(windows[full], pct, axis=1)
        out[lookback:] = values
    return out


def _starts(mask: NDArray[np.bool_]) -> NDArray[np.bool_]:
    """True on the first bar of each run of ``mask``."""
    return mask & ~np.concatenate(([False], mask[:-1]))


class VolatilityAnalyzer:
    name = "volatility"
    version = "1"

    def __init__(self, config: VolatilityConfig, indicators: IndicatorResult) -> None:
        self.config = config
        self.indicators = indicators

    def analyze(self, bars: pd.DataFrame, context: AnalysisContext) -> VolatilityResult:
        cfg = self.config
        cb = complete_bars(bars, context, self.indicators)
        n = cb.n
        atr = numeric(self.indicators, "atr", n)
        atr_pct = numeric(self.indicators, "atr_percent", n)
        width = numeric(self.indicators, "bollinger_bandwidth", n)
        tr = f.true_range(cb.high, cb.low, cb.close)
        atr_thr = _threshold(atr_pct, cfg.lookback, cfg.compression_percentile)
        width_thr = _threshold(width, cfg.lookback, cfg.compression_percentile)
        with np.errstate(invalid="ignore"):
            compressed = atr_pct <= atr_thr  # NaN compares False
            contracted = width <= width_thr
        narrow = np.zeros(n, dtype=bool)
        w = cfg.nr_window
        if n >= w:
            windows = sliding_window_view(tr, w)
            earlier = windows[:, :-1].min(axis=1)
            with np.errstate(invalid="ignore"):
                narrow[w - 1 :] = (windows[:, -1] < earlier) & ~np.isnan(windows).any(axis=1)
        masks: dict[ContractionKind, NDArray[np.bool_]] = {
            "ATR_COMPRESSION": compressed,
            "BOLLINGER_CONTRACTION": contracted,
            "RANGE_CONTRACTION": narrow,
        }
        events: list[VolatilityEvent] = []
        episode: dict[ContractionKind, list[str | None]] = {}
        for kind, mask in masks.items():
            ids: list[str | None] = [None] * n
            current: str | None = None
            for t in range(n):
                if mask[t] and (t == 0 or not mask[t - 1]):
                    current = f"{cb.segment}:VOLATILITY:{kind}:{cb.dates[t]}"
                    values = {
                        "ATR_COMPRESSION": {
                            "atr_percent": float(atr_pct[t]),
                            "threshold": float(atr_thr[t]),
                        },
                        "BOLLINGER_CONTRACTION": {
                            "bandwidth": float(width[t]),
                            "threshold": float(width_thr[t]),
                        },
                        "RANGE_CONTRACTION": {"true_range": float(tr[t]), "window": float(w)},
                    }[kind]
                    events.append(
                        VolatilityEvent(
                            event_id=current,
                            kind=kind,
                            continuity_segment_id=cb.segment,
                            bar_date=cb.dates[t],
                            known_at=cb.dates[t],
                            values=values,
                            depends_on=(),
                            provisional=bool(cb.special[t]),
                        )
                    )
                ids[t] = current if mask[t] else None
            episode[kind] = ids
        any_contraction = compressed | contracted | narrow
        prev_atr = np.concatenate(([np.nan], atr[:-1]))
        with np.errstate(invalid="ignore"):
            wide = tr >= cfg.expansion_range_atr * prev_atr
        recent = np.zeros(n, dtype=bool)
        for lag in range(1, cfg.expansion_window + 1):
            if lag < n:
                recent[lag:] |= any_contraction[:-lag]
        expanding = wide & recent
        for t in np.flatnonzero(_starts(expanding)).tolist():
            window = range(max(0, t - cfg.expansion_window), t)
            refs = sorted({r for ids in episode.values() for b in window if (r := ids[b])})
            events.append(
                VolatilityEvent(
                    event_id=f"{cb.segment}:VOLATILITY:EXPANSION_AFTER_CONTRACTION:{cb.dates[t]}",
                    kind="EXPANSION_AFTER_CONTRACTION",
                    continuity_segment_id=cb.segment,
                    bar_date=cb.dates[t],
                    known_at=cb.dates[t],
                    values={"true_range_atr": float(tr[t] / prev_atr[t])},
                    depends_on=tuple(refs),
                    provisional=bool(cb.special[t]),
                )
            )
        events.sort(key=lambda e: (e.known_at, e.event_id))
        current_state = None
        if n:
            t = n - 1
            current_state = VolatilityState(
                date=cb.dates[t],
                atr=value(atr, t),
                atr_percent=value(atr_pct, t),
                atr_percent_threshold=value(atr_thr, t),
                bandwidth=value(width, t),
                bandwidth_threshold=value(width_thr, t),
                true_range=value(tr, t),
                atr_compressed=bool(compressed[t]),
                bollinger_contracted=bool(contracted[t]),
                range_contracted=bool(narrow[t]),
                expanding=bool(expanding[t]),
            )
        return VolatilityResult(
            analyzer=self.name,
            analyzer_version=self.version,
            context=context,
            state_date=cb.state_date,
            state=current_state,
            events=events,
        )
