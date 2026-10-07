"""One chart, one snapshot (ADR-0027 §3).

``GET /securities/{id}/chart`` is a thin envelope around two existing payloads read
from **one** published snapshot in one request, so a chart can never mix the bars of
snapshot A with the analysis of snapshot B:

- ``weekly``: exactly what ``/weekly`` returns for the same ``segments`` choice;
- ``analysis``: exactly what ``/analysis`` returns for the same ``sections``, or null
  with the stored reason (``analysis_status``) when the security is not analysed.

Every component names the ``meta_version`` it was read from (the envelope, ``weekly``
and ``analysis.envelope``), and they are equal by construction; the client checks
rather than infers. The request is declarative (security, segments, whole sections):
the server selects requested existing sections and returns them. Nothing here
filters by an analytical notion, ranks, computes or reinterprets.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Response, status
from pydantic import BaseModel

from chartlens_api.deps import CurrentUser, Snapshots
from chartlens_api.lake import LakeUnavailable
from chartlens_api.routers.analysis import (
    AnalysisResponse,
    ExplanationResponse,
    analysis_payload,
    explanation_payload,
)
from chartlens_api.routers.securities import WeeklyResponse, weekly_payload
from chartlens_core.canonical import canonical_json

router = APIRouter(prefix="/securities", tags=["chart"])

DEFAULT_SECTIONS = "identity,versions,current,provenance"
"""The small sections a chart needs first (ADR-0027 §6); heavier ones are requested
when their layer is turned on."""


class ChartResponse(BaseModel):
    security_id: str
    meta_version: str
    """The snapshot every component below was read from."""
    data_as_of: date
    segments: Literal["valid", "all"]
    analysis_status: Literal["analysed", "not_analysed", "no_analysis_in_snapshot"]
    weekly: WeeklyResponse
    analysis: AnalysisResponse | None
    explanations: ExplanationResponse | None
    """When asked for (``explanations=true``) and published: the bound explanation."""


@router.get(
    "/{security_id}/chart",
    response_model=ChartResponse,
    responses={404: {"description": "unknown security"}},
)
def chart(
    _: CurrentUser,
    snapshots: Snapshots,
    security_id: str,
    segments: Annotated[Literal["valid", "all"], Query()] = "valid",
    sections: Annotated[
        str, Query(description="Comma-separated whole sections of the published document")
    ] = DEFAULT_SECTIONS,
    explanations: Annotated[
        bool, Query(description="Also return the published explanation (ADR-0028)")
    ] = False,
    as_of: Annotated[str | None, Query(include_in_schema=False)] = None,
) -> Response:
    """Bars and requested analysis sections of one security, from one snapshot."""
    if as_of is not None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "point-in-time charts are not served by the API (K3, ADR-0016)",
        )
    try:
        read = snapshots.chart(security_id, explanations=explanations)
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown security {security_id}") from None
    except LakeUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from None
    snap = read.snapshot
    weekly = weekly_payload(snap, security_id, read.bars, segments).model_dump(mode="json")
    analysis = (
        analysis_payload(snap, read.entry, read.document, sections)
        if read.entry is not None and read.document is not None
        else None
    )
    body = {
        "security_id": security_id,
        "meta_version": snap.meta_version,
        "data_as_of": snap.as_of.isoformat(),
        "segments": segments,
        "analysis_status": read.analysis_status,
        "weekly": weekly,
        "analysis": analysis,
        "explanations": explanation_payload(snap, *read.explanation)
        if read.explanation is not None
        else None,
    }
    return Response(content=canonical_json(body), media_type="application/json")
