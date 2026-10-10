"""Frozen bar evidence shared by event-producing layers (ADR-0022 §18.4–§19).

An event records what the engine knew at the event bar; downstream layers consume the
recorded evidence rather than recomputing it (§18.5). ``BarVolumeEvidence`` is that
record for volume: one implementation, used under distinct field names by each source
(a pattern's ``breakout_bar_volume``, a level role change's ``change_bar_volume``).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Literal

from chartlens_engine.causal import Array, CompleteBars, Frozen, numeric
from chartlens_engine.indicators import IndicatorResult

BAR_VOLUME_VERSION = "1"


class BarVolumeEvidence(Frozen):
    """One bar's volume, frozen when the event is created. Data through that bar only:
    the bar's volume and the ``baseline_bars`` bars before it. It answers "what was
    volume like on the event bar?" and is never a later or current classification."""

    bar_date: date
    volume: float
    baseline_bars: int
    baseline_mean_volume: float | None
    """Mean volume of the ``baseline_bars`` bars before the event bar (the bar itself is
    not in its own baseline); None before enough history."""
    rvol: float | None
    """``volume / baseline_mean_volume`` (the indicator layer's relative volume at the
    bar); None during warm-up or with a zero baseline."""
    classification: Literal["EXPANSION", "NORMAL", "CONTRACTION"] | None
    """The indicator layer's volume state at the bar, by its own thresholds."""
    expansion_threshold: float
    contraction_threshold: float
    evidence_refs: tuple[str, ...]
    measurement_version: str


@dataclass(frozen=True)
class VolumeSource:
    """The indicator layer's relative volume and volume state, read at an event bar only
    (never recomputed or reclassified)."""

    rvol: Array
    state: list[object]
    baseline: int
    expansion: float
    contraction: float

    @classmethod
    def from_indicators(cls, indicators: IndicatorResult, n: int) -> VolumeSource:
        vs = indicators.get("volume_state")
        return cls(
            rvol=numeric(indicators, "relative_volume", n),
            state=list(vs.data[:n]),
            baseline=int(vs.params["baseline"]),
            expansion=float(vs.params["expansion"]),
            contraction=float(vs.params["contraction"]),
        )


def bar_volume_evidence(cb: CompleteBars, source: VolumeSource, t: int) -> BarVolumeEvidence:
    """The volume evidence of complete bar t, from data through bar t only."""
    n = source.baseline
    base = float(cb.volume[t - n : t].mean()) if t >= n else None
    r = float(source.rvol[t])
    state = source.state[t]
    day = cb.dates[t]
    return BarVolumeEvidence(
        bar_date=day,
        volume=float(cb.volume[t]),
        baseline_bars=n,
        baseline_mean_volume=base,
        rvol=None if math.isnan(r) else r,
        classification=None if state is None else str(state),  # type: ignore[arg-type]
        expansion_threshold=source.expansion,
        contraction_threshold=source.contraction,
        evidence_refs=(f"indicators:relative_volume@{day}", f"indicators:volume_state@{day}"),
        measurement_version=BAR_VOLUME_VERSION,
    )
