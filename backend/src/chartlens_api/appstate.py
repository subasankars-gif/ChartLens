"""Application state in Firestore: users (the allowlist) and watchlists (ADR-0016).

This is the only state the API writes. Market data, security metadata and data quality
are never stored here: the lake is their single source of truth.

    users/{uid}                         email, enabled, role, created_at, updated_at
    users/{uid}/watchlists/{watchlist}  name, security_ids, updated_at
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from chartlens_core.domain import utc_now

Role = Literal["admin", "user"]


class User(BaseModel):
    uid: str
    email: str | None = None
    enabled: bool = False
    role: Role = "user"
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class Watchlist(BaseModel):
    watchlist_id: str
    name: str
    security_ids: list[str]
    updated_at: datetime = Field(default_factory=utc_now)


class AppState(Protocol):
    def get_user(self, uid: str) -> User | None: ...

    def create_user(self, user: User) -> User:
        """Create unless it exists (a concurrent first sign-in); return what is stored."""
        ...

    def update_user(self, uid: str, *, enabled: bool | None, role: Role | None) -> User: ...

    def list_users(self) -> list[User]: ...

    def list_watchlists(self, uid: str) -> list[Watchlist]: ...

    def put_watchlist(self, uid: str, watchlist: Watchlist) -> Watchlist: ...

    def delete_watchlist(self, uid: str, watchlist_id: str) -> bool: ...


class UserNotFound(KeyError):
    pass


class FirestoreAppState:
    """Firestore-backed app state. Honours ``FIRESTORE_EMULATOR_HOST`` (the client does)."""

    def __init__(self, project_id: str) -> None:
        from google.cloud import firestore

        self._db = firestore.Client(project=project_id)

    def _users(self) -> Any:
        return self._db.collection("users")

    def get_user(self, uid: str) -> User | None:
        snap = self._users().document(uid).get()
        return User(uid=uid, **snap.to_dict()) if snap.exists else None

    def create_user(self, user: User) -> User:
        from google.api_core.exceptions import AlreadyExists

        ref = self._users().document(user.uid)
        try:
            ref.create(user.model_dump(exclude={"uid"}))
        except AlreadyExists:
            existing = self.get_user(user.uid)
            assert existing is not None
            return existing
        return user

    def update_user(self, uid: str, *, enabled: bool | None, role: Role | None) -> User:
        user = self.get_user(uid)
        if user is None:
            raise UserNotFound(uid)
        changes: dict[str, Any] = {"updated_at": utc_now()}
        if enabled is not None:
            changes["enabled"] = enabled
        if role is not None:
            changes["role"] = role
        self._users().document(uid).update(changes)
        return user.model_copy(update=changes)

    def list_users(self) -> list[User]:
        return sorted(
            (User(uid=d.id, **d.to_dict()) for d in self._users().stream()),
            key=lambda u: (u.email or "", u.uid),
        )

    def _watchlists(self, uid: str) -> Any:
        return self._users().document(uid).collection("watchlists")

    def list_watchlists(self, uid: str) -> list[Watchlist]:
        return sorted(
            (Watchlist(watchlist_id=d.id, **d.to_dict()) for d in self._watchlists(uid).stream()),
            key=lambda w: w.watchlist_id,
        )

    def put_watchlist(self, uid: str, watchlist: Watchlist) -> Watchlist:
        self._watchlists(uid).document(watchlist.watchlist_id).set(
            watchlist.model_dump(exclude={"watchlist_id"})
        )
        return watchlist

    def delete_watchlist(self, uid: str, watchlist_id: str) -> bool:
        ref = self._watchlists(uid).document(watchlist_id)
        if not ref.get().exists:
            return False
        ref.delete()
        return True
