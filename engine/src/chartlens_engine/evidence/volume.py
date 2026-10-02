"""Volume evidence (ADR-0021 §F and its Phase 4 rules).

* **State** as of the last complete bar: volume, its SMA, relative volume, the volume
  state (EXPANSION / CONTRACTION / NORMAL) and trend, OBV and its trend.
* **Events**:
  - breakout-volume confirmation / contradiction, on the bar of every break the earlier
    layers recorded (structure BOS/CHoCH, trendline breaks); patterns add theirs later;
  - volume climax: RVOL ≥ ``climax_rvol`` and a true range ≥ ``climax_range_atr`` × the
    ATR of the bar before;
  - volume divergence: structure labels a swing HH (LL) while the volume SMA at it is
    lower than at the previous swing by more than ``divergence_min``.

Wording stays neutral: no "accumulation", "institutional" or "smart money".
"""

from __future__ import annotations

from datetime import date
from typing import Literal

import numpy as np
import pandas as pd

from chartlens_core.config import VolumeEvidenceConfig
from chartlens_engine.causal import Frozen, complete_bars, numeric, state, value
from chartlens_engine.indicators import IndicatorResult
from chartlens_engine.indicators import functions as f
from chartlens_engine.interfaces import AnalysisContext, AnalyzerResult
from chartlens_engine.levels import LevelsResult
from chartlens_engine.structure import StructureResult
from chartlens_engine.swings import SwingResult

VolumeEventKind = Literal[
    "BREAKOUT_VOLUME_CONFIRMATION",
    "BREAKOUT_VOLUME_CONTRADICTION",
    "VOLUME_CLIMAX",
    "VOLUME_DIVERGENCE",
]


class VolumeEvent(Frozen):
    event_id: str
    kind: VolumeEventKind
    continuity_segment_id: str
    bar_date: date
    known_at: date
    values: dict[str, float]
    description: str
    depends_on: tuple[str, ...]
    provisional: bool


class VolumeState(Frozen):
    date: date
    volume: float
    volume_sma: float | None
    relative_volume: float | None
    volume_state: str | None
    volume_trend: str | None
    obv: float
    obv_trend: Literal["RISING", "FALLING", "FLAT"] | None
    obv_change_ratio: float | None
    """OBV change over ``obv_trend_bars`` / (bars × volume SMA): the net share of the
    volume traded that went with up weeks (+) or down weeks (−)."""


class VolumeResult(AnalyzerResult):
    state_date: date | None
    state: VolumeState | None
    events: list[VolumeEvent]
    """In the order they became known."""


