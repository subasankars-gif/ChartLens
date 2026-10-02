"""Run store contract (ADR-0018): one active run, claims, lost runs, finalizer guard,
history. The same tests run against the in-memory store and — under the Firebase
emulator, as in CI — against Firestore, so both enforce identical rules."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta

import pytest

from chartlens_core.runs import (
    RunRecord,
    RunStatus,
    SnapshotOutcome,
    SnapshotRecord,
    Stage,
    fail_stage,
    finish_stage,
    start_run,
    start_stage,
    succeed_run,
)
from chartlens_pipeline.runs import (
    ActiveRunExists,
    FirestoreRunStore,
    MemoryRunStore,
    RunNotClaimable,
    RunStore,
)

T0 = datetime(2026, 10, 2, 14, 45, tzinfo=UTC)
EMULATOR = os.environ.get("FIRESTORE_EMULATOR_HOST")

if os.environ.get("CHARTLENS_REQUIRE_EMULATORS") and not EMULATOR:
    raise RuntimeError("CHARTLENS_REQUIRE_EMULATORS is set but the emulators are not running")

BACKENDS = [
    "memory",
    pytest.param(
        "firestore",
        marks=[
            pytest.mark.emulator,
            pytest.mark.skipif(not EMULATOR, reason="Firestore emulator not running"),
        ],
    ),
]


@pytest.fixture(params=BACKENDS)
def store(request: pytest.FixtureRequest) -> Iterator[RunStore]:
    if request.param == "memory":
        yield MemoryRunStore()
    else:  # a fresh project per test: the emulator keeps them apart
        yield FirestoreRunStore(f"demo-runs-{uuid.uuid4().hex[:10]}")


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def api_run(run_id: str, minutes: float = 0) -> RunRecord:
    return RunRecord(
        run_id=run_id, trigger="api", requested_by="boss@x.com", requested_at=at(minutes)
    )


def scheduled(run_id: str, minutes: float) -> RunRecord:
    run = RunRecord(
        run_id=run_id, trigger="schedule", requested_by="schedule", requested_at=at(minutes)
    )
    return start_run(run, at(minutes), github_run_id=run_id, github_run_attempt=1)


def test_at_most_one_active_run(store: RunStore) -> None:
    store.create(api_run("run-a"), at(0))
    with pytest.raises(ActiveRunExists) as err:
        store.create(api_run("run-b", 1), at(1))
    assert err.value.run.run_id == "run-a"
    active = store.active()
    assert active is not None and active.run_id == "run-a"
    assert store.get("run-b") is None


def test_claim_starts_the_queued_run_once(store: RunStore) -> None:
    store.create(api_run("run-a"), at(0))
    run = store.claim("run-a", at(2), github_run_id="777", github_run_attempt=1)
    assert (run.status, run.github_run_id, run.started_at) == (RunStatus.RUNNING, "777", at(2))
    with pytest.raises(RunNotClaimable, match="already RUNNING"):
        store.claim("run-a", at(3), github_run_id="778", github_run_attempt=1)
    with pytest.raises(RunNotClaimable, match="no such run"):
        store.claim("run-zz", at(3), github_run_id="1", github_run_attempt=1)


def test_closing_a_run_releases_the_lock_and_records_success(store: RunStore) -> None:
    store.create(api_run("run-a"), at(0))
    run = store.claim("run-a", at(1), github_run_id="1", github_run_attempt=1)
    for stage in Stage:
        run = finish_stage(start_stage(run, stage, at(2)), stage, at(3))
    run = succeed_run(
        run,
        at(4),
        snapshot_outcome=SnapshotOutcome.UNCHANGED,
        serving_version="meta-0123456789ab",
        data_as_of=date(2026, 10, 1),
        weekly_version="w",
    )
    store.close(run)
    assert store.active() is None
    last = store.last_successful()
    assert last is not None and last.run_id == "run-a"
    assert last.snapshot_outcome == SnapshotOutcome.UNCHANGED
    assert last.data_as_of == date(2026, 10, 1)
    store.create(api_run("run-b", 5), at(5))  # free again
    failed = fail_stage(
        start_stage(
            store.claim("run-b", at(6), github_run_id="2", github_run_attempt=1),
            Stage.INGEST,
            at(6),
        ),
        Stage.INGEST,
        at(7),
        "boom",
    )
    store.close(failed)
    assert store.active() is None
    last = store.last_successful()
    assert last is not None and last.run_id == "run-a"  # a failure is not a success


def test_a_scheduled_run_supersedes_a_queued_one_but_never_a_running_one(
    store: RunStore,
) -> None:
    store.create(api_run("run-q"), at(0))  # dispatched, but GitHub cancelled it pending
    store.create(scheduled("gh-1-1", 1), at(1), supersede_queued=True)
    q = store.get("run-q")
    assert q is not None and q.status == RunStatus.CANCELLED
    assert q.error_summary == "superseded by gh-1-1"
    with pytest.raises(RunNotClaimable):  # its workflow, if it starts later, does nothing
        store.claim("run-q", at(2), github_run_id="9", github_run_attempt=1)
    with pytest.raises(ActiveRunExists):
        store.create(scheduled("gh-2-1", 2), at(2), supersede_queued=True)


def test_a_lost_run_fails_and_frees_the_lock_after_six_hours(store: RunStore) -> None:
    store.create(scheduled("gh-1-1", 0), at(0))
    assert store.expire_lost(at(359)) is None
    with pytest.raises(ActiveRunExists):
        store.create(api_run("run-b", 300), at(300))
    store.create(api_run("run-b", 361), at(361))  # 6 h 1 min: the holder is lost
    lost = store.get("gh-1-1")
    assert lost is not None and lost.status == RunStatus.FAILED
    assert lost.error_summary is not None and lost.error_summary.startswith("lost")
    # A queued run nobody started is lost the same way, and cannot be claimed late.
    assert store.expire_lost(at(361 + 361)) is not None
    with pytest.raises(RunNotClaimable):
        store.claim("run-b", at(800), github_run_id="5", github_run_attempt=1)
    assert store.active() is None


def test_the_finalizer_only_closes_its_own_run(store: RunStore) -> None:
    store.create(api_run("run-a"), at(0))
    store.claim("run-a", at(1), github_run_id="100", github_run_attempt=1)
    assert store.end_active("run-a", at(2), RunStatus.FAILED, "x", github_run_id="200") is None
    run = store.get("run-a")
    assert run is not None and run.status == RunStatus.RUNNING
    ended = store.end_active("run-a", at(3), RunStatus.CANCELLED, "cancelled", github_run_id="100")
    assert ended is not None and ended.status == RunStatus.CANCELLED
    assert store.active() is None
    # A queued run whose workflow failed before claiming it is closed too.
    store.create(api_run("run-b", 4), at(4))
    ended = store.end_active("run-b", at(5), RunStatus.FAILED, "setup failed", github_run_id="300")
    assert ended is not None and ended.status == RunStatus.FAILED and store.active() is None
    # Finished runs are never reopened or rewritten.
    assert store.end_active("run-b", at(6), RunStatus.CANCELLED, "late") is None


def test_recent_runs_newest_first_with_a_cursor(store: RunStore) -> None:
    for i in range(5):
        store.create(api_run(f"run-{i}", i * 10), at(i * 10))
        store.end_active(f"run-{i}", at(i * 10 + 1), RunStatus.FAILED, "x")
    assert [r.run_id for r in store.list(3)] == ["run-4", "run-3", "run-2"]
    assert [r.run_id for r in store.list(3, before=at(20))] == ["run-1", "run-0"]


def test_snapshot_history_is_staged_then_published_and_never_rewritten(store: RunStore) -> None:
    snap = SnapshotRecord(
        snapshot_id="meta-0123456789ab",
        exchange="NSE",
        schema_version=2,
        status="STAGED",
        staged_at=at(0),
        run_id="run-a",
        data_as_of=date(2026, 10, 1),
        versions={"weekly_version": "w", "methodology_hash": "m"},
        counts={"securities": 3},
    )
    store.stage_snapshot(snap)
    staged = store.get_snapshot(snap.snapshot_id)
    assert staged is not None and staged.status == "STAGED"
    store.publish_snapshot(snap.snapshot_id, at(1))
    store.stage_snapshot(snap.model_copy(update={"run_id": "run-other"}))
    published = store.get_snapshot(snap.snapshot_id)
    assert published is not None
    assert (published.status, published.published_at, published.run_id) == (
        "PUBLISHED",
        at(1),
        "run-a",
    )
    assert published.versions == snap.versions and published.data_as_of == snap.data_as_of
    assert [s.snapshot_id for s in store.list_snapshots(5)] == [snap.snapshot_id]
