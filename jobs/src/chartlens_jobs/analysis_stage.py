"""The ANALYSIS stage (ADR-0024 §3, ADR-0025).

For the analytical universe of one weekly version, each security's analysis is reused
or computed, written as content-addressed artifacts, and listed in an analysis manifest
that is written last and only if the stage succeeded in full.

- **Coverage.** The expected universe is derived once from the published data-quality
  status. The manifest is generated from the completed result set, never from the
  expected list, and is written only if the two are equal (no missing, duplicate or
  unexpected security).
- **Reuse** (ADR-0025 §3.3). A previous result is reused only when the weekly file is the
  one the current weekly manifest names, the dependency fingerprint built from the bars
  read from it equals the previous entry's key, and every artifact it names still
  exists.
- **Recompute check** (§3.4). The reused securities with the smallest
  ``SHA-256(weekly_version | security_id)`` (at most 32) are recomputed and must match
  the stored canonical bytes exactly.
- **Failure** (amendment F). Anything wrong fails the whole stage: pending work is
  cancelled and no manifest is written, so the previous one and the live snapshot stay
  as they were. Written artifacts are inert until a published manifest names them.
"""

from __future__ import annotations

import hashlib
import logging
import multiprocessing
import os
import time
from collections.abc import Iterator
from concurrent.futures import Future, ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Final, Literal

import pyarrow as pa
import pyarrow.parquet as pq

from chartlens_core.canonical import CANONICAL_SERIALIZATION_VERSION, canonical_json
from chartlens_core.config import AnalysisConfig, ChartLensSettings
from chartlens_core.domain import SecurityId, Timeframe
from chartlens_core.logs import log_event
from chartlens_core.weekly import to_bar_frame
from chartlens_engine.analysis import (
    DATASETS,
    DOCUMENT_SCHEMA_VERSION,
    EVENT_SCHEMA_VERSION,
    AnalysisInputs,
    analysis_version,
    analyze_security,
    bars_content_hash,
    serialize,
)
from chartlens_engine.interfaces import AnalysisContext
from chartlens_jobs.fingerprint import REUSE_KEY_VERSION, DependencyFingerprint, runtime_versions
from chartlens_pipeline.analysis_store import (
    UNIVERSE_RULE_VERSION,
    AnalysisEntry,
    AnalysisInputSet,
    AnalysisManifest,
    AnalysisStoreError,
    EventArtifact,
    analysis_set_hash,
    read_document,
    read_events,
    read_manifest,
    universe_sha256,
    write_document,
    write_events,
    write_manifest,
)
from chartlens_pipeline.storage import (
    DataLakeLayout,
    GcsObjectStore,
    LocalObjectStore,
    ObjectStore,
    StorageError,
)
from chartlens_pipeline.weekly import bars_from_table

log = logging.getLogger("chartlens.jobs.analysis")

RECOMPUTE_SAMPLE_SIZE: Final = 32
SAMPLE_SELECTION_VERSION: Final = "1"
"""Locked verification methodology (ADR-0025 §3.4): recorded in the manifest, not a
setting, and outside ``analysis_version``."""


class AnalysisStageFailed(RuntimeError):
    """The stage stopped; no manifest was written."""


# ----------------------------------------------------------------------------- stores


class OverlayObjectStore:
    """Reads from ``base`` where ``overlay`` has nothing; writes only to ``overlay``. For
    rehearsals against production inputs that must stay read-only towards the lake."""

    def __init__(self, overlay: ObjectStore, base: ObjectStore) -> None:
        self.overlay = overlay
        self.base = base

    def put(self, key: str, data: bytes) -> None:
        self.overlay.put(key, data)

    def put_immutable(self, key: str, data: bytes) -> bool:
        return self.overlay.put_immutable(key, data)

    def get(self, key: str) -> bytes:
        if self.overlay.exists(key):
            return self.overlay.get(key)
        return self.base.get(key)

    def exists(self, key: str) -> bool:
        return self.overlay.exists(key) or self.base.exists(key)

    def list(self, prefix: str) -> list[str]:
        return sorted(set(self.overlay.list(prefix)) | set(self.base.list(prefix)))

    def delete(self, key: str) -> None:
        self.overlay.delete(key)


