"""The tracked production run: the daily chain, stage by stage (ADR-0018).

Each stage is one existing pipeline command, called in this process. This module adds
no pipeline logic. It records each stage's start, end, counts and version, and stops at
the first failure. Every write goes to the run store, so the record outlives the
process. Publication is the last stage, so a failed run never reaches the serving
pointer.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from chartlens_core.domain import utc_now
from chartlens_core.logs import log_event
from chartlens_core.runs import (
    STAGES,
    RunRecord,
    RunStatus,
    SnapshotOutcome,
    Stage,
    end_run,
    fail_stage,
    finish_stage,
    start_stage,
    succeed_run,
)
from chartlens_pipeline.runs import RunStore

log = logging.getLogger("chartlens.pipeline.production")


@dataclass
class StageResult:
    records_processed: int | None = None
    version: str | None = None
    details: dict[str, str | int | float | bool | None] = field(default_factory=dict)
    data_as_of: date | None = None
    """WEEKLY: the data the weekly bars run through."""
    snapshot_outcome: SnapshotOutcome | None = None
    """PUBLISH_SERVING: PUBLISHED or UNCHANGED."""


class StageFailed(RuntimeError):
    """A stage ended unsuccessfully; the message is the short, user-facing reason."""


StageFn = Callable[[], StageResult]


class ProductionRunner:
    def __init__(
        self,
        store: RunStore,
        run: RunRecord,
        stages: Mapping[Stage, StageFn],
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        missing = set(STAGES) - set(stages)
        if missing:
            raise ValueError(f"no implementation for stages {sorted(missing)}")
        self.store = store
        self.run = run
        self.stages = stages
        self.clock = clock

    def _log(self, event: str, stage: Stage | None = None, **fields: Any) -> None:
        log_event(
            log,
            event,
            run_id=self.run.run_id,
            github_run_id=self.run.github_run_id,
            stage=str(stage) if stage else None,
            status=str(self.run.status),
            **fields,
        )

    def execute(self) -> RunRecord:
        """Run every stage in order. Returns the closed run (SUCCEEDED or FAILED)."""
        if self.run.status != RunStatus.RUNNING:
            raise ValueError(f"run {self.run.run_id} is {self.run.status}, not RUNNING")
        results: dict[Stage, StageResult] = {}
        self._log("run.started", trigger=self.run.trigger, requested_by=self.run.requested_by)
        try:
            for stage in STAGES:
                self.run = start_stage(self.run, stage, self.clock())
                self.store.save(self.run)
                self._log("run.stage_started", stage)
                error: str | None = None
                result = StageResult()
                try:
                    result = self.stages[stage]()
                except StageFailed as exc:
                    error = str(exc)
                except Exception as exc:
                    log.exception("stage %s raised", stage)
                    error = f"unexpected error ({type(exc).__name__}); see the workflow log"
                if error is not None:
                    self.run = fail_stage(self.run, stage, self.clock(), error)
                    self.store.close(self.run)
                    self._log("run.failed", stage, error=self.run.error_summary)
                    return self.run
                results[stage] = result
                self.run = finish_stage(
                    self.run,
                    stage,
                    self.clock(),
                    records_processed=result.records_processed,
                    version=result.version,
                    details=result.details,
                )
                self.store.save(self.run)
                record = self.run.stage(stage)
                self._log(
                    "run.stage_succeeded",
                    stage,
                    duration_seconds=record.duration_seconds,
                    records_processed=record.records_processed,
                    version=record.version,
                )
        except BaseException:
            # Interrupted (cancelled workflow, Ctrl-C): record it, then let it propagate.
            if self.run.active:
                self.run = end_run(self.run, self.clock(), RunStatus.CANCELLED, "interrupted")
                self.store.close(self.run)
                self._log("run.cancelled")
            raise

        weekly = results[Stage.WEEKLY]
        publish = results[Stage.PUBLISH_SERVING]
        self.run = succeed_run(
            self.run,
            self.clock(),
            snapshot_outcome=publish.snapshot_outcome or SnapshotOutcome.PUBLISHED,
            serving_version=publish.version,
            data_as_of=weekly.data_as_of,
            weekly_version=weekly.version,
        )
        self.store.close(self.run)
        self._log(
            "run.succeeded",
            duration_seconds=self.run.duration_seconds,
            data_as_of=self.run.data_as_of,
            weekly_version=self.run.weekly_version,
            serving_version=self.run.serving_version,
            snapshot_outcome=str(self.run.snapshot_outcome),
        )
        return self.run
