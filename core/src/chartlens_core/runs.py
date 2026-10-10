"""Production runs and snapshot history: the operational record (ADR-0018).

Pure model and state transitions. Persistence lives in ``chartlens_pipeline.runs``; the
API and the pipeline share these types so both read and write the same documents.

A run moves ``QUEUED → RUNNING → SUCCEEDED | FAILED | CANCELLED``. Its stages run in a
fixed order and use the same statuses: a stage not reached yet is QUEUED, and the stages
left after a failure become CANCELLED. Every transition takes ``now`` explicitly so the
lifecycle is deterministic under test.
"""

from __future__ import annotations

import re
import secrets
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Final, Literal

from pydantic import BaseModel, Field


class RunStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


ACTIVE_STATUSES: Final = frozenset({RunStatus.QUEUED, RunStatus.RUNNING})


class Stage(StrEnum):
    INGEST = "INGEST"
    CORPORATE_ACTIONS = "CORPORATE_ACTIONS"
    ADJUSTMENT = "ADJUSTMENT"
    DATA_QUALITY = "DATA_QUALITY"
    WEEKLY = "WEEKLY"
    ANALYSIS = "ANALYSIS"
    """Technical analysis of the analytical universe (ADR-0024, ADR-0025)."""
    PUBLISH_SERVING = "PUBLISH_SERVING"


STAGES: Final[tuple[Stage, ...]] = tuple(Stage)


class SnapshotOutcome(StrEnum):
    PUBLISHED = "PUBLISHED"
    """A new serving snapshot went live."""
    UNCHANGED = "UNCHANGED"
    """The pipeline produced exactly the live snapshot; nothing was written."""
    NOT_PUBLISHED = "NOT_PUBLISHED"
    """The run did not reach, or did not complete, publication: the old snapshot is live."""


Trigger = Literal["api", "schedule", "manual"]
JobType = Literal["DAILY"]

RUN_ID_PATTERN: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
SNAPSHOT_ID_PATTERN: Final = re.compile(r"^meta-[0-9a-f]{12}$")
LOST_AFTER: Final = timedelta(hours=6)
"""Longer than the workflow's timeout (350 min): a run silent for this long is dead."""
ERROR_SUMMARY_MAX: Final = 300


class InvalidTransition(RuntimeError):
    """A run or stage was asked to move to a state it cannot reach from where it is."""


def new_run_id(now: datetime) -> str:
    return f"run-{now:%Y%m%dT%H%M%SZ}-{secrets.token_hex(3)}"


def github_run_id(github_run: str, attempt: str | int) -> str:
    return f"gh-{github_run}-{attempt}"


def valid_run_id(run_id: str) -> bool:
    return bool(RUN_ID_PATTERN.fullmatch(run_id))


def one_line(text: str, limit: int = ERROR_SUMMARY_MAX) -> str:
    """A short single-line summary: never a traceback, never multi-line output."""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


class StageRecord(BaseModel):
    stage: Stage
    status: RunStatus = RunStatus.QUEUED
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_seconds: float | None = None
    records_processed: int | None = None
    version: str | None = None
    error_summary: str | None = None
    details: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


def _fresh_stages() -> list[StageRecord]:
    return [StageRecord(stage=s) for s in STAGES]


