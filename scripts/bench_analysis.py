"""Time each analysis layer per security (ADR-0019: measure before optimizing).

    uv run python scripts/bench_analysis.py [SECURITIES] [SEED]

Synthetic weekly series whose lengths follow the production lake (4,061 securities,
1,780,720 bars: 1–1,083 bars, mean ~440). Layers are added here as they are built.
"""

from __future__ import annotations

import statistics
import sys
import time
from datetime import date

import numpy as np

from chartlens_core.config import IndicatorConfig
from chartlens_core.domain import Timeframe
from chartlens_core.testing import make_bars
from chartlens_engine.indicators import IndicatorAnalyzer
from chartlens_engine.interfaces import AnalysisContext, run_analyzer

securities = int(sys.argv[1]) if len(sys.argv) > 1 else 4061
rng = np.random.default_rng(int(sys.argv[2]) if len(sys.argv) > 2 else 0)
lengths = np.clip(rng.exponential(440, securities).astype(int), 1, 1083)
analyzer = IndicatorAnalyzer(IndicatorConfig())
timings: list[float] = []
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
    t = time.perf_counter()
    run_analyzer(analyzer, bars, ctx)
    timings.append(time.perf_counter() - t)
ms = [x * 1000 for x in timings]
print(f"securities={securities} bars={int(lengths.sum()):,}")
print(
    f"indicators: total {sum(timings):.1f} s | per security mean {statistics.mean(ms):.1f} ms, "
    f"median {statistics.median(ms):.1f} ms, max {max(ms):.1f} ms"
)
