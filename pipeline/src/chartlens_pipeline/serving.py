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

From serving schema 3 (ADR-0026) the snapshot also pins the analysis set: a verbatim
copy of the ANALYSIS stage's manifest, ``v={meta_version}/analysis_manifest.json``, named
by its SHA-256 in the manifest's ``analysis`` block. The documents and event files it
names were written by ANALYSIS; publication copies, verifies and points, and never
re-encodes or derives analytical content.

From serving schema 4 (ADR-0028 §6) it also pins the explanation set: a verbatim copy of
the explanation manifest, ``v={meta_version}/explanations_manifest.json``, named by its
SHA-256 in the ``explanations`` block and bound to the analysis manifest by its
``analysis_set_hash``. Each explanation object is bound to exactly one analysis
document; publication validates every claim of a new object against that document with
the shared checker (``chartlens_core.claims``) and never derives ``explain_version``.

Publication order (ADR-0018, ADR-0026 §1.5): validate the inputs and the analysis set
(checks 1-6) → verify every analysis object the live snapshot does not already cover
(checks 7-9) → copy the weekly files → write the version's files → read them back →
record the snapshot as STAGED → **commit: a compare-and-swap of the pointer** → mark it
PUBLISHED → remove what neither this snapshot, the previous one nor the latest analysis
manifest refers to. Before the commit, everything the new snapshot needs is verified and
written; any failure leaves the live snapshot untouched. After it, the snapshot is
immutable: the same inputs give the same ``meta_version``, which returns UNCHANGED
before any write.

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
import time
from collections import defaultdict
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Final, Protocol

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from chartlens_core.claims import validate as validate_claims
from chartlens_core.config import ChartLensSettings
from chartlens_core.domain import utc_now
from chartlens_core.logs import log_event
from chartlens_core.runs import SnapshotOutcome, SnapshotRecord
from chartlens_core.weekly import WeeklyBar
from chartlens_pipeline.analysis_store import (
    EVENT_DATASETS,
    AnalysisEntry,
    AnalysisManifest,
    AnalysisStoreError,
    ExplanationEntry,
    ExplanationManifest,
    analysis_set_hash,
    analysis_universe,
    explanation_disagreements,
    explanation_manifest_bytes,
    explanation_set_hash,
    inspect_document,
    inspect_events,
    inspect_explanation,
    manifest_bytes,
    read_explanation_manifest,
    read_manifest,
    universe_sha256,
)
from chartlens_pipeline.daily import to_parquet_bytes
from chartlens_pipeline.providers.base import ExchangeProvider
from chartlens_pipeline.storage import DataLakeLayout, ObjectStore, StorageError, SwapConflict
from chartlens_pipeline.weekly import bars_from_table

log = logging.getLogger("chartlens.pipeline.serving")

SERVING_SCHEMA_VERSION: Final = 4
"""2: weekly bars are served from immutable content-hashed copies (ADR-0018).
3: plus the analysis set, pinned by a verbatim copy of its manifest (ADR-0026).
4: plus the explanation set, pinned by a verbatim copy of its manifest (ADR-0028)."""
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

    def get_snapshot(self, snapshot_id: str) -> SnapshotRecord | None: ...


class SnapshotUnavailable(RuntimeError):
    """No serving snapshot, or one whose files do not match its manifest."""


class StaleSnapshot(RuntimeError):
    """A file the snapshot refers to has been republished since: reload the pointer."""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def analysis_summary(manifest: AnalysisManifest, data: bytes) -> dict[str, Any]:
    """The snapshot's ``analysis`` block, from the manifest it pins (ADR-0026 §1.2)."""
    return {
        "manifest_sha256": _sha(data),
        "analysis_version": manifest.analysis_version,
        "analysis_methodology_hash": manifest.analysis_methodology_hash,
        "universe_sha256": manifest.universe_sha256,
        "analysis_set_hash": manifest.analysis_set_hash,
        "securities": len(manifest.universe),
    }