class RunRecord(BaseModel):
    run_id: str
    job_type: JobType = "DAILY"
    trigger: Trigger
    requested_by: str
    """``"schedule"``, ``"github:<actor>"`` or the requesting admin's email."""
    requested_by_uid: str | None = None
    requested_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    heartbeat_at: datetime | None = None
    status: RunStatus = RunStatus.QUEUED
    current_stage: Stage | None = None
    data_as_of: date | None = None
    weekly_version: str | None = None
    serving_version: str | None = None
    """The snapshot this run made live (PUBLISHED) or found live (UNCHANGED)."""
    snapshot_outcome: SnapshotOutcome | None = None
    error_summary: str | None = None
    github_run_id: str | None = None
    github_run_attempt: int | None = None
    stages: list[StageRecord] = Field(default_factory=_fresh_stages)

    @property
    def active(self) -> bool:
        return self.status in ACTIVE_STATUSES

    @property
    def duration_seconds(self) -> float | None:
        if self.started_at is None or self.completed_at is None:
            return None
        return round((self.completed_at - self.started_at).total_seconds(), 1)

    def stage(self, stage: Stage) -> StageRecord:
        found = self.stage_or_none(stage)
        if found is None:
            raise KeyError(f"run {self.run_id} has no stage {stage}")
        return found

    def stage_or_none(self, stage: Stage) -> StageRecord | None:
        """Records written before a stage existed (six-stage runs before ANALYSIS) simply
        lack it."""
        return next((s for s in self.stages if s.stage == stage), None)

    def last_progress(self) -> datetime:
        return self.heartbeat_at or self.started_at or self.requested_at

    def is_lost(self, now: datetime, lost_after: timedelta = LOST_AFTER) -> bool:
        return self.active and now - self.last_progress() > lost_after


# ----------------------------------------------------------------------------- transitions


def _require(run: RunRecord, *allowed: RunStatus) -> None:
    if run.status not in allowed:
        raise InvalidTransition(
            f"run {run.run_id} is {run.status}, expected {' or '.join(map(str, allowed))}"
        )


def _with_stage(run: RunRecord, stage: StageRecord, **changes: object) -> RunRecord:
    stages = [stage if s.stage == stage.stage else s for s in run.stages]
    return run.model_copy(update={"stages": stages, **changes})


def _aligned_stages(stages: list[StageRecord]) -> list[StageRecord]:
    """The current stage sequence, keeping any record the run already has. A run queued by
    code that predates a stage (say, the API before ANALYSIS existed) gains it, QUEUED,
    in its place when it starts."""
    have = {s.stage: s for s in stages}
    return [have.get(stage, StageRecord(stage=stage)) for stage in STAGES]


def start_run(
    run: RunRecord, now: datetime, *, github_run_id: str | None, github_run_attempt: int | None
) -> RunRecord:
    _require(run, RunStatus.QUEUED)
    return run.model_copy(
        update={
            "stages": _aligned_stages(run.stages),
            "status": RunStatus.RUNNING,
            "started_at": now,
            "heartbeat_at": now,
            "github_run_id": github_run_id,
            "github_run_attempt": github_run_attempt,
        }
    )


def start_stage(run: RunRecord, stage: Stage, now: datetime) -> RunRecord:
    _require(run, RunStatus.RUNNING)
    ahead = [run.stage_or_none(s) for s in STAGES[: STAGES.index(stage)]]
    if any(s is None or s.status != RunStatus.SUCCEEDED for s in ahead):
        raise InvalidTransition(f"{stage} cannot start before the stages ahead of it succeed")
    record = run.stage(stage)
    if record.status != RunStatus.QUEUED:
        raise InvalidTransition(f"{stage} is already {record.status}")
    started = record.model_copy(update={"status": RunStatus.RUNNING, "started_at": now})
    return _with_stage(run, started, current_stage=stage, heartbeat_at=now)


def _close_stage(record: StageRecord, now: datetime, **changes: object) -> StageRecord:
    started = record.started_at or now
    return record.model_copy(
        update={
            "completed_at": now,
            "duration_seconds": round((now - started).total_seconds(), 1),
            **changes,
        }
    )


def finish_stage(
    run: RunRecord,
    stage: Stage,
    now: datetime,
    *,
    records_processed: int | None = None,
    version: str | None = None,
    details: dict[str, str | int | float | bool | None] | None = None,
) -> RunRecord:
    _require(run, RunStatus.RUNNING)
    record = run.stage(stage)
    if record.status != RunStatus.RUNNING:
        raise InvalidTransition(f"{stage} is {record.status}, not RUNNING")
    done = _close_stage(
        record,
        now,
        status=RunStatus.SUCCEEDED,
        records_processed=records_processed,
        version=version,
        details=details or {},
    )
    return _with_stage(run, done, heartbeat_at=now)