@dataclass(frozen=True)
class StoreSpec:
    """A picklable description of a store, so each worker process opens its own."""

    kind: Literal["local", "gcs", "overlay"]
    root: str | None = None
    bucket: str | None = None

    @classmethod
    def from_settings(cls, settings: ChartLensSettings) -> StoreSpec:
        if settings.storage.backend == "gcs":
            return cls("gcs", bucket=settings.storage.gcs_bucket)
        return cls("local", root=str(settings.storage.local_root))

    def open(self) -> ObjectStore:
        if self.kind == "gcs":
            if not self.bucket:
                raise StorageError("a gcs store needs a bucket")
            return GcsObjectStore(self.bucket)
        if not self.root:
            raise StorageError(f"a {self.kind} store needs a root")
        local = LocalObjectStore(Path(self.root))
        if self.kind == "overlay":
            if not self.bucket:
                raise StorageError("an overlay store needs a base bucket")
            return OverlayObjectStore(local, GcsObjectStore(self.bucket))
        return local


# ----------------------------------------------------------------------------- per security


@dataclass(frozen=True)
class SecurityTask:
    exchange: str
    security_id: str
    weekly_file_sha256: str
    segment_id: str
    segment_start: date
    usable_from: date | None
    data_methodology_hash: str
    weekly_schema_version: str
    weekly_builder_version: str
    previous: AnalysisEntry | None
    """The previous manifest's entry, when every artifact it names still exists."""


@dataclass(frozen=True)
class SecurityOutcome:
    entry: AnalysisEntry
    computed: bool
    document_bytes: int = 0


@dataclass
class _Worker:
    store: ObjectStore
    config: AnalysisConfig
    analysis_version: str
    runtime: dict[str, str]
    allow_reuse: bool


_worker_state: _Worker | None = None


def _init_worker(
    spec: StoreSpec,
    config: dict[str, Any],
    version: str,
    runtime: dict[str, str],
    allow_reuse: bool,
) -> None:
    global _worker_state
    _worker_state = _Worker(
        spec.open(), AnalysisConfig.model_validate(config), version, runtime, allow_reuse
    )


def _worker() -> _Worker:
    if _worker_state is None:  # pragma: no cover - a pool always runs the initializer
        raise RuntimeError("analysis worker not initialised")
    return _worker_state


def _fail(task: SecurityTask, reason: str) -> AnalysisStageFailed:
    return AnalysisStageFailed(f"{task.security_id}: {reason}")


@dataclass(frozen=True)
class _Prepared:
    frame: Any
    context: AnalysisContext
    inputs: AnalysisInputs
    bars_sha256: str
    reuse_key: str


def _prepare(task: SecurityTask) -> _Prepared:
    """Read and verify the weekly file, take the current segment, fingerprint it."""
    w = _worker()
    data = w.store.get(DataLakeLayout.curated_weekly_key(task.exchange, task.security_id))
    if hashlib.sha256(data).hexdigest() != task.weekly_file_sha256:
        raise _fail(task, "the weekly file does not match the weekly manifest")
    bars = bars_from_table(pq.read_table(pa.BufferReader(data)))
    if not bars:
        raise _fail(task, "the weekly file has no bars")
    if bars[-1].continuity_segment_id != task.segment_id:
        raise _fail(
            task,
            f"the file's current segment {bars[-1].continuity_segment_id} is not the "
            f"data-quality current segment {task.segment_id}",
        )
    current = [b for b in bars if b.continuity_segment_id == task.segment_id]
    if current[0].first_session_date != task.segment_start:
        raise _fail(
            task,
            f"the segment's first bar starts {current[0].first_session_date}, "
            f"not at the segment start {task.segment_start}",
        )
    frame = to_bar_frame(current, task.security_id)
    context = AnalysisContext(
        security_id=SecurityId(task.security_id),
        timeframe=Timeframe.WEEKLY,
        as_of=current[-1].last_session_date,
        methodology_hash=task.data_methodology_hash,
        continuity_segment_id=task.segment_id,
    )
    inputs = AnalysisInputs(
        exchange=task.exchange,
        weekly_schema_version=task.weekly_schema_version,
        weekly_builder_version=task.weekly_builder_version,
        usable_from=task.usable_from,
    )
    bars_sha = bars_content_hash(frame)
    key = DependencyFingerprint(
        context=context,
        bars_sha256=bars_sha,
        inputs=inputs,
        analysis_version=w.analysis_version,
        runtime=w.runtime,
    ).reuse_key()
    return _Prepared(frame, context, inputs, bars_sha, key)