class VolumeAnalyzer:
    name = "volume"
    version = "1"

    def __init__(
        self,
        config: VolumeEvidenceConfig,
        indicators: IndicatorResult,
        swings: SwingResult,
        structure: StructureResult,
        levels: LevelsResult,
    ) -> None:
        self.config = config
        self.indicators = indicators
        self.swings = swings
        self.structure = structure
        self.levels = levels

    def analyze(self, bars: pd.DataFrame, context: AnalysisContext) -> VolumeResult:
        for layer in (self.swings, self.structure, self.levels):
            if layer.context != context:
                raise ValueError(f"{layer.analyzer} was computed for another context")
        cfg = self.config
        cb = complete_bars(bars, context, self.indicators)
        index_of = cb.index()
        rvol = numeric(self.indicators, "relative_volume", cb.n)
        atr = numeric(self.indicators, "atr", cb.n)
        vsma = numeric(self.indicators, "volume_sma", cb.n)
        obv = numeric(self.indicators, "obv", cb.n)
        tr = f.true_range(cb.high, cb.low, cb.close)
        events: list[VolumeEvent] = []

        def add(
            kind: VolumeEventKind,
            t: int,
            values: dict[str, float],
            description: str,
            depends_on: tuple[str, ...],
            suffix: str = "",
        ) -> None:
            events.append(
                VolumeEvent(
                    event_id=f"{cb.segment}:VOLUME:{kind}:{cb.dates[t]}{suffix}",
                    kind=kind,
                    continuity_segment_id=cb.segment,
                    bar_date=cb.dates[t],
                    known_at=cb.dates[t],
                    values=values,
                    description=description,
                    depends_on=depends_on,
                    provisional=bool(cb.special[t]),
                )
            )

        # Breakout volume, on the bars of breaks already recorded upstream.
        breaks: list[tuple[date, str, str]] = [
            (e.known_at, e.event_id, f"{e.kind} {e.direction}") for e in self.structure.events
        ]
        for line in self.levels.trendlines:
            for entry in line.status_history:
                if entry.status == "BROKEN":
                    breaks.append(
                        (entry.date, line.trendline_id, f"{line.type.lower()} trendline break")
                    )
        for day, ref, what in breaks:
            t = index_of.get(day)
            if t is None or np.isnan(rvol[t]):
                continue
            r = float(rvol[t])
            if r >= cfg.breakout_rvol_confirm:
                add(
                    "BREAKOUT_VOLUME_CONFIRMATION",
                    t,
                    {"relative_volume": r, "threshold": cfg.breakout_rvol_confirm},
                    f"{what} on {r:.2f}x the 20-week average volume",
                    (ref,),
                    f":{ref}",
                )
            elif r < cfg.breakout_rvol_contradict:
                add(
                    "BREAKOUT_VOLUME_CONTRADICTION",
                    t,
                    {"relative_volume": r, "threshold": cfg.breakout_rvol_contradict},
                    f"{what} on only {r:.2f}x the 20-week average volume",
                    (ref,),
                    f":{ref}",
                )

        # Climax: very high volume on a very wide bar (against the ATR before the bar).
        prev_atr = np.concatenate(([np.nan], atr[:-1]))
        with np.errstate(invalid="ignore"):
            climax = (rvol >= cfg.climax_rvol) & (tr >= cfg.climax_range_atr * prev_atr)
        for t in np.flatnonzero(climax).tolist():
            add(
                "VOLUME_CLIMAX",
                t,
                {
                    "relative_volume": float(rvol[t]),
                    "true_range_atr": float(tr[t] / prev_atr[t]),
                },
                f"volume {float(rvol[t]):.2f}x average on a range of "
                f"{float(tr[t] / prev_atr[t]):.2f} ATR",
                (),
            )

        # Volume divergence on structure's HH / LL labels.
        by_id = {s.swing_id: s for s in self.swings.primary()}
        for label in self.structure.labels:
            if label.label not in ("HH", "LL"):
                continue
            first, second = by_id.get(label.previous_swing_id), by_id.get(label.swing_id)
            t = index_of.get(label.known_at)
            if first is None or second is None or t is None:
                continue
            v1, v2 = float(vsma[first.bar_index]), float(vsma[second.bar_index])
            if np.isnan(v1) or np.isnan(v2) or v1 <= 0:
                continue
            drop = (v1 - v2) / v1
            if drop > cfg.divergence_min:
                add(
                    "VOLUME_DIVERGENCE",
                    t,
                    {"volume_sma_1": v1, "volume_sma_2": v2, "drop": drop},
                    f"{label.label} in price on {drop:.0%} lower average volume",
                    (first.swing_id, second.swing_id),
                    f":{second.bar_date}",
                )

        events.sort(key=lambda e: (e.known_at, e.event_id))
        return VolumeResult(
            analyzer=self.name,
            analyzer_version=self.version,
            context=context,
            state_date=cb.state_date,
            state=self._state(cb.n, cb.dates, cb.volume, rvol, vsma, obv),
            events=events,
        )

    def _state(
        self,
        n: int,
        dates: list[date],
        volume: np.ndarray,
        rvol: np.ndarray,
        vsma: np.ndarray,
        obv: np.ndarray,
    ) -> VolumeState | None:
        if n == 0:
            return None
        cfg = self.config
        t = n - 1
        k = cfg.obv_trend_bars
        ratio: float | None = None
        trend: Literal["RISING", "FALLING", "FLAT"] | None = None
        sma_now = value(vsma, t)
        if t >= k and sma_now is not None and sma_now > 0:
            ratio = float(obv[t] - obv[t - k]) / (k * sma_now)
            band = cfg.obv_trend_band
            trend = "RISING" if ratio > band else "FALLING" if ratio < -band else "FLAT"
        return VolumeState(
            date=dates[t],
            volume=float(volume[t]),
            volume_sma=sma_now,
            relative_volume=value(rvol, t),
            volume_state=state(self.indicators, "volume_state", n)[t],
            volume_trend=state(self.indicators, "volume_trend", n)[t],
            obv=float(obv[t]),
            obv_trend=trend,
            obv_change_ratio=ratio,
        )
