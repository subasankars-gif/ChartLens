"""Candlestick evidence (ADR-0021 §F, the K5 subset).

Candles are evidence, never signals: each event records the measurements that made it
and its context at that bar — the prior direction, structure's trend state, the volume
state and relative volume. Complete bars only; ``known_at`` is the candle's last bar.

body = |c − o|; range = h − l; upper shadow = h − max(o, c); lower shadow = min(o, c) − l.
Prior direction at ``t`` = sign of c[t−1] − c[t−1−context_bars] (for a three-bar star,
measured before its first bar). Every bar a candle uses must have range > 0.
"""

from __future__ import annotations

from bisect import bisect_right
from datetime import date
from typing import Literal

import numpy as np
import pandas as pd
from numpy.typing import NDArray

from chartlens_core.config import CandleConfig
from chartlens_engine.causal import Frozen, complete_bars, numeric, state
from chartlens_engine.indicators import IndicatorResult
from chartlens_engine.interfaces import AnalysisContext, AnalyzerResult
from chartlens_engine.structure import StructureResult

CandleType = Literal[
    "DOJI",
    "HAMMER",
    "HANGING_MAN",
    "INVERTED_HAMMER",
    "SHOOTING_STAR",
    "BULLISH_ENGULFING",
    "BEARISH_ENGULFING",
    "MORNING_STAR",
    "EVENING_STAR",
    "INSIDE_BAR",
    "OUTSIDE_BAR",
]
_DIRECTION: dict[str, Literal["BULLISH", "BEARISH", "NEUTRAL"]] = {
    "DOJI": "NEUTRAL",
    "HAMMER": "BULLISH",
    "HANGING_MAN": "BEARISH",
    "INVERTED_HAMMER": "BULLISH",
    "SHOOTING_STAR": "BEARISH",
    "BULLISH_ENGULFING": "BULLISH",
    "BEARISH_ENGULFING": "BEARISH",
    "MORNING_STAR": "BULLISH",
    "EVENING_STAR": "BEARISH",
    "INSIDE_BAR": "NEUTRAL",
    "OUTSIDE_BAR": "NEUTRAL",
}
_BARS = {
    "MORNING_STAR": 3,
    "EVENING_STAR": 3,
    "BULLISH_ENGULFING": 2,
    "BEARISH_ENGULFING": 2,
    "INSIDE_BAR": 2,
    "OUTSIDE_BAR": 2,
}


class CandleContext(Frozen):
    prior_direction: Literal["UP", "DOWN", "FLAT"] | None
    prior_change_atr: float | None
    trend_state: str | None
    """Structure's trend state as of the candle's bar."""
    volume_state: str | None
    relative_volume: float | None


class CandleEvent(Frozen):
    event_id: str
    type: CandleType
    direction: Literal["BULLISH", "BEARISH", "NEUTRAL"]
    continuity_segment_id: str
    start_date: date
    """The first bar of the candle pattern."""
    bar_date: date
    """Its last bar."""
    known_at: date
    measurements: dict[str, float]
    context: CandleContext
    depends_on: tuple[str, ...]
    provisional: bool


class CandleResult(AnalyzerResult):
    events: list[CandleEvent]


def _shift(x: NDArray[np.float64], k: int) -> NDArray[np.float64]:
    """x[t − k] at t (NaN before the start)."""
    out = np.full(len(x), np.nan)
    if k < len(x):
        out[k:] = x[: len(x) - k]
    return out


