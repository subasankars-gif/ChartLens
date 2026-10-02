"""Durable run state and snapshot history in Firestore (ADR-0018).

Operational metadata only — never market data:

    runs/{run_id}                 one production run, its stages inside
    ops/active_run                {run_id, acquired_at}: the single-active-run lock
    ops/last_successful_run       {run_id, completed_at}
    snapshots/{meta_version}      one serving snapshot as published

Every decision that touches the lock is made by the pure functions below and applied
atomically — in a Firestore transaction, or under a mutex in :class:`MemoryRunStore` —
so the two stores cannot disagree about who may run.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

from chartlens_core.runs import (
    LOST_AFTER,
    RunRecord,
    RunStatus,
    SnapshotRecord,
    end_run,
    start_run,
)

RUNS = "runs"
OPS = "ops"
SNAPSHOTS = "snapshots"
LOCK_DOC = "active_run"
LAST_SUCCESS_DOC = "last_successful_run"


class ActiveRunExists(RuntimeError):
    """Another production run holds the lock."""

    def __init__(self, run: RunRecord) -> None:
        super().__init__(f"run {run.run_id} is {run.status}")
        self.run = run


class RunNotClaimable(RuntimeError):
    """The run cannot be started by this workflow (finished, superseded, or not locked)."""


class RunNotFound(KeyError):
    pass


# ----------------------------------------------------------------------------- decisions


@dataclass
class Decision:
    """What one atomic operation writes."""

    runs: list[RunRecord] = field(default_factory=list)
    lock: str | None = None
    """Run id to hold the lock afterwards."""
    release: bool = False
    last_success: RunRecord | None = None


def _expire_holder(
    holder: RunRecord | None, now: datetime, lost_after: timedelta
) -> tuple[RunRecord | None, list[RunRecord]]:
    """The lock holder, unless it is finished or lost (then the lock is free)."""
    if holder is None or not holder.active:
        return None, []
    if holder.is_lost(now, lost_after):
        lost = end_run(
            holder,
            now,
            RunStatus.FAILED,
            f"lost: no progress for {lost_after.total_seconds() / 3600:g} h; lock released",
        )
        return None, [lost]
    return holder, []


def decide_create(
    holder: RunRecord | None,
    new: RunRecord,
    now: datetime,
    *,
    lost_after: timedelta = LOST_AFTER,
    supersede_queued: bool = False,
) -> Decision:
    holder, writes = _expire_holder(holder, now, lost_after)
    if holder is not None:
        if supersede_queued and holder.status == RunStatus.QUEUED:
            writes.append(end_run(holder, now, RunStatus.CANCELLED, f"superseded by {new.run_id}"))
        else:
            raise ActiveRunExists(holder)
    return Decision(runs=[*writes, new], lock=new.run_id)


def decide_claim(
    run: RunRecord | None,
    holder_id: str | None,
    now: datetime,
    *,
    github_run_id: str | None,
    github_run_attempt: int | None,
    lost_after: timedelta = LOST_AFTER,
) -> Decision:
    if run is None:
        raise RunNotClaimable("no such run")
    if run.status != RunStatus.QUEUED:
        raise RunNotClaimable(f"run {run.run_id} is already {run.status}")
    if run.is_lost(now, lost_after):
        lost = end_run(run, now, RunStatus.FAILED, "lost: never started; lock released")
        return Decision(runs=[lost], release=holder_id == run.run_id)
    if holder_id != run.run_id:
        raise RunNotClaimable(f"run {run.run_id} does not hold the lock")
    started = start_run(
        run, now, github_run_id=github_run_id, github_run_attempt=github_run_attempt
    )
    return Decision(runs=[started], lock=run.run_id)


def decide_close(stored: RunRecord | None, run: RunRecord, holder_id: str | None) -> Decision:
    """Persist a finished run and release the lock if it still holds it. Only the
    process that owns the running run may close it (see :func:`decide_save`)."""
    decide_save(stored, run)
    return Decision(
        runs=[run],
        release=holder_id == run.run_id,
        last_success=run if run.status == RunStatus.SUCCEEDED else None,
    )


def decide_end(
    run: RunRecord | None,
    holder_id: str | None,
    now: datetime,
    status: RunStatus,
    reason: str,
    *,
    github_run_id: str | None,
) -> Decision:
    """Close an active run from outside (finalizer, dispatch failure). With
    ``github_run_id``, only a run that belongs to that GitHub run — or a QUEUED run that
    no workflow has claimed yet — is touched."""
    if run is None or not run.active:
        return Decision()
    if github_run_id is not None and run.github_run_id not in (None, github_run_id):
        return Decision()
    ended = end_run(run, now, status, reason)
    return Decision(runs=[ended], release=holder_id == run.run_id)


def decide_save(stored: RunRecord | None, run: RunRecord) -> Decision:
    """Progress is recorded only by the process that owns the run: it must still be
    RUNNING under the same GitHub run. A run closed elsewhere (lost, cancelled) is never
    brought back to life by a late write."""
    if stored is None or stored.status != RunStatus.RUNNING:
        raise RunNotClaimable(f"run {run.run_id} is no longer running here")
    if stored.github_run_id != run.github_run_id:
        raise RunNotClaimable(f"run {run.run_id} belongs to another workflow run")
    return Decision(runs=[run])


def decide_cancel_queued(
    run: RunRecord | None, holder_id: str | None, now: datetime, reason: str
) -> Decision:
    """An admin cancels a run that no workflow has started (ADR-0018). A running run is
    cancelled in GitHub Actions instead, where its finalizer records it."""
    if run is None:
        raise RunNotFound("no such run")
    if run.status != RunStatus.QUEUED:
        raise RunNotClaimable(
            f"run {run.run_id} is {run.status}; only a queued run can be cancelled here"
        )
    ended = end_run(run, now, RunStatus.CANCELLED, reason)
    return Decision(runs=[ended], release=holder_id == run.run_id)


# ----------------------------------------------------------------------------- stores


class RunStore(Protocol):
    def create(
        self,
        run: RunRecord,
        now: datetime,
        *,
        lost_after: timedelta = LOST_AFTER,
        supersede_queued: bool = False,
    ) -> RunRecord:
        """Take the lock and store ``run``; raise :class:`ActiveRunExists` if held."""
        ...

    def claim(
        self,
        run_id: str,
        now: datetime,
        *,
        github_run_id: str | None,
        github_run_attempt: int | None,
        lost_after: timedelta = LOST_AFTER,
    ) -> RunRecord:
        """QUEUED → RUNNING for the workflow that was dispatched to run it."""
        ...

    def save(self, run: RunRecord) -> None:
        """Record progress of a running run (stages, heartbeat); raise
        :class:`RunNotClaimable` if it is no longer this process's run."""
        ...

    def cancel_queued(self, run_id: str, now: datetime, reason: str) -> RunRecord:
        """Cancel a run no workflow has started, and release the lock."""
        ...

    def close(self, run: RunRecord) -> None:
        """Store a finished run and release the lock."""
        ...

    def end_active(
        self,
        run_id: str,
        now: datetime,
        status: RunStatus,
        reason: str,
        *,
        github_run_id: str | None = None,
    ) -> RunRecord | None: ...

    def expire_lost(self, now: datetime, lost_after: timedelta = LOST_AFTER) -> RunRecord | None:
        """Fail and unlock a lost lock holder; return it, or None."""
        ...

    def get(self, run_id: str) -> RunRecord | None: ...

    def list(self, limit: int, before: datetime | None = None) -> list[RunRecord]:
        """Most recent first (by ``requested_at``), strictly before ``before``."""
        ...

    def active(self) -> RunRecord | None: ...

    def last_successful(self) -> RunRecord | None: ...

    # snapshot history
    def stage_snapshot(self, snapshot: SnapshotRecord) -> None: ...

    def publish_snapshot(self, snapshot_id: str, now: datetime) -> None: ...

    def get_snapshot(self, snapshot_id: str) -> SnapshotRecord | None: ...

    def list_snapshots(self, limit: int) -> list[SnapshotRecord]: ...


