"""A synthetic published lake for the ANALYSIS stage: long weekly histories (so the
engine finds swings, levels, patterns and breakout events), with the weekly manifest,
data-quality status, continuity segments and report the stage reads.

Each security is described by a :class:`Spec`; the weekly files are written with the
pipeline's own schema (``weekly_table``) so the stage reads them exactly as in
production.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from chartlens_core.testing import make_bars
from chartlens_core.weekly import WeeklyBar
from chartlens_pipeline.daily import to_parquet_bytes
from chartlens_pipeline.storage import DataLakeLayout, ObjectStore
from chartlens_pipeline.weekly import (
    WEEKLY_BUILDER_VERSION,
    WEEKLY_SCHEMA_VERSION,
    Provenance,
    bars_from_table,
    weekly_table,
)

EX = "NSE"
START = date(2010, 1, 4)


@dataclass(frozen=True)
class Spec:
    security_id: str
    weeks: int = 400
    seed: int = 0
    analytical: bool = True
    status: str = "USABLE"
    break_at: int | None = None
    """A continuity break before this week: two segments, the second current."""
    forming: bool = False
    """The last week is still forming (``is_complete`` false)."""


def _bars(spec: Spec) -> list[WeeklyBar]:
    frame = make_bars(START, spec.weeks, freq="W-FRI", seed=spec.seed)
    out: list[WeeklyBar] = []
    seg = f"{spec.security_id}@{START + timedelta(days=0)}"
    for i, row in enumerate(frame.itertuples()):
        friday = row.bar_date.date()
        monday_ = friday - timedelta(days=4)
        if spec.break_at is not None and i == spec.break_at:
            seg = f"{spec.security_id}@{monday_}"
        if i == 0:
            seg = f"{spec.security_id}@{monday_}"

        def d(x: float) -> Decimal:
            return Decimal(f"{x:.4f}")

        out.append(
            WeeklyBar(
                continuity_segment_id=seg,
                iso_year=friday.isocalendar()[0],
                iso_week=friday.isocalendar()[1],
                week_start_date=monday_,
                week_end_date=monday_ + timedelta(days=6),
                first_session_date=monday_,
                last_session_date=friday,
                open=d(row.open),
                high=d(row.high),
                low=d(row.low),
                close=d(row.close),
                volume=Decimal(int(row.volume)),
                raw_close=d(row.close),
                trading_days=5,
                is_complete=not (spec.forming and i == spec.weeks - 1),
                partial_reason=None,
                special_sessions=0,
                closes_on_special_session=False,
                closing_session_type=None,
            )
        )
    return out


def _segment_start(bars: list[WeeklyBar]) -> tuple[str, date]:
    current = bars[-1].continuity_segment_id
    first = next(b for b in bars if b.continuity_segment_id == current)
    return current, first.first_session_date


def _write_file(store: ObjectStore, sid: str, bars: list[WeeklyBar], weekly_version: str) -> str:
    last = bars[-1].last_session_date
    prov = Provenance(last, "adj-1", "id-1", "data-1", weekly_version)
    data = to_parquet_bytes(weekly_table(EX, sid, bars, prov))
    store.put(DataLakeLayout.curated_weekly_key(EX, sid), data)
    return hashlib.sha256(data).hexdigest()


def build(store: ObjectStore, specs: list[Spec], weekly_version: str = "wk-1") -> None:
    files: dict[str, str] = {}
    status: list[dict[str, Any]] = []
    segments: list[dict[str, Any]] = []
    as_of = START
    for spec in specs:
        bars = _bars(spec)
        files[spec.security_id] = _write_file(store, spec.security_id, bars, weekly_version)
        seen: dict[str, date] = {}
        for b in bars:
            seen.setdefault(b.continuity_segment_id, b.first_session_date)
        for seg, start in seen.items():
            segments.append(
                {
                    "security_id": spec.security_id,
                    "continuity_segment_id": seg,
                    "segment_start": start,
                }
            )
        _, start = _segment_start(bars)
        status.append(
            {
                "security_id": spec.security_id,
                "analytical_universe": spec.analytical,
                "status": spec.status,
                "usable_from": start,
                "dq_version": "dq-1",
            }
        )
        as_of = max(as_of, bars[-1].last_session_date)
    store.put(
        DataLakeLayout.data_quality_status_key(EX), to_parquet_bytes(pa.Table.from_pylist(status))
    )
    store.put(
        DataLakeLayout.continuity_segments_key(EX), to_parquet_bytes(pa.Table.from_pylist(segments))
    )
    store.put(
        DataLakeLayout.data_quality_report_key(EX), json.dumps({"dq_version": "dq-1"}).encode()
    )
    _write_manifest(store, files, weekly_version, as_of)


def _write_manifest(
    store: ObjectStore, files: dict[str, str], weekly_version: str, as_of: date
) -> None:
    manifest = {
        "exchange": EX,
        "weekly_version": weekly_version,
        "schema_version": WEEKLY_SCHEMA_VERSION,
        "builder_version": WEEKLY_BUILDER_VERSION,
        "as_of": str(as_of),
        "data_version": "data-1",
        "dq_version": "dq-1",
        "methodology_hash": "f9e40bee87d3",
        "files": files,
    }
    store.put(DataLakeLayout.weekly_manifest_key(EX), json.dumps(manifest).encode())


def manifest(store: ObjectStore) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(store.get(DataLakeLayout.weekly_manifest_key(EX)))
    return loaded


def read_bars(store: ObjectStore, sid: str) -> list[WeeklyBar]:
    data = store.get(DataLakeLayout.curated_weekly_key(EX, sid))
    return bars_from_table(pq.read_table(pa.BufferReader(data)))


def republish(
    store: ObjectStore, weekly_version: str, changed: dict[str, list[WeeklyBar]] | None = None
) -> None:
    """A new weekly build: every file rewritten with the new provenance columns (so every
    file hash changes), with ``changed`` securities given new bars."""
    m = manifest(store)
    files: dict[str, str] = {}
    for sid in m["files"]:
        bars = (changed or {}).get(sid) or read_bars(store, sid)
        files[sid] = _write_file(store, sid, bars, weekly_version)
    _write_manifest(store, files, weekly_version, date.fromisoformat(m["as_of"]))


def with_new_close(bars: list[WeeklyBar], close: Decimal) -> list[WeeklyBar]:
    last = bars[-1]
    high = max(last.high, close)
    low = min(last.low, close)
    return [*bars[:-1], replace(last, close=close, raw_close=close, high=high, low=low)]
