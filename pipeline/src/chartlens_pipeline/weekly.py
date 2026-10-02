"""The weekly data product (ADR-0004, ADR-0014): adjusted daily → deterministic weekly bars.

Reads only published artefacts — the adjusted manifest and files, the continuity segments
and data-quality report, the adjustment events and the trading calendar — and writes:

* ``curated/weekly/exchange={EX}/{security_id}.parquet`` — one file per security, every
  continuity segment, the primary query representation; then ``_manifest.json`` (last).
* ``curated/weekly_scan/exchange={EX}/v={version}/part-NNN.parquet`` — the same bars for
  all securities, for universe-wide scans; then ``_manifest.json`` (last). Derived from
  the per-security files' content and never authoritative: deleting it loses nothing.

Weekly bars are built for every canonical security; analytical eligibility (instrument
type, status, ``usable_from``) is a consumer-level filter, never a production one.

:class:`WeeklyReader` is how analysis reads weekly bars. For the data's own ``as_of`` it
returns the stored bars; for an earlier ``as_of`` it rebuilds them point-in-time from the
daily bars on or before it, adjusted only by corporate actions with ex-date on or before
it, split only at continuity breaks known by then (ADR-0006, ADR-0014).
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import defaultdict
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, fields
from datetime import date, timedelta
from typing import Any, Final

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from chartlens_core.adjustment import (
    FactorEvent,
    adjust_price,
    adjust_volume,
    cumulative_factors,
    parse_fraction,
)
from chartlens_core.config import ChartLensSettings
from chartlens_core.domain import utc_now
from chartlens_core.logs import log_event
from chartlens_core.quality import ContinuitySegment
from chartlens_core.weekly import (
    WEEKLY_BUILDER_VERSION,
    DailyBar,
    WeeklyBar,
    build_weekly,
    current_segment,
    monday,
    to_bar_frame,
    week_last_sessions,
)
from chartlens_pipeline.adjust import ADJ_PRICE_TYPE, ADJ_VOLUME_TYPE
from chartlens_pipeline.calendar import TradingCalendar
from chartlens_pipeline.daily import PRICE_TYPE, to_parquet_bytes
from chartlens_pipeline.providers.base import ExchangeProvider
from chartlens_pipeline.storage import DataLakeLayout, ObjectStore, StorageError

log = logging.getLogger("chartlens.pipeline.weekly")

WEEKLY_SCHEMA_VERSION: Final = 2
"""v2: closing_session_type (ADR-0015)."""
SCAN_ROWS_PER_PART: Final = 2_000_000
"""Rows per scan part. The scan dataset is a *logical* dataset of one or more parts; a
reader goes through its manifest and never assumes a single file."""
SCAN_VERSIONS_KEPT: Final = 2
"""The current scan version and the previous one (a reader may still be using it)."""

_DAILY_COLUMNS: Final = [
    "trading_date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "adj_open",
    "adj_high",
    "adj_low",
    "adj_close",
    "adj_volume",
]


def weekly_schema() -> pa.Schema:
    return pa.schema(
        [
            pa.field("exchange", pa.string(), nullable=False),
            pa.field("security_id", pa.string(), nullable=False),
            pa.field("continuity_segment_id", pa.string(), nullable=False),
            pa.field("iso_year", pa.int32(), nullable=False),
            pa.field("iso_week", pa.int32(), nullable=False),
            pa.field("week_start_date", pa.date32(), nullable=False),
            pa.field("week_end_date", pa.date32(), nullable=False),
            pa.field("first_session_date", pa.date32(), nullable=False),
            pa.field("last_session_date", pa.date32(), nullable=False),
            pa.field("open", ADJ_PRICE_TYPE, nullable=False),
            pa.field("high", ADJ_PRICE_TYPE, nullable=False),
            pa.field("low", ADJ_PRICE_TYPE, nullable=False),
            pa.field("close", ADJ_PRICE_TYPE, nullable=False),
            pa.field("volume", ADJ_VOLUME_TYPE, nullable=False),
            pa.field("raw_close", PRICE_TYPE, nullable=False),
            pa.field("trading_days", pa.int32(), nullable=False),
            pa.field("is_complete", pa.bool_(), nullable=False),
            pa.field("partial_reason", pa.string()),
            pa.field("special_sessions", pa.int32(), nullable=False),
            pa.field("closes_on_special_session", pa.bool_(), nullable=False),
            pa.field("closing_session_type", pa.string()),
            pa.field("as_of", pa.date32(), nullable=False),
            pa.field("adjustment_version", pa.string(), nullable=False),
            pa.field("identity_version", pa.string(), nullable=False),
            pa.field("data_version", pa.string(), nullable=False),
            pa.field("weekly_version", pa.string(), nullable=False),
        ],
        metadata={
            b"chartlens.dataset": b"weekly",
            b"chartlens.schema_version": str(WEEKLY_SCHEMA_VERSION).encode(),
            b"chartlens.weekly_builder": WEEKLY_BUILDER_VERSION.encode(),
        },
    )


_BAR_FIELDS: Final = [f.name for f in fields(WeeklyBar)]
assert set(_BAR_FIELDS) <= set(weekly_schema().names)


@dataclass(frozen=True)
class Provenance:
    as_of: date
    adjustment_version: str
    identity_version: str
    data_version: str
    weekly_version: str


def weekly_table(
    exchange: str, security_id: str, bars: Sequence[WeeklyBar], p: Provenance
) -> pa.Table:
    rows = [
        {
            "exchange": exchange,
            "security_id": security_id,
            **{name: getattr(b, name) for name in _BAR_FIELDS},
            "partial_reason": None if b.partial_reason is None else str(b.partial_reason),
            "as_of": p.as_of,
            "adjustment_version": p.adjustment_version,
            "identity_version": p.identity_version,
            "data_version": p.data_version,
            "weekly_version": p.weekly_version,
        }
        for b in bars
    ]
    return pa.Table.from_pylist(rows, schema=weekly_schema())


def bars_from_table(table: pa.Table) -> list[WeeklyBar]:
    return [WeeklyBar(**{name: r[name] for name in _BAR_FIELDS}) for r in table.to_pylist()]


def data_version(adjusted_manifest: bytes) -> str:
    """The adjusted dataset's identity: the hash of its manifest (VersionStamp.data_version)."""
    return "data-" + hashlib.sha256(adjusted_manifest).hexdigest()[:12]


