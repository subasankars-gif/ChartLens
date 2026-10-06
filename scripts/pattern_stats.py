"""Pattern counts, lifecycle statistics and definition-fit diagnostics for review
(ADR-0022, Phases 5b-A to 5b-C).

The definition-fit section is descriptive only: distributions, component statuses and a
sensitivity check of the ranking to the shape share. It never relates a fit to an
outcome, and the sensitivity check is never used to choose the constant.

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
from chartlens_engine.evidence import DivergenceAnalyzer, VolatilityAnalyzer
from chartlens_engine.fibonacci import FibonacciAnalyzer
from chartlens_engine.indicators import IndicatorAnalyzer
from chartlens_engine.interfaces import AnalysisContext, run_analyzer
from chartlens_engine.levels import LevelsAnalyzer
from chartlens_engine.patterns import (
    FAMILIES,
    PatternAnalyzer,
    RelevanceAnalyzer,
    definition_fit,
)
from chartlens_engine.structure import StructureAnalyzer
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
    context: dict[str, Counter[str]] = defaultdict(Counter)
    trend: dict[str, Counter[str]] = defaultdict(Counter)
    decline: dict[str, list[float]] = defaultdict(list)
    rise: dict[str, list[float]] = defaultdict(list)
    fit_rows: list[dict[str, Any]] = []
    comp: dict[str, Counter[str]] = defaultdict(Counter)
    comp_scores: dict[str, list[float]] = defaultdict(list)
    rel_current: dict[str, Counter[str]] = defaultdict(Counter)
    rel_included_per_security: list[int] = []
    rel_entries: list[int] = []
    rel_transitions: Counter[str] = Counter()
    rel_reasons_ever: Counter[str] = Counter()
    rel_tags: Counter[str] = Counter()
    rel_seconds = 0.0
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
        st = run_analyzer(StructureAnalyzer(cfg.structure, ind, sw), frame, ctx)
        fib = run_analyzer(FibonacciAnalyzer(cfg.fibonacci, ind, sw), frame, ctx)
        lv = run_analyzer(LevelsAnalyzer(cfg.levels, ind, sw, st, fib), frame, ctx)
        div = run_analyzer(DivergenceAnalyzer(cfg.divergence, ind, sw, st), frame, ctx)
        vty = run_analyzer(VolatilityAnalyzer(cfg.volatility, ind), frame, ctx)
        t0 = time.perf_counter()
        result = run_analyzer(
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
        seconds += time.perf_counter() - t0
        t1 = time.perf_counter()
        rel = run_analyzer(RelevanceAnalyzer(cfg.patterns, ind, result, st), frame, ctx)
        rel_seconds += time.perf_counter() - t1
        family_of = {p.pattern_id: p.family for p in result.patterns}
        included_now = 0
        for r in rel.relevance:
            cur = r.current
            rel_current[family_of[r.pattern_id]][cur.reason] += 1
            included_now += cur.included
            rel_entries.append(len(r.history))
            prev = "START"
            for e in r.history:
                rel_reasons_ever[e.reason] += 1
                if e.reason != prev:
                    rel_transitions[f"{prev} > {e.reason}"] += 1
                prev = e.reason
            for tg in cur.tags:
                rel_tags[tg.tag] += 1
        rel_included_per_security.append(included_now)
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
            c = p.context
            if c is not None:
                context[t]["levels_near"] += bool(c.levels_near)
                context[t][f"divergence_{c.divergence.presence}"] += 1
                if c.divergence.not_applicable_reason:
                    context[t][f"divergence_na_{c.divergence.not_applicable_reason}"] += 1
                context[t]["contraction"] += bool(c.volatility.contraction_event_ids)
                context[t]["fibonacci"] += bool(c.fibonacci)
                trend[t][c.structure.state or "NONE"] += 1
                if c.prior_move.decline_into_atr is not None:
                    decline[t].append(c.prior_move.decline_into_atr)
                if c.prior_move.rise_into_atr is not None:
                    rise[t].append(c.prior_move.rise_into_atr)
            f = p.definition_fit
            if f is not None:
                row: dict[str, Any] = {
                    "type": t,
                    "family": p.family,
                    "forming": h[-1].status == "FORMING",
                    "value": f.value,
                    "exact": f.exact,
                    "shape": f.shape_score,
                    "shape_weight": f.shape_weight,
                    "shape_only": f.shape_weight > 1 - 1e-9,
                }
                for g in SHARES:
                    row[f"exact_{g}"] = definition_fit(p, cfg.patterns, g).exact
                fit_rows.append(row)
                for c in f.components:
                    key = f"{p.family}|{c.component}"
                    comp[key][c.status if not c.reason else f"{c.status}:{c.reason}"] += 1
                    if c.status == "APPLICABLE" and c.score is not None:
                        comp_scores[key].append(c.score)
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
                "context_with": dict(context[t]),
                "trend_state_at_known": dict(trend[t].most_common()),
                "median_prior_decline_atr": statistics.median(decline[t]) if decline[t] else None,
                "median_prior_rise_atr": statistics.median(rise[t]) if rise[t] else None,
            }
            for t in sorted(final, key=lambda t: -sum(final[t].values()))
        },
    }
    out["definition_fit"] = fit_report(fit_rows, comp, comp_scores)
    out["relevance"] = {
        "note": "Attention annotations by named rules; no score, no definition fit.",
        "relevance_ms_per_security": round(1000 * rel_seconds / max(securities, 1), 2),
        "current_reason_by_family": {
            f: dict(c.most_common()) for f, c in sorted(rel_current.items())
        },
        "included_now_per_security": _q([float(x) for x in rel_included_per_security])
        if rel_included_per_security
        else None,
        "securities_with_any_included_now": sum(1 for x in rel_included_per_security if x),
        "included_now_total": sum(rel_included_per_security),
        "entries_per_pattern": _q([float(x) for x in rel_entries]) if rel_entries else None,
        "reasons_ever": dict(rel_reasons_ever.most_common()),
        "transitions": dict(rel_transitions.most_common(30)),
        "current_tags": dict(rel_tags.most_common()),
    }
    print(json.dumps(out, indent=1))


SHARES = (0.6, 0.75)


def _q(xs: list[float]) -> dict[str, float]:
    a = np.asarray(xs, dtype=float)
    return {
        "n": len(a),
        "mean": round(float(a.mean()), 3),
        **{f"p{q}": round(float(np.percentile(a, q)), 3) for q in (5, 10, 25, 50, 75, 90, 95)},
    }


def _spearman(a: list[float], b: list[float]) -> float | None:
    if len(a) < 3:
        return None
    ra = pd.Series(a).rank().to_numpy()
    rb = pd.Series(b).rank().to_numpy()
    if ra.std() == 0 or rb.std() == 0:
        return None
    return round(float(np.corrcoef(ra, rb)[0, 1]), 4)


def _top_overlap(a: list[float], b: list[float], frac: float) -> float | None:
    """Share of the top ``frac`` by ``a`` that is also in the top ``frac`` by ``b``
    (ties broken by position, deterministically)."""
    n = max(1, round(len(a) * frac))
    if len(a) < 10:
        return None
    ta = set(sorted(range(len(a)), key=lambda i: (-a[i], i))[:n])
    tb = set(sorted(range(len(b)), key=lambda i: (-b[i], i))[:n])
    return round(len(ta & tb) / n, 4)


def _status_totals(comp: dict[str, Counter[str]]) -> dict[str, dict[str, int]]:
    """Per component, across families: how often it participated or not, and why."""
    out: dict[str, Counter[str]] = defaultdict(Counter)
    for key, statuses in comp.items():
        out[key.split("|")[1]].update(statuses)
    return {k: dict(v.most_common()) for k, v in sorted(out.items())}


def _sensitivity(rows: list[dict[str, Any]]) -> dict[str, Any]:
    base = [r["exact"] for r in rows]
    out: dict[str, Any] = {"n": len(rows)}
    for g in SHARES:
        alt = [r[f"exact_{g}"] for r in rows]
        out[f"shape_share_{g}"] = {
            "spearman": _spearman(base, alt),
            "top_10pct_overlap": _top_overlap(base, alt, 0.10),
            "top_25pct_overlap": _top_overlap(base, alt, 0.25),
            "max_abs_value_change": round(
                max((abs(x - y) for x, y in zip(base, alt, strict=True)), default=0.0) * 100, 2
            ),
        }
    return out


def fit_report(
    rows: list[dict[str, Any]],
    comp: dict[str, Counter[str]],
    comp_scores: dict[str, list[float]],
) -> dict[str, Any]:
    by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_family: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_type[r["type"]].append(r)
        by_family[r["family"]].append(r)
    forming = [r for r in rows if r["forming"]]
    return {
        "note": "Descriptive only. No outcome is used; sensitivity is diagnostic, never "
        "used to choose the shape share.",
        "all": {"value": _q([r["value"] for r in rows]), "shape": _q([r["shape"] for r in rows])},
        "by_type": {
            t: {
                "value": _q([r["value"] for r in rs]),
                "shape_score": _q([r["shape"] for r in rs]),
                "mean_shape_weight": round(statistics.mean(r["shape_weight"] for r in rs), 4),
                "shape_only_share": round(sum(r["shape_only"] for r in rs) / len(rs), 4),
            }
            for t, rs in sorted(by_type.items(), key=lambda kv: -len(kv[1]))
        },
        "status_totals": _status_totals(comp),
        "components": {
            k: {
                "statuses": dict(v.most_common()),
                "mean_score_when_applicable": round(statistics.mean(comp_scores[k]), 4)
                if comp_scores[k]
                else None,
                "score_values": dict(Counter(round(x, 3) for x in comp_scores[k]).most_common(6)),
            }
            for k, v in sorted(comp.items())
        },
        "sensitivity": {
            "all": _sensitivity(rows),
            "forming_now": _sensitivity(forming),
            "by_family": {f: _sensitivity(rs) for f, rs in sorted(by_family.items())},
        },
    }


if __name__ == "__main__":
    main()
