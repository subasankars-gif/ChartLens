"""Throwaway, read-only towards the lake: a census of every negative number in the
authoritative real-NSE analysis documents and event files (the 6e follow-up).

Reads the analysis manifest, documents and event files the ANALYSIS stage writes into a
local overlay over the live lake (nothing is written to the bucket). Does not use the
explanation artifacts. Changes nothing: it only reads and counts.
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import os
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from chartlens_jobs.analysis_stage import AnalysisStage, StoreSpec

from chartlens_core.config import get_settings
from chartlens_pipeline.analysis_store import (
    EVENT_DATASETS,
    read_document,
    read_events,
    read_manifest,
)

ID_FIELDS = (
    "pattern_id",
    "fib_id",
    "zone_id",
    "trendline_id",
    "level_id",
    "event_id",
    "swing_id",
    "divergence_id",
)
DATE_FIELDS = (
    "target_calculated_at",
    "effective_date",
    "bar_date",
    "counter_bar_date",
    "date",
    "end_date",
    "known_at",
)
PRICE_NAMES = {
    "price",
    "level",
    "target_low",
    "target_high",
    "price_low",
    "price_high",
    "line_value",
    "start_value",
    "end_value",
    "anchor_value",
    "anchor_price",
    "counter_price",
    "anchor_1_price",
    "confirmation_level",
    "invalidation_level",
    "level_at_break",
    "close",
    "open",
    "high",
    "low",
    "threshold",
    "base_price",
    "extreme",
    "previous_price",
    "last_close",
    "value",
}


def norm(path: list[Any]) -> str:
    return "/".join("[]" if isinstance(p, int) else str(p) for p in path)


def walk(node: Any, path: list[Any], chain: list[dict[str, Any]], out: list[tuple]) -> None:
    if isinstance(node, dict):
        chain2 = [*chain, node]
        for k, v in node.items():
            walk(v, [*path, k], chain2, out)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            walk(v, [*path, i], chain, out)
    elif isinstance(node, (int, float)) and not isinstance(node, bool) and node < 0:
        out.append((path, node, chain))


def owner(chain: list[dict[str, Any]]) -> tuple[str, str]:
    for obj in reversed(chain):
        for f in ID_FIELDS:
            if isinstance(obj.get(f), str):
                return f, obj[f]
    return "", ""


def nearest_date(chain: list[dict[str, Any]]) -> str:
    for obj in reversed(chain):
        for f in DATE_FIELDS:
            if isinstance(obj.get(f), str):
                return f"{f}={obj[f]}"
    return ""


def pct(values: list[float], q: float) -> float:
    values = sorted(values)
    return values[min(len(values) - 1, int(q * len(values)))] if values else 0.0


def main() -> None:
    root = sys.argv[1]
    out_dir = Path(sys.argv[2])
    settings = get_settings()
    spec = (
        StoreSpec("local", root=root)
        if os.environ.get("PROBE_LOCAL")
        else StoreSpec("overlay", root=root, bucket=settings.storage.gcs_bucket)
    )
    store = spec.open()
    t = time.monotonic()
    summary = AnalysisStage(settings, "NSE", spec).run()
    result: dict[str, Any] = {
        "analysis": summary.details,
        "analysis_seconds": round(time.monotonic() - t, 1),
    }
    manifest = read_manifest(store, "NSE")
    assert manifest is not None
    result["analysis_set_hash"] = manifest.analysis_set_hash
    result["analysis_version"] = manifest.analysis_version
    result["weekly_version"] = manifest.weekly_version
    result["as_of"] = manifest.as_of

    by_path: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"count": 0, "securities": set(), "min": 0.0, "values": []}
    )
    rows: list[dict[str, Any]] = []
    min_bar_prices: dict[str, float] = {}
    nonpositive_bar_prices = 0
    for entry in manifest.entries:
        sid = entry.security_id
        doc = json.loads(read_document(store, "NSE", entry.document_sha256))
        last_close = doc["levels"].get("last_close")
        current = doc["current"]
        current_ids = {
            *current["zone_ids"],
            *current["active_trendline_ids"],
            *current["fibonacci_ids"],
            *current["included_pattern_ids"],
        }
        swing_prices = [s["price"] for s in doc["swings"]["swings"]]
        if swing_prices:
            min_bar_prices[sid] = min(swing_prices)
            nonpositive_bar_prices += sum(1 for p in swing_prices if p <= 0)
        found: list[tuple] = []
        walk({k: v for k, v in doc.items() if k != "indicators"}, [], [], found)
        # Indicator series: counted by series name only (oscillators are signed).
        for series in doc["indicators"]["series"]:
            neg = [v for v in series["data"] if isinstance(v, (int, float)) and v < 0]
            if neg:
                key = f"indicators/series[{series['name']}]"
                agg = by_path[key]
                agg["count"] += len(neg)
                agg["securities"].add(sid)
                agg["min"] = min(agg["min"], min(neg))
        patterns = {p["pattern_id"]: p for p in doc["patterns"]["patterns"]}
        fibs = {f["fib_id"]: f for f in doc["fibonacci"]["structures"]}
        for path, value, chain in found:
            key = norm(path)
            agg = by_path[key]
            agg["count"] += 1
            agg["securities"].add(sid)
            agg["min"] = min(agg["min"], value)
            if len(agg["values"]) < 200000:
                agg["values"].append(value)
            leaf = path[-1] if path else ""
            if leaf not in PRICE_NAMES:
                continue
            id_field, oid = owner(chain)
            p = patterns.get(oid) if id_field == "pattern_id" else None
            f = fibs.get(oid) if id_field == "fib_id" else None
            fib_level = None
            if f is not None and len(path) >= 5 and path[3] == "levels":
                fib_level = f["levels"][path[4]]
            rows.append(
                {
                    "security_id": sid,
                    "document_sha256": entry.document_sha256,
                    "path": key,
                    "full_path": "/" + "/".join(str(x) for x in path),
                    "value": value,
                    "last_close": last_close,
                    "owner": oid,
                    "in_current": oid in current_ids,
                    "pattern_type": p["pattern_type"] if p else "",
                    "family": p["family"] if p else "",
                    "pattern_status": p["status_history"][-1]["status"] if p else "",
                    "direction": (p or f or {}).get("direction", ""),
                    "fib_kind": fib_level["kind"] if fib_level else "",
                    "fib_ratio": fib_level["ratio"] if fib_level else "",
                    "fib_status": f["status_history"][-1]["status"] if f else "",
                    "anchor_price": f["anchor_price"] if f else "",
                    "counter_price": f["counter_price"] if f else "",
                    "date": nearest_date(chain),
                }
            )
        for dataset in EVENT_DATASETS:
            ev_rows, _ = read_events(store, "NSE", dataset, entry.events[dataset].content_sha256)
            ev_found: list[tuple] = []
            walk(ev_rows, [f"events:{dataset}"], [], ev_found)
            for path, value, chain in ev_found:
                key = norm(path)
                agg = by_path[key]
                agg["count"] += 1
                agg["securities"].add(sid)
                agg["min"] = min(agg["min"], value)
                agg["values"].append(value)
                if (path[-1] if path else "") in PRICE_NAMES:
                    _, oid = owner(chain)
                    rows.append(
                        {
                            "security_id": sid,
                            "document_sha256": entry.document_sha256,
                            "path": key,
                            "full_path": "/".join(str(x) for x in path),
                            "value": value,
                            "last_close": last_close,
                            "owner": oid,
                            "in_current": False,
                            "pattern_type": chain[0].get("pattern_type", "") if chain else "",
                            "family": chain[0].get("family", "") if chain else "",
                            "pattern_status": "",
                            "direction": chain[0].get("direction", "") if chain else "",
                            "fib_kind": "",
                            "fib_ratio": "",
                            "fib_status": "",
                            "anchor_price": "",
                            "counter_price": "",
                            "date": nearest_date(chain),
                        }
                    )
    result["paths"] = {
        k: {
            "count": v["count"],
            "securities": len(v["securities"]),
            "min": v["min"],
            "p50": pct(v["values"], 0.5),
            "price_named": k.split("/")[-1] in PRICE_NAMES,
        }
        for k, v in sorted(by_path.items(), key=lambda kv: -kv[1]["count"])
    }
    result["price_rows"] = len(rows)
    result["price_securities"] = len({r["security_id"] for r in rows})
    result["swing_prices_nonpositive"] = nonpositive_bar_prices
    result["swing_price_min"] = min(min_bar_prices.values()) if min_bar_prices else None
    by_cat: Counter[str] = Counter(r["path"] for r in rows)
    result["price_rows_by_path"] = dict(by_cat.most_common())
    result["price_rows_by_family"] = dict(
        Counter(r["family"] for r in rows if r["family"]).most_common()
    )
    result["price_rows_by_fib"] = dict(
        Counter(
            f"{r['direction']}:{r['fib_kind']}:{r['fib_ratio']}" for r in rows if r["fib_kind"]
        ).most_common()
    )
    result["price_rows_in_current"] = sum(1 for r in rows if r["in_current"])
    out_dir.mkdir(parents=True, exist_ok=True)
    buf = io.StringIO()
    if rows:
        writer = csv.DictWriter(buf, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    (out_dir / "negative_price_rows.csv.gz").write_bytes(gzip.compress(buf.getvalue().encode()))
    (out_dir / "census.json").write_text(json.dumps(result, indent=1, default=str))
    print(json.dumps({k: v for k, v in result.items() if k != "paths"}, indent=1, default=str))


if __name__ == "__main__":
    main()
