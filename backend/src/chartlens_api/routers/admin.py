"""User administration: the allowlist (ADR-0016). Admins only."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from chartlens_api.appstate import Role, User, UserNotFound
from chartlens_api.deps import AdminUser, AppStateDep

router = APIRouter(prefix="/admin", tags=["admin"])


class UserPatch(BaseModel):
    enabled: bool | None = None
    role: Role | None = None


@router.get("/users", response_model=list[User])
def list_users(_: AdminUser, appstate: AppStateDep) -> list[User]:
    return appstate.list_users()


@router.patch("/users/{uid}", response_model=User)
def update_user(admin: AdminUser, appstate: AppStateDep, uid: str, body: UserPatch) -> User:
    if uid == admin.uid and (body.enabled is False or body.role == "user"):
        raise HTTPException(status.HTTP_409_CONFLICT, "admins cannot lock themselves out")
    try:
        return appstate.update_user(uid, enabled=body.enabled, role=body.role)
    except UserNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such user") from None
