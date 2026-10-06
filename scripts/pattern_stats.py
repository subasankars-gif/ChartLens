"""Pattern counts and lifecycle statistics for review (ADR-0022, Phase 5b-A).

    uv run python scripts/pattern_stats.py synthetic [SECURITIES]
    uv run python scripts/pattern_stats.py serving BUCKET [--all]   # the published snapshot

``serving`` reads the live serving snapshot read-only (immutable weekly copies) and
analyses each security's current continuity segment; by default the analytical universe
only. Nothing is written anywhere. Output: one JSON document on stdout.
"""

from __future__ import annotations

import json
import statistics
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Iterator
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from chartlens_core.config import AnalysisConfig
from chartlens_core.domain import Timeframe
from chartlens_core.testing import make_bars
from chartlens_engine.indicators import IndicatorAnalyzer
from chartlens_engine.interfaces import AnalysisContext, run_analyzer
from chartlens_engine.patterns import FAMILIES, PatternAnalyzer
from chartlens_engine.swings import SwingAnalyzer


def synthetic(n: int) -> Iterator[tuple[str, pd.DataFrame]]:
    rng = np.random.default_rng(0)
    for i, length in enumerate(np.clip(rng.exponential(440, n).astype(int), 1, 1083).tolist()):
        sid = f"SEC-{i}"
        yield (
            sid,
            make_bars(date(2006, 1, 2), length, freq="W-FRI", seed=i).assign(
                security_id=sid, continuity_segment_id=f"{sid}@2006-01-06"
            ),
        )


def serving(bucket: str, analytical_only: bool) -> Iterator[tuple[str, pd.DataFrame]]:
    from chartlens_core.weekly import to_bar_frame
    from chartlens_pipeline.serving import ServingSnapshot
    from chartlens_pipeline.storage import GcsObjectStore

    store = GcsObjectStore(bucket)
    snap = ServingSnapshot.load(store, "NSE")
    print(f"snapshot {snap.meta_version} as_of {snap.as_of}", file=sys.stderr)
    for sid, sec in sorted(snap.securities.items()):
        if analytical_only and not sec["analytical_universe"]:
            continue
        bars = snap.weekly_bars(store, sid)
        if not bars:
            continue
        current = bars[-1].continuity_segment_id
        yield sid, to_bar_frame([b for b in bars if b.continuity_segment_id == current], sid)


def main() -> None:
    mode = sys.argv[1]
    source = (
        synthetic(int(sys.argv[2]) if len(sys.argv) > 2 else 4061)
        if mode == "synthetic"
        else serving(sys.argv[2], "--all" not in sys.argv)
    )
    cfg = AnalysisConfig()
    generated: Counter[str] = Counter()
    valid: Counter[str] = Counter()
    same: Counter[str] = Counter()
    final: dict[str, Counter[str]] = defaultdict(Counter)
    reasons: dict[str, Counter[str]] = defaultdict(Counter)
    to_breakout: dict[str, list[int]] = defaultdict(list)
    to_terminal: dict[str, list[int]] = defaultdict(list)
    lag: dict[str, list[int]] = defaultdict(list)
    to_invalidation: dict[str, list[int]] = defaultdict(list)
    securities = bars_total = 0
    seconds = 0.0
    for sid, frame in source:
        if frame.empty:
            continue
        securities += 1
        bars_total += len(frame)
        seg = str(frame["continuity_segment_id"].iloc[0])
        ctx = AnalysisContext(
            security_id=sid,
            timeframe=Timeframe.WEEKLY,
            as_of=pd.Timestamp(frame["bar_date"].iloc[-1]).date(),
            methodology_hash="stats",
            continuity_segment_id=seg,
        )
        ind = run_analyzer(IndicatorAnalyzer(cfg.indicators), frame, ctx)
        sw = run_analyzer(SwingAnalyzer(cfg.swings, ind), frame, ctx)
        t0 = time.perf_counter()
        result = run_analyzer(PatternAnalyzer(cfg.patterns, ind, sw), frame, ctx)
        seconds += time.perf_counter() - t0
        index = {pd.Timestamp(d).date(): i for i, d in enumerate(frame["bar_date"])}
        for family, c in result.candidates.items():
            generated[family] += c.generated
            valid[family] += c.valid
            same[family] += c.same_formation
        for p in result.patterns:
            t = p.pattern_type
            h = p.status_history
            k = index[p.known_at]
            lag[t].append(k - index[p.end_date])
            path = " > ".join(e.status for e in h)
            final[t][path] += 1
            for e in h[1:]:
                reasons[t][f"{e.status}:{e.reason}"] += 1
            if h[-1].status == "INVALIDATED":
                to_invalidation[t].append(index[h[-1].effective_date] - k)
            if p.breakout is not None:
                to_breakout[t].append(index[p.breakout.effective_date] - k)
                if len(h) > 2:
                    to_terminal[t].append(
                        index[h[-1].effective_date] - index[p.breakout.effective_date]
                    )

    def med(xs: list[int]) -> float | None:
        return statistics.median(xs) if xs else None

    out: dict[str, Any] = {
        "mode": mode,
        "securities": securities,
        "bars": bars_total,
        "pattern_ms_per_security": round(1000 * seconds / max(securities, 1), 2),
        "candidates": {
            f: {"generated": generated[f], "valid": valid[f], "same_formation": same[f]}
            for f in FAMILIES
        },
        "types": {
            t: {
                "patterns": sum(final[t].values()),
                "paths": dict(final[t].most_common()),
                "reasons": dict(sorted(reasons[t].items())),
                "median_bars_last_swing_to_known": med(lag[t]),
                "median_bars_known_to_breakout": med(to_breakout[t]),
                "median_bars_breakout_to_terminal": med(to_terminal[t]),
                "invalidated_on_recognition": sum(1 for x in to_invalidation[t] if x == 0),
                "median_bars_known_to_invalidation": med(to_invalidation[t]),
            }
            for t in sorted(final, key=lambda t: -sum(final[t].values()))
        },
    }
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
