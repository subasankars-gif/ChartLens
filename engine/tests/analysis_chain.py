"""Test helper: run layers A–F in order, as the orchestrator will (ADR-0019), and collect
every object that carries ``known_at`` for the causal-composition checks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd

from chartlens_core.config import AnalysisConfig
from chartlens_core.domain import Timeframe
from chartlens_core.testing import make_bars
from chartlens_engine.evidence import (
    CandleAnalyzer,
    CandleResult,
    DivergenceAnalyzer,
    DivergenceResult,
    VolatilityAnalyzer,
    VolatilityResult,
    VolumeAnalyzer,
    VolumeResult,
)
from chartlens_engine.fibonacci import FibonacciAnalyzer, FibonacciResult
from chartlens_engine.indicators import IndicatorAnalyzer, IndicatorResult
from chartlens_engine.interfaces import AnalysisContext, run_analyzer
from chartlens_engine.levels import LevelsAnalyzer, LevelsResult
from chartlens_engine.structure import StructureAnalyzer, StructureResult
from chartlens_engine.swings import SwingAnalyzer, SwingPoint, SwingResult

SID = "SEC-EV"
SEG = f"{SID}@2006-01-06"


@dataclass(frozen=True)
class Chain:
    indicators: IndicatorResult
    swings: SwingResult
    structure: StructureResult
    fibonacci: FibonacciResult
    levels: LevelsResult
    divergence: DivergenceResult
    volume: VolumeResult
    volatility: VolatilityResult
    candles: CandleResult


def context(bars: pd.DataFrame, sid: str = SID, seg: str = SEG) -> AnalysisContext:
    return AnalysisContext(
        security_id=sid,
        timeframe=Timeframe.WEEKLY,
        as_of=pd.Timestamp(bars["bar_date"].iloc[-1]).date(),
        methodology_hash="test",
        continuity_segment_id=seg,
    )


def random_bars(periods: int, seed: int, start: date = date(2006, 1, 2)) -> pd.DataFrame:
    return make_bars(start, periods, freq="W-FRI", seed=seed).assign(
        security_id=SID, continuity_segment_id=SEG
    )


def run_chain(
    bars: pd.DataFrame,
    cfg: AnalysisConfig | None = None,
    swings: SwingResult | None = None,
) -> Chain:
    cfg = cfg or AnalysisConfig()
    ctx = context(
        bars, str(bars["security_id"].iloc[0]), str(bars["continuity_segment_id"].iloc[0])
    )
    ind = run_analyzer(IndicatorAnalyzer(cfg.indicators), bars, ctx)
    sw = swings or run_analyzer(SwingAnalyzer(cfg.swings, ind), bars, ctx)
    st = run_analyzer(StructureAnalyzer(cfg.structure, ind, sw), bars, ctx)
    fib = run_analyzer(FibonacciAnalyzer(cfg.fibonacci, ind, sw), bars, ctx)
    lv = run_analyzer(LevelsAnalyzer(cfg.levels, ind, sw, st, fib), bars, ctx)
    div = run_analyzer(DivergenceAnalyzer(cfg.divergence, ind, sw, st), bars, ctx)
    vol = run_analyzer(VolumeAnalyzer(cfg.volume, ind, sw, st, lv), bars, ctx)
    vty = run_analyzer(VolatilityAnalyzer(cfg.volatility, ind), bars, ctx)
    cdl = run_analyzer(CandleAnalyzer(cfg.candles, ind, st), bars, ctx)
    return Chain(ind, sw, st, fib, lv, div, vol, vty, cdl)


def known_at_of(chain: Chain) -> dict[str, date]:
    """Every identified object with its ``known_at``."""
    out: dict[str, date] = {}
    for s in chain.swings.swings:
        assert s.known_at is not None
        out[s.swing_id] = s.known_at
    out |= {e.event_id: e.known_at for e in chain.structure.events}
    out |= {f.fib_id: f.known_at for f in chain.fibonacci.structures}
    out |= {t.trendline_id: t.known_at for t in chain.levels.trendlines}
    out |= {lv.level_id: lv.known_at for lv in chain.levels.levels}
    out |= {z.zone_id: z.known_at for z in chain.levels.zones}
    for z in chain.levels.zones:
        out |= {s.ref_id: s.known_at for s in z.sources}
    out |= {d.divergence_id: d.known_at for d in chain.divergence.divergences}
    out |= {e.event_id: e.known_at for e in chain.volume.events}
    out |= {e.event_id: e.known_at for e in chain.volatility.events}
    out |= {e.event_id: e.known_at for e in chain.candles.events}
    return out


def derived(chain: Chain) -> list[tuple[str, date, tuple[str, ...]]]:
    """Every derived object: (id, known_at, the ids it was built from)."""
    rows: list[tuple[str, date, tuple[str, ...]]] = []
    rows += [(f.fib_id, f.known_at, f.depends_on) for f in chain.fibonacci.structures]
    rows += [(t.trendline_id, t.known_at, t.depends_on) for t in chain.levels.trendlines]
    rows += [(lv.level_id, lv.known_at, lv.depends_on) for lv in chain.levels.levels]
    rows += [(z.zone_id, z.known_at, z.depends_on) for z in chain.levels.zones]
    rows += [(d.divergence_id, d.known_at, d.depends_on) for d in chain.divergence.divergences]
    rows += [(e.event_id, e.known_at, e.depends_on) for e in chain.volume.events]
    rows += [(e.event_id, e.known_at, e.depends_on) for e in chain.volatility.events]
    rows += [(e.event_id, e.known_at, e.depends_on) for e in chain.candles.events]
    return rows


def bars_from_closes(closes: list[float], **extra: object) -> pd.DataFrame:
    """Hand-checkable weekly bars: open = close, high = close + 0.5, low = close − 0.5.
    While consecutive closes differ by at most 0.5 the true range is exactly 1, so
    ATR = 1.0 once warm."""
    c = [float(x) for x in closes]
    frame = pd.DataFrame(
        {
            "bar_date": pd.date_range(date(2010, 1, 8), periods=len(c), freq="W-FRI"),
            "open": c,
            "high": [x + 0.5 for x in c],
            "low": [x - 0.5 for x in c],
            "close": c,
            "volume": [1000.0] * len(c),
            "security_id": SID,
            "continuity_segment_id": SEG,
        }
    )
    return frame.assign(**extra) if extra else frame


def week(i: int) -> date:
    return (pd.Timestamp(date(2010, 1, 8)) + pd.Timedelta(weeks=i)).date()


def manual_swings(
    bars: pd.DataFrame,
    swings: list[tuple[str, int, int, float]],
    method: str = "FRACTAL",
    sensitivity: str = "MICRO",
    pending: list[tuple[str, int, float]] | None = None,
) -> SwingResult:
    """Primary swings placed by hand: (type, pivot bar, confirming bar, price)."""
    dates = [pd.Timestamp(d).date() for d in bars["bar_date"]]
    points = [
        SwingPoint(
            swing_id=f"{SEG}:{method}:{sensitivity}:{kind}:{dates[bar]}",
            security_id=SID,
            timeframe="WEEKLY",
            continuity_segment_id=SEG,
            method=method,  # type: ignore[arg-type]
            sensitivity=sensitivity,  # type: ignore[arg-type]
            type=kind,  # type: ignore[arg-type]
            confirmed=True,
            bar_date=dates[bar],
            known_at=dates[known],
            bar_index=bar,
            price=price,
            bars_from_previous=None,
            price_change=None,
            atr_change=None,
            strength=None,
        )
        for kind, bar, known, price in swings
    ]
    waiting = [
        SwingPoint(
            swing_id=f"{SEG}:{method}:{sensitivity}:{kind}:pending",
            security_id=SID,
            timeframe="WEEKLY",
            continuity_segment_id=SEG,
            method=method,  # type: ignore[arg-type]
            sensitivity=sensitivity,  # type: ignore[arg-type]
            type=kind,  # type: ignore[arg-type]
            confirmed=False,
            bar_date=dates[bar],
            known_at=None,
            bar_index=bar,
            price=price,
            bars_from_previous=None,
            price_change=None,
            atr_change=None,
            strength=None,
        )
        for kind, bar, price in (pending or [])
    ]
    return SwingResult(
        analyzer="swings",
        analyzer_version="1",
        context=context(bars),
        primary_method=method,  # type: ignore[arg-type]
        primary_sensitivity=sensitivity,  # type: ignore[arg-type]
        swings=points,
        pending=waiting,
    )


def bars_from_ohlc(
    rows: list[tuple[float, float, float, float]], volume: list[float] | None = None
) -> pd.DataFrame:
    """Weekly bars from explicit (open, high, low, close) rows."""
    o, h, lo, c = (list(x) for x in zip(*rows, strict=True))
    return pd.DataFrame(
        {
            "bar_date": pd.date_range(date(2010, 1, 8), periods=len(rows), freq="W-FRI"),
            "open": o,
            "high": h,
            "low": lo,
            "close": c,
            "volume": volume or [1000.0] * len(rows),
            "security_id": SID,
            "continuity_segment_id": SEG,
        }
    ).astype({"open": "float64", "high": "float64", "low": "float64", "close": "float64"})