def explanation_summary(manifest: ExplanationManifest, data: bytes) -> dict[str, Any]:
    """The snapshot's ``explanations`` block, from the manifest it pins (ADR-0028 §6)."""
    return {
        "manifest_sha256": _sha(data),
        "explain_version": manifest.explain_version,
        "analysis_set_hash": manifest.analysis_set_hash,
        "explanation_set_hash": manifest.explanation_set_hash,
        "securities": len(manifest.entries),
    }


def explanation_keys(exchange: str, manifest: ExplanationManifest) -> set[str]:
    return {
        DataLakeLayout.serving_explanation_key(exchange, e.explanation_sha256)
        for e in manifest.entries
    }


def artifact_keys(exchange: str, manifest: AnalysisManifest) -> set[str]:
    """Every object key an analysis manifest names."""
    keys: set[str] = set()
    for e in manifest.entries:
        keys.add(DataLakeLayout.serving_analysis_key(exchange, e.document_sha256))
        for name, artifact in e.events.items():
            keys.add(
                DataLakeLayout.serving_events_key(exchange, name, artifact.content_sha256)  # type: ignore[arg-type]
            )
    return keys


def _document_disagreements(
    entry: AnalysisEntry, manifest: AnalysisManifest, canonical: bytes
) -> list[str]:
    """Check 7: the document says what its entry says (identity, segment, bars,
    analysis version, event content hashes and row counts)."""
    doc = json.loads(canonical)
    sid = entry.security_id
    found = {
        "security_id": doc["identity"]["security_id"],
        "continuity_segment_id": doc["identity"]["continuity_segment_id"],
        "bars_sha256": doc["inputs"]["bars_sha256"],
        "analysis_version": doc["versions"]["analysis_version"],
        **{
            f"{name}.content_sha256": doc["breakout_events"]["datasets"][name]["content_sha256"]
            for name in EVENT_DATASETS
        },
        **{
            f"{name}.row_count": doc["breakout_events"]["datasets"][name]["row_count"]
            for name in EVENT_DATASETS
        },
    }
    expected = {
        "security_id": sid,
        "continuity_segment_id": entry.continuity_segment_id,
        "bars_sha256": entry.bars_sha256,
        "analysis_version": manifest.analysis_version,
        **{f"{n}.content_sha256": entry.events[n].content_sha256 for n in EVENT_DATASETS},
        **{f"{n}.row_count": entry.events[n].row_count for n in EVENT_DATASETS},
    }
    return [
        f"{sid}: the document's {k} is {found[k]}, its entry says {expected[k]}"
        for k in expected
        if found[k] != expected[k]
    ]


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
        expected_analysis_version: str,
        expected_explain_version: str,
        history: SnapshotHistory | None = None,
        run_id: str | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.settings = settings
        self.exchange = provider.exchange_code
        self.store = store
        self.expected_analysis_version = expected_analysis_version
        """Supplied by the job layer, which knows the engine; publication never derives or
        substitutes one (ADR-0026 §1.3, check 2)."""
        self.expected_explain_version = expected_explain_version
        """Likewise for explanations (ADR-0028 §6, check 10): never derived here."""
        self.history = history
        self.run_id = run_id
        self.clock = clock
        self.verification: dict[str, Any] = {}

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
        # Checks 1-6 (ADR-0026 §1.3): the analysis set against this weekly version.
        analysis_bytes, analysis = self._analysis_set(weekly, status, rows, files)
        summary = analysis_summary(analysis, analysis_bytes)
        # Checks 10-11 (ADR-0028 §6): the explanation set against this analysis set.
        explained_bytes, explained = self._explanation_set(analysis)
        explained_summary = explanation_summary(explained, explained_bytes)
        meta_version = (
            "meta-"
            + _sha(
                json.dumps(
                    {
                        "schema": SERVING_SCHEMA_VERSION,
                        "versions": versions,
                        "files": {n: _sha(b) for n, b in payloads.items()},
                        "weekly_files": files,
                        "analysis": summary,
                        "explanations": explained_summary,
                    },
                    sort_keys=True,
                ).encode()
            )[:12]
        )
        pointer_key = DataLakeLayout.serving_manifest_key(ex)
        previous_bytes = store.get(pointer_key) if store.exists(pointer_key) else None
        previous: dict[str, Any] | None = (
            json.loads(previous_bytes) if previous_bytes is not None else None
        )
        if previous is not None and previous.get("meta_version") == meta_version:
            # Idempotent: the same inputs are the same snapshot; nothing is written.
            log_event(log, "serving.unchanged", meta_version=meta_version)
            if self.history is not None:  # a process that died between pointer and record
                record = self.history.get_snapshot(meta_version)
                if record is not None and record.status == "STAGED":
                    self.history.publish_snapshot(meta_version, self.clock())
            return {
                "outcome": SnapshotOutcome.UNCHANGED,
                "meta_version": meta_version,
                "as_of": weekly["as_of"],
                "versions": versions,
                "analysis": summary,
                "explanations": explained_summary,
            }

        # Checks 7-9: every artifact not covered by the live, verified snapshot.
        self.verification = self._verify_artifacts(
            analysis, explained, self._live_addresses(previous)
        )
        self._copy_weekly(files)
        analysis_key = DataLakeLayout.serving_analysis_manifest_key(ex, meta_version)
        entries: dict[str, dict[str, Any]] = {}
        written: dict[str, tuple[str, str]] = {}
        for name, data in payloads.items():
            key = DataLakeLayout.serving_file_key(ex, meta_version, name)
            store.put(key, data)
            entries[name] = {"key": key, "sha256": _sha(data), "rows": tables[name].num_rows}
            written[key] = (name, _sha(data))
        store.put(analysis_key, analysis_bytes)  # the analysis manifest, verbatim
        written[analysis_key] = ("analysis_manifest", summary["manifest_sha256"])
        explained_key = DataLakeLayout.serving_explanations_manifest_key(ex, meta_version)
        store.put(explained_key, explained_bytes)  # the explanation manifest, verbatim
        written[explained_key] = ("explanations_manifest", explained_summary["manifest_sha256"])
        counts = {
            "securities": len(rows),
            "analytical": sum(1 for r in rows if r["analytical_universe"]),
            "analysed": int(summary["securities"]),
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
            "analysis": {**summary, "manifest_key": analysis_key},
            "explanations": {**explained_summary, "manifest_key": explained_key},
            "counts": counts,
            "generated_at": self.clock().isoformat(),
            "run_id": self.run_id,
        }
        body = json.dumps(manifest, indent=1, sort_keys=True).encode()
        version_key = DataLakeLayout.serving_version_manifest_key(ex, meta_version)
        store.put(version_key, body)
        written[version_key] = ("manifest", _sha(body))
        # Step 5: what was written is what was meant to be written.
        for key, (name, digest) in written.items():
            if _sha(store.get(key)) != digest:
                raise PublicationFailed(f"{name} did not read back as written")

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
                    analysis={k: v for k, v in summary.items() if k != "manifest_sha256"},
                )
            )
        # Step 7: the commit. A compare-and-swap of the pointer read at the start: if
        # anyone moved it meanwhile, nothing changes and publication fails.
        try:
            store.swap(pointer_key, body, None if previous_bytes is None else _sha(previous_bytes))
        except SwapConflict as exc:
            raise PublicationFailed(f"the live pointer moved during publication ({exc})") from None
        if self.history is not None:
            try:
                self.history.publish_snapshot(meta_version, self.clock())
            except Exception:  # live already; the next publication corrects the record
                log.exception("snapshot %s is live but could not be marked PUBLISHED", meta_version)
        try:  # live already: a failed clean-up must not turn a publication into a failure
            self._remove_unreferenced(manifest, previous)
        except Exception:
            log.exception("clean-up after publishing %s failed; the next run retries", meta_version)
        log_event(
            log,
            "serving.published",
            meta_version=meta_version,
            securities=len(rows),
            verification=self.verification,
        )
        return {
            "outcome": SnapshotOutcome.PUBLISHED,
            "verification": self.verification,
            **{k: v for k, v in manifest.items() if k != "weekly_files"},
        }

    # ------------------------------------------------------------------ analysis set

    def _analysis_set(
        self,
        weekly: dict[str, Any],
        status: list[dict[str, Any]],
        rows: list[dict[str, Any]],
        files: dict[str, str],
    ) -> tuple[bytes, AnalysisManifest]:
        """Checks 1-6 of ADR-0026 §1.3 on the ANALYSIS stage's manifest. Nothing it
        claims is trusted: each fact is re-derived from the published inputs."""
        key = DataLakeLayout.analysis_manifest_key(self.exchange)
        if not self.store.exists(key):
            raise ServingInputsNotReady("no analysis manifest; run the ANALYSIS stage")
        data = self.store.get(key)
        try:
            manifest = AnalysisManifest.model_validate(json.loads(data))
        except Exception as exc:
            raise PublicationFailed(f"the analysis manifest is unreadable ({exc})") from None
        if manifest_bytes(manifest) != data:
            raise PublicationFailed("the analysis manifest is not in canonical form")
        # 1. inputs in step
        for name, expected in (
            ("weekly_version", weekly["weekly_version"]),
            ("dq_version", weekly["dq_version"]),
            ("as_of", str(weekly["as_of"])),
            ("methodology_hash", weekly["methodology_hash"]),
        ):
            if getattr(manifest, name) != expected:
                raise ServingInputsNotReady(
                    f"the analysis was built on {name} {getattr(manifest, name)}, the weekly "
                    f"data is {expected}; run the ANALYSIS stage"
                )
        # 2. methodology: never derived or substituted here
        if manifest.analysis_methodology_hash != self.settings.analysis.methodology_hash():
            raise PublicationFailed("the analysis was built with other [analysis] settings")
        if manifest.analysis_version != self.expected_analysis_version:
            raise PublicationFailed(
                f"the analysis is {manifest.analysis_version}, the job expects "
                f"{self.expected_analysis_version}"
            )
        # 3. integrity
        if analysis_set_hash(manifest.entries) != manifest.analysis_set_hash:
            raise PublicationFailed("the analysis manifest does not match its set hash")
        # 4. universe, re-derived
        try:
            universe = analysis_universe(status, str(weekly["dq_version"]))
        except AnalysisStoreError as exc:
            raise ServingInputsNotReady(str(exc)) from None
        if universe != manifest.universe or universe_sha256(universe) != manifest.universe_sha256:
            missing = sorted(set(universe) - set(manifest.universe))[:5]
            extra = sorted(set(manifest.universe) - set(universe))[:5]
            raise PublicationFailed(
                f"the analysed universe differs from the published one "
                f"(missing {missing}, unexpected {extra})"
            )
        # 5. coverage
        if [e.security_id for e in manifest.entries] != universe:
            raise PublicationFailed("the analysis entries do not cover exactly the universe")
        # 6. inputs per security
        current = {r["security_id"]: r["current_segment_id"] for r in rows}
        for e in manifest.entries:
            if files.get(e.security_id) != e.weekly_file_sha256:
                raise PublicationFailed(f"{e.security_id}: analysed from a stale weekly file")
            if current.get(e.security_id) != e.continuity_segment_id:
                raise PublicationFailed(f"{e.security_id}: analysed another segment")
        return data, manifest

    def _explanation_set(self, analysis: AnalysisManifest) -> tuple[bytes, ExplanationManifest]:
        """Checks 10-11 of ADR-0028 §6: the explanation manifest is bound to this analysis
        set, has the job's expected ``explain_version`` (never derived here), and binds
        exactly one explanation to each analysed security's document."""
        key = DataLakeLayout.explanations_manifest_key(self.exchange)
        if not self.store.exists(key):
            raise ServingInputsNotReady("no explanation manifest; run the ANALYSIS stage")
        data = self.store.get(key)
        try:
            manifest = ExplanationManifest.model_validate(json.loads(data))
        except Exception as exc:
            raise PublicationFailed(f"the explanation manifest is unreadable ({exc})") from None
        if explanation_manifest_bytes(manifest) != data:
            raise PublicationFailed("the explanation manifest is not in canonical form")
        # 10. binding and version
        if manifest.analysis_set_hash != analysis.analysis_set_hash:
            raise ServingInputsNotReady(
                "the explanations describe another analysis set; run the ANALYSIS stage"
            )
        if manifest.explain_version != self.expected_explain_version:
            raise PublicationFailed(
                f"the explanations are {manifest.explain_version}, the job expects "
                f"{self.expected_explain_version}"
            )
        if explanation_set_hash(manifest.entries) != manifest.explanation_set_hash:
            raise PublicationFailed("the explanation manifest does not match its set hash")
        # 11. coverage: one per analysed security, bound to that security's document
        documents = [(e.security_id, e.document_sha256) for e in analysis.entries]
        bound = [(e.security_id, e.document_sha256) for e in manifest.entries]
        if bound != documents:
            missing = sorted({d[0] for d in documents} - {b[0] for b in bound})[:5]
            raise PublicationFailed(
                "the explanations do not bind exactly one explanation to each analysed "
                f"document (missing {missing})"
            )
        return data, manifest

    def _live_addresses(self, previous: dict[str, Any] | None) -> set[str]:
        """Object keys covered by the live, previously verified schema-3 snapshot: the
        addresses its verbatim analysis manifest names, trusted only after that copy is
        verified against the hash the live pointer records (ADR-0026 §1.3)."""
        if previous is None or int(previous.get("schema_version", 1)) < 3:
            return set()
        block = previous.get("analysis") or {}
        try:
            data = self.store.get(str(block["manifest_key"]))
        except (KeyError, StorageError):
            return set()
        if _sha(data) != block.get("manifest_sha256"):
            log.warning("the live analysis manifest does not match its pointer; verifying all")
            return set()
        live = AnalysisManifest.model_validate(json.loads(data))
        keys = artifact_keys(self.exchange, live)
        block4 = (
            previous.get("explanations") if int(previous.get("schema_version", 1)) >= 4 else None
        )
        if block4:
            try:
                ex_data = self.store.get(str(block4["manifest_key"]))
            except (KeyError, StorageError):
                return keys
            if _sha(ex_data) == block4.get("manifest_sha256"):
                live_ex = ExplanationManifest.model_validate(json.loads(ex_data))
                keys |= explanation_keys(self.exchange, live_ex)
        return keys

    def _verify_artifacts(
        self,
        manifest: AnalysisManifest,
        explained: ExplanationManifest,
        live: set[str],
    ) -> dict[str, Any]:
        """Checks 7-9 of ADR-0026 §1.3 and check 12 of ADR-0028 §6, in parallel. Raises
        before anything is written."""
        started = time.monotonic()
        ex, store = self.exchange, self.store
        # Existence of covered objects: three listings, not thousands of lookups.
        present: set[str] = set()
        if live:
            for prefix in (
                DataLakeLayout.serving_analysis_prefix(ex),
                *(DataLakeLayout.serving_events_prefix(ex, n) for n in EVENT_DATASETS),
                DataLakeLayout.serving_explanations_prefix(ex),
            ):
                present |= set(store.list(prefix))
        explanation_of = {e.security_id: e for e in explained.entries}

        def check(entry: AnalysisEntry) -> tuple[list[str], int, int]:
            problems: list[str] = []
            full = existence = 0
            doc_key = DataLakeLayout.serving_analysis_key(ex, entry.document_sha256)
            if doc_key in live:
                existence += 1
                if doc_key not in present:
                    problems.append(f"{doc_key}: missing")
            else:
                full += 1
                canonical, problem = inspect_document(store, ex, entry.document_sha256)
                if problem is not None:
                    problems.append(f"{problem.key}: {problem.kind}: {problem.detail}")
                else:
                    assert canonical is not None
                    problems += _document_disagreements(entry, manifest, canonical)
            for name in EVENT_DATASETS:
                artifact = entry.events.get(name)
                if artifact is None:
                    problems.append(f"{entry.security_id}: no {name} entry")
                    continue
                key = DataLakeLayout.serving_events_key(ex, name, artifact.content_sha256)
                if key in live:
                    existence += 1
                    if key not in present:
                        problems.append(f"{key}: missing")
                    continue
                full += 1
                _, problem = inspect_events(store, ex, name, artifact)
                if problem is not None:
                    problems.append(f"{problem.key}: {problem.kind}: {problem.detail}")
            x_problems, x_full = self._check_explanation(
                explanation_of[entry.security_id], present, live
            )
            return problems + x_problems, full + x_full, existence + (1 - x_full)

        with ThreadPoolExecutor(max_workers=COPY_WORKERS) as pool:
            results = list(pool.map(check, manifest.entries))
        found = [p for problems, _, _ in results for p in problems]
        if found:
            raise PublicationFailed(
                f"{len(found)} analysis artifacts failed verification: {'; '.join(found[:5])}"
            )
        return {
            "objects_fully_verified": sum(r[1] for r in results),
            "objects_existence_only": sum(r[2] for r in results),
            "seconds": round(time.monotonic() - started, 1),
        }

    def _check_explanation(
        self, x: ExplanationEntry, present: set[str], live: set[str]
    ) -> tuple[list[str], int]:
        """Check 12: an explanation the live verified snapshot covers must exist; any
        other must verify against its address and entry, and every claim must hold
        against exactly the document it is bound to (ADR-0028 §6). Returns the problems
        and 1 if fully verified, 0 if existence only."""
        ex, store = self.exchange, self.store
        key = DataLakeLayout.serving_explanation_key(ex, x.explanation_sha256)
        if key in live:
            return ([] if key in present else [f"{key}: missing"]), 0
        body, problem = inspect_explanation(store, ex, x.explanation_sha256)
        if problem is not None or body is None:
            return [
                f"{key}: {problem.kind}: {problem.detail}" if problem else f"{key}: unreadable"
            ], 1
        problems = explanation_disagreements(x, body)
        if problems:
            return problems, 1
        document, doc_problem = inspect_document(store, ex, x.document_sha256)
        if doc_problem is not None or document is None:
            return [f"{x.security_id}: the explained document does not verify"], 1
        claims = validate_claims(json.loads(body), json.loads(document), x.document_sha256)
        return [f"{x.security_id}: {c}" for c in claims[:3]], 1

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
        """Keep this snapshot, the one before it (the API may hold it for a minute) and,
        for analysis objects, the latest analysis manifest (tomorrow's reuse) too
        (ADR-0026 §1.6). Everything else in the serving store is removed."""
        ex, store = self.exchange, self.store
        prefix = DataLakeLayout.serving_prefix(ex)
        keep_versions = {f"v={manifest['meta_version']}"}
        keep_hashes = set(manifest["weekly_files"].values())
        keep_objects = self._named_by(manifest)
        if previous is not None:
            keep_versions.add(f"v={previous['meta_version']}")
            keep_hashes |= set(previous.get("weekly_files", {}).values())
            keep_objects |= self._named_by(previous)
        latest = read_manifest(store, ex)
        if latest is not None:
            keep_objects |= artifact_keys(ex, latest)
        latest_explained = read_explanation_manifest(store, ex)
        if latest_explained is not None:
            keep_objects |= explanation_keys(ex, latest_explained)
        for key in store.list(prefix + "v="):
            if key[len(prefix) :].split("/", 1)[0] not in keep_versions:
                store.delete(key)
        weekly_prefix = DataLakeLayout.serving_weekly_prefix(ex)
        stale = [
            key
            for key in store.list(weekly_prefix)
            if key[len(weekly_prefix) :].removesuffix(".parquet") not in keep_hashes
        ]
        for analysis_prefix in (
            DataLakeLayout.serving_analysis_prefix(ex),
            *(DataLakeLayout.serving_events_prefix(ex, n) for n in EVENT_DATASETS),
            DataLakeLayout.serving_explanations_prefix(ex),
        ):
            stale += [key for key in store.list(analysis_prefix) if key not in keep_objects]
        with ThreadPoolExecutor(max_workers=COPY_WORKERS) as pool:
            list(pool.map(store.delete, stale))

    def _named_by(self, manifest: dict[str, Any]) -> set[str]:
        keys: set[str] = set()
        block = manifest.get("analysis")
        if block:
            try:
                live = AnalysisManifest.model_validate(
                    json.loads(self.store.get(block["manifest_key"]))
                )
                keys |= artifact_keys(self.exchange, live)
            except Exception:  # an unreadable copy keeps nothing alive through it
                pass
        block4 = manifest.get("explanations")
        if block4:
            try:
                live_ex = ExplanationManifest.model_validate(
                    json.loads(self.store.get(block4["manifest_key"]))
                )
                keys |= explanation_keys(self.exchange, live_ex)
            except Exception:
                pass
        return keys


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
    analysis: dict[str, Any] | None = None
    """Schema 3: the snapshot's analysis block (ADR-0026 §1.2); None before schema 3."""
    analysis_entries: dict[str, AnalysisEntry] = field(default_factory=dict)
    """Schema 3: the pinned analysis manifest's entries, by security."""
    explanations: dict[str, Any] | None = None
    """Schema 4: the snapshot's explanations block (ADR-0028 §6); None before schema 4."""
    explanation_entries: dict[str, ExplanationEntry] = field(default_factory=dict)
    """Schema 4: the pinned explanation manifest's entries, by security."""

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

        analysis = manifest.get("analysis") if int(manifest.get("schema_version", 1)) >= 3 else None
        entries: dict[str, AnalysisEntry] = {}
        if analysis is not None:
            try:
                data = store.get(analysis["manifest_key"])
            except StorageError as exc:
                raise SnapshotUnavailable(f"analysis manifest: {exc}") from None
            if _sha(data) != analysis["manifest_sha256"]:
                raise SnapshotUnavailable("the analysis manifest does not match the snapshot")
            pinned = AnalysisManifest.model_validate(json.loads(data))
            entries = {e.security_id: e for e in pinned.entries}
        explained = (
            manifest.get("explanations") if int(manifest.get("schema_version", 1)) >= 4 else None
        )
        explanation_entries: dict[str, ExplanationEntry] = {}
        if explained is not None:
            try:
                data = store.get(explained["manifest_key"])
            except StorageError as exc:
                raise SnapshotUnavailable(f"explanation manifest: {exc}") from None
            if _sha(data) != explained["manifest_sha256"]:
                raise SnapshotUnavailable("the explanation manifest does not match the snapshot")
            pinned_ex = ExplanationManifest.model_validate(json.loads(data))
            explanation_entries = {e.security_id: e for e in pinned_ex.entries}
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
            analysis=analysis,
            analysis_entries=entries,
            explanations=explained,
            explanation_entries=explanation_entries,
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
