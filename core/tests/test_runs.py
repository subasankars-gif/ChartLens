"""Run and stage lifecycle (ADR-0018): pure transitions, deterministic under a fixed clock."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from chartlens_core.runs import (
    STAGES,
    InvalidTransition,
    RunRecord,
    RunStatus,
    SnapshotOutcome,
    Stage,
    end_run,
    fail_stage,
    finish_stage,
    new_run_id,
    one_line,
    start_run,
    start_stage,
    succeed_run,
    valid_run_id,
)

T0 = datetime(2026, 10, 2, 14, 45, tzinfo=UTC)


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def queued() -> RunRecord:
    return RunRecord(run_id="run-1", trigger="api", requested_by="boss@x.com", requested_at=T0)


def running() -> RunRecord:
    return start_run(queued(), at(1), github_run_id="99", github_run_attempt=1)


def test_queued_running_succeeded_with_stage_timestamps() -> None:
    run = running()
    assert (run.status, run.started_at, run.github_run_id) == (RunStatus.RUNNING, at(1), "99")
    for i, stage in enumerate(STAGES):
        run = start_stage(run, stage, at(2 + i))
        assert run.current_stage == stage and run.stage(stage).status == RunStatus.RUNNING
        run = finish_stage(run, stage, at(2.5 + i), records_processed=i, version=f"v{i}")
    run = succeed_run(
        run,
        at(10),
        snapshot_outcome=SnapshotOutcome.PUBLISHED,
        serving_version="meta-0123456789ab",
        data_as_of=date(2026, 10, 1),
        weekly_version="w1",
    )
    assert run.status == RunStatus.SUCCEEDED and not run.active
    assert (run.completed_at, run.duration_seconds) == (at(10), 540.0)
    assert run.current_stage is None and run.error_summary is None
    ingest = run.stage(Stage.INGEST)
    assert (ingest.started_at, ingest.completed_at, ingest.duration_seconds) == (at(2), at(2.5), 30)
    assert [s.version for s in run.stages] == [f"v{i}" for i in range(len(STAGES))]


def test_queued_running_failed_cancels_the_stages_left() -> None:
    run = running()
    for i, stage in enumerate(STAGES[:2]):
        run = finish_stage(start_stage(run, stage, at(2 + i)), stage, at(2.5 + i))
    run = start_stage(run, Stage.ADJUSTMENT, at(5))
    run = fail_stage(run, Stage.ADJUSTMENT, at(6), "hard requirement failed (exit 5)")
    assert run.status == RunStatus.FAILED and run.current_stage == Stage.ADJUSTMENT
    assert run.error_summary == "ADJUSTMENT: hard requirement failed (exit 5)"
    assert run.snapshot_outcome == SnapshotOutcome.NOT_PUBLISHED and run.serving_version is None
    assert [s.status for s in run.stages] == [
        RunStatus.SUCCEEDED,
        RunStatus.SUCCEEDED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
        RunStatus.CANCELLED,
        RunStatus.CANCELLED,
        RunStatus.CANCELLED,
    ]
    assert run.stage(Stage.ADJUSTMENT).duration_seconds == 60


def test_stages_run_strictly_in_order_and_once() -> None:
    run = running()
    with pytest.raises(InvalidTransition):
        start_stage(run, Stage.WEEKLY, at(2))
    run = start_stage(run, Stage.INGEST, at(2))
    with pytest.raises(InvalidTransition):
        start_stage(run, Stage.INGEST, at(3))
    with pytest.raises(InvalidTransition):
        succeed_run(
            run,
            at(4),
            snapshot_outcome=SnapshotOutcome.PUBLISHED,
            serving_version=None,
            data_as_of=None,
            weekly_version=None,
        )
    with pytest.raises(InvalidTransition):
        start_stage(queued(), Stage.INGEST, at(1))  # not started
    with pytest.raises(InvalidTransition):
        start_run(run, at(5), github_run_id=None, github_run_attempt=None)  # already running


def test_a_run_ended_from_outside_keeps_where_it_stopped() -> None:
    run = start_stage(running(), Stage.INGEST, at(2))
    cancelled = end_run(run, at(3), RunStatus.CANCELLED, "the workflow was cancelled")
    assert cancelled.status == RunStatus.CANCELLED
    assert cancelled.error_summary == "INGEST: the workflow was cancelled"
    assert cancelled.stage(Stage.INGEST).status == RunStatus.CANCELLED
    never = end_run(queued(), at(1), RunStatus.FAILED, "dispatch refused")
    assert (never.status, never.started_at, never.error_summary) == (
        RunStatus.FAILED,
        None,
        "dispatch refused",
    )
    with pytest.raises(InvalidTransition):
        end_run(never, at(2), RunStatus.FAILED, "again")
    with pytest.raises(InvalidTransition):
        end_run(queued(), at(1), RunStatus.SUCCEEDED, "no")


def test_a_run_is_lost_after_six_hours_without_progress() -> None:
    q = queued()
    assert not q.is_lost(T0 + timedelta(hours=6))
    assert q.is_lost(T0 + timedelta(hours=6, seconds=1))
    run = start_stage(running(), Stage.INGEST, at(300))  # heartbeat at 5 h
    assert not run.is_lost(at(300) + timedelta(hours=5))
    assert run.is_lost(at(300) + timedelta(hours=6, minutes=1))
    done = end_run(run, at(301), RunStatus.FAILED, "x")
    assert not done.is_lost(at(10_000))


def test_run_ids_and_error_summaries_are_safe_to_show() -> None:
    rid = new_run_id(T0)
    assert rid.startswith("run-20261002T144500Z-") and valid_run_id(rid)
    for bad in ("", "../x", "a b", "x" * 81, "run/1", "-x"):
        assert not valid_run_id(bad)
    summary = one_line("Traceback\n  line 1\n" + "y" * 500)
    assert "\n" not in summary and len(summary) <= 300


def test_analysis_runs_between_weekly_and_publication() -> None:
    from chartlens_core.runs import Stage

    assert STAGES.index(Stage.ANALYSIS) == STAGES.index(Stage.WEEKLY) + 1
    assert STAGES.index(Stage.PUBLISH_SERVING) == STAGES.index(Stage.ANALYSIS) + 1


def test_a_six_stage_record_still_reads_and_a_queued_one_gains_analysis() -> None:
    """Records written before ANALYSIS existed keep their six stages; a run queued by
    older code (an API not yet redeployed) gains the stage when it starts."""
    from datetime import UTC, datetime

    from chartlens_core.runs import RunRecord, Stage, StageRecord, start_run

    now = datetime(2026, 10, 7, tzinfo=UTC)
    old_stages = [StageRecord(stage=s) for s in STAGES if s != Stage.ANALYSIS]
    old = RunRecord.model_validate(
        {
            "run_id": "run-old",
            "trigger": "api",
            "requested_by": "a@b.c",
            "requested_at": now,
            "stages": [s.model_dump() for s in old_stages],
        }
    )
    assert old.stage_or_none(Stage.ANALYSIS) is None
    assert old.stage(Stage.WEEKLY).stage == Stage.WEEKLY
    started = start_run(old, now, github_run_id="1", github_run_attempt=1)
    assert [s.stage for s in started.stages] == list(STAGES)
