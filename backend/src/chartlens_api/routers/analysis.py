"""Published technical analysis (ADR-0026 Part 2).

> The API serves authoritative published results; it does not recompute, reinterpret,
> rank or modify them.

Two routes, both reading only what the live snapshot names:

- ``/securities/{id}/analysis``: the published document, verbatim, or whole named
  sections of it (``sections=``). Section selection is the only shaping.
- ``/securities/{id}/breakout-events``: rows of one published event dataset
  (``source`` is required; the datasets are never merged), matching explicit predicates
  on stored fields, in the stored order, paged by a cursor bound to the dataset's
  content hash.

Bodies are written with the canonical encoder, so every number is the stored canonical
text (ADR-0026 §2.3: serialization preserves the stored value's canonical numeric
representation). Formatting for display belongs to the frontend.
"""

from __future__ import annotations

import base64
import json
from datetime import date
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Response, status
from pydantic import BaseModel

from chartlens_api.deps import CurrentUser, Snapshots
from chartlens_api.lake import LakeUnavailable, NoAnalysisInSnapshot, NotAnalysed
from chartlens_core.canonical import canonical_json, content_hash
from chartlens_pipeline.serving import ServingSnapshot

router = APIRouter(prefix="/securities", tags=["analysis"])

MAX_EVENTS_PAGE = 500


class AnalysisEnvelope(BaseModel):
    """Provenance: describes the served object, never alters it."""

    security_id: str
    meta_version: str
    snapshot_generated_at: str
    data_as_of: date
    weekly_version: str
    methodology_hash: str
    analysis_version: str
    analysis_methodology_hash: str
    document_sha256: str
    """The content address of the published document (the canonical encoding of the
    full ``document`` hashes to it)."""
    sections: list[str]


class AnalysisResponse(BaseModel):
    envelope: AnalysisEnvelope
    document: dict[str, Any]
    """The requested sections of the published document, exactly as stored."""


class EventsEnvelope(BaseModel):
    security_id: str
    meta_version: str
    snapshot_generated_at: str
    data_as_of: date
    analysis_version: str
    dataset: str
    content_sha256: str
    physical_sha256: str
    row_count: int


class EventsResponse(BaseModel):
    envelope: EventsEnvelope
    rows: list[dict[str, Any]]
    """Stored rows matching the predicates, in the stored order (bar date, event key)."""
    next_cursor: str | None


def _json(body: dict[str, Any]) -> Response:
    return Response(content=canonical_json(body), media_type="application/json")


def _refuse_as_of(as_of: str | None) -> None:
    if as_of is not None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "historical analysis is not served by the API (K3, ADR-0016)",
        )


def _known(snap: ServingSnapshot, security_id: str) -> None:
    if security_id not in snap.securities:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown security {security_id}")


def _not_found(exc: LookupError) -> HTTPException:
    code = "no_analysis_in_snapshot" if isinstance(exc, NoAnalysisInSnapshot) else "not_analysed"
    return HTTPException(status.HTTP_404_NOT_FOUND, {"code": code, "message": str(exc)})


def _base(snap: ServingSnapshot, security_id: str) -> dict[str, Any]:
    analysis = snap.analysis or {}
    return {
        "security_id": security_id,
        "meta_version": snap.meta_version,
        "snapshot_generated_at": snap.generated_at,
        "data_as_of": snap.as_of.isoformat(),
        "analysis_version": analysis.get("analysis_version"),
    }


@router.get(
    "/{security_id}/analysis",
    response_model=AnalysisResponse,
    responses={404: {"description": "unknown_security, not_analysed, no_analysis_in_snapshot"}},
)
def analysis(
    _: CurrentUser,
    snapshots: Snapshots,
    security_id: str,
    sections: Annotated[
        str | None,
        Query(description="Comma-separated top-level sections; default: the whole document"),
    ] = None,
    as_of: Annotated[str | None, Query(include_in_schema=False)] = None,
) -> Response:
    """The published analysis of one security, verbatim, or whole named sections of it."""
    _refuse_as_of(as_of)
    try:
        snap, entry, document = snapshots.analysis_document(security_id)
    except (NoAnalysisInSnapshot, NotAnalysed) as exc:
        _known(snapshots.get(), security_id)
        raise _not_found(exc) from None
    except LakeUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from None
    chosen = list(document) if sections is None else [s for s in sections.split(",") if s]
    unknown = [s for s in chosen if s not in document]
    if unknown or not chosen:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"unknown sections {unknown}; the document has {list(document)}",
        )
    analysis_block = snap.analysis or {}
    envelope = {
        **_base(snap, security_id),
        "weekly_version": snap.versions["weekly_version"],
        "methodology_hash": snap.versions["methodology_hash"],
        "analysis_methodology_hash": analysis_block.get("analysis_methodology_hash"),
        "document_sha256": entry.document_sha256,
        "sections": chosen,
    }
    return _json({"envelope": envelope, "document": {s: document[s] for s in chosen}})