class WeeklyInputsNotReady(RuntimeError):
    """The adjusted dataset or its data-quality assessment is missing or out of date."""


@dataclass
class WeeklyResult:
    weekly_version: str
    data_version: str
    as_of: date
    securities: int
    bars: int
    incomplete_bars: int
    partial_bars: int
    files_written: int
    scan_parts: list[str]
    duration_seconds: float
    summary: dict[str, Any] = field(default_factory=dict)


def _segments_by_security(table: pa.Table) -> dict[str, list[ContinuitySegment]]:
    out: dict[str, list[ContinuitySegment]] = defaultdict(list)
    for r in sorted(table.to_pylist(), key=lambda r: (r["security_id"], r["segment_start"])):
        out[r["security_id"]].append(
            ContinuitySegment(r["security_id"], r["segment_start"], r["cause"])
        )
    return dict(out)


def _calendar_weeks(
    calendar: TradingCalendar, first: date, last: date
) -> tuple[dict[date, date], dict[date, str]]:
    """Over the weeks spanning ``first``–``last``: each ISO week's last scheduled session,
    and the type of every non-regular session (ADR-0015)."""
    sessions = calendar.expected_sessions(monday(first), monday(last) + timedelta(6))
    types = {d: t for d in sessions if (t := calendar.session_type(d)) is not None}
    return week_last_sessions(sessions), types


class _Inputs:
    """The published inputs every weekly build and read starts from, read once."""

    def __init__(self, provider: ExchangeProvider, store: ObjectStore) -> None:
        self.exchange = provider.exchange_code
        self.store = store
        key = DataLakeLayout.adjusted_manifest_key(self.exchange)
        if not store.exists(key):
            raise WeeklyInputsNotReady(f"no published adjusted dataset ({key}); run `adjust`")
        raw = store.get(key)
        self.adjusted: dict[str, Any] = json.loads(raw)
        self.data_version = data_version(raw)
        self.as_of = date.fromisoformat(self.adjusted["data_end"])
        report_key = DataLakeLayout.data_quality_report_key(self.exchange)
        segments_key = DataLakeLayout.continuity_segments_key(self.exchange)
        if not store.exists(report_key) or not store.exists(segments_key):
            raise WeeklyInputsNotReady("no data-quality assessment; run `data-quality`")
        dq = json.loads(store.get(report_key))
        if dq["adjustment_version"] != self.adjusted["adjustment_version"]:
            raise WeeklyInputsNotReady(
                f"data quality was assessed on {dq['adjustment_version']}, the adjusted "
                f"dataset is {self.adjusted['adjustment_version']}; run `data-quality`"
            )
        self.dq_version: str = dq["dq_version"]
        self.segments = _segments_by_security(
            pq.read_table(pa.BufferReader(store.get(segments_key)))
        )
        self.calendar = provider.trading_calendar()

    def adjusted_rows(self, security_id: str) -> dict[str, list[Any]]:
        digest = self.adjusted["files"].get(security_id)
        if digest is None:
            raise KeyError(f"{security_id} is not in the adjusted dataset")
        data = self.store.get(DataLakeLayout.adjusted_daily_key(self.exchange, security_id))
        if hashlib.sha256(data).hexdigest() != digest:
            raise StorageError(f"adjusted file for {security_id} does not match its manifest")
        return pq.read_table(pa.BufferReader(data), columns=_DAILY_COLUMNS).to_pydict()