class MemoryRunStore:
    """In-process store with the same semantics (tests, local runs)."""

    def __init__(self) -> None:
        self._mutex = threading.RLock()
        self.runs: dict[str, RunRecord] = {}
        self.lock_holder: str | None = None
        self.last_success_id: str | None = None
        self.snapshots: dict[str, SnapshotRecord] = {}

    def _apply(self, d: Decision) -> None:
        for r in d.runs:
            self.runs[r.run_id] = r
        if d.release:
            self.lock_holder = None
        if d.lock is not None:
            self.lock_holder = d.lock
        if d.last_success is not None:
            self.last_success_id = d.last_success.run_id

    def _holder(self) -> RunRecord | None:
        return self.runs.get(self.lock_holder) if self.lock_holder else None

    def create(
        self,
        run: RunRecord,
        now: datetime,
        *,
        lost_after: timedelta = LOST_AFTER,
        supersede_queued: bool = False,
    ) -> RunRecord:
        with self._mutex:
            if run.run_id in self.runs:
                raise ValueError(f"run {run.run_id} already exists")
            self._apply(
                decide_create(
                    self._holder(),
                    run,
                    now,
                    lost_after=lost_after,
                    supersede_queued=supersede_queued,
                )
            )
            return run

    def claim(
        self,
        run_id: str,
        now: datetime,
        *,
        github_run_id: str | None,
        github_run_attempt: int | None,
        lost_after: timedelta = LOST_AFTER,
    ) -> RunRecord:
        with self._mutex:
            d = decide_claim(
                self.runs.get(run_id),
                self.lock_holder,
                now,
                github_run_id=github_run_id,
                github_run_attempt=github_run_attempt,
                lost_after=lost_after,
            )
            self._apply(d)
            claimed = d.runs[0]
            if claimed.status != RunStatus.RUNNING:
                raise RunNotClaimable(claimed.error_summary or "run is not claimable")
            return claimed

    def save(self, run: RunRecord) -> None:
        with self._mutex:
            self._apply(decide_save(self.runs.get(run.run_id), run))

    def cancel_queued(self, run_id: str, now: datetime, reason: str) -> RunRecord:
        with self._mutex:
            d = decide_cancel_queued(self.runs.get(run_id), self.lock_holder, now, reason)
            self._apply(d)
            return d.runs[0]

    def close(self, run: RunRecord) -> None:
        with self._mutex:
            self._apply(decide_close(self.runs.get(run.run_id), run, self.lock_holder))

    def end_active(
        self,
        run_id: str,
        now: datetime,
        status: RunStatus,
        reason: str,
        *,
        github_run_id: str | None = None,
    ) -> RunRecord | None:
        with self._mutex:
            d = decide_end(
                self.runs.get(run_id),
                self.lock_holder,
                now,
                status,
                reason,
                github_run_id=github_run_id,
            )
            self._apply(d)
            return d.runs[0] if d.runs else None

    def expire_lost(self, now: datetime, lost_after: timedelta = LOST_AFTER) -> RunRecord | None:
        with self._mutex:
            holder = self._holder()
            if holder is None:
                self.lock_holder = None
                return None
            live, writes = _expire_holder(holder, now, lost_after)
            if live is None:
                self._apply(Decision(runs=writes, release=True))
            return writes[0] if writes else None

    def get(self, run_id: str) -> RunRecord | None:
        return self.runs.get(run_id)

    def list(self, limit: int, before: datetime | None = None) -> list[RunRecord]:
        rows = sorted(self.runs.values(), key=lambda r: r.requested_at, reverse=True)
        if before is not None:
            rows = [r for r in rows if r.requested_at < before]
        return rows[:limit]

    def active(self) -> RunRecord | None:
        holder = self._holder()
        return holder if holder and holder.active else None

    def last_successful(self) -> RunRecord | None:
        return self.runs.get(self.last_success_id) if self.last_success_id else None

    def stage_snapshot(self, snapshot: SnapshotRecord) -> None:
        with self._mutex:
            existing = self.snapshots.get(snapshot.snapshot_id)
            if existing is None or existing.status == "STAGED":
                self.snapshots[snapshot.snapshot_id] = snapshot

    def publish_snapshot(self, snapshot_id: str, now: datetime) -> None:
        with self._mutex:
            snap = self.snapshots[snapshot_id]
            self.snapshots[snapshot_id] = snap.model_copy(
                update={"status": "PUBLISHED", "published_at": now}
            )

    def get_snapshot(self, snapshot_id: str) -> SnapshotRecord | None:
        return self.snapshots.get(snapshot_id)

    def list_snapshots(self, limit: int) -> list[SnapshotRecord]:
        return sorted(self.snapshots.values(), key=lambda s: s.staged_at, reverse=True)[:limit]


