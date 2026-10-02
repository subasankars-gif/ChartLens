"""Divergence evidence (ADR-0021 §F and its Phase 4 rules).

Price is never compared here: the price side of a divergence is the **structure label**
of the second swing (LL, HL, HH, LH against the previous swing of its type), so the
equality band and the swing pairing are structure's, not re-derived. The indicator is
read at the two swing bars. A divergence is evidence, never a signal.

``known_at`` is the label's (the second swing's ``known_at``). Status: FORMING, then
CONFIRMED (a complete close beyond the intervening opposite swing), INVALIDATED (a
complete close beyond the second swing) or EXPIRED (neither within ``max_wait_bars``) —
all terminal, the first judged on the bar that makes the divergence known.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

import numpy as np
import pandas as pd

from chartlens_core.config import DivergenceConfig
from chartlens_engine.causal import (
    Array,
    Frozen,
    StatusEntry,
    complete_bars,
    history_as_of,
    numeric,
)
from chartlens_engine.indicators import IndicatorResult
from chartlens_engine.interfaces import AnalysisContext, AnalyzerResult
from chartlens_engine.structure import Condition, StructureResult
from chartlens_engine.swings import SwingPoint, SwingResult

DivergenceType = Literal["REGULAR_BULLISH", "HIDDEN_BULLISH", "REGULAR_BEARISH", "HIDDEN_BEARISH"]

# label of the second swing -> (divergence type, required sign of the indicator change)
_RULES: dict[str, tuple[DivergenceType, int]] = {
    "LL": ("REGULAR_BULLISH", +1),  # lower low in price, higher low in the indicator
    "HL": ("HIDDEN_BULLISH", -1),  # higher low in price, lower low in the indicator
    "HH": ("REGULAR_BEARISH", -1),  # higher high in price, lower high in the indicator
    "LH": ("HIDDEN_BEARISH", +1),  # lower high in price, higher high in the indicator
}
IndicatorName = Literal["rsi", "macd", "obv"]


class Divergence(Frozen):
    divergence_id: str
    continuity_segment_id: str
    indicator: IndicatorName
    type: DivergenceType
    price_swing_1: str
    price_swing_2: str
    price_1: float
    price_2: float
    price_label: str
    """Structure's label of the second swing."""
    indicator_1: float
    indicator_2: float
    """The indicator at the two swing bars."""
    date_start: date
    date_end: date
    known_at: date
    strength: float
    """|indicator change| in multiples of its minimum delta: how clearly it disagrees."""
    strength_components: dict[str, float]
    confirmation: Condition
    invalidation: Condition
    depends_on: tuple[str, ...]
    status_history: list[StatusEntry]

    @property
    def status(self) -> str:
        return self.status_history[-1].status

    def as_of(self, day: date) -> Divergence | None:
        if self.known_at > day:
            return None
        return self.model_copy(update={"status_history": history_as_of(self.status_history, day)})


class DivergenceResult(AnalyzerResult):
    divergences: list[Divergence]
    """In the order they became known."""


