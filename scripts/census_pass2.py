"""Throwaway, read-only towards the lake: pass 2 of the negative-level census.

Denominators and diagnostics for the three price-level constructions that produced
negative values (pass 1): measured-move zones, Fibonacci extensions, pattern boundary
lines. For every such object (negative or not): its construction inputs, and whether a
corporate-action adjustment falls inside its span (weekly close / traded close changes
between the span's ends) and the security's data-quality status, to test whether
negatives concentrate around data events. Reads the authoritative documents and weekly
files only; writes nothing to the bucket.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
from chartlens_jobs.analysis_stage import AnalysisStage, StoreSpec

from chartlens_core.config import get_settings
from chartlens_pipeline.analysis_store import read_document, read_manifest
from chartlens_pipeline.storage import DataLakeLayout
from chartlens_pipeline.weekly import bars_from_table


def factor_at(bars: dict[str, Any], day: str) -> float | None:
    bar = bars.get(day)
    if bar is None or float(bar.raw_close) == 0:
        return None
    return round(float(bar.close) / float(bar.raw_close), 6)


def adjusted_between(bars: dict[str, Any], d1: str, d2: str) -> bool | None:
    a, b = factor_at(bars, d1), factor_at(bars, d2)
    if a is None or b is None:
        return None
    return a != b


def rate(c: Counter[str], key: str) -> dict[str, Any]:
    n = c[key + ":all"]
    k = c[key + ":neg"]
    return {"all": n, "negative": k, "share": round(k / n, 4) if n else None}


def main() -> None:
    root, out_dir = sys.argv[1], Path(sys.argv[2])
    settings = get_settings()
    spec = (
        StoreSpec("local", root=root)
        if os.environ.get("PROBE_LOCAL")
        else StoreSpec("overlay", root=root, bucket=settings.storage.gcs_bucket)
    )
    store = spec.open()
    t = time.monotonic()
    AnalysisStage(settings, "NSE", spec).run()
    manifest = read_manifest(store, "NSE")
    assert manifest is not None
    status = {
        r["security_id"]: r
        for r in pq.read_table(
            pa.BufferReader(store.get(DataLakeLayout.data_quality_status_key("NSE")))
        ).to_pylist()
    }
    c: Counter[str] = Counter()
    ratios: dict[str, list[float]] = defaultdict(list)
    examples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in manifest.entries:
        sid = entry.security_id
        doc = json.loads(read_document(store, "NSE", entry.document_sha256))
        weekly = bars_from_table(
            pq.read_table(pa.BufferReader(store.get(DataLakeLayout.curated_weekly_key("NSE", sid))))
        )
        bars = {b.last_session_date.isoformat(): b for b in weekly}
        dq = status.get(sid, {})
        dq_key = f"{dq.get('status')}"
        # Measured moves: every stored one.
        for p in doc["patterns"]["patterns"]:
            for entry_ in p["status_history"]:
                mm = entry_.get("measured_move")
                if not mm:
                    continue
                inputs = mm["target_inputs"]
                neg = mm["target_low"] < 0
                tag = "neg" if neg else "pos"
                fam = p["family"]
                c[f"mm:{fam}:all"] += 1
                c[f"mm:{fam}:neg"] += neg
                c["mm:all"] += 1
                c["mm:neg"] += neg
                d = inputs.get("direction", 0.0)
                c[f"mm:dir{d}:all"] += 1
                c[f"mm:dir{d}:neg"] += neg
                level = inputs.get("level") or 0.0
                height = inputs.get("height") or 0.0
                if level > 0 and d < 0:
                    ratios[f"mm_height_over_level:{tag}"].append(height / level)
                adj = adjusted_between(bars, p["start_date"], mm["target_calculated_at"])
                c[f"mm:adj{adj}:all"] += 1
                c[f"mm:adj{adj}:neg"] += neg
                c[f"mm:dq{dq_key}:all"] += 1
                c[f"mm:dq{dq_key}:neg"] += neg
                if neg and len(examples["mm"]) < 12:
                    examples["mm"].append(
                        {
                            "security_id": sid,
                            "pattern_id": p["pattern_id"],
                            "status": entry_["status"],
                            "method": mm["target_method"],
                            "level": level,
                            "height": height,
                            "target_low": mm["target_low"],
                            "target_high": mm["target_high"],
                            "start": p["start_date"],
                            "calculated_at": mm["target_calculated_at"],
                            "key_points": [
                                (k["label"], k["bar_date"], k["price"])
                                for k in p["geometry"]["key_points"]
                            ],
                            "adjusted_in_span": adj,
                            "dq_status": dq_key,
                        }
                    )
        # Fibonacci down-leg extensions.
        for f in doc["fibonacci"]["structures"]:
            if f["direction"] != "DOWN":
                continue
            decline = (f["anchor_price"] - f["counter_price"]) / f["anchor_price"]
            adj = adjusted_between(bars, f["anchor_bar_date"], f["counter_bar_date"])
            for lv in f["levels"]:
                if lv["kind"] != "EXTENSION":
                    continue
                neg = lv["price"] < 0
                key = f"fib:{lv['ratio']}"
                c[f"{key}:all"] += 1
                c[f"{key}:neg"] += neg
                c[f"fib:adj{adj}:all"] += 1
                c[f"fib:adj{adj}:neg"] += neg
                c[f"fib:dq{dq_key}:all"] += 1
                c[f"fib:dq{dq_key}:neg"] += neg
            ratios["fib_decline"].append(decline)
            c["fib:legs:all"] += 1
            c["fib:legs:neg"] += any(lv["price"] < 0 for lv in f["levels"])
        c["fib:up_legs"] += sum(1 for f in doc["fibonacci"]["structures"] if f["direction"] == "UP")
        # Pattern boundary lines.
        for p in doc["patterns"]["patterns"]:
            kps = p["geometry"]["key_points"]
            for ln in p["geometry"]["lines"]:
                neg = ln["start_value"] < 0 or ln["end_value"] < 0
                c[f"line:{p['pattern_type']}:all"] += 1
                c[f"line:{p['pattern_type']}:neg"] += neg
                c["line:all"] += 1
                c["line:neg"] += neg
                if neg and len(examples["line"]) < 12:
                    examples["line"].append(
                        {
                            "security_id": sid,
                            "pattern_id": p["pattern_id"],
                            "line": ln["label"],
                            "start": (ln["start_date"], ln["start_value"]),
                            "end": (ln["end_date"], ln["end_value"]),
                            "anchor_value": ln.get("anchor_value"),
                            "slope_per_bar": ln.get("slope_per_bar"),
                            "key_points": [(k["label"], k["bar_date"], k["price"]) for k in kps],
                        }
                    )

    def summary(values: list[float]) -> dict[str, float]:
        v = sorted(values)
        if not v:
            return {}
        q = lambda x: round(v[min(len(v) - 1, int(x * len(v)))], 4)  # noqa: E731
        return {
            "n": len(v),
            "p10": q(0.1),
            "p50": q(0.5),
            "p90": q(0.9),
            "min": round(v[0], 4),
            "max": round(v[-1], 4),
        }

    families = sorted(
        {
            k.split(":")[1]
            for k in c
            if k.startswith("mm:")
            and k.count(":") == 2
            and not k.split(":")[1].startswith(("dir", "adj", "dq"))
        }
    )
    types = sorted({k.split(":")[1] for k in c if k.startswith("line:") and k.count(":") == 2})
    result = {
        "analysis_set_hash": manifest.analysis_set_hash,
        "seconds": round(time.monotonic() - t, 1),
        "measured_moves": {
            "all": rate(c, "mm"),
            "by_family": {f: rate(c, f"mm:{f}") for f in families},
            "by_direction": {d: rate(c, f"mm:dir{d}") for d in ("-1.0", "1.0")},
            "by_adjustment_in_span": {a: rate(c, f"mm:adj{a}") for a in ("True", "False", "None")},
            "by_dq_status": {s: rate(c, f"mm:dq{s}") for s in ("USABLE", "USABLE_WITH_WARNINGS")},
            "height_over_level_down": {
                "negative": summary(ratios["mm_height_over_level:neg"]),
                "positive": summary(ratios["mm_height_over_level:pos"]),
            },
        },
        "fibonacci_down_extensions": {
            "by_ratio": {r: rate(c, f"fib:{r}") for r in ("1.272", "1.618", "2.618")},
            "legs_with_any_negative": rate(c, "fib:legs"),
            "up_legs": c["fib:up_legs"],
            "decline_fraction": summary(ratios["fib_decline"]),
            "by_adjustment_in_span": {a: rate(c, f"fib:adj{a}") for a in ("True", "False", "None")},
            "by_dq_status": {s: rate(c, f"fib:dq{s}") for s in ("USABLE", "USABLE_WITH_WARNINGS")},
        },
        "pattern_lines": {
            "all": rate(c, "line"),
            "by_type": {t_: rate(c, f"line:{t_}") for t_ in types},
        },
        "examples": examples,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "census2.json").write_text(json.dumps(result, indent=1, default=str))
    print(json.dumps({k: v for k, v in result.items() if k != "examples"}, indent=1, default=str))


if __name__ == "__main__":
    main()