def _compute(task: SecurityTask, p: _Prepared) -> tuple[Any, Any]:
    w = _worker()
    try:
        analysis = analyze_security(p.frame, p.context, w.config, p.inputs)
    except Exception as exc:
        raise _fail(task, f"analysis failed ({type(exc).__name__}: {exc})") from exc
    if analysis.inputs.bars_sha256 != p.bars_sha256:
        raise _fail(task, "the document's bars_sha256 differs from the job layer's")
    return analysis, serialize(analysis)


def analyse_security(task: SecurityTask) -> SecurityOutcome:
    """Reuse or compute one security (runs in a worker)."""
    w = _worker()
    p = _prepare(task)
    prev = task.previous
    if w.allow_reuse and prev is not None and prev.reuse_key == p.reuse_key:
        entry = prev.model_copy(update={"weekly_file_sha256": task.weekly_file_sha256})
        return SecurityOutcome(entry, computed=False)
    _, stored = _compute(task, p)
    try:
        address = write_document(w.store, task.exchange, stored.document)
        events: dict[str, EventArtifact] = {}
        for name in DATASETS:
            ds = stored.events[name]
            digest, physical = write_events(w.store, task.exchange, name, ds.rows, ds.metadata)
            if digest != ds.content_sha256:
                raise _fail(task, f"{name}: the stored content hash differs from the document's")
            events[name] = EventArtifact(
                content_sha256=digest, physical_sha256=physical, row_count=ds.row_count
            )
    except (AnalysisStoreError, StorageError) as exc:
        raise _fail(task, f"writing failed ({exc})") from exc
    if address != stored.document_sha256:  # pragma: no cover - same bytes, same hash
        raise _fail(task, "the stored address differs from the document hash")
    entry = AnalysisEntry(
        security_id=task.security_id,
        continuity_segment_id=task.segment_id,
        reuse_key=p.reuse_key,
        weekly_file_sha256=task.weekly_file_sha256,
        bars_sha256=p.bars_sha256,
        document_sha256=address,
        events=events,
    )
    return SecurityOutcome(entry, computed=True, document_bytes=len(stored.document))


def recompute_security(task: SecurityTask, entry: AnalysisEntry) -> str | None:
    """Recompute a reused result and compare it with the stored artifacts byte for byte:
    the stored document's canonical bytes against the fresh ones, the stored events'
    canonical rows against the fresh rows, and so the content hashes. Returns a
    description of the difference, or None (runs in a worker)."""
    w = _worker()
    p = _prepare(task)
    if p.reuse_key != entry.reuse_key:
        return "its reuse key changed between the two passes"
    _, fresh = _compute(task, p)
    try:
        stored = read_document(w.store, task.exchange, entry.document_sha256)
        stored_events = {
            name: read_events(w.store, task.exchange, name, entry.events[name].content_sha256)[0]
            for name in DATASETS
        }
    except (AnalysisStoreError, StorageError) as exc:
        return f"its stored artifacts cannot be read back ({exc})"
    if stored != fresh.document or fresh.document_sha256 != entry.document_sha256:
        return "the recomputed document differs from the stored one"
    for name in DATASETS:
        if canonical_json(stored_events[name]) != canonical_json(fresh.events[name].rows):
            return f"the recomputed {name} differ from the stored ones"
        if fresh.events[name].content_sha256 != entry.events[name].content_sha256:
            return f"the recomputed {name} content hash differs"  # pragma: no cover
    return None


