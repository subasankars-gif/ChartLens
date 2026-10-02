"""The API's view of the lake: one verified serving snapshot at a time (ADR-0016).

The in-memory metadata is keyed by ``(exchange, meta_version)`` and swapped whole: a
request sees exactly one snapshot, never a mixture. Every ``snapshot_refresh_seconds``
the provider checks the snapshot pointer; a new version is loaded and verified *before*
it replaces the old one, so a half-published snapshot is never served. A weekly file
that no longer matches the snapshot (a newer run republished it) triggers an immediate
reload; if the reloaded snapshot still does not match, the request fails with 503.

Read-only: this module never builds, adjusts or writes anything.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict

from chartlens_core.weekly import WeeklyBar
from chartlens_pipeline.serving import ServingSnapshot, SnapshotUnavailable, StaleSnapshot
from chartlens_pipeline.storage import ObjectStore

log = logging.getLogger("chartlens.api.lake")


class LakeUnavailable(RuntimeError):
    """No consistent snapshot can be served right now (→ 503)."""


class SnapshotProvider:
    def __init__(
        self,
        store: ObjectStore,
        exchange: str,
        *,
        refresh_seconds: float = 60.0,
        weekly_cache_size: int = 512,
        clock: object = time.monotonic,
    ) -> None:
        self.store = store
        self.exchange = exchange
        self.refresh_seconds = refresh_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._snapshot: ServingSnapshot | None = None
        self._checked_at = float("-inf")
        self.loaded_at: float | None = None
        self._weekly: OrderedDict[tuple[str, str, str], list[WeeklyBar]] = OrderedDict()
        self._weekly_cache_size = weekly_cache_size

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
            except (SnapshotUnavailable, OSError) as exc:
                # Keep serving the previous complete snapshot; never a partial one.
                log.warning("serving snapshot not loaded: %s", exc)
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
