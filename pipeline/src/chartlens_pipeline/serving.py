"""The serving snapshot: what the API reads, bound to one version of the lake (ADR-0016).

The API is a read-only presentation layer. It must never mix metadata from different lake
versions, yet the security master, data-quality tables and weekly files are rewritten in
place by every daily run. So the last step of the daily chain publishes an immutable,
versioned snapshot of exactly what the API serves:

``curated/serving/exchange={EX}/v={meta_version}/``

* ``securities.parquet``  one row per security: identity, instrument type, status,
                          ``usable_from``, current continuity segment;
* ``identifiers.parquet`` symbol / ISIN / name history;
* ``segments.parquet``    continuity segments;
* ``findings.parquet``    data-quality findings of each security;

* ``manifest.json``       a copy of the manifest below;

plus, from serving schema 2 (ADR-0018), an immutable copy of every weekly file it refers
to, named by content: ``curated/serving/exchange={EX}/weekly/{sha256}.parquet``. Then,
last, ``curated/serving/exchange={EX}/_manifest.json`` points at it, with every component
version (weekly, data, adjustment, identity, dq, calendar, methodology) and the SHA-256
of each snapshot file and of every weekly file.

Publication order (ADR-0018): validate the inputs → copy the weekly files → check every
copy exists → write the version's files → record the snapshot as STAGED → move the
pointer → mark it PUBLISHED → remove what neither this snapshot nor the previous one
refers to. Any failure before the pointer moves leaves the live snapshot untouched, and
nothing a later run writes can change what a published snapshot serves.

:class:`ServingSnapshot` is the read side. It verifies every hash it reads, so a snapshot
is either served whole or not at all. A schema-1 snapshot (published before ADR-0018)
still reads weekly bars from the curated files; one rewritten since raises
:class:`StaleSnapshot`.

Nothing here builds, adjusts or assesses anything: the publisher copies published
artefacts, the reader reads them.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import defaultdict
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Final, Protocol

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from chartlens_core.config import ChartLensSettings
from chartlens_core.domain import utc_now
from chartlens_core.logs import log_event
from chartlens_core.runs import SnapshotOutcome, SnapshotRecord
from chartlens_core.weekly import WeeklyBar
from chartlens_pipeline.daily import to_parquet_bytes
from chartlens_pipeline.providers.base import ExchangeProvider
from chartlens_pipeline.storage import DataLakeLayout, ObjectStore, StorageError
from chartlens_pipeline.weekly import bars_from_table

log = logging.getLogger("chartlens.pipeline.serving")

SERVING_SCHEMA_VERSION: Final = 2
"""2: weekly bars are served from immutable content-hashed copies (ADR-0018)."""
SNAPSHOT_FILES: Final = ("securities", "identifiers", "segments", "findings")
COPY_WORKERS: Final = 16

SECURITIES_SCHEMA: Final = pa.schema(
    [
        pa.field("security_id", pa.string(), nullable=False),
        pa.field("exchange", pa.string(), nullable=False),
        pa.field("symbol", pa.string()),
        pa.field("name", pa.string()),
        pa.field("isin", pa.string()),
        pa.field("series", pa.string()),
        pa.field("listing_status", pa.string(), nullable=False),
        pa.field("instrument_type", pa.string(), nullable=False),
        pa.field("analytical_universe", pa.bool_(), nullable=False),
        pa.field("status", pa.string(), nullable=False),
        pa.field("usable_from", pa.date32()),
        pa.field("first_date", pa.date32(), nullable=False),
        pa.field("last_date", pa.date32(), nullable=False),
        pa.field("sessions", pa.int64(), nullable=False),
        pa.field("usable_sessions", pa.int64(), nullable=False),
        pa.field("continuity_breaks", pa.int64(), nullable=False),
        pa.field("warnings", pa.int64(), nullable=False),
        pa.field("failures", pa.int64(), nullable=False),
        pa.field("current_segment_id", pa.string(), nullable=False),
        pa.field("segments", pa.int64(), nullable=False),
    ],
    metadata={b"chartlens.dataset": b"serving_securities", b"chartlens.schema_version": b"1"},
)


class ServingInputsNotReady(RuntimeError):
    """The weekly dataset or the data-quality assessment is missing or out of step."""


class PublicationFailed(RuntimeError):
    """Publication stopped before the pointer moved; the live snapshot is unchanged."""


class SnapshotHistory(Protocol):
    """Where published snapshots are recorded (Firestore in production, ADR-0018)."""

    def stage_snapshot(self, snapshot: SnapshotRecord) -> None: ...

    def publish_snapshot(self, snapshot_id: str, now: datetime) -> None: ...


class SnapshotUnavailable(RuntimeError):
    """No serving snapshot, or one whose files do not match its manifest."""


class StaleSnapshot(RuntimeError):
    """A file the snapshot refers to has been republished since: reload the pointer."""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _table(store: ObjectStore, key: str) -> pa.Table:
    return pq.read_table(pa.BufferReader(store.get(key)))


# ----------------------------------------------------------------------------- publishing


class ServingPublisher:
    def __init__(
        self,
        settings: ChartLensSettings,
        provider: ExchangeProvider,
        store: ObjectStore,
        *,
        history: SnapshotHistory | None = None,
        run_id: str | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.settings = settings
        self.exchange = provider.exchange_code
        self.store = store
        self.history = history
        self.run_id = run_id
        self.clock = clock

    def run(self) -> dict[str, Any]:
        ex, store = self.exchange, self.store
        weekly_key = DataLakeLayout.weekly_manifest_key(ex)
        if not store.exists(weekly_key):
            raise ServingInputsNotReady("no published weekly dataset; run `weekly`")
        weekly = json.loads(store.get(weekly_key))
        adjusted = json.loads(store.get(DataLakeLayout.adjusted_manifest_key(ex)))
        dq = json.loads(store.get(DataLakeLayout.data_quality_report_key(ex)))
        if dq["dq_version"] != weekly["dq_version"]:
            raise ServingInputsNotReady(
                f"weekly was built on {weekly['dq_version']}, data quality is now "
                f"{dq['dq_version']}; run `weekly`"
            )
        if adjusted["adjustment_version"] != weekly["adjustment_version"]:
            raise ServingInputsNotReady("the adjusted dataset is newer than weekly; run `weekly`")

        status = _table(store, DataLakeLayout.data_quality_status_key(ex)).to_pylist()
        segments = _table(store, DataLakeLayout.continuity_segments_key(ex))
        findings = _table(store, DataLakeLayout.data_quality_findings_key(ex))
        master = {
            r["security_id"]: r
            for r in _table(store, DataLakeLayout.securities_key(ex)).to_pylist()
        }
        history = _table(store, DataLakeLayout.identifier_history_key(ex))
        if any(s["dq_version"] != weekly["dq_version"] for s in status):
            raise ServingInputsNotReady("data-quality status is out of step with its report")

        files: dict[str, str] = weekly["files"]
        segs_by_sid: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in segments.to_pylist():
            segs_by_sid[r["security_id"]].append(r)
        rows: list[dict[str, Any]] = []
        for s in sorted(status, key=lambda s: s["security_id"]):
            sid = s["security_id"]
            if sid not in files:
                raise ServingInputsNotReady(f"{sid} has a status but no weekly file")
            m = master.get(sid, {})
            segs = sorted(segs_by_sid[sid], key=lambda r: r["segment_start"])
            rows.append(
                {
                    "security_id": sid,
                    "exchange": ex,
                    "symbol": m.get("current_symbol") or s["symbol"],
                    "name": m.get("security_name"),
                    "isin": m.get("isin") or s["isin"],
                    "series": m.get("current_series"),
                    "listing_status": m.get("listing_status", "UNKNOWN"),
                    "instrument_type": s["instrument_type"],
                    "analytical_universe": s["analytical_universe"],
                    "status": s["status"],
                    "usable_from": s["usable_from"],
                    "first_date": s["first_date"],
                    "last_date": s["last_date"],
                    "sessions": s["sessions"],
                    "usable_sessions": s["usable_sessions"],
                    "continuity_breaks": s["continuity_breaks"],
                    "warnings": s["warnings"],
                    "failures": s["failures"],
                    "current_segment_id": segs[-1]["continuity_segment_id"],
                    "segments": len(segs),
                }
            )
        sids = pa.array([r["security_id"] for r in rows])
        tables = {
            "securities": pa.Table.from_pylist(rows, schema=SECURITIES_SCHEMA),
            "identifiers": history.filter(pc.is_in(history["security_id"], value_set=sids)),
            "segments": segments,
            "findings": findings.filter(pc.is_valid(findings["security_id"])),
        }
        payloads = {name: to_parquet_bytes(t) for name, t in tables.items()}
        versions = {
            k: weekly[k]
            for k in (
                "weekly_version",
                "data_version",
                "adjustment_version",
                "identity_version",
                "dq_version",
                "calendar_version",
                "methodology_hash",
            )
        }
        meta_version = (
            "meta-"
            + _sha(
                json.dumps(
                    {
                        "schema": SERVING_SCHEMA_VERSION,
                        "versions": versions,
                        "files": {n: _sha(b) for n, b in payloads.items()},
                        "weekly_files": files,
                    },
                    sort_keys=True,
                ).encode()
            )[:12]
        )
        pointer_key = DataLakeLayout.serving_manifest_key(ex)
        previous: dict[str, Any] | None = (
            json.loads(store.get(pointer_key)) if store.exists(pointer_key) else None
        )
        if previous is not None and previous.get("meta_version") == meta_version:
            log_event(log, "serving.unchanged", meta_version=meta_version)
            return {
                "outcome": SnapshotOutcome.UNCHANGED,
                "meta_version": meta_version,
                "as_of": weekly["as_of"],
                "versions": versions,
            }

        self._copy_weekly(files)
        entries: dict[str, dict[str, Any]] = {}
        for name, data in payloads.items():
            key = DataLakeLayout.serving_file_key(ex, meta_version, name)
            store.put(key, data)
            entries[name] = {"key": key, "sha256": _sha(data), "rows": tables[name].num_rows}
        counts = {
            "securities": len(rows),
            "analytical": sum(1 for r in rows if r["analytical_universe"]),
            "weekly_bars": weekly["row_count"],
        }
        manifest = {
            "exchange": ex,
            "meta_version": meta_version,
            "schema_version": SERVING_SCHEMA_VERSION,
            "as_of": weekly["as_of"],
            "versions": versions,
            "files": entries,
            "weekly_files": files,
            "counts": counts,
            "generated_at": self.clock().isoformat(),
            "run_id": self.run_id,
        }
        body = json.dumps(manifest, indent=1, sort_keys=True).encode()
        store.put(DataLakeLayout.serving_version_manifest_key(ex, meta_version), body)

        if self.history is not None:
            self.history.stage_snapshot(
                SnapshotRecord(
                    snapshot_id=meta_version,
                    exchange=ex,
                    schema_version=SERVING_SCHEMA_VERSION,
                    status="STAGED",
                    staged_at=self.clock(),
                    run_id=self.run_id,
                    data_as_of=date.fromisoformat(weekly["as_of"]),
                    versions=versions,
                    counts=counts,
                )
            )
        store.put(pointer_key, body)  # the snapshot goes live here, and only here
        if self.history is not None:
            try:
                self.history.publish_snapshot(meta_version, self.clock())
            except Exception:  # live already; the record says STAGED until next time
                log.exception("snapshot %s is live but could not be marked PUBLISHED", meta_version)
        self._remove_unreferenced(manifest, previous)
        log_event(log, "serving.published", meta_version=meta_version, securities=len(rows))
        return {
            "outcome": SnapshotOutcome.PUBLISHED,
            **{k: v for k, v in manifest.items() if k != "weekly_files"},
        }

    def _copy_weekly(self, files: dict[str, str]) -> None:
        """Copy every weekly file the snapshot refers to into the immutable store, then
        check that all copies exist. Raises :class:`PublicationFailed` before anything is
        live if a file does not match the weekly manifest or a copy is missing."""
        ex, store = self.exchange, self.store
        prefix = DataLakeLayout.serving_weekly_prefix(ex)
        stored = set(store.list(prefix))
        pending: dict[str, str] = {}  # digest → a security whose file has that content
        for sid, digest in sorted(files.items()):
            if DataLakeLayout.serving_weekly_key(ex, digest) not in stored:
                pending.setdefault(digest, sid)

        def copy(item: tuple[str, str]) -> None:
            digest, sid = item
            data = store.get(DataLakeLayout.curated_weekly_key(ex, sid))
            if _sha(data) != digest:
                raise PublicationFailed(
                    f"the weekly file of {sid} no longer matches the weekly manifest"
                )
            store.put_immutable(DataLakeLayout.serving_weekly_key(ex, digest), data)

        # Thousands of small objects on the first publication, a day's changes after it.
        with ThreadPoolExecutor(max_workers=COPY_WORKERS) as pool:
            list(pool.map(copy, sorted(pending.items())))
        copied = len(pending)
        present = set(store.list(prefix))
        missing = {DataLakeLayout.serving_weekly_key(ex, d) for d in files.values()} - present
        if missing:
            raise PublicationFailed(f"{len(missing)} weekly copies are missing after copying")
        log_event(log, "serving.weekly_copied", copied=copied, referenced=len(files))

    def _remove_unreferenced(
        self, manifest: dict[str, Any], previous: dict[str, Any] | None
    ) -> None:
        """Keep this snapshot and the one before it (the API may hold it for a minute)."""
        ex, store = self.exchange, self.store
        prefix = DataLakeLayout.serving_prefix(ex)
        keep_versions = {f"v={manifest['meta_version']}"}
        keep_hashes = set(manifest["weekly_files"].values())
        if previous is not None:
            keep_versions.add(f"v={previous['meta_version']}")
            keep_hashes |= set(previous.get("weekly_files", {}).values())
        for key in store.list(prefix + "v="):
            if key[len(prefix) :].split("/", 1)[0] not in keep_versions:
                store.delete(key)
        weekly_prefix = DataLakeLayout.serving_weekly_prefix(ex)
        stale = [
            key
            for key in store.list(weekly_prefix)
            if key[len(weekly_prefix) :].removesuffix(".parquet") not in keep_hashes
        ]
        with ThreadPoolExecutor(max_workers=COPY_WORKERS) as pool:
            list(pool.map(store.delete, stale))


# ----------------------------------------------------------------------------- reading


@dataclass
class ServingSnapshot:
    """One verified serving snapshot, held in memory. Read-only."""

    exchange: str
    meta_version: str
    as_of: date
    versions: dict[str, str]
    generated_at: str
    securities: dict[str, dict[str, Any]]
    identifiers: dict[str, list[dict[str, Any]]]
    segments: dict[str, list[dict[str, Any]]]
    findings: dict[str, list[dict[str, Any]]]
    weekly_files: dict[str, str]
    counts: dict[str, int] = field(default_factory=dict)
    schema_version: int = 1

    @classmethod
    def load(cls, store: ObjectStore, exchange: str) -> ServingSnapshot:
        key = DataLakeLayout.serving_manifest_key(exchange)
        if not store.exists(key):
            raise SnapshotUnavailable("no serving snapshot published yet")
        manifest = json.loads(store.get(key))
        tables: dict[str, list[dict[str, Any]]] = {}
        for name in SNAPSHOT_FILES:
            entry = manifest["files"][name]
            try:
                data = store.get(entry["key"])
            except StorageError as exc:
                raise SnapshotUnavailable(f"{name}: {exc}") from None
            if _sha(data) != entry["sha256"]:
                raise SnapshotUnavailable(f"{name} does not match the snapshot manifest")
            tables[name] = pq.read_table(pa.BufferReader(data)).to_pylist()

        def grouped(rows: Sequence[dict[str, Any]], order: str) -> dict[str, list[dict[str, Any]]]:
            out: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for r in sorted(rows, key=lambda r: (r["security_id"], r[order] or date.min)):
                out[r["security_id"]].append(r)
            return dict(out)

        return cls(
            exchange=exchange,
            meta_version=manifest["meta_version"],
            as_of=date.fromisoformat(manifest["as_of"]),
            versions=dict(manifest["versions"]),
            generated_at=manifest["generated_at"],
            securities={r["security_id"]: r for r in tables["securities"]},
            identifiers=grouped(tables["identifiers"], "valid_from"),
            segments=grouped(tables["segments"], "segment_start"),
            findings=grouped(tables["findings"], "start_date"),
            weekly_files=dict(manifest["weekly_files"]),
            counts=dict(manifest["counts"]),
            schema_version=int(manifest.get("schema_version", 1)),
        )

    @staticmethod
    def current_version(store: ObjectStore, exchange: str) -> str | None:
        key = DataLakeLayout.serving_manifest_key(exchange)
        if not store.exists(key):
            return None
        version: str = json.loads(store.get(key))["meta_version"]
        return version

    def weekly_bars(self, store: ObjectStore, security_id: str) -> list[WeeklyBar]:
        """The stored weekly bars of a security, exactly as this snapshot published them."""
        digest = self.weekly_files.get(security_id)
        if digest is None:
            raise KeyError(security_id)
        if self.schema_version >= 2:
            # Immutable, named by content: nothing a later run writes can change it.
            data = store.get(DataLakeLayout.serving_weekly_key(self.exchange, digest))
            if _sha(data) != digest:
                raise SnapshotUnavailable(f"the weekly copy of {security_id} is corrupt")
        else:  # schema 1 (before ADR-0018): the curated file, until the next publication
            data = store.get(DataLakeLayout.curated_weekly_key(self.exchange, security_id))
            if _sha(data) != digest:
                raise StaleSnapshot(f"weekly file of {security_id} was republished")
        return bars_from_table(pq.read_table(pa.BufferReader(data)))

    def search(self, query: str, *, analytical_only: bool, limit: int) -> list[dict[str, Any]]:
        """Securities matching a symbol (current or past), ISIN or name, best match first:
        exact current symbol, exact past symbol or ISIN, symbol prefix, name contains."""
        q = query.strip().upper()
        if not q:
            return []
        scored: list[tuple[int, int, str, dict[str, Any]]] = []
        for sid, s in self.securities.items():
            if analytical_only and not s["analytical_universe"]:
                continue
            symbol = (s["symbol"] or "").upper()
            past = {
                r["identifier_value"].upper()
                for r in self.identifiers.get(sid, [])
                if r["identifier_type"] in ("SYMBOL", "ISIN")
            }
            name = (s["name"] or "").upper()
            if symbol == q:
                rank = 0
            elif q in past or (s["isin"] or "").upper() == q:
                rank = 1
            elif symbol.startswith(q):
                rank = 2
            elif any(p.startswith(q) for p in past):
                rank = 3
            elif len(q) >= 3 and q in name:
                rank = 4
            else:
                continue
            active = 0 if s["listing_status"] == "ACTIVE" else 1
            scored.append((rank, active, symbol, s))
        scored.sort(key=lambda t: t[:3])
        return [t[3] for t in scored[:limit]]
