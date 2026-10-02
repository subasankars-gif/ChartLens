"""What the API is serving right now (ADR-0016)."""

from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter
from pydantic import BaseModel

from chartlens_api.deps import CurrentUser, Snapshot, Snapshots

router = APIRouter(prefix="/system", tags=["system"])


class ServingStatus(BaseModel):
    exchange: str
    meta_version: str
    data_as_of: date
    versions: dict[str, str]
    """weekly, data, adjustment, identity, dq, calendar versions and methodology hash."""
    counts: dict[str, int]
    snapshot_generated_at: datetime
    snapshot_loaded_at: datetime | None
    refresh_seconds: float


@router.get("/status", response_model=ServingStatus)
def serving_status(_: CurrentUser, snap: Snapshot, snapshots: Snapshots) -> ServingStatus:
    return ServingStatus(
        exchange=snap.exchange,
        meta_version=snap.meta_version,
        data_as_of=snap.as_of,
        versions=snap.versions,
        counts=snap.counts,
        snapshot_generated_at=datetime.fromisoformat(snap.generated_at),
        snapshot_loaded_at=(
            datetime.fromtimestamp(snapshots.loaded_at).astimezone()
            if snapshots.loaded_at
            else None
        ),
        refresh_seconds=snapshots.refresh_seconds,
    )
