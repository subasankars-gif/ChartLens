"""The API's view of the lake: one verified serving snapshot at a time (ADR-0016).

The in-memory metadata is keyed by ``(exchange, meta_version)`` and swapped whole: a
request sees exactly one snapshot, never a mixture. Every ``snapshot_refresh_seconds``
the provider checks the snapshot pointer; a new version is loaded and verified *before*
it replaces the old one, so a half-published snapshot is never served. Weekly bars come
from the snapshot's immutable, content-hashed copies (ADR-0018), so a run in progress or
a failed run cannot change them. (A schema-1 snapshot, published before ADR-0018, reads
the curated files instead: one rewritten since triggers an immediate reload, then 503.)

Schema 3 (ADR-0026) adds the analysis set. Its documents and event files are fetched on
demand from the objects the snapshot's pinned analysis manifest names, verified against
their addresses on every read (a document must decompress to bytes hashing to its name;
an event file must hash to its content address and recorded bytes), and served as
stored. A missing or corrupt object reloads the pointer once, then 503, as weekly bars do.

Read-only: this module never builds, adjusts, analyses or writes anything.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from chartlens_core.weekly import WeeklyBar
from chartlens_pipeline.analysis_store import (
    AnalysisEntry,
    Dataset,
    EventArtifact,
    inspect_document,
    inspect_events,
)
from chartlens_pipeline.serving import ServingSnapshot, SnapshotUnavailable, StaleSnapshot
from chartlens_pipeline.storage import ObjectStore, StorageError

log = logging.getLogger("chartlens.api.lake")


class LakeUnavailable(RuntimeError):
    """No consistent snapshot can be served right now (→ 503)."""


class NoAnalysisInSnapshot(LookupError):
    """The live snapshot predates schema 3: it carries no analysis (→ 404)."""


class NotAnalysed(LookupError):
    """The security is outside the snapshot's analysed universe (→ 404)."""