class CandleAnalyzer:
    name = "candles"
    version = "1"

    def __init__(
        self, config: CandleConfig, indicators: IndicatorResult, structure: StructureResult
    ) -> None:
        self.config = config
        self.indicators = indicators
        self.structure = structure

    def analyze(self, bars: pd.DataFrame, context: AnalysisContext) -> CandleResult:
        if self.structure.context != context:
            raise ValueError("structure was computed for another context")
        cfg = self.config
        cb = complete_bars(bars, context, self.indicators)
        n = cb.n
        o, h, lo, c = cb.open, cb.high, cb.low, cb.close
        body = np.abs(c - o)
        rng = h - lo
        upper = h - np.maximum(o, c)
        lower = np.minimum(o, c) - lo
        ok = rng > 0
        bull, bear = c > o, c < o
        prior = _shift(c, 1) - _shift(c, 1 + cfg.context_bars)  # NaN when not enough bars
        down, up = prior < 0, prior > 0
        ph, pl, po, pc = _shift(h, 1), _shift(lo, 1), _shift(o, 1), _shift(c, 1)
        p_ok = np.concatenate(([False], ok[:-1])) if n else ok

        with np.errstate(invalid="ignore"):
            hammer_shape = (
                ok
                & (body > 0)
                & (lower >= cfg.shadow_body * body)
                & (upper <= cfg.opposite_shadow * rng)
            )
            inverted_shape = (
                ok
                & (body > 0)
                & (upper >= cfg.shadow_body * body)
                & (lower <= cfg.opposite_shadow * rng)
            )
            p_bull, p_bear = _shift(c, 1) > po, _shift(c, 1) < po
            covers_bull = (o <= pc) & (c >= po) & ((o < pc) | (c > po))
            covers_bear = (o >= pc) & (c <= po) & ((o > pc) | (c < po))
            # Three-bar stars at t: bars t-2 (first), t-1 (middle), t (third).
            f_o, f_c, f_body, f_rng = _shift(o, 2), _shift(c, 2), _shift(body, 2), _shift(rng, 2)
            m_body, m_rng = _shift(body, 1), _shift(rng, 1)
            star_prior = _shift(prior, 2)  # the prior direction before the first bar
            long_first = (f_rng > 0) & (f_body >= cfg.star_first_body * f_rng)
            small_middle = (m_rng > 0) & (m_body <= cfg.star_middle_body * f_body)
            mid = (f_o + f_c) / 2
            masks: dict[CandleType, NDArray[np.bool_]] = {
                "DOJI": ok & (body <= cfg.doji_body * rng),
                "HAMMER": hammer_shape & down,
                "HANGING_MAN": hammer_shape & up,
                "INVERTED_HAMMER": inverted_shape & down,
                "SHOOTING_STAR": inverted_shape & up,
                "BULLISH_ENGULFING": ok & p_ok & p_bear & bull & covers_bull,
                "BEARISH_ENGULFING": ok & p_ok & p_bull & bear & covers_bear,
                "MORNING_STAR": ok
                & long_first
                & (f_c < f_o)
                & small_middle
                & bull
                & (c > mid)
                & (star_prior < 0),
                "EVENING_STAR": ok
                & long_first
                & (f_c > f_o)
                & small_middle
                & bear
                & (c < mid)
                & (star_prior > 0),
                "INSIDE_BAR": ok & p_ok & (h <= ph) & (lo >= pl),
                "OUTSIDE_BAR": ok & p_ok & (h >= ph) & (lo <= pl) & ((h > ph) | (lo < pl)),
            }

        atr = numeric(self.indicators, "atr", n)
        rvol = numeric(self.indicators, "relative_volume", n)
        vstate = state(self.indicators, "volume_state", n)
        history = self.structure.trend_history
        since = [s.since for s in history]
        events: list[CandleEvent] = []
        for kind, mask in masks.items():
            span = _BARS.get(kind, 1)
            for t in np.flatnonzero(mask).tolist():
                p = float(prior[t]) if span < 3 else float(star_prior[t])
                a = float(atr[t])
                at = bisect_right(since, cb.dates[t]) - 1
                ctx = CandleContext(
                    prior_direction=None
                    if np.isnan(p)
                    else "UP"
                    if p > 0
                    else "DOWN"
                    if p < 0
                    else "FLAT",
                    prior_change_atr=None if np.isnan(p) or np.isnan(a) or a <= 0 else p / a,
                    trend_state=history[at].state if at >= 0 else None,
                    volume_state=vstate[t],
                    relative_volume=None if np.isnan(rvol[t]) else float(rvol[t]),
                )
                events.append(
                    CandleEvent(
                        event_id=f"{cb.segment}:CANDLE:{kind}:{cb.dates[t]}",
                        type=kind,
                        direction=_DIRECTION[kind],
                        continuity_segment_id=cb.segment,
                        start_date=cb.dates[t - span + 1],
                        bar_date=cb.dates[t],
                        known_at=cb.dates[t],
                        measurements={
                            "body": float(body[t]),
                            "range": float(rng[t]),
                            "upper_shadow": float(upper[t]),
                            "lower_shadow": float(lower[t]),
                        },
                        context=ctx,
                        depends_on=(),
                        provisional=bool(cb.special[t]),
                    )
                )
        events.sort(key=lambda e: (e.known_at, e.event_id))
        return CandleResult(
            analyzer=self.name, analyzer_version=self.version, context=context, events=events
        )
