"""Regression fingerprint of the pattern engine's output (ADR-0022 §18.4 checks).

    uv run python scripts/pattern_fingerprint.py synthetic [SECURITIES]
    uv run python scripts/pattern_fingerprint.py serving BUCKET

Hashes, per security, every pattern (id, geometry, touches, context, definition fit and
every lifecycle event) and every relevance history (dates, inclusion, reason, container,
kept ids, tag names). Excluded, because an evidence-only amendment adds or stamps them:
an event's ``breakout_bar_volume`` and ``methodology_version``, and tag evidence refs.
Run it on two commits; equal hashes mean nothing else changed. Read-only; JSON on stdout.
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
from typing import Any

import pandas as pd

from chartlens_core.config import AnalysisConfig
from chartlens_core.domain import Timeframe
from chartlens_engine.evidence import DivergenceAnalyzer, VolatilityAnalyzer
from chartlens_engine.fibonacci import FibonacciAnalyzer
from chartlens_engine.indicators import IndicatorAnalyzer
from chartlens_engine.interfaces import AnalysisContext, run_analyzer
from chartlens_engine.levels import LevelsAnalyzer
from chartlens_engine.patterns import PatternAnalyzer, RelevanceAnalyzer
from chartlens_engine.structure import StructureAnalyzer
from chartlens_engine.swings import SwingAnalyzer

sys.path.insert(0, "scripts")
from pattern_stats import serving, synthetic

EVENT_EXCLUDE = {"breakout_bar_volume", "methodology_version"}
EVIDENCE_KEYS = {"breakout_bar_volume", "change_bar", "change_bar_volume"}
"""Evidence-only fields added by amendments (5b-A, Phase 4): excluded wherever they sit."""

try:  # the breakout-event layer exists only on newer commits
    from chartlens_engine.breakouts import BreakoutEventAnalyzer
except ImportError:  # pragma: no cover
    BreakoutEventAnalyzer = None  # type: ignore[assignment,misc]

try:  # the orchestrator (ADR-0024 phase 6a) exists only on newer commits
    from chartlens_engine.analysis import AnalysisInputs, analyze_security, serialize

    INPUTS = AnalysisInputs(
        exchange="NSE",
        weekly_schema_version="fingerprint",
        weekly_builder_version="fingerprint",
        usable_from=None,
    )
except ImportError:  # pragma: no cover
    analyze_security = None  # type: ignore[assignment]


def strip(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: strip(v) for k, v in obj.items() if k not in EVIDENCE_KEYS}
    if isinstance(obj, list):
        return [strip(v) for v in obj]
    return obj


def canonical_levels(levels: Any) -> str:
    d = levels.model_dump(
        mode="json", include={"levels", "zones", "trendlines", "active_trendlines"}
    )
    return json.dumps(strip(d), sort_keys=True)


def canonical(patterns: list[Any], relevance: list[Any]) -> str:
    out: list[Any] = []
    for p in patterns:
        d = p.model_dump(mode="json", exclude={"status_history"})
        d["events"] = [e.model_dump(mode="json", exclude=EVENT_EXCLUDE) for e in p.status_history]
        out.append(d)
    rel = [
        {
            "id": r.pattern_id,
            "history": [
                [
                    str(e.effective_date),
                    e.included,
                    e.reason,
                    e.container_pattern_id,
                    list(e.kept_pattern_ids),
                    sorted(t.tag for t in e.tags),
                ]
                for e in r.history
            ],
        }
        for r in relevance
    ]
    return json.dumps({"patterns": out, "relevance": rel}, sort_keys=True)


def main() -> None:
    mode = sys.argv[1]
    source = (
        synthetic(int(sys.argv[2]) if len(sys.argv) > 2 else 300)
        if mode == "synthetic"
        else serving(sys.argv[2], True)
    )
    cfg = AnalysisConfig()
    per: dict[str, str] = {}
    patterns = breakouts = 0
    classes: dict[str, int] = {}
    level_hash: dict[str, str] = {}
    docs: dict[str, Any] = {
        "hashes": {},
        "bytes": [],
        "event_rows": 0,
        "analyze_s": 0.0,
        "serialize_s": 0.0,
        "recomputed": 0,
        "mismatches": 0,
    }
    scale: dict[str, Any] = {
        "level_events": [],
        "pattern_events": [],
        "level_bytes": 0,
        "pattern_bytes": 0,
        "seconds": 0.0,
        "level_follow": {},
        "pattern_follow": {},
        "level_status": {},
        "pattern_status": {},
    }
    for sid, frame in source:
        if frame.empty:
            continue
        seg = str(frame["continuity_segment_id"].iloc[0])
        ctx = AnalysisContext(
            security_id=sid,
            timeframe=Timeframe.WEEKLY,
            as_of=pd.Timestamp(frame["bar_date"].iloc[-1]).date(),
            methodology_hash="fingerprint",
            continuity_segment_id=seg,
        )
        bo = None
        if analyze_security is not None:
            t0 = time.perf_counter()
            a = analyze_security(frame, ctx, cfg, INPUTS)
            t1 = time.perf_counter()
            stored = serialize(a)
            t2 = time.perf_counter()
            lv, pat, rel, bo = a.levels, a.patterns, a.relevance, a.breakout_events
            docs["hashes"][sid] = stored.document_sha256
            docs["bytes"].append(len(stored.document))
            docs["event_rows"] += sum(d.row_count for d in stored.events.values())
            docs["analyze_s"] += t1 - t0
            docs["serialize_s"] += t2 - t1
            if len(docs["bytes"]) % 50 == 1:  # determinism on real data, every 50th security
                again = serialize(analyze_security(frame, ctx, cfg, INPUTS)).document_sha256
                docs["recomputed"] += 1
                docs["mismatches"] += again != stored.document_sha256
        else:
            ind = run_analyzer(IndicatorAnalyzer(cfg.indicators), frame, ctx)
            sw = run_analyzer(SwingAnalyzer(cfg.swings, ind), frame, ctx)
            st = run_analyzer(StructureAnalyzer(cfg.structure, ind, sw), frame, ctx)
            fib = run_analyzer(FibonacciAnalyzer(cfg.fibonacci, ind, sw), frame, ctx)
            lv = run_analyzer(LevelsAnalyzer(cfg.levels, ind, sw, st, fib), frame, ctx)
            div = run_analyzer(DivergenceAnalyzer(cfg.divergence, ind, sw, st), frame, ctx)
            vty = run_analyzer(VolatilityAnalyzer(cfg.volatility, ind), frame, ctx)
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
                frame,
                ctx,
            )
            rel = run_analyzer(RelevanceAnalyzer(cfg.patterns, ind, pat, st), frame, ctx)
        patterns += len(pat.patterns)
        breakouts += sum(
            e.status in ("CONFIRMED", "RECOGNISED_AFTER_BREAKOUT")
            for p in pat.patterns
            for e in p.status_history
        )
        for p in pat.patterns:
            for e in p.status_history:
                v = getattr(e, "breakout_bar_volume", None)
                if v is not None:
                    key = str(v.classification)
                    classes[key] = classes.get(key, 0) + 1
        level_hash[sid] = hashlib.sha256(canonical_levels(lv).encode()).hexdigest()
        if bo is None and BreakoutEventAnalyzer is not None:
            t0 = time.perf_counter()
            bo = run_analyzer(BreakoutEventAnalyzer(cfg, ind, lv, pat), frame, ctx)  # pyright: ignore[reportPossiblyUnbound]
            scale["seconds"] += time.perf_counter() - t0
        if bo is not None:
            scale["level_events"].append(len(bo.level_events))
            scale["pattern_events"].append(len(bo.pattern_events))
            for key, events in (("level", bo.level_events), ("pattern", bo.pattern_events)):
                for ev in events:
                    scale[f"{key}_bytes"] += len(ev.model_dump_json())
                    seq = " > ".join(f.kind for f in ev.history) or "(open)"
                    scale[f"{key}_follow"][seq] = scale[f"{key}_follow"].get(seq, 0) + 1
                    scale[f"{key}_status"][ev.status] = scale[f"{key}_status"].get(ev.status, 0) + 1
        per[sid] = hashlib.sha256(canonical(pat.patterns, rel.relevance).encode()).hexdigest()
    overall = hashlib.sha256(json.dumps(sorted(per.items())).encode()).hexdigest()
    levels_overall = hashlib.sha256(json.dumps(sorted(level_hash.items())).encode()).hexdigest()
    if scale["level_events"]:
        import statistics

        for key in ("level_events", "pattern_events"):
            xs = sorted(scale[key])
            scale[key] = {
                "total": sum(xs),
                "mean_per_security": round(statistics.mean(xs), 1),
                "median": xs[len(xs) // 2],
                "p90": xs[int(len(xs) * 0.9)],
                "max": xs[-1],
            }
        seconds = scale.pop("seconds")
        if seconds:  # timed here only when the layers are composed by hand
            scale["ms_per_security"] = round(1000 * seconds / max(len(per), 1), 2)
    else:
        scale = {}
    analysis: dict[str, Any] = {}
    if docs["bytes"]:
        xs = sorted(docs["bytes"])
        n_docs = len(xs)
        analysis = {
            "documents_overall": hashlib.sha256(
                json.dumps(sorted(docs["hashes"].items())).encode()
            ).hexdigest(),
            "document_mb": {
                "total": round(sum(xs) / 1e6, 1),
                "mean": round(sum(xs) / n_docs / 1e6, 3),
                "median": round(xs[n_docs // 2] / 1e6, 3),
                "p90": round(xs[int(n_docs * 0.9)] / 1e6, 3),
                "max": round(xs[-1] / 1e6, 3),
            },
            "event_rows": docs["event_rows"],
            "ms_analyze_per_security": round(1000 * docs["analyze_s"] / n_docs, 1),
            "ms_serialize_per_security": round(1000 * docs["serialize_s"] / n_docs, 1),
            "recomputed": docs["recomputed"],
            "recompute_mismatches": docs["mismatches"],
        }
    print(
        json.dumps(
            {
                "overall": overall,
                "securities": len(per),
                "patterns": patterns,
                "breakouts": breakouts,
                "breakout_bar_volume_classes": classes,
                "levels_overall": levels_overall,
                "per_security_levels": level_hash,
                "breakout_scale": scale,
                "analysis": analysis,
                "per_security": per,
            }
        )
    )


if __name__ == "__main__":
    main()