def _stored_daily(t: Mapping[str, list[Any]]) -> list[DailyBar]:
    """The latest view: the adjusted columns exactly as published."""
    return [
        DailyBar(d, o, h, lo, c, v, rc)
        for d, o, h, lo, c, v, rc in zip(
            t["trading_date"],
            t["adj_open"],
            t["adj_high"],
            t["adj_low"],
            t["adj_close"],
            t["adj_volume"],
            t["close"],
            strict=True,
        )
    ]


def point_in_time_daily(
    t: Mapping[str, list[Any]], events: Sequence[FactorEvent], as_of: date
) -> list[DailyBar]:
    """Daily bars known as of ``as_of``, adjusted only by events with ex-date ≤ ``as_of``.

    Recomputed from the raw prices with the exact cumulative factor and a single rounding
    (ADR-0011), so at the data's own ``as_of`` it equals the published adjusted columns."""
    n = sum(1 for d in t["trading_date"] if d <= as_of)
    days = t["trading_date"][:n]
    factors = cumulative_factors(days, events, as_of)
    out: list[DailyBar] = []
    for i, (pf, vf) in enumerate(factors):
        raw = [t[c][i] for c in ("open", "high", "low", "close")]
        o, h, lo, c = (adjust_price(x, pf) for x in raw)  # exact at factor 1 as well
        out.append(DailyBar(days[i], o, h, lo, c, adjust_volume(t["volume"][i], vf), raw[3]))
    return out


