"""Milestone 4 verification report (used by .github/workflows/m4-verify.yml).

Reads a local lake after ``data-quality`` and ``weekly`` and writes a compact JSON review:

* an independent re-aggregation of every security's weekly bars from the adjusted daily
  files (a different implementation from the builder) — exact equality required;
* no bar crosses a continuity segment; labels are the last actual session;
* the point-in-time reader at the data's own as_of reproduces every stored file;
* the scan dataset equals the per-security files;
* worked examples: split weeks at breaks, holiday weeks, weekend sessions, and
  point-in-time histories (as of earlier dates, only actions known by then).

usage: python scripts/verify_m4.py LAKE_DIR OUT_DIR
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from chartlens_core.config import get_settings
from chartlens_pipeline.providers import get_provider
from chartlens_pipeline.storage import DataLakeLayout, LocalObjectStore, ObjectStore
from chartlens_pipeline.weekly import WeeklyReader

EX = "NSE"
EXAMPLES: dict[str, list[str]] = {
    # symbol: as-of dates for point-in-time views (besides the data's own as_of)
    "RELIANCE": ["2015-06-30", "2023-07-19", "2023-07-21"],
    "HDFCBANK": ["2019-09-13", "2019-09-27"],
    "ITC": ["2024-12-31"],
    "3IINFOLTD": ["2021-10-01"],
    "TATACOMM": ["2019-09-13"],
    "INFY": ["2018-08-31"],
}


def table(store: ObjectStore, key: str) -> list[dict[str, Any]]:
    return pq.read_table(pa.BufferReader(store.get(key))).to_pylist()


def iso_key(d: date) -> tuple[int, int]:
    c = d.isocalendar()
    return c.year, c.week


def independent_weekly(daily: dict[str, list[Any]], starts: list[date]) -> list[dict[str, Any]]:
    """Re-aggregation written independently of chartlens_core.weekly: bucket every row by
    (segment start, ISO year, ISO week) in a dict, then reduce each bucket."""
    buckets: dict[tuple[date, int, int], list[int]] = defaultdict(list)
    for i, d in enumerate(daily["trading_date"]):
        start = max(s for s in starts if s <= d)
        buckets[(start, *iso_key(d))].append(i)
    out = []
    for (start, year, week), idx in sorted(buckets.items(), key=lambda kv: kv[1][0]):
        out.append(
            {
                "segment_start": start,
                "iso_year": year,
                "iso_week": week,
                "first_session_date": daily["trading_date"][idx[0]],
                "last_session_date": daily["trading_date"][idx[-1]],
                "open": daily["adj_open"][idx[0]],
                "high": max(daily["adj_high"][i] for i in idx),
                "low": min(daily["adj_low"][i] for i in idx),
                "close": daily["adj_close"][idx[-1]],
                "volume": sum((daily["adj_volume"][i] for i in idx), Decimal(0)),
                "raw_close": daily["close"][idx[-1]],
                "trading_days": len(idx),
            }
        )
    return out


def bar_summary(b: Any) -> dict[str, Any]:
    return {
        "week": f"{b.iso_year}-W{b.iso_week:02d}",
        "sessions": f"{b.first_session_date}..{b.last_session_date}",
        "o": str(b.open),
        "h": str(b.high),
        "l": str(b.low),
        "c": str(b.close),
        "raw_close": str(b.raw_close),
        "v": str(b.volume),
        "days": b.trading_days,
        "complete": b.is_complete,
        "partial": b.partial_reason,
        "special": b.special_sessions,
        "segment": b.continuity_segment_id,
    }


def main(lake: Path, out: Path, provider: Any = None) -> None:
    out.mkdir(parents=True, exist_ok=True)
    store = LocalObjectStore(lake)
    if provider is None:
        from chartlens_pipeline.http import HttpFetcher

        settings = get_settings()
        provider = get_provider(EX, settings, HttpFetcher(settings.http))  # no request made
    manifest = json.loads(store.get(DataLakeLayout.weekly_manifest_key(EX)))
    adjusted = json.loads(store.get(DataLakeLayout.adjusted_manifest_key(EX)))
    segments: dict[str, list[date]] = defaultdict(list)
    seg_rows = table(store, DataLakeLayout.continuity_segments_key(EX))
    for r in seg_rows:
        segments[r["security_id"]].append(r["segment_start"])
    status = {s["security_id"]: s for s in table(store, DataLakeLayout.data_quality_status_key(EX))}
    reader = WeeklyReader(provider, store)
    as_of = date.fromisoformat(manifest["as_of"])

    failures: dict[str, list[str]] = defaultdict(list)
    stats: Counter[str] = Counter()
    label_vs_calendar: Counter[str] = Counter()
    cal = provider.trading_calendar()
    last_scheduled: dict[tuple[int, int], date] = {}
    first_year = min(min(v) for v in segments.values())
    for d in cal.expected_sessions(
        first_year - timedelta(days=first_year.weekday()), as_of + timedelta(days=6)
    ):
        last_scheduled[iso_key(d)] = d

    def check(sid: str) -> dict[str, Any]:
        result: dict[str, Any] = {"sid": sid, "fail": []}
        daily = pq.read_table(
            pa.BufferReader(store.get(DataLakeLayout.adjusted_daily_key(EX, sid)))
        ).to_pydict()
        weekly_bytes = store.get(DataLakeLayout.curated_weekly_key(EX, sid))
        if hashlib.sha256(weekly_bytes).hexdigest() != manifest["files"][sid]:
            result["fail"].append(("hash", "file hash != manifest"))
        stored = pq.read_table(pa.BufferReader(weekly_bytes)).to_pylist()
        expect = independent_weekly(daily, sorted(segments[sid]))
        fields = [k for k in expect[0] if k != "segment_start"] if expect else []
        if len(stored) != len(expect):
            result["fail"].append(("reaggregation", f"bar count {len(stored)} != {len(expect)}"))
        for s, e in zip(stored, expect, strict=False):
            if any(s[k] != e[k] for k in fields):
                result["fail"].append(("reaggregation", f"mismatch at {s['last_session_date']}"))
                break
            if s["continuity_segment_id"] != f"{sid}@{e['segment_start'].isoformat()}":
                result["fail"].append(("segment_id", f"segment id at {s['last_session_date']}"))
                break
        starts = segments[sid]
        crossing = [
            s["last_session_date"]
            for s in stored
            if any(s["first_session_date"] < x <= s["last_session_date"] for x in starts)
        ]
        if crossing:
            result["fail"].append(("crosses_break", f"crosses a break: {crossing[:3]}"))
        # point-in-time at the data end == stored
        t0 = time.perf_counter()
        pit = reader.load(sid, all_segments=True, force_point_in_time=True)
        result["pit_seconds"] = time.perf_counter() - t0
        stored_bars = reader.load(sid, all_segments=True).bars
        if [repr(b) for b in pit.bars] != [repr(b) for b in stored_bars]:
            result["fail"].append(("point_in_time", "differs from stored at the data end"))
        result["bars"] = len(stored)
        result["partial"] = sum(1 for s in stored if s["partial_reason"])
        result["incomplete"] = sum(1 for s in stored if not s["is_complete"])
        result["special"] = sum(1 for s in stored if s["special_sessions"])
        result["closes_special"] = sum(1 for s in stored if s["closes_on_special_session"])
        result["labels"] = Counter(
            "label=calendar_last"
            if last_scheduled[(s["iso_year"], s["iso_week"])] == s["last_session_date"]
            else "label<calendar_last (security did not trade that day, or break-split)"
            for s in stored
        )
        result["non_friday_label"] = sum(1 for s in stored if s["last_session_date"].weekday() != 4)
        result["has_factor"] = any(f != "1/1" for f in daily["price_factor"])
        return result

    started = time.perf_counter()
    sids = sorted(manifest["files"])
    pit_times: list[float] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for r in pool.map(check, sids):
            for kind, detail in r["fail"]:
                failures[kind].append(f"{r['sid']}: {detail}")
            for k in ("bars", "partial", "incomplete", "special", "closes_special"):
                stats[k] += r[k]
            stats["securities_with_partial_bars"] += bool(r["partial"])
            stats["bars_labelled_not_friday"] += r["non_friday_label"]
            label_vs_calendar.update(r["labels"])
            pit_times.append(r["pit_seconds"])
            if r["has_factor"]:
                stats["securities_with_factors"] += 1
    check_seconds = time.perf_counter() - started

    # scan dataset == per-security files
    scan = json.loads(store.get(DataLakeLayout.weekly_scan_manifest_key(EX)))
    parts = [pq.read_table(pa.BufferReader(store.get(p["key"]))) for p in scan["parts"]]
    scan_table = pa.concat_tables(parts)
    per_security = pa.concat_tables(
        pq.read_table(pa.BufferReader(store.get(DataLakeLayout.curated_weekly_key(EX, s))))
        for s in sids
    )
    scan_ok = scan_table.equals(per_security)
    for p, t in zip(scan["parts"], parts, strict=True):
        data = store.get(p["key"])
        scan_ok &= hashlib.sha256(data).hexdigest() == p["sha256"] and t.num_rows == p["rows"]

    # worked examples
    by_symbol = {s["symbol"]: sid for sid, s in status.items()}
    examples: dict[str, Any] = {}
    for symbol, dates in EXAMPLES.items():
        sid = by_symbol.get(symbol)
        if sid is None:
            examples[symbol] = "not found"
            continue
        full = reader.load(sid, all_segments=True)
        segs = [r for r in seg_rows if r["security_id"] == sid]
        partial = [bar_summary(b) for b in full.bars if b.partial_reason]
        views: dict[str, Any] = {}
        for d in dates:
            t0 = time.perf_counter()
            view = reader.load(sid, date.fromisoformat(d))
            latest_same = {b.last_session_date: b for b in full.bars}
            views[d] = {
                "seconds": round(time.perf_counter() - t0, 3),
                "segment_ids_known": view.segment_ids,
                "bars_in_valid_segment": len(view.bars),
                "first_bar": bar_summary(view.bars[0]) if view.bars else None,
                "last_3_bars": [
                    {
                        **bar_summary(b),
                        "latest_view_close": str(latest_same[b.last_session_date].close)
                        if b.last_session_date in latest_same
                        else None,
                    }
                    for b in view.bars[-3:]
                ],
            }
        current = reader.load(sid)
        examples[symbol] = {
            "security_id": sid,
            "usable_from": str(status[sid]["usable_from"]),
            "segments": [
                {
                    k: str(r[k])
                    for k in (
                        "continuity_segment_id",
                        "segment_start",
                        "segment_end",
                        "sessions",
                        "cause",
                    )
                }
                for r in segs
            ],
            "bars_all_segments": len(full.bars),
            "bars_valid_segment": len(current.bars),
            "valid_segment_first_bar": bar_summary(current.bars[0]) if current.bars else None,
            "latest_3_bars": [bar_summary(b) for b in current.bars[-3:]],
            "partial_bars": partial[:6],
            "special_session_bars": [bar_summary(b) for b in full.bars if b.special_sessions][-4:],
            "point_in_time": views,
        }

    # holiday weeks seen market-wide: the last-week label of an active large cap
    report = {
        "as_of": str(as_of),
        "weekly_manifest": {k: v for k, v in manifest.items() if k != "files"},
        "adjusted_data_end": adjusted["data_end"],
        "checks": {
            "securities_checked": len(sids),
            "failures_by_kind": {k: len(v) for k, v in failures.items()},
            "failure_examples": {k: v[:10] for k, v in failures.items()},
            "independent_reaggregation_exact": "reaggregation" not in failures,
            "segment_ids_derived_from_segments": "segment_id" not in failures,
            "no_bar_crosses_a_break": "crosses_break" not in failures,
            "point_in_time_at_data_end_equals_stored": "point_in_time" not in failures,
            "file_hashes_match_manifest": "hash" not in failures,
            "scan_dataset_equals_per_security": bool(scan_ok),
            "seconds": round(check_seconds, 1),
        },
        "stats": dict(stats),
        "labels_vs_calendar": dict(label_vs_calendar),
        "point_in_time_seconds": {
            "mean": round(sum(pit_times) / len(pit_times), 4),
            "max": round(max(pit_times), 3),
        },
        "segments": {
            "total": len(seg_rows),
            "securities_with_more_than_one": sum(1 for v in segments.values() if len(v) > 1),
            "by_cause": dict(Counter(r["cause"] for r in seg_rows)),
        },
        "examples": examples,
    }
    (out / "m4-report.json").write_text(json.dumps(report, indent=2, default=str))
    headline = {
        k: report[k] for k in ("as_of", "checks", "stats", "segments", "point_in_time_seconds")
    }
    print(json.dumps(headline, indent=2, default=str))


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
