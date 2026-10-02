"""Authentication (who is calling) and authorization (may they), kept separate (ADR-0016).

    Firebase ID token → verify → uid → users/{uid} in Firestore → enabled? → access

A valid Google sign-in alone grants nothing: the user must also be enabled in the
Firestore allowlist. A first sign-in creates a *pending* user record (admins listed in
``api.admin_emails`` are enabled as admins on their first sign-in, with a verified email).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class AuthenticationError(Exception):
    """No token, or one that does not verify."""


@dataclass(frozen=True)
class Claims:
    uid: str
    email: str | None
    email_verified: bool


class TokenVerifier(Protocol):
    def verify(self, token: str) -> Claims: ...


class FirebaseTokenVerifier:
    """Verifies Firebase ID tokens with the Firebase Admin SDK.

    Honours ``FIREBASE_AUTH_EMULATOR_HOST`` (the SDK does), so the same code path runs
    against the emulator in tests."""

    def __init__(self, project_id: str) -> None:
        import firebase_admin

        name = f"chartlens-{project_id}"
        try:
            self._app = firebase_admin.get_app(name)
        except ValueError:
            self._app = firebase_admin.initialize_app(options={"projectId": project_id}, name=name)

    def verify(self, token: str) -> Claims:
        from firebase_admin import auth

        try:
            decoded = auth.verify_id_token(token, app=self._app, clock_skew_seconds=10)
        except Exception as exc:  # the SDK raises several types; all mean "not verified"
            raise AuthenticationError(type(exc).__name__) from None
        return Claims(
            uid=str(decoded["uid"]),
            email=decoded.get("email"),
            email_verified=bool(decoded.get("email_verified", False)),
        )