class WeeklyService:
    def __init__(
        self, settings: ChartLensSettings, provider: ExchangeProvider, store: ObjectStore
    ) -> None:
        self.settings = settings
        self.provider = provider
        self.exchange = provider.exchange_code
        self.store = store

    def run(self) -> WeeklyResult:
        started = time.monotonic()
        inputs = _Inputs(self.provider, self.store)
        as_of = inputs.as_of
        weekly_version = (
            "wk-"
            + hashlib.sha256(
                json.dumps(
                    {
                        "builder": WEEKLY_BUILDER_VERSION,
                        "schema": WEEKLY_SCHEMA_VERSION,
                        "methodology": self.settings.weekly.model_dump(mode="json"),
                        "data": inputs.data_version,
                        "dq": inputs.dq_version,
                        "calendar": inputs.calendar.version,
                    },
                    sort_keys=True,
                ).encode()
            ).hexdigest()[:12]
        )
        provenance = Provenance(
            as_of,
            inputs.adjusted["adjustment_version"],
            inputs.adjusted["identity_version"],
            inputs.data_version,
            weekly_version,
        )
        sids = sorted(inputs.adjusted["files"])
        missing = [s for s in sids if s not in inputs.segments]
        if missing:
            raise WeeklyInputsNotReady(f"{len(missing)} securities have no continuity segments")
        first = min(seg[0].start for seg in inputs.segments.values())
        weeks, session_types = _calendar_weeks(inputs.calendar, first, as_of)
        previous = self._previous_files()

        def build(sid: str) -> tuple[str, pa.Table, list[WeeklyBar]]:
            bars = build_weekly(
                _stored_daily(inputs.adjusted_rows(sid)),
                inputs.segments[sid],
                weeks,
                as_of,
                session_types,
            )
            return sid, weekly_table(self.exchange, sid, bars, provenance), bars

        files: dict[str, str] = {}
        tables: list[pa.Table] = []
        written = n_bars = incomplete = partial = 0
        sessions: list[date] = []
        with ThreadPoolExecutor(max_workers=16) as pool:
            for n, (sid, table, bars) in enumerate(pool.map(build, sids), 1):
                data = to_parquet_bytes(table)
                digest = hashlib.sha256(data).hexdigest()
                key = DataLakeLayout.curated_weekly_key(self.exchange, sid)
                if previous.get(sid) != digest or not self.store.exists(key):
                    self.store.put(key, data)
                    written += 1
                files[sid] = digest
                tables.append(table)
                n_bars += len(bars)
                sessions += [b.first_session_date for b in bars[:1]]
                sessions += [b.last_session_date for b in bars[-1:]]
                incomplete += sum(1 for b in bars if not b.is_complete)
                partial += sum(1 for b in bars if b.partial_reason is not None)
                if n % 500 == 0:
                    log_event(log, "weekly.progress", securities=n, of=len(sids))

        common = {
            "exchange": self.exchange,
            "weekly_version": weekly_version,
            "schema_version": WEEKLY_SCHEMA_VERSION,
            "builder_version": WEEKLY_BUILDER_VERSION,
            "as_of": str(as_of),
            "data_version": inputs.data_version,
            "adjustment_version": provenance.adjustment_version,
            "identity_version": provenance.identity_version,
            "dq_version": inputs.dq_version,
            "calendar_version": inputs.calendar.version,
            "methodology_hash": self.settings.methodology_hash(),
            "source": DataLakeLayout.adjusted_manifest_key(self.exchange),
            "generated_at": utc_now().isoformat(),
            "row_count": n_bars,
            "security_count": len(sids),
            "min_session": str(min(sessions)) if sessions else None,
            "max_session": str(max(sessions)) if sessions else None,
        }
        self.store.put(
            DataLakeLayout.weekly_manifest_key(self.exchange),
            json.dumps({**common, "files": files}, indent=1, sort_keys=True).encode(),
        )
        parts = self._publish_scan(tables, weekly_version, common)
        result = WeeklyResult(
            weekly_version,
            inputs.data_version,
            as_of,
            len(sids),
            n_bars,
            incomplete,
            partial,
            written,
            parts,
            time.monotonic() - started,
        )
        result.summary = {
            **{k: v for k, v in common.items() if k != "generated_at"},
            "incomplete_bars": incomplete,
            "partial_bars": partial,
            "files_written": written,
            "scan_parts": parts,
            "duration_seconds": round(result.duration_seconds, 1),
        }
        log_event(log, "weekly.complete", weekly_version=weekly_version, bars=n_bars)
        return result

    def _previous_files(self) -> dict[str, str]:
        key = DataLakeLayout.weekly_manifest_key(self.exchange)
        if not self.store.exists(key):
            return {}
        files: dict[str, str] = json.loads(self.store.get(key)).get("files", {})
        return files

    def _publish_scan(
        self, tables: Sequence[pa.Table], weekly_version: str, common: Mapping[str, Any]
    ) -> list[str]:
        """Write the parts under their version, then the manifest, then prune old versions."""
        combined = (
            pa.concat_tables(tables) if tables else weekly_schema().empty_table()
        ).combine_chunks()
        parts: list[dict[str, Any]] = []
        for i, offset in enumerate(range(0, max(combined.num_rows, 1), SCAN_ROWS_PER_PART)):
            chunk = combined.slice(offset, SCAN_ROWS_PER_PART)
            data = to_parquet_bytes(chunk)
            key = DataLakeLayout.weekly_scan_part_key(self.exchange, weekly_version, i)
            self.store.put(key, data)
            parts.append(
                {"key": key, "rows": chunk.num_rows, "sha256": hashlib.sha256(data).hexdigest()}
            )
        manifest_key = DataLakeLayout.weekly_scan_manifest_key(self.exchange)
        previous = (
            json.loads(self.store.get(manifest_key)).get("weekly_version")
            if self.store.exists(manifest_key)
            else None
        )
        self.store.put(
            manifest_key,
            json.dumps(
                {**common, "derived_from": "per-security weekly files", "parts": parts},
                indent=1,
                sort_keys=True,
            ).encode(),
        )
        # Keep this version and the one the previous manifest pointed at (a reader may
        # still be reading it); everything older is pruned.
        prefix = DataLakeLayout.weekly_scan_prefix(self.exchange)
        keep = {f"v={weekly_version}"} | ({f"v={previous}"} if previous else set())
        for key in self.store.list(prefix + "v="):
            if key[len(prefix) :].split("/", 1)[0] not in keep:
                self.store.delete(key)
        return [p["key"] for p in parts]


# ----------------------------------------------------------------------------- reading


@dataclass
class WeeklySeries:
    security_id: str
    as_of: date
    """The as_of the bars are valid for (never later than the data)."""
    source: str
    """"stored" (the published bars) or "point_in_time" (rebuilt for an earlier as_of)."""
    bars: list[WeeklyBar]
    segment_ids: list[str]
    """Every continuity segment known as of ``as_of``, oldest first (even when ``bars``
    holds only the current one)."""
    versions: dict[str, str]

    def frame(self) -> pd.DataFrame:
        """The engine bar frame of these bars (one continuity segment, by the contract)."""
        return to_bar_frame(self.bars, self.security_id)