def _end(run: RunRecord, now: datetime, status: RunStatus, **changes: object) -> RunRecord:
    stages: list[StageRecord] = []
    for s in run.stages:
        if s.status == RunStatus.QUEUED:
            stages.append(s.model_copy(update={"status": RunStatus.CANCELLED}))
        elif s.status == RunStatus.RUNNING:
            stages.append(
                _close_stage(
                    s,
                    now,
                    status=status if status != RunStatus.SUCCEEDED else RunStatus.FAILED,
                    error_summary=changes.get("error_summary"),
                )
            )
        else:
            stages.append(s)
    outcome = changes.pop("snapshot_outcome", None) or (
        run.snapshot_outcome or SnapshotOutcome.NOT_PUBLISHED
    )
    return run.model_copy(
        update={
            "status": status,
            "completed_at": now,
            "heartbeat_at": now,
            "stages": stages,
            "snapshot_outcome": outcome,
            **changes,
        }
    )


def fail_stage(run: RunRecord, stage: Stage, now: datetime, error: str) -> RunRecord:
    """The stage failed: the run is FAILED there, later stages are CANCELLED."""
    _require(run, RunStatus.RUNNING)
    summary = one_line(f"{stage}: {error}")
    record = run.stage(stage)
    if record.status == RunStatus.RUNNING:
        record = _close_stage(record, now, status=RunStatus.FAILED, error_summary=one_line(error))
        run = _with_stage(run, record)
    return _end(run, now, RunStatus.FAILED, error_summary=summary, current_stage=stage)


def succeed_run(
    run: RunRecord,
    now: datetime,
    *,
    snapshot_outcome: SnapshotOutcome,
    serving_version: str | None,
    data_as_of: date | None,
    weekly_version: str | None,
) -> RunRecord:
    _require(run, RunStatus.RUNNING)
    unfinished = [s.stage for s in run.stages if s.status != RunStatus.SUCCEEDED]
    if unfinished:
        raise InvalidTransition(f"run {run.run_id} cannot succeed: {unfinished} not succeeded")
    return _end(
        run,
        now,
        RunStatus.SUCCEEDED,
        snapshot_outcome=snapshot_outcome,
        serving_version=serving_version,
        data_as_of=data_as_of,
        weekly_version=weekly_version,
        current_stage=None,
        error_summary=None,
    )


def end_run(run: RunRecord, now: datetime, status: RunStatus, reason: str) -> RunRecord:
    """Close an active run from outside its stages: a failed dispatch, a cancelled or
    killed workflow, a superseded or lost run."""
    if status not in (RunStatus.FAILED, RunStatus.CANCELLED):
        raise InvalidTransition(f"end_run closes a run as FAILED or CANCELLED, not {status}")
    _require(run, *ACTIVE_STATUSES)
    stage = run.current_stage
    summary = one_line(f"{stage}: {reason}" if stage else reason)
    return _end(run, now, status, error_summary=summary)


# ----------------------------------------------------------------------------- snapshots


SnapshotStatus = Literal["STAGED", "PUBLISHED"]


class SnapshotRecord(BaseModel):
    """A serving snapshot as published (or about to be). Whether it is *live* is never
    stored here: the serving pointer decides that (ADR-0018)."""

    snapshot_id: str
    """The snapshot's ``meta_version``."""
    exchange: str
    schema_version: int
    status: SnapshotStatus
    staged_at: datetime
    published_at: datetime | None = None
    run_id: str | None = None
    data_as_of: date
    versions: dict[str, str]
    counts: dict[str, int] = Field(default_factory=dict)
    analysis: dict[str, str | int] | None = None
    """Schema 3: the analysis set's summary (version, methodology hash, universe and set
    hashes, securities analysed). Run facts stay in the run record (ADR-0026 §1.7)."""
