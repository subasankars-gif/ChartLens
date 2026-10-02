"""Production operations (ADR-0018): start a refresh, follow runs, see what is served.

The API initiates and reports; GitHub Actions runs the pipeline.

- Only admins may start a refresh, and the check is made here, on the server.
- Any approved user may read runs and snapshot history. Requester emails and GitHub run
  details are shown to admins only.
- Reads never write. A run that has gone silent past its lease is *shown* as lost, and
  the next refresh records that.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import chartlens_api
from chartlens_api.appstate import User
from chartlens_api.deps import AdminUser, CurrentUser, Dispatcher, Runs, Settings, Snapshots
from chartlens_api.github import DispatchFailed
from chartlens_api.lake import LakeUnavailable
from chartlens_core.domain import utc_now
from chartlens_core.runs import (
    LOST_AFTER,
    RUN_ID_PATTERN,
    RunRecord,
    RunStatus,
    SnapshotOutcome,
    Stage,
    StageRecord,
    end_run,
    new_run_id,
)
from chartlens_pipeline.runs import (
    ActiveRunExists,
    RunNotClaimable,
    RunNotFound,
    RunStore,
    RunStoreBusy,
)

router = APIRouter(tags=["operations"])
log = logging.getLogger("chartlens.api.operations")

RunIdPath = Annotated[str, Path(pattern=RUN_ID_PATTERN.pattern, max_length=80)]


class RunView(BaseModel):
    run_id: str
    job_type: str
    trigger: str
    requested_by: str
    """The admin's email for admins; "an administrator" for everyone else."""
    requested_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    duration_seconds: float | None
    status: RunStatus
    current_stage: Stage | None
    data_as_of: date | None
    weekly_version: str | None
    serving_version: str | None
    snapshot_outcome: SnapshotOutcome | None
    error_summary: str | None
    github_run_id: str | None
    """Admins only."""
    github_run_url: str | None
    """Admins only."""
    stages: list[StageRecord]


class RunsPage(BaseModel):
    runs: list[RunView]
    next_before: datetime | None
    """Pass as ``before`` for the next (older) page; null on the last page."""


class RefreshAccepted(BaseModel):
    run_id: str
    status: RunStatus


class SnapshotView(BaseModel):
    snapshot_id: str
    live: bool
    """Whether the API is serving it now (the pointer decides; never stored)."""
    status: str
    staged_at: datetime
    published_at: datetime | None
    run_id: str | None
    data_as_of: date
    schema_version: int
    versions: dict[str, str]
    counts: dict[str, int]


class ServingView(BaseModel):
    meta_version: str
    data_as_of: date
    versions: dict[str, str]
    counts: dict[str, int]
    snapshot_generated_at: datetime


class OperationsStatus(BaseModel):
    api_status: str
    api_version: str
    serving: ServingView | None
    """Null until the first snapshot is published: not unhealthy, just nothing yet."""
    active_run: RunView | None
    last_run: RunView | None
    last_successful_run: RunView | None
    refresh_configured: bool
    can_refresh: bool
    """Whether this viewer may start a refresh (the server checks again on POST)."""


def _view(run: RunRecord, viewer: User, repository: str | None) -> RunView:
    now = utc_now()
    if run.is_lost(now, LOST_AFTER):  # shown as it will be recorded; reads never write
        run = end_run(run, now, RunStatus.FAILED, "lost: no progress for 6 h")
    admin = viewer.role == "admin"
    who = run.requested_by
    if not admin and run.trigger == "api":
        who = "an administrator"
    elif not admin and who.startswith("github:"):
        who = "github:a maintainer"
    url = (
        f"https://github.com/{repository}/actions/runs/{run.github_run_id}"
        if admin and repository and run.github_run_id
        else None
    )
    return RunView(
        **run.model_dump(
            include={
                "run_id",
                "job_type",
                "trigger",
                "requested_at",
                "started_at",
                "completed_at",
                "status",
                "current_stage",
                "data_as_of",
                "weekly_version",
                "serving_version",
                "snapshot_outcome",
                "error_summary",
            }
        ),
        requested_by=who,
        duration_seconds=run.duration_seconds,
        github_run_id=run.github_run_id if admin else None,
        github_run_url=url,
        stages=run.stages,
    )


def _store_call(fn: Any, *args: Any, **kwargs: Any) -> Any:
    try:
        return fn(*args, **kwargs)
    except (ActiveRunExists, RunNotFound, RunNotClaimable, HTTPException):
        raise
    except RunStoreBusy:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "run history is busy; try again in a moment"
        ) from None
    except Exception:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "run history is unavailable right now"
        ) from None