class WeeklyReader:
    """Weekly bars for analysis, point-in-time (ADR-0014)."""

    def __init__(self, provider: ExchangeProvider, store: ObjectStore) -> None:
        self.provider = provider
        self.exchange = provider.exchange_code
        self.store = store
        self._inputs: _Inputs | None = None
        self._manifest: dict[str, Any] | None = None
        self._events: dict[str, list[FactorEvent]] | None = None

    @property
    def inputs(self) -> _Inputs:
        if self._inputs is None:
            self._inputs = _Inputs(self.provider, self.store)
        return self._inputs

    @property
    def manifest(self) -> dict[str, Any]:
        if self._manifest is None:
            key = DataLakeLayout.weekly_manifest_key(self.exchange)
            if not self.store.exists(key):
                raise WeeklyInputsNotReady("no published weekly dataset; run `weekly`")
            manifest: dict[str, Any] = json.loads(self.store.get(key))
            self._manifest = manifest
            return manifest
        return self._manifest

    def events(self, security_id: str) -> list[FactorEvent]:
        """Applied factor events (exact), as the adjusted dataset applied them."""
        if self._events is None:
            table = pq.read_table(
                pa.BufferReader(
                    self.store.get(DataLakeLayout.adjustment_events_key(self.exchange))
                ),
                columns=["security_id", "ex_date", "applied", "factor", "volume_factor"],
            )
            out: dict[str, list[FactorEvent]] = defaultdict(list)
            for r in table.to_pylist():
                if r["applied"] and r["factor"] is not None:
                    out[r["security_id"]].append(
                        FactorEvent(
                            r["ex_date"],
                            parse_fraction(r["factor"]),
                            parse_fraction(r["volume_factor"]),
                        )
                    )
            self._events = dict(out)
        return self._events.get(security_id, [])

    def load(
        self,
        security_id: str,
        as_of: date | None = None,
        *,
        all_segments: bool = False,
        force_point_in_time: bool = False,
    ) -> WeeklySeries:
        """Weekly bars of ``security_id`` as known ``as_of`` (default: the data's as_of).

        By default only the continuity segment valid at ``as_of`` is returned — that is what
        analysis may use. ``all_segments`` returns every segment (charts, inspection); a
        segment that started after ``as_of`` does not exist yet from that point of view."""
        stored_as_of = date.fromisoformat(self.manifest["as_of"])
        point_in_time = force_point_in_time or (as_of is not None and as_of < stored_as_of)
        effective = stored_as_of if as_of is None or as_of > stored_as_of else as_of
        if point_in_time:
            # Rebuilt from the published adjusted dataset and continuity segments directly;
            # the stored weekly files play no part.
            inputs = self.inputs
            rows = inputs.adjusted_rows(security_id)
            segments = [s for s in inputs.segments[security_id] if s.start <= effective]
            days = point_in_time_daily(rows, self.events(security_id), effective)
            weeks, types = (
                _calendar_weeks(inputs.calendar, days[0].day, effective) if days else ({}, {})
            )
            bars = build_weekly(days, segments, weeks, effective, types)
            segment_ids = [s.id for s in segments]
            versions = {
                "data_version": inputs.data_version,
                "adjustment_version": inputs.adjusted["adjustment_version"],
                "identity_version": inputs.adjusted["identity_version"],
                "dq_version": inputs.dq_version,
            }
            source = "point_in_time"
        else:
            digest = self.manifest["files"].get(security_id)
            if digest is None:
                raise KeyError(f"{security_id} is not in the weekly dataset")
            data = self.store.get(DataLakeLayout.curated_weekly_key(self.exchange, security_id))
            if hashlib.sha256(data).hexdigest() != digest:
                raise StorageError(f"weekly file for {security_id} does not match its manifest")
            bars = bars_from_table(pq.read_table(pa.BufferReader(data)))
            segment_ids = list(dict.fromkeys(b.continuity_segment_id for b in bars))
            versions = {
                k: self.manifest[k]
                for k in (
                    "weekly_version",
                    "data_version",
                    "adjustment_version",
                    "identity_version",
                    "dq_version",
                )
            }
            source = "stored"
        if not all_segments:
            bars = current_segment(bars)
        return WeeklySeries(security_id, effective, source, bars, segment_ids, versions)