# ----------------------------------------------------------------------------- events


def _predicates(
    from_: date | None,
    to: date | None,
    direction: str | None,
    field: str | None,
    value: str | None,
) -> dict[str, str]:
    """Explicit predicates on stored fields only (ADR-0026 §2.2)."""
    out: dict[str, str] = {}
    if from_ is not None:
        out["from"] = from_.isoformat()
    if to is not None:
        out["to"] = to.isoformat()
    if direction is not None:
        out["direction"] = direction
    if field is not None and value is not None:
        out[field] = value
    return out


def _matches(row: dict[str, Any], predicates: dict[str, str]) -> bool:
    bar_date = str(row["bar_date"])  # ISO text: textual order is date order
    for name, value in predicates.items():
        if name == "from" and bar_date < value:
            return False
        if name == "to" and bar_date > value:
            return False
        if name not in ("from", "to") and row[name] != value:
            return False
    return True


def _encode_cursor(content_sha256: str, predicates: dict[str, str], offset: int) -> str:
    raw = json.dumps({"c": content_sha256, "p": content_hash(predicates), "o": offset})
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> dict[str, Any]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        decoded: dict[str, Any] = json.loads(base64.urlsafe_b64decode(padded))
        int(decoded["o"])
        str(decoded["c"])
        str(decoded["p"])
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid cursor") from None
    return decoded


@router.get(
    "/{security_id}/breakout-events",
    response_model=EventsResponse,
    responses={
        404: {"description": "unknown_security, not_analysed, no_analysis_in_snapshot"},
        409: {"description": "the snapshot changed between pages; restart without a cursor"},
    },
)
def breakout_events(
    _: CurrentUser,
    snapshots: Snapshots,
    security_id: str,
    source: Annotated[Literal["pattern", "level"], Query(description="One dataset; never merged")],
    from_: Annotated[date | None, Query(alias="from")] = None,
    to: date | None = None,
    direction: Literal["BREAKOUT", "BREAKDOWN"] | None = None,
    pattern_type: Annotated[str | None, Query(description="source=pattern only")] = None,
    level_source_type: Annotated[str | None, Query(description="source=level only")] = None,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_EVENTS_PAGE)] = 100,
    as_of: Annotated[str | None, Query(include_in_schema=False)] = None,
) -> Response:
    """Rows of one published breakout-event dataset matching explicit predicates on
    stored fields, in the stored order. Retrieval, never ranking or selection."""
    _refuse_as_of(as_of)
    if source == "level" and pattern_type is not None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "pattern_type applies to source=pattern")
    if source == "pattern" and level_source_type is not None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "level_source_type applies to source=level"
        )
    field, value = (
        ("pattern_type", pattern_type)
        if source == "pattern"
        else ("level_source_type", level_source_type)
    )
    predicates = _predicates(from_, to, direction, field, value)
    dataset = "pattern_breakouts" if source == "pattern" else "level_breakouts"
    try:
        snap, artifact, rows = snapshots.breakout_rows(security_id, dataset)
    except (NoAnalysisInSnapshot, NotAnalysed) as exc:
        _known(snapshots.get(), security_id)
        raise _not_found(exc) from None
    except LakeUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from None
    offset = 0
    if cursor is not None:
        decoded = _decode_cursor(cursor)
        if decoded["c"] != artifact.content_sha256:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "the snapshot changed between pages; restart"
            )
        if decoded["p"] != content_hash(predicates):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "the cursor is for other predicates")
        offset = int(decoded["o"])
    page: list[dict[str, Any]] = []
    position = offset
    while position < len(rows) and len(page) < limit:
        if _matches(rows[position], predicates):
            page.append(rows[position])
        position += 1
    more = position < len(rows)
    envelope = {
        **_base(snap, security_id),
        "dataset": dataset,
        "content_sha256": artifact.content_sha256,
        "physical_sha256": artifact.physical_sha256,
        "row_count": artifact.row_count,
    }
    return _json(
        {
            "envelope": envelope,
            "rows": page,
            "next_cursor": _encode_cursor(artifact.content_sha256, predicates, position)
            if more
            else None,
        }
    )
