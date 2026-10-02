"""FastAPI dependencies. Tests override them via ``app.dependency_overrides``.

Every route except ``/health`` depends on :func:`current_user`: authentication (a valid
Firebase ID token) *and* authorization (an enabled user in the Firestore allowlist).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status

from chartlens_api.appstate import AppState, FirestoreAppState, User
from chartlens_api.auth import AuthenticationError, FirebaseTokenVerifier, TokenVerifier
from chartlens_api.lake import LakeUnavailable, SnapshotProvider
from chartlens_core.config import ChartLensSettings, get_settings
from chartlens_pipeline.serving import ServingSnapshot
from chartlens_pipeline.storage import object_store_from_config


def settings_dep() -> ChartLensSettings:
    return get_settings()


_providers: dict[str, SnapshotProvider] = {}


def _snapshots(settings: ChartLensSettings) -> SnapshotProvider:
    """One provider per lake location and exchange for the life of the process."""
    key = f"{settings.storage.model_dump_json()}|{settings.universe.exchange}"
    if key not in _providers:
        _providers[key] = SnapshotProvider(
            object_store_from_config(settings.storage),
            settings.universe.exchange,
            refresh_seconds=settings.api.snapshot_refresh_seconds,
            weekly_cache_size=settings.api.weekly_cache_size,
        )
    return _providers[key]


def snapshots_dep(
    settings: Annotated[ChartLensSettings, Depends(settings_dep)],
) -> SnapshotProvider:
    return _snapshots(settings)


def _project(settings: ChartLensSettings) -> str:
    project = settings.api.firebase_project_id
    if not project:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "authentication not configured")
    return project


@lru_cache(maxsize=4)
def _verifier(project: str) -> TokenVerifier:
    return FirebaseTokenVerifier(project)


@lru_cache(maxsize=4)
def _appstate(project: str) -> AppState:
    return FirestoreAppState(project)


def verifier_dep(settings: Annotated[ChartLensSettings, Depends(settings_dep)]) -> TokenVerifier:
    return _verifier(_project(settings))


def appstate_dep(settings: Annotated[ChartLensSettings, Depends(settings_dep)]) -> AppState:
    return _appstate(_project(settings))


def current_user(
    settings: Annotated[ChartLensSettings, Depends(settings_dep)],
    verifier: Annotated[TokenVerifier, Depends(verifier_dep)],
    appstate: Annotated[AppState, Depends(appstate_dep)],
    authorization: Annotated[str | None, Header()] = None,
) -> User:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "sign in required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        claims = verifier.verify(token)
    except AuthenticationError:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "invalid or expired sign-in",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None
    user = appstate.get_user(claims.uid)
    if user is None:
        admins = {e.lower() for e in settings.api.admin_emails}
        bootstrap = bool(claims.email_verified and claims.email and claims.email.lower() in admins)
        user = appstate.create_user(
            User(
                uid=claims.uid,
                email=claims.email,
                enabled=bootstrap,
                role="admin" if bootstrap else "user",
            )
        )
    if not user.enabled:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "access pending approval")
    return user


def admin_user(user: Annotated[User, Depends(current_user)]) -> User:
    if user.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin only")
    return user


def snapshot_dep(
    snapshots: Annotated[SnapshotProvider, Depends(snapshots_dep)],
) -> ServingSnapshot:
    try:
        return snapshots.get()
    except LakeUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from None


CurrentUser = Annotated[User, Depends(current_user)]
AdminUser = Annotated[User, Depends(admin_user)]
Snapshot = Annotated[ServingSnapshot, Depends(snapshot_dep)]
Snapshots = Annotated[SnapshotProvider, Depends(snapshots_dep)]
AppStateDep = Annotated[AppState, Depends(appstate_dep)]
Settings = Annotated[ChartLensSettings, Depends(settings_dep)]