def guarded_analyse(task: SecurityTask) -> SecurityOutcome:
    """Any failure names its security and is a stage failure."""
    try:
        return analyse_security(task)
    except AnalysisStageFailed:
        raise
    except Exception as exc:
        raise _fail(task, f"{type(exc).__name__}: {exc}") from exc


def guarded_recompute(task: SecurityTask, entry: AnalysisEntry) -> str | None:
    try:
        return recompute_security(task, entry)
    except AnalysisStageFailed:
        raise
    except Exception as exc:
        raise _fail(task, f"recompute failed ({type(exc).__name__}: {exc})") from exc


# ----------------------------------------------------------------------------- the stage


@dataclass
class AnalysisSummary:
    manifest: AnalysisManifest
    computed: int
    reused: int
    sample: list[str]
    document_mb: float
    seconds: float
    details: dict[str, str | int | float | bool | None] = field(default_factory=dict)


def sample_of(reused: list[str], weekly_version: str) -> list[str]:
    """ADR-0025 §3.4: the reused securities with the smallest
    SHA-256(``weekly_version|security_id``), at most ``RECOMPUTE_SAMPLE_SIZE``; chosen from
    the complete reused set, so independent of processing order."""

    def rank(sid: str) -> str:
        return hashlib.sha256(f"{weekly_version}|{sid}".encode()).hexdigest()

    return sorted(sorted(set(reused), key=rank)[:RECOMPUTE_SAMPLE_SIZE])