class DivergenceAnalyzer:
    name = "divergence"
    version = "1"

    def __init__(
        self,
        config: DivergenceConfig,
        indicators: IndicatorResult,
        swings: SwingResult,
        structure: StructureResult,
    ) -> None:
        self.config = config
        self.indicators = indicators
        self.swings = swings
        self.structure = structure

    def analyze(self, bars: pd.DataFrame, context: AnalysisContext) -> DivergenceResult:
        for layer in (self.swings, self.structure):
            if layer.context != context:
                raise ValueError(f"{layer.analyzer} was computed for another context")
        cfg = self.config
        cb = complete_bars(bars, context, self.indicators)
        index_of = cb.index()
        atr = numeric(self.indicators, "atr", cb.n)
        volume_sma = numeric(self.indicators, "volume_sma", cb.n)
        series: dict[IndicatorName, Array] = {
            name: numeric(self.indicators, name, cb.n) for name in cfg.indicators
        }
        primary = [s for s in self.swings.primary() if s.known_at in index_of]
        by_id = {s.swing_id: s for s in primary}
        out: list[Divergence] = []
        for label in self.structure.labels:
            rule = _RULES.get(label.label)
            second = by_id.get(label.swing_id)
            first = by_id.get(label.previous_swing_id)
            if rule is None or first is None or second is None:
                continue
            bars_apart = second.bar_index - first.bar_index
            if not cfg.min_bars <= bars_apart <= cfg.max_bars:
                continue
            kind, sign = rule
            k = index_of[label.known_at]
            b = second.bar_index
            for name, values in series.items():
                i1, i2 = float(values[first.bar_index]), float(values[b])
                if name == "rsi":
                    delta = cfg.rsi_min_delta
                elif name == "macd":
                    delta = cfg.macd_min_delta_atr * float(atr[b])
                else:
                    delta = cfg.obv_min_delta_volume * float(volume_sma[b])
                if np.isnan(i1) or np.isnan(i2) or np.isnan(delta) or delta <= 0:
                    continue  # warm-up: not measurable
                change = i2 - i1
                if sign * change <= delta:
                    continue
                confirmation = _confirmation(first, second, primary, label.known_at)
                invalidation = Condition(
                    rule="complete_close_beyond_swing",
                    level=second.price,
                    swing_id=second.swing_id,
                    description=(
                        f"weekly close {'below' if second.type == 'LOW' else 'above'} "
                        f"the second swing at {second.price:.4f}"
                    ),
                )
                out.append(
                    Divergence(
                        divergence_id=f"{cb.segment}:DIV:{name}:{kind}:{first.bar_date}:{second.bar_date}",
                        continuity_segment_id=cb.segment,
                        indicator=name,
                        type=kind,
                        price_swing_1=first.swing_id,
                        price_swing_2=second.swing_id,
                        price_1=first.price,
                        price_2=second.price,
                        price_label=label.label,
                        indicator_1=i1,
                        indicator_2=i2,
                        date_start=first.bar_date,
                        date_end=second.bar_date,
                        known_at=label.known_at,
                        strength=abs(change) / delta,
                        strength_components={
                            "price_change_atr": label.difference_atr or 0.0,
                            "indicator_change": change,
                            "indicator_min_delta": delta,
                            "bars": float(bars_apart),
                        },
                        confirmation=confirmation,
                        invalidation=invalidation,
                        depends_on=tuple(
                            x for x in (first.swing_id, second.swing_id, confirmation.swing_id) if x
                        ),
                        status_history=self._status(
                            cb.close, cb.dates, cb.special, k, second, confirmation
                        ),
                    )
                )
        return DivergenceResult(
            analyzer=self.name, analyzer_version=self.version, context=context, divergences=out
        )

    def _status(
        self,
        close: np.ndarray,
        dates: list[date],
        special: np.ndarray,
        k: int,
        second: SwingPoint,
        confirmation: Condition,
    ) -> list[StatusEntry]:
        bullish = second.type == "LOW"
        tail = close[k:]
        invalid = tail < second.price if bullish else tail > second.price
        if confirmation.level is None:
            confirm = np.zeros(len(tail), dtype=bool)
        else:
            confirm = tail > confirmation.level if bullish else tail < confirmation.level
        either = np.flatnonzero(invalid | confirm)
        first_hit = int(either[0]) if either.size else None
        wait = self.config.max_wait_bars
        history = [StatusEntry(status="FORMING", date=dates[k], provisional=bool(special[k]))]
        if first_hit is not None and first_hit <= wait:
            t = k + first_hit
            status = "CONFIRMED" if confirm[first_hit] else "INVALIDATED"
            entry = StatusEntry(status=status, date=dates[t], provisional=bool(special[t]))
            return [entry] if t == k else [*history, entry]
        if k + wait < len(close):
            history.append(
                StatusEntry(
                    status="EXPIRED", date=dates[k + wait], provisional=bool(special[k + wait])
                )
            )
        return history


def _confirmation(
    first: SwingPoint, second: SwingPoint, primary: list[SwingPoint], known_at: date
) -> Condition:
    """The most extreme opposite swing between the two, known by the divergence's
    ``known_at``."""
    opposite = "HIGH" if second.type == "LOW" else "LOW"
    between = [
        s
        for s in primary
        if s.type == opposite
        and first.bar_index < s.bar_index < second.bar_index
        and s.known_at is not None
        and s.known_at <= known_at
    ]
    if not between:
        return Condition(
            rule="none_defined",
            level=None,
            description="no opposite swing between the two: confirmation is not defined",
        )
    pick = (
        max(between, key=lambda s: s.price)
        if opposite == "HIGH"
        else min(between, key=lambda s: s.price)
    )
    side = "above" if opposite == "HIGH" else "below"
    return Condition(
        rule="complete_close_beyond_swing",
        level=pick.price,
        swing_id=pick.swing_id,
        description=(
            f"weekly close {side} the intervening swing at {pick.price:.4f} ({pick.bar_date})"
        ),
    )