@router.post(
    "/refresh/daily",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=RefreshAccepted,
    responses={409: {"description": "A refresh is already active"}},
)
def refresh_daily(
    admin: AdminUser, runs: Runs, dispatcher: Dispatcher
) -> RefreshAccepted | JSONResponse:
    """Start the production refresh. Admins only. Returns at once with the run id: the
    refresh runs in GitHub Actions, and ``GET /jobs/{run_id}`` follows it."""
    if dispatcher is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "refresh is not configured")
    now = utc_now()
    run = RunRecord(
        run_id=new_run_id(now),
        trigger="api",
        requested_by=admin.email or admin.uid,
        requested_by_uid=admin.uid,
        requested_at=now,
    )
    try:
        _store_call(runs.expire_lost, now)
        _store_call(runs.create, run, now)
    except ActiveRunExists as exc:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "detail": "a refresh is already in progress",
                "run_id": exc.run.run_id,
                "status": str(exc.run.status),
            },
        )
    try:
        dispatcher.dispatch(run.run_id)
    except Exception as exc:  # whatever went wrong, the lock must not stay held
        reason = (
            str(exc)
            if isinstance(exc, DispatchFailed)
            else f"unexpected error ({type(exc).__name__})"
        )
        if not isinstance(exc, DispatchFailed):
            log.exception("dispatching %s failed", run.run_id)
        _store_call(
            runs.end_active,
            run.run_id,
            utc_now(),
            RunStatus.FAILED,
            f"the workflow could not be started: {reason}",
        )
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={"detail": "GitHub did not start the refresh", "run_id": run.run_id},
        )
    return RefreshAccepted(run_id=run.run_id, status=RunStatus.QUEUED)


@router.get("/jobs", response_model=RunsPage)
def list_jobs(
    user: CurrentUser,
    runs: Runs,
    settings: Settings,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    before: Annotated[datetime | None, Query(description="requested_at cursor")] = None,
) -> RunsPage:
    page: list[RunRecord] = _store_call(runs.list, limit, before)
    repo = settings.api.github_repository
    return RunsPage(
        runs=[_view(r, user, repo) for r in page],
        next_before=page[-1].requested_at if len(page) == limit else None,
    )


@router.get("/jobs/{run_id}", response_model=RunView)
def get_job(user: CurrentUser, runs: Runs, settings: Settings, run_id: RunIdPath) -> RunView:
    run: RunRecord | None = _store_call(runs.get, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such run")
    return _view(run, user, settings.api.github_repository)


@router.post("/jobs/{run_id}/cancel", response_model=RunView)
def cancel_job(admin: AdminUser, runs: Runs, settings: Settings, run_id: RunIdPath) -> RunView:
    """Cancel a refresh that no workflow has started (admins only). It frees the lock if
    GitHub dropped the workflow before it ran. A running refresh is cancelled in GitHub
    Actions, where the workflow records it."""
    try:
        run: RunRecord = _store_call(
            runs.cancel_queued,
            run_id,
            utc_now(),
            f"cancelled by {admin.email or admin.uid} before it started",
        )
    except RunNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such run") from None
    except RunNotClaimable:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "only a queued run can be cancelled here; cancel a running one in GitHub Actions",
        ) from None
    return _view(run, admin, settings.api.github_repository)


def _live_version(snapshots: Any) -> str | None:
    try:
        return str(snapshots.get().meta_version)
    except LakeUnavailable:
        return None


@router.get("/system/snapshots", response_model=list[SnapshotView])
def list_snapshots(
    _: CurrentUser,
    runs: Runs,
    snapshots: Snapshots,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> list[SnapshotView]:
    """Published snapshot history, newest first. The API still serves only what the
    pointer names; this is a record of publications, never a way to choose one."""
    live = _live_version(snapshots)
    return [
        SnapshotView(**s.model_dump(), live=s.snapshot_id == live)
        for s in _store_call(runs.list_snapshots, limit)
    ]


@router.get("/system/operations", response_model=OperationsStatus)
def operations_status(
    user: CurrentUser,
    runs: Runs,
    snapshots: Snapshots,
    settings: Settings,
    dispatcher: Dispatcher,
) -> OperationsStatus:
    serving: ServingView | None = None
    try:
        snap = snapshots.get()
        serving = ServingView(
            meta_version=snap.meta_version,
            data_as_of=snap.as_of,
            versions=snap.versions,
            counts=snap.counts,
            snapshot_generated_at=datetime.fromisoformat(snap.generated_at),
        )
    except LakeUnavailable:
        pass
    repo = settings.api.github_repository
    store: RunStore = runs
    recent: list[RunRecord] = _store_call(store.list, 1)
    active: RunRecord | None = _store_call(store.active)
    if active is not None and active.is_lost(utc_now(), LOST_AFTER):
        active = None  # shown FAILED in the history; it no longer blocks a refresh
    success: RunRecord | None = _store_call(store.last_successful)

    def view(r: RunRecord | None) -> RunView | None:
        return _view(r, user, repo) if r else None

    return OperationsStatus(
        api_status="ok",
        api_version=chartlens_api.__version__,
        serving=serving,
        active_run=view(active),
        last_run=view(recent[0] if recent else None),
        last_successful_run=view(success),
        refresh_configured=dispatcher is not None,
        can_refresh=user.role == "admin" and dispatcher is not None,
    )
