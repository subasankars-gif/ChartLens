"""Securities, weekly bars and data quality — read from the serving snapshot (ADR-0016).

Every response names the ``meta_version`` it was served from. Prices are exact decimal
strings, never binary floats. Nothing here computes analysis or builds bars: weekly bars
are the stored ones (the data's own ``as_of``); point-in-time views of earlier dates are
not served by the API (they will come from a batch-built dataset).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel

from chartlens_api.deps import CurrentUser, Snapshot, Snapshots
from chartlens_api.lake import LakeUnavailable
from chartlens_pipeline.serving import ServingSnapshot

router = APIRouter(prefix="/securities", tags=["securities"])


class SecuritySummary(BaseModel):
    security_id: str
    symbol: str | None
    name: str | None
    isin: str | None
    series: str | None
    listing_status: str
    instrument_type: str
    analytical_universe: bool
    status: str
    usable_from: date | None
    last_date: date


class SearchResponse(BaseModel):
    meta_version: str
    data_as_of: date
    query: str
    results: list[SecuritySummary]


class Segment(BaseModel):
    continuity_segment_id: str
    segment_start: date
    segment_end: date
    sessions: int
    cause: str


class Identifier(BaseModel):
    identifier_type: str
    identifier_value: str
    valid_from: date
    valid_to: date


class SecurityDetail(SecuritySummary):
    meta_version: str
    data_as_of: date
    first_date: date
    sessions: int
    usable_sessions: int
    continuity_breaks: int
    warnings: int
    failures: int
    current_segment_id: str
    segments: list[Segment]
    identifiers: list[Identifier]


class WeeklyBarOut(BaseModel):
    continuity_segment_id: str
    iso_year: int
    iso_week: int
    week_start_date: date
    week_end_date: date
    first_session_date: date
    last_session_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    raw_close: Decimal
    trading_days: int
    is_complete: bool
    partial_reason: str | None
    special_sessions: int
    closes_on_special_session: bool
    closing_session_type: str | None


class WeeklyResponse(BaseModel):
    security_id: str
    meta_version: str
    as_of: date
    versions: dict[str, str]
    segments: Literal["valid", "all"]
    current_segment_id: str
    segment_ids: list[str]
    bars: list[WeeklyBarOut]


class Finding(BaseModel):
    start_date: date | None
    end_date: date | None
    dimension: str
    severity: str
    code: str
    breaks_continuity: bool
    detail: str
    evidence: str


class DataQualityResponse(BaseModel):
    security_id: str
    meta_version: str
    data_as_of: date
    dq_version: str
    status: str
    usable_from: date | None
    continuity_breaks: int
    warnings: int
    failures: int
    findings: list[Finding]


def _security(snap: ServingSnapshot, security_id: str) -> dict[str, Any]:
    row = snap.securities.get(security_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown security {security_id}")
    return row


@router.get("", response_model=SearchResponse)
def search(
    _: CurrentUser,
    snap: Snapshot,
    q: Annotated[str, Query(min_length=1, max_length=40, description="Symbol, ISIN or name")],
    universe: Annotated[Literal["analytical", "all"], Query()] = "analytical",
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> SearchResponse:
    """Search current and past symbols, ISINs and names."""
    rows = snap.search(q, analytical_only=universe == "analytical", limit=limit)
    return SearchResponse(
        meta_version=snap.meta_version,
        data_as_of=snap.as_of,
        query=q,
        results=[SecuritySummary.model_validate(r) for r in rows],
    )


@router.get("/{security_id}", response_model=SecurityDetail)
def detail(_: CurrentUser, snap: Snapshot, security_id: str) -> SecurityDetail:
    row = _security(snap, security_id)
    return SecurityDetail.model_validate(
        {
            **row,
            "meta_version": snap.meta_version,
            "data_as_of": snap.as_of,
            "segments": snap.segments.get(security_id, []),
            "identifiers": snap.identifiers.get(security_id, []),
        }
    )


@router.get("/{security_id}/weekly", response_model=WeeklyResponse)
def weekly(
    _: CurrentUser,
    snapshots: Snapshots,
    security_id: str,
    segments: Annotated[Literal["valid", "all"], Query()] = "valid",
    as_of: Annotated[str | None, Query(include_in_schema=False)] = None,
) -> WeeklyResponse:
    """Stored weekly bars as of the data's ``as_of``. ``segments=valid`` (default) returns
    only the continuity segment analysis may use; ``all`` returns every segment (charts)."""
    if as_of is not None:
        # Refused explicitly rather than ignored: a client must never believe it received
        # a point-in-time view. Those need bars built and adjusted as of the date, which
        # the API never does (ADR-0016).
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "point-in-time weekly views are not served by the API (ADR-0016)",
        )
    try:
        snap, bars = snapshots.weekly_bars(security_id)
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown security {security_id}") from None
    except LakeUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from None
    row = _security(snap, security_id)
    current = row["current_segment_id"]
    chosen = bars if segments == "all" else [b for b in bars if b.continuity_segment_id == current]
    return WeeklyResponse(
        security_id=security_id,
        meta_version=snap.meta_version,
        as_of=snap.as_of,
        versions=snap.versions,
        segments=segments,
        current_segment_id=current,
        segment_ids=[s["continuity_segment_id"] for s in snap.segments.get(security_id, [])],
        bars=[WeeklyBarOut.model_validate(b, from_attributes=True) for b in chosen],
    )


@router.get("/{security_id}/data-quality", response_model=DataQualityResponse)
def data_quality(_: CurrentUser, snap: Snapshot, security_id: str) -> DataQualityResponse:
    row = _security(snap, security_id)
    return DataQualityResponse.model_validate(
        {
            **row,
            "security_id": security_id,
            "meta_version": snap.meta_version,
            "data_as_of": snap.as_of,
            "dq_version": snap.versions["dq_version"],
            "findings": snap.findings.get(security_id, []),
        }
    )