# ----------------------------------------------------------------------------- Firestore


_NATIVE_TIMESTAMPS = ("requested_at", "staged_at")
"""Stored as Firestore timestamps (ordered queries); everything else as JSON values."""


def _to_doc(model: RunRecord | SnapshotRecord) -> dict[str, Any]:
    doc: dict[str, Any] = model.model_dump(mode="json")
    native = model.model_dump(mode="python")
    for key in _NATIVE_TIMESTAMPS:
        if key in native:
            doc[key] = native[key]
    return doc


def _run(doc: dict[str, Any] | None) -> RunRecord | None:
    return RunRecord.model_validate(doc) if doc else None


class FirestoreRunStore:
    """Firestore-backed store. Honours ``FIRESTORE_EMULATOR_HOST`` (the client does)."""

    def __init__(self, project_id: str, client: Any | None = None) -> None:
        if client is None:
            from google.cloud import firestore

            client = firestore.Client(project=project_id)
        self._db = client

    # -- references
    def _run_ref(self, run_id: str) -> Any:
        return self._db.collection(RUNS).document(run_id)

    def _lock_ref(self) -> Any:
        return self._db.collection(OPS).document(LOCK_DOC)

    def _success_ref(self) -> Any:
        return self._db.collection(OPS).document(LAST_SUCCESS_DOC)

    def _snap_ref(self, snapshot_id: str) -> Any:
        return self._db.collection(SNAPSHOTS).document(snapshot_id)

    # -- transactions
    def _transact(self, body: Any) -> Any:
        from google.cloud import firestore

        return firestore.transactional(body)(self._db.transaction())

    def _read_lock(self, tx: Any) -> tuple[str | None, RunRecord | None]:
        lock = self._lock_ref().get(transaction=tx)
        holder_id = (lock.to_dict() or {}).get("run_id") if lock.exists else None
        holder = _run(self._run_ref(holder_id).get(transaction=tx).to_dict()) if holder_id else None
        return holder_id, holder

    def _write(self, tx: Any, d: Decision, now: datetime) -> None:
        for r in d.runs:
            tx.set(self._run_ref(r.run_id), _to_doc(r))
        if d.lock is not None:
            tx.set(self._lock_ref(), {"run_id": d.lock, "acquired_at": now})
        elif d.release:
            tx.delete(self._lock_ref())
        if d.last_success is not None:
            tx.set(
                self._success_ref(),
                {
                    "run_id": d.last_success.run_id,
                    "completed_at": d.last_success.completed_at,
                },
            )

    def create(
        self,
        run: RunRecord,
        now: datetime,
        *,
        lost_after: timedelta = LOST_AFTER,
        supersede_queued: bool = False,
    ) -> RunRecord:
        def body(tx: Any) -> None:
            _, holder = self._read_lock(tx)
            if self._run_ref(run.run_id).get(transaction=tx).exists:
                raise ValueError(f"run {run.run_id} already exists")
            d = decide_create(
                holder, run, now, lost_after=lost_after, supersede_queued=supersede_queued
            )
            self._write(tx, d, now)

        self._transact(body)
        return run

    def claim(
        self,
        run_id: str,
        now: datetime,
        *,
        github_run_id: str | None,
        github_run_attempt: int | None,
        lost_after: timedelta = LOST_AFTER,
    ) -> RunRecord:
        def body(tx: Any) -> RunRecord:
            holder_id, _ = self._read_lock(tx)
            run = _run(self._run_ref(run_id).get(transaction=tx).to_dict())
            d = decide_claim(
                run,
                holder_id,
                now,
                github_run_id=github_run_id,
                github_run_attempt=github_run_attempt,
                lost_after=lost_after,
            )
            self._write(tx, d, now)
            return d.runs[0]

        claimed: RunRecord = self._transact(body)
        if claimed.status != RunStatus.RUNNING:
            raise RunNotClaimable(claimed.error_summary or "run is not claimable")
        return claimed

    def save(self, run: RunRecord) -> None:
        def body(tx: Any) -> None:
            stored = _run(self._run_ref(run.run_id).get(transaction=tx).to_dict())
            self._write(tx, decide_save(stored, run), run.requested_at)

        self._transact(body)

    def cancel_queued(self, run_id: str, now: datetime, reason: str) -> RunRecord:
        def body(tx: Any) -> RunRecord:
            holder_id, _ = self._read_lock(tx)
            run = _run(self._run_ref(run_id).get(transaction=tx).to_dict())
            d = decide_cancel_queued(run, holder_id, now, reason)
            self._write(tx, d, now)
            return d.runs[0]

        cancelled: RunRecord = self._transact(body)
        return cancelled

    def close(self, run: RunRecord) -> None:
        def body(tx: Any) -> None:
            holder_id, _ = self._read_lock(tx)
            stored = _run(self._run_ref(run.run_id).get(transaction=tx).to_dict())
            d = decide_close(stored, run, holder_id)
            self._write(tx, d, run.completed_at or run.requested_at)

        self._transact(body)

    def end_active(
        self,
        run_id: str,
        now: datetime,
        status: RunStatus,
        reason: str,
        *,
        github_run_id: str | None = None,
    ) -> RunRecord | None:
        def body(tx: Any) -> RunRecord | None:
            holder_id, _ = self._read_lock(tx)
            run = _run(self._run_ref(run_id).get(transaction=tx).to_dict())
            d = decide_end(run, holder_id, now, status, reason, github_run_id=github_run_id)
            self._write(tx, d, now)
            return d.runs[0] if d.runs else None

        ended: RunRecord | None = self._transact(body)
        return ended

    def expire_lost(self, now: datetime, lost_after: timedelta = LOST_AFTER) -> RunRecord | None:
        def body(tx: Any) -> RunRecord | None:
            holder_id, holder = self._read_lock(tx)
            if holder_id is None:
                return None
            live, writes = _expire_holder(holder, now, lost_after)
            if live is None:
                self._write(tx, Decision(runs=writes, release=True), now)
            return writes[0] if writes else None

        expired: RunRecord | None = self._transact(body)
        return expired

    def get(self, run_id: str) -> RunRecord | None:
        return _run(self._run_ref(run_id).get().to_dict())

    def list(self, limit: int, before: datetime | None = None) -> list[RunRecord]:
        from google.cloud import firestore

        query = self._db.collection(RUNS).order_by(
            "requested_at", direction=firestore.Query.DESCENDING
        )
        if before is not None:
            query = query.where(filter=firestore.FieldFilter("requested_at", "<", before))
        return [RunRecord.model_validate(d.to_dict()) for d in query.limit(limit).stream()]

    def active(self) -> RunRecord | None:
        lock = self._lock_ref().get()
        if not lock.exists:
            return None
        run = self.get((lock.to_dict() or {})["run_id"])
        return run if run and run.active else None

    def last_successful(self) -> RunRecord | None:
        doc = self._success_ref().get()
        return self.get((doc.to_dict() or {})["run_id"]) if doc.exists else None

    def stage_snapshot(self, snapshot: SnapshotRecord) -> None:
        def body(tx: Any) -> None:
            ref = self._snap_ref(snapshot.snapshot_id)
            existing = ref.get(transaction=tx)
            if existing.exists and (existing.to_dict() or {}).get("status") == "PUBLISHED":
                return  # history is never rewritten
            tx.set(ref, _to_doc(snapshot))

        self._transact(body)

    def publish_snapshot(self, snapshot_id: str, now: datetime) -> None:
        self._snap_ref(snapshot_id).update({"status": "PUBLISHED", "published_at": now})

    def get_snapshot(self, snapshot_id: str) -> SnapshotRecord | None:
        doc = self._snap_ref(snapshot_id).get()
        return SnapshotRecord.model_validate(doc.to_dict()) if doc.exists else None

    def list_snapshots(self, limit: int) -> list[SnapshotRecord]:
        from google.cloud import firestore

        query = (
            self._db.collection(SNAPSHOTS)
            .order_by("staged_at", direction=firestore.Query.DESCENDING)
            .limit(limit)
        )
        return [SnapshotRecord.model_validate(d.to_dict()) for d in query.stream()]
