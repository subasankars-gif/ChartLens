"""The signed-in user and their watchlists (Firestore app state, ADR-0016)."""

from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, status
from pydantic import BaseModel, Field

from chartlens_api.appstate import User, Watchlist
from chartlens_api.deps import AppStateDep, CurrentUser, Snapshot
from chartlens_core.domain import utc_now

router = APIRouter(prefix="/me", tags=["me"])

MAX_WATCHLISTS = 20
MAX_SECURITIES = 200
WatchlistId = Annotated[str, Path(pattern=r"^[a-z0-9][a-z0-9-]{0,39}$")]


class WatchlistIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    security_ids: list[str] = Field(max_length=MAX_SECURITIES)


@router.get("", response_model=User)
def me(user: CurrentUser) -> User:
    return user


@router.get("/watchlists", response_model=list[Watchlist])
def list_watchlists(user: CurrentUser, appstate: AppStateDep) -> list[Watchlist]:
    return appstate.list_watchlists(user.uid)


@router.put("/watchlists/{watchlist_id}", response_model=Watchlist)
def put_watchlist(
    user: CurrentUser,
    appstate: AppStateDep,
    snap: Snapshot,
    watchlist_id: WatchlistId,
    body: WatchlistIn,
) -> Watchlist:
    """Create or replace a watchlist. Every security must exist in the served snapshot."""
    unknown = [s for s in body.security_ids if s not in snap.securities]
    if unknown:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"unknown security ids: {unknown[:5]}"
        )
    existing = {w.watchlist_id for w in appstate.list_watchlists(user.uid)}
    if watchlist_id not in existing and len(existing) >= MAX_WATCHLISTS:
        raise HTTPException(status.HTTP_409_CONFLICT, f"at most {MAX_WATCHLISTS} watchlists")
    ids = list(dict.fromkeys(body.security_ids))  # keep order, drop duplicates
    name = re.sub(r"\s+", " ", body.name).strip()
    return appstate.put_watchlist(
        user.uid,
        Watchlist(watchlist_id=watchlist_id, name=name, security_ids=ids, updated_at=utc_now()),
    )


@router.delete("/watchlists/{watchlist_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_watchlist(user: CurrentUser, appstate: AppStateDep, watchlist_id: WatchlistId) -> None:
    if not appstate.delete_watchlist(user.uid, watchlist_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such watchlist")