class SnapshotProvider:
    def __init__(
        self,
        store: ObjectStore | Callable[[], ObjectStore],
        exchange: str,
        *,
        refresh_seconds: float = 60.0,
        weekly_cache_size: int = 512,
        analysis_cache_size: int = 32,
        clock: object = time.monotonic,
    ) -> None:
        self._store = store
        self.exchange = exchange
        self.refresh_seconds = refresh_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._snapshot: ServingSnapshot | None = None
        self._checked_at = float("-inf")
        self.loaded_at: float | None = None
        self._weekly: OrderedDict[tuple[str, str, str], list[WeeklyBar]] = OrderedDict()
        self._weekly_cache_size = weekly_cache_size
        self._analysis: OrderedDict[tuple[str, str, str], Any] = OrderedDict()
        self._analysis_cache_size = analysis_cache_size

    @property
    def store(self) -> ObjectStore:
        """The object store, opened on first use: a misconfigured lake makes the data
        routes answer 503 instead of stopping the service from starting."""
        if callable(self._store):
            self._store = self._store()
        return self._store

    def _now(self) -> float:
        return float(self._clock())  # type: ignore[operator]

    def get(self) -> ServingSnapshot:
        """The current snapshot, reloading when the pointer has moved."""
        if self._now() - self._checked_at >= self.refresh_seconds or self._snapshot is None:
            self.refresh()
        if self._snapshot is None:
            raise LakeUnavailable("no serving snapshot is available yet")
        return self._snapshot

    def refresh(self, *, force: bool = False) -> None:
        with self._lock:
            self._checked_at = self._now()
            try:
                current = ServingSnapshot.current_version(self.store, self.exchange)
                if current is None:
                    return
                if not force and self._snapshot and self._snapshot.meta_version == current:
                    return
                loaded = ServingSnapshot.load(self.store, self.exchange)
            except SnapshotUnavailable as exc:
                # Keep serving the previous complete snapshot; never a partial one.
                log.warning("serving snapshot not loaded: %s", exc)
                return
            except Exception:  # lake unreachable or misconfigured: same rule, louder
                log.exception("serving snapshot could not be read")
                return
            if self._snapshot is None or loaded.meta_version != self._snapshot.meta_version:
                log.info("serving snapshot %s loaded", loaded.meta_version)
            self._snapshot = loaded
            self.loaded_at = time.time()

    def weekly_bars(self, security_id: str) -> tuple[ServingSnapshot, list[WeeklyBar]]:
        """Stored weekly bars and the snapshot they belong to (the two always match)."""
        for attempt in (1, 2):
            snap = self.get()
            key = (snap.exchange, snap.meta_version, security_id)
            if key in self._weekly:
                self._weekly.move_to_end(key)
                return snap, self._weekly[key]
            try:
                bars = snap.weekly_bars(self.store, security_id)
            except (StorageError, SnapshotUnavailable):
                # A copy removed after a newer publication: this process may still hold
                # an older snapshot. Reload the pointer once before giving up.
                if attempt == 2:
                    raise LakeUnavailable("a weekly file is missing from the lake") from None
                self.refresh(force=True)
                continue
            except StaleSnapshot:
                if attempt == 2:
                    raise LakeUnavailable("the lake is being republished; retry shortly") from None
                self.refresh(force=True)
                continue
            if self._weekly_cache_size:
                self._weekly[key] = bars
                while len(self._weekly) > self._weekly_cache_size:
                    self._weekly.popitem(last=False)
            return snap, bars
        raise LakeUnavailable("unreachable")

    # ------------------------------------------------------------------ analysis (schema 3)

    def _entry(self, snap: ServingSnapshot, security_id: str) -> AnalysisEntry:
        if snap.analysis is None:
            raise NoAnalysisInSnapshot("the live snapshot carries no analysis (schema < 3)")
        entry = snap.analysis_entries.get(security_id)
        if entry is None:
            raise NotAnalysed(f"{security_id} is not in this snapshot's analysed universe")
        return entry

    def _cached(self, key: tuple[str, str, str], value: Any = None) -> Any:
        if value is None:
            found = self._analysis.get(key)
            if found is not None:
                self._analysis.move_to_end(key)
            return found
        if self._analysis_cache_size:
            self._analysis[key] = value
            while len(self._analysis) > self._analysis_cache_size:
                self._analysis.popitem(last=False)
        return value

    def analysis_document(
        self, security_id: str
    ) -> tuple[ServingSnapshot, AnalysisEntry, dict[str, Any]]:
        """The published document, verified against its address, and the snapshot and
        entry it belongs to (the three always match)."""
        for attempt in (1, 2):
            snap = self.get()
            entry = self._entry(snap, security_id)
            key = (snap.meta_version, security_id, "document")
            cached = self._cached(key)
            if cached is not None:
                return snap, entry, cached
            canonical, problem = inspect_document(self.store, snap.exchange, entry.document_sha256)
            if problem is not None or canonical is None:
                if attempt == 2:
                    raise LakeUnavailable("an analysis document is missing or corrupt")
                self.refresh(force=True)
                continue
            return snap, entry, self._cached(key, json.loads(canonical))
        raise LakeUnavailable("unreachable")

    def breakout_rows(
        self, security_id: str, dataset: Dataset
    ) -> tuple[ServingSnapshot, EventArtifact, list[dict[str, Any]]]:
        """A published event dataset's rows as stored, verified logically and
        physically, with the snapshot and artifact they belong to."""
        for attempt in (1, 2):
            snap = self.get()
            artifact = self._entry(snap, security_id).events[dataset]
            key = (snap.meta_version, security_id, dataset)
            cached = self._cached(key)
            if cached is not None:
                return snap, artifact, cached
            found, problem = inspect_events(self.store, snap.exchange, dataset, artifact)
            if problem is not None or found is None:
                if attempt == 2:
                    raise LakeUnavailable("an event file is missing or corrupt")
                self.refresh(force=True)
                continue
            return snap, artifact, self._cached(key, found[0])
        raise LakeUnavailable("unreachable")