class AnalysisStage:
    def __init__(
        self,
        settings: ChartLensSettings,
        exchange: str,
        spec: StoreSpec,
        *,
        workers: int | None = None,
        allow_reuse: bool = True,
    ) -> None:
        self.settings = settings
        self.exchange = exchange
        self.spec = spec
        configured = settings.jobs.analysis_workers if workers is None else workers
        self.workers = configured or os.cpu_count() or 1
        self.allow_reuse = allow_reuse
        self.config = settings.analysis
        self.analysis_version = analysis_version(self.config)
        self.runtime = runtime_versions()

    # -- the parts a test may replace -------------------------------------------------

    def _init_args(self) -> tuple[Any, ...]:
        return (
            self.spec,
            self.config.model_dump(mode="json"),
            self.analysis_version,
            self.runtime,
            self.allow_reuse,
        )

    def _execute(
        self, pool: ProcessPoolExecutor | None, tasks: list[SecurityTask]
    ) -> list[SecurityOutcome]:
        """One outcome per task that finished. Any failure cancels the rest and raises."""
        if pool is None:
            return [guarded_analyse(t) for t in tasks]
        return list(self._gather(pool, {pool.submit(guarded_analyse, t): t for t in tasks}))

    def _recompute(
        self, pool: ProcessPoolExecutor | None, items: list[tuple[SecurityTask, AnalysisEntry]]
    ) -> dict[str, str | None]:
        if pool is None:
            return {t.security_id: guarded_recompute(t, e) for t, e in items}
        futures = {pool.submit(guarded_recompute, t, e): t for t, e in items}
        out: dict[str, str | None] = {}
        for fut in as_completed(futures):
            out[futures[fut].security_id] = self._result(pool, fut, futures[fut])
        return out

    @staticmethod
    def _result(pool: ProcessPoolExecutor, fut: Future[Any], task: SecurityTask) -> Any:
        try:
            return fut.result()
        except AnalysisStageFailed:
            pool.shutdown(wait=False, cancel_futures=True)
            raise
        except Exception as exc:
            pool.shutdown(wait=False, cancel_futures=True)
            raise AnalysisStageFailed(f"{task.security_id}: {type(exc).__name__}: {exc}") from exc

    def _gather(
        self, pool: ProcessPoolExecutor, futures: dict[Future[SecurityOutcome], SecurityTask]
    ) -> Iterator[SecurityOutcome]:
        for fut in as_completed(futures):
            yield self._result(pool, fut, futures[fut])

    # -- the stage --------------------------------------------------------------------

    def run(self) -> AnalysisSummary:
        started = time.monotonic()
        ex = self.exchange
        store = self.spec.open()
        try:
            inputs = AnalysisInputSet(store, ex)
        except AnalysisStoreError as exc:
            raise AnalysisStageFailed(str(exc)) from exc
        weekly = inputs.weekly
        expected = inputs.universe()
        tasks = self._tasks(store, inputs, expected)

        pool: ProcessPoolExecutor | None = None
        if self.workers > 1:
            pool = ProcessPoolExecutor(
                max_workers=self.workers,
                mp_context=multiprocessing.get_context("spawn"),
                initializer=_init_worker,
                initargs=self._init_args(),
            )
        else:
            _init_worker(*self._init_args())
        try:
            outcomes = self._execute(pool, tasks)
            by_id = self._covering(outcomes, expected)
            reused = [sid for sid, o in by_id.items() if not o.computed]
            sample = sample_of(reused, str(weekly["weekly_version"]))
            task_of = {t.security_id: t for t in tasks}
            mismatches = {
                sid: why
                for sid, why in self._recompute(
                    pool, [(task_of[sid], by_id[sid].entry) for sid in sample]
                ).items()
                if why is not None
            }
        finally:
            if pool is not None:
                pool.shutdown(wait=True, cancel_futures=True)
        if mismatches:
            listed = "; ".join(f"{sid}: {why}" for sid, why in sorted(mismatches.items()))
            raise AnalysisStageFailed(f"recompute check failed for {listed}")

        entries = [by_id[sid].entry for sid in sorted(by_id)]
        universe = [e.security_id for e in entries]
        if universe_sha256(universe) != universe_sha256(expected):  # pragma: no cover
            raise AnalysisStageFailed("the result set's universe hash differs from the expected")
        manifest = AnalysisManifest(
            exchange=ex,
            weekly_version=str(weekly["weekly_version"]),
            dq_version=inputs.dq_version,
            as_of=str(weekly["as_of"]),
            methodology_hash=str(weekly["methodology_hash"]),
            analysis_version=self.analysis_version,
            analysis_methodology_hash=self.config.methodology_hash(),
            document_schema_version=DOCUMENT_SCHEMA_VERSION,
            canonical_serialization_version=CANONICAL_SERIALIZATION_VERSION,
            event_schema_version=EVENT_SCHEMA_VERSION,
            runtime=self.runtime,
            reuse_key_version=REUSE_KEY_VERSION,
            universe_rule_version=UNIVERSE_RULE_VERSION,
            recompute_sample_size=RECOMPUTE_SAMPLE_SIZE,
            sample_selection_version=SAMPLE_SELECTION_VERSION,
            universe=universe,
            universe_sha256=universe_sha256(universe),
            entries=entries,
            analysis_set_hash=analysis_set_hash(entries),
        )
        try:
            write_manifest(store, manifest)
        except AnalysisStoreError as exc:  # pragma: no cover - validated above
            raise AnalysisStageFailed(str(exc)) from exc
        computed = sum(1 for o in by_id.values() if o.computed)
        summary = AnalysisSummary(
            manifest=manifest,
            computed=computed,
            reused=len(by_id) - computed,
            sample=sample,
            document_mb=round(sum(o.document_bytes for o in by_id.values()) / 1e6, 1),
            seconds=round(time.monotonic() - started, 1),
        )
        summary.details = {
            "securities": len(universe),
            "computed": summary.computed,
            "reused": summary.reused,
            "recompute_sample": len(sample),
            "recompute_mismatches": 0,
            "universe_sha256": manifest.universe_sha256[:12],
            "analysis_set_hash": manifest.analysis_set_hash[:12],
            "document_mb_written": summary.document_mb,
            "workers": self.workers,
        }
        log_event(log, "analysis.completed", details=summary.details, seconds=summary.seconds)
        return summary

    def _tasks(
        self, store: ObjectStore, inputs: AnalysisInputSet, expected: list[str]
    ) -> list[SecurityTask]:
        ex = self.exchange
        weekly = inputs.weekly
        files = inputs.files
        previous = self._previous(store)
        tasks: list[SecurityTask] = []
        for sid in expected:
            if sid not in files:
                raise AnalysisStageFailed(f"{sid}: no weekly file in the weekly manifest")
            segment = inputs.current_segment.get(sid)
            if segment is None:
                raise AnalysisStageFailed(f"{sid}: no continuity segment")
            usable_from = inputs.status[sid]["usable_from"]
            if usable_from != segment["segment_start"]:
                raise AnalysisStageFailed(
                    f"{sid}: usable_from {usable_from} is not the current segment's start "
                    f"{segment['segment_start']}"
                )
            tasks.append(
                SecurityTask(
                    exchange=ex,
                    security_id=sid,
                    weekly_file_sha256=files[sid],
                    segment_id=str(segment["continuity_segment_id"]),
                    segment_start=segment["segment_start"],
                    usable_from=usable_from,
                    data_methodology_hash=str(weekly["methodology_hash"]),
                    weekly_schema_version=str(weekly["schema_version"]),
                    weekly_builder_version=str(weekly["builder_version"]),
                    previous=previous.get(sid),
                )
            )
        return tasks

    def _previous(self, store: ObjectStore) -> dict[str, AnalysisEntry]:
        """The latest complete manifest's entries whose artifacts all still exist. An
        unreadable manifest only costs reuse: everything is computed."""
        if not self.allow_reuse:
            return {}
        try:
            manifest = read_manifest(store, self.exchange)
        except Exception:
            log.exception("the previous analysis manifest is unreadable; computing everything")
            return {}
        if manifest is None:
            return {}
        ex = self.exchange
        documents = set(store.list(DataLakeLayout.serving_analysis_prefix(ex)))
        events = {n: set(store.list(DataLakeLayout.serving_events_prefix(ex, n))) for n in DATASETS}

        def present(e: AnalysisEntry) -> bool:
            if DataLakeLayout.serving_analysis_key(ex, e.document_sha256) not in documents:
                return False
            return all(
                n in e.events
                and DataLakeLayout.serving_events_key(ex, n, e.events[n].content_sha256)
                in events[n]
                for n in DATASETS
            )

        return {e.security_id: e for e in manifest.entries if present(e)}

    @staticmethod
    def _covering(
        outcomes: list[SecurityOutcome], expected: list[str]
    ) -> dict[str, SecurityOutcome]:
        """The result set must equal the expected universe exactly (ADR-0025 §4)."""
        by_id: dict[str, SecurityOutcome] = {}
        duplicates: list[str] = []
        for o in outcomes:
            sid = o.entry.security_id
            if sid in by_id:
                duplicates.append(sid)
            by_id[sid] = o
        if duplicates:
            raise AnalysisStageFailed(f"duplicate results for {sorted(set(duplicates))[:5]}")
        missing = sorted(set(expected) - set(by_id))
        unexpected = sorted(set(by_id) - set(expected))
        if missing:
            raise AnalysisStageFailed(f"no result for {len(missing)} securities: {missing[:5]}")
        if unexpected:
            raise AnalysisStageFailed(f"results outside the universe: {unexpected[:5]}")
        return by_id
