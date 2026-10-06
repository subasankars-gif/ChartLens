"""Time each analysis layer per security (ADR-0019: measure before optimizing).

    uv run python scripts/bench_analysis.py [SECURITIES] [SEED]

Synthetic weekly series whose lengths follow the production lake (4,061 securities,
1,780,720 bars: 1–1,083 bars, mean ~440). Layers are added here as they are built.
"""

from __future__ import annotations

import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import date

import numpy as np

from chartlens_core.config import AnalysisConfig
from chartlens_core.domain import Timeframe
from chartlens_core.testing import make_bars
from chartlens_engine.evidence import (
    CandleAnalyzer,
    DivergenceAnalyzer,
    VolatilityAnalyzer,
    VolumeAnalyzer,
)
from chartlens_engine.fibonacci import FibonacciAnalyzer
from chartlens_engine.indicators import IndicatorAnalyzer
from chartlens_engine.interfaces import AnalysisContext, run_analyzer
from chartlens_engine.levels import LevelsAnalyzer
from chartlens_engine.patterns import PatternAnalyzer
from chartlens_engine.structure import StructureAnalyzer
from chartlens_engine.swings import SwingAnalyzer

securities = int(sys.argv[1]) if len(sys.argv) > 1 else 4061
rng = np.random.default_rng(int(sys.argv[2]) if len(sys.argv) > 2 else 0)
lengths = np.clip(rng.exponential(440, securities).astype(int), 1, 1083)
cfg = AnalysisConfig()
timings: dict[str, list[float]] = defaultdict(list)
counts: dict[str, int] = defaultdict(int)
candidates: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
pattern_types: Counter[str] = Counter()


class Clock:
    """Charge the time since the last call to a layer."""

    def __init__(self) -> None:
        self.t = time.perf_counter()

    def lap(self, layer: str) -> None:
        now = time.perf_counter()
        timings[layer].append(now - self.t)
        self.t = now


for i, n in enumerate(lengths.tolist()):
    sid = f"SEC-{i}"
    bars = make_bars(date(2006, 1, 2), n, freq="W-FRI", seed=i).assign(
        security_id=sid, continuity_segment_id=f"{sid}@2006-01-06"
    )
    ctx = AnalysisContext(
        security_id=sid,
        timeframe=Timeframe.WEEKLY,
        as_of=bars["bar_date"].iloc[-1].date(),
        methodology_hash="bench",
        continuity_segment_id=f"{sid}@2006-01-06",
    )
    clock = Clock()
    ind = run_analyzer(IndicatorAnalyzer(cfg.indicators), bars, ctx)
    clock.lap("indicators")
    sw = run_analyzer(SwingAnalyzer(cfg.swings, ind), bars, ctx)
    clock.lap("swings")
    st = run_analyzer(StructureAnalyzer(cfg.structure, ind, sw), bars, ctx)
    clock.lap("structure")
    fib = run_analyzer(FibonacciAnalyzer(cfg.fibonacci, ind, sw), bars, ctx)
    clock.lap("fibonacci")
    lv = run_analyzer(LevelsAnalyzer(cfg.levels, ind, sw, st, fib), bars, ctx)
    clock.lap("levels")
    div = run_analyzer(DivergenceAnalyzer(cfg.divergence, ind, sw, st), bars, ctx)
    clock.lap("divergence")
    vol = run_analyzer(VolumeAnalyzer(cfg.volume, ind, sw, st, lv), bars, ctx)
    clock.lap("volume")
    vty = run_analyzer(VolatilityAnalyzer(cfg.volatility, ind), bars, ctx)
    clock.lap("volatility")
    cdl = run_analyzer(CandleAnalyzer(cfg.candles, ind, st), bars, ctx)
    clock.lap("candles")
    pat = run_analyzer(
        PatternAnalyzer(
            cfg.patterns,
            ind,
            sw,
            structure=st,
            levels=lv,
            fibonacci=fib,
            divergence=div,
            volatility=vty,
        ),
        bars,
        ctx,
    )
    clock.lap("patterns")
    for family, c in pat.candidates.items():
        candidates[family][0] += c.generated
        candidates[family][1] += c.valid
        candidates[family][2] += c.same_formation
    pattern_types.update(p.pattern_type for p in pat.patterns)
    counts["swings"] += len(sw.swings)
    counts["structure events"] += len(st.events)
    counts["fibonacci structures"] += len(fib.structures)
    counts["zones"] += len(lv.zones)
    counts["trendlines"] += len(lv.trendlines)
    counts["divergences"] += len(div.divergences)
    counts["volume events"] += len(vol.events)
    counts["volatility events"] += len(vty.events)
    counts["candle events"] += len(cdl.events)

print(f"securities={securities} bars={int(lengths.sum()):,}")
total = 0.0
for layer, seconds in timings.items():
    ms = [x * 1000 for x in seconds]
    total += sum(seconds)
    print(
        f"{layer:<11} total {sum(seconds):7.1f} s | per security mean "
        f"{statistics.mean(ms):6.2f} ms, median {statistics.median(ms):6.2f} ms, "
        f"max {max(ms):7.1f} ms"
    )
print(f"{'all':<11} total {total:7.1f} s")
print(", ".join(f"{k}: {v:,}" for k, v in counts.items()))
print("candidates (generated / valid / same formation):")
for family, (g, v, same) in candidates.items():
    print(f"  {family:<15} {g:>9,} {v:>7,} {same:>5,}")
print("patterns:", ", ".join(f"{t} {n:,}" for t, n in pattern_types.most_common()))
