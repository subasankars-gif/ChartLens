"""The tracked production run (ADR-0018): stages recorded in order, failure where it
happened, and a live snapshot that only a fully successful run can replace."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from test_adjust import SESSIONS, build_lake
from typer.testing import CliRunner

from chartlens_core.runs import STAGES, RunRecord, RunStatus, SnapshotOutcome, Stage, start_run
from chartlens_pipeline import cli
from chartlens_pipeline.adjust import AdjustmentService, CorporateActionOverrides
from chartlens_pipeline.data_quality import DataQualityService
from chartlens_pipeline.identity import IdentityOverrides
from chartlens_pipeline.production import ProductionRunner, StageFailed, StageResult
from chartlens_pipeline.runs import MemoryRunStore
from chartlens_pipeline.serving import ServingPublisher, ServingSnapshot
from chartlens_pipeline.storage import DataLakeLayout
from chartlens_pipeline.weekly import WeeklyService

T0 = datetime(2026, 10, 2, 14, 45, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.t = T0

    def __call__(self) -> datetime:
        self.t += timedelta(seconds=30)
        return self.t


def ok(**kw: Any) -> Callable[[], StageResult]:
    return lambda: StageResult(**kw)


def fake_stages(**overrides: Callable[[], StageResult]) -> dict[Stage, Callable[[], StageResult]]:
    stages: dict[Stage, Callable[[], StageResult]] = {
        Stage.INGEST: ok(records_processed=4000),
        Stage.CORPORATE_ACTIONS: ok(records_processed=2),
        Stage.ADJUSTMENT: ok(version="adj-1"),
        Stage.DATA_QUALITY: ok(version="dq-1"),
        Stage.WEEKLY: ok(version="wk-1", data_as_of=date(2026, 10, 1), records_processed=9),
        Stage.PUBLISH_SERVING: ok(
            version="meta-0123456789ab", snapshot_outcome=SnapshotOutcome.PUBLISHED
        ),
    }
    stages.update({Stage(k): v for k, v in overrides.items()})
    return stages


def running_run(store: MemoryRunStore, run_id: str = "run-a") -> RunRecord:
    store.create(
        RunRecord(run_id=run_id, trigger="api", requested_by="boss@x.com", requested_at=T0), T0
    )
    return store.claim(run_id, T0, github_run_id="77", github_run_attempt=1)


def test_a_successful_run_records_every_stage_and_the_snapshot() -> None:
    store = MemoryRunStore()
    run = ProductionRunner(store, running_run(store), fake_stages(), clock=Clock()).execute()
    assert run.status == RunStatus.SUCCEEDED
    assert [s.status for s in run.stages] == [RunStatus.SUCCEEDED] * 6
    assert all(s.duration_seconds == 30 for s in run.stages)
    assert (run.serving_version, run.snapshot_outcome) == (
        "meta-0123456789ab",
        SnapshotOutcome.PUBLISHED,
    )
    assert (run.data_as_of, run.weekly_version) == (date(2026, 10, 1), "wk-1")
    assert run.stage(Stage.INGEST).records_processed == 4000
    assert store.get("run-a") == run and store.active() is None
    last = store.last_successful()
    assert last is not None and last.run_id == "run-a"


@pytest.mark.parametrize("failing", STAGES)
def test_a_failed_stage_fails_the_run_there_and_nothing_after_runs(failing: Stage) -> None:
    store = MemoryRunStore()
    called: list[Stage] = []

    def track(stage: Stage) -> Callable[[], StageResult]:
        def fn() -> StageResult:
            called.append(stage)
            if stage == failing:
                raise StageFailed("hard requirement failed (exit 5)")
            return StageResult(version="v")

        return fn

    stages = {s: track(s) for s in STAGES}
    run = ProductionRunner(store, running_run(store), stages, clock=Clock()).execute()
    assert called == list(STAGES[: STAGES.index(failing) + 1])
    assert run.status == RunStatus.FAILED and run.current_stage == failing
    assert run.error_summary == f"{failing}: hard requirement failed (exit 5)"
    assert run.snapshot_outcome == SnapshotOutcome.NOT_PUBLISHED
    assert run.serving_version is None
    after = STAGES[STAGES.index(failing) + 1 :]
    assert all(run.stage(s).status == RunStatus.CANCELLED for s in after)
    assert store.active() is None and store.last_successful() is None


def test_an_unexpected_error_is_summarised_without_its_details() -> None:
    def boom() -> StageResult:
        raise ValueError("gs://chartlens-lake-13934-data/secret-path failed")

    store = MemoryRunStore()
    run = ProductionRunner(
        store, running_run(store), fake_stages(WEEKLY=boom), clock=Clock()
    ).execute()
    assert run.error_summary == "WEEKLY: unexpected error (ValueError); see the workflow log"


def test_an_interrupted_run_is_recorded_as_cancelled() -> None:
    def interrupt() -> StageResult:
        raise KeyboardInterrupt

    store = MemoryRunStore()
    runner = ProductionRunner(
        store, running_run(store), fake_stages(ADJUSTMENT=interrupt), clock=Clock()
    )
    with pytest.raises(KeyboardInterrupt):
        runner.execute()
    run = store.get("run-a")
    assert run is not None and run.status == RunStatus.CANCELLED
    assert run.stage(Stage.ADJUSTMENT).status == RunStatus.CANCELLED
    assert store.active() is None


# ----------------------------------------------------------------------------- real lake


@pytest.fixture
def lake(tmp_path: Path):  # type: ignore[no-untyped-def]
    settings, provider, store, today = build_lake(tmp_path, ex=SESSIONS[17])
    return settings, provider, store, today


def real_stages(lake: Any, history: MemoryRunStore, run_id: str, **broken: Any) -> Any:
    settings, provider, store, today = lake

    def adjustment() -> StageResult:
        r = AdjustmentService(
            settings, provider, store, overrides=CorporateActionOverrides(), today=today
        ).run()
        if not r.published:
            raise StageFailed("hard requirement failed; adjusted dataset not published (exit 5)")
        return StageResult(version=r.adjustment_version)

    def dq() -> StageResult:
        r = DataQualityService(
            settings, provider, store, identity_overrides=IdentityOverrides()
        ).run()
        return StageResult(version=r.dq_version)

    def weekly() -> StageResult:
        r = WeeklyService(settings, provider, store).run()
        return StageResult(
            version=r.summary["weekly_version"], data_as_of=date.fromisoformat(r.summary["as_of"])
        )

    def publish() -> StageResult:
        r = ServingPublisher(settings, provider, store, history=history, run_id=run_id).run()
        return StageResult(version=r["meta_version"], snapshot_outcome=r["outcome"])

    stages = {
        Stage.INGEST: ok(),
        Stage.CORPORATE_ACTIONS: ok(),
        Stage.ADJUSTMENT: adjustment,
        Stage.DATA_QUALITY: dq,
        Stage.WEEKLY: weekly,
        Stage.PUBLISH_SERVING: publish,
    }
    stages.update({Stage(k): v for k, v in broken.items()})
    return stages


def test_only_a_fully_successful_run_moves_the_serving_pointer(lake: Any) -> None:
    _, _, store, _ = lake
    runs = MemoryRunStore()
    first = ProductionRunner(
        runs, running_run(runs, "run-1"), real_stages(lake, runs, "run-1"), clock=Clock()
    ).execute()
    assert first.status == RunStatus.SUCCEEDED
    assert first.snapshot_outcome == SnapshotOutcome.PUBLISHED
    snap = runs.get_snapshot(first.serving_version or "")
    assert snap is not None and snap.status == "PUBLISHED" and snap.run_id == "run-1"
    pointer_key = DataLakeLayout.serving_manifest_key("NSE")
    live = store.get(pointer_key)
    served = ServingSnapshot.load(store, "NSE")
    sid = sorted(served.securities)[0]
    bars = served.weekly_bars(store, sid)

    # A second run gets as far as rewriting the weekly files, then publication fails.
    def weekly_then_corrupt() -> StageResult:
        key = DataLakeLayout.curated_weekly_key("NSE", sid)
        store.put(key, store.get(key) + b"partial")
        manifest_key = DataLakeLayout.weekly_manifest_key("NSE")
        manifest = json.loads(store.get(manifest_key))
        manifest["files"][sid] = "0" * 64  # the manifest no longer matches the file
        store.put(manifest_key, json.dumps(manifest).encode())
        return StageResult(version="wk-2", data_as_of=SESSIONS[-1])

    def publish() -> StageResult:
        settings, provider, _, _ = lake
        r = ServingPublisher(settings, provider, store, history=runs, run_id="run-2").run()
        return StageResult(version=r["meta_version"], snapshot_outcome=r["outcome"])

    second = ProductionRunner(
        runs,
        running_run(runs, "run-2"),
        real_stages(lake, runs, "run-2", WEEKLY=weekly_then_corrupt, PUBLISH_SERVING=publish),
        clock=Clock(),
    ).execute()
    assert second.status == RunStatus.FAILED and second.current_stage == Stage.PUBLISH_SERVING
    assert second.snapshot_outcome == SnapshotOutcome.NOT_PUBLISHED
    assert store.get(pointer_key) == live  # the pointer never moved
    again = ServingSnapshot.load(store, "NSE")
    assert again.meta_version == served.meta_version
    assert again.weekly_bars(store, sid) == bars  # and what it serves is untouched
    last = runs.last_successful()
    assert last is not None and last.run_id == "run-1"


def test_a_run_with_no_new_data_succeeds_without_a_new_snapshot(lake: Any) -> None:
    runs = MemoryRunStore()
    first = ProductionRunner(
        runs, running_run(runs, "run-1"), real_stages(lake, runs, "run-1"), clock=Clock()
    ).execute()
    second = ProductionRunner(
        runs, running_run(runs, "run-2"), real_stages(lake, runs, "run-2"), clock=Clock()
    ).execute()
    assert second.status == RunStatus.SUCCEEDED
    assert second.snapshot_outcome == SnapshotOutcome.UNCHANGED
    assert second.serving_version == first.serving_version
    assert len(runs.snapshots) == 1


# ----------------------------------------------------------------------------- CLI


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch) -> MemoryRunStore:
    store = MemoryRunStore()
    monkeypatch.setattr(cli, "_tracking_store", lambda settings: store)
    monkeypatch.setattr(
        cli, "_production_stages", lambda settings, exchange, run_id, workdir: fake_stages()
    )
    return store


def test_daily_claims_the_dispatched_run(cli_env: MemoryRunStore) -> None:
    now = datetime.now(UTC)
    cli_env.create(
        RunRecord(run_id="run-a", trigger="api", requested_by="boss@x.com", requested_at=now), now
    )
    result = CliRunner().invoke(
        cli.app,
        ["daily", "--run-id", "run-a", "--github-run-id", "55", "--github-run-attempt", "1"],
    )
    assert result.exit_code == 0, result.output
    run = cli_env.get("run-a")
    assert run is not None and run.status == RunStatus.SUCCEEDED and run.github_run_id == "55"
    # A re-run of the same workflow does not reopen a finished run.
    again = CliRunner().invoke(cli.app, ["daily", "--run-id", "run-a", "--github-run-id", "55"])
    assert again.exit_code == 3


def test_a_scheduled_run_records_itself(cli_env: MemoryRunStore) -> None:
    result = CliRunner().invoke(
        cli.app,
        [
            "daily",
            "--run-id",
            "gh-901-1",
            "--create",
            "--trigger",
            "schedule",
            "--requested-by",
            "schedule",
            "--github-run-id",
            "901",
        ],
    )
    assert result.exit_code == 0, result.output
    run = cli_env.get("gh-901-1")
    assert run is not None
    assert (run.trigger, run.requested_by, run.status) == (
        "schedule",
        "schedule",
        RunStatus.SUCCEEDED,
    )


def test_daily_refuses_while_another_run_is_active(cli_env: MemoryRunStore) -> None:
    held = start_run(
        RunRecord(run_id="gh-1-1", trigger="manual", requested_by="github:x", requested_at=T0),
        T0,
        github_run_id="1",
        github_run_attempt=1,
    )
    # Created "now" so it is not lost.
    cli_env.create(
        held.model_copy(
            update={"requested_at": datetime.now(UTC), "heartbeat_at": datetime.now(UTC)}
        ),
        T0,
    )
    result = CliRunner().invoke(
        cli.app, ["daily", "--run-id", "gh-2-1", "--create", "--trigger", "manual"]
    )
    assert result.exit_code == 3
    assert cli_env.get("gh-2-1") is None


def test_run_finalize_closes_only_its_own_run(cli_env: MemoryRunStore) -> None:
    now = datetime.now(UTC)
    cli_env.create(
        RunRecord(run_id="run-a", trigger="api", requested_by="boss@x.com", requested_at=now),
        now,
    )
    cli_env.claim("run-a", now, github_run_id="10", github_run_attempt=1)
    other = CliRunner().invoke(
        cli.app,
        ["run-finalize", "--run-id", "run-a", "--outcome", "cancelled", "--github-run-id", "11"],
    )
    assert other.exit_code == 0
    run = cli_env.get("run-a")
    assert run is not None and run.status == RunStatus.RUNNING
    mine = CliRunner().invoke(
        cli.app,
        ["run-finalize", "--run-id", "run-a", "--outcome", "cancelled", "--github-run-id", "10"],
    )
    assert mine.exit_code == 0
    run = cli_env.get("run-a")
    assert run is not None and run.status == RunStatus.CANCELLED
    assert run.error_summary == "the workflow was cancelled"
