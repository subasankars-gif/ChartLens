"""The real Firebase code paths against the Firebase emulators (ADR-0016).

Runs only when the emulators are up, as in CI:

    npx firebase-tools emulators:exec --only auth,firestore --project demo-chartlens \
        "uv run pytest -m emulator backend/tests"

Exercises ``FirebaseTokenVerifier`` and ``FirestoreAppState`` exactly as Cloud Run uses
them: real ID tokens from the Auth emulator, real documents in the Firestore emulator.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from test_adjust import SESSIONS, build_lake

from chartlens_api.deps import settings_dep, snapshots_dep
from chartlens_api.lake import SnapshotProvider
from chartlens_api.main import create_app
from chartlens_core.config import ApiConfig, ChartLensSettings
from chartlens_pipeline.adjust import AdjustmentService, CorporateActionOverrides
from chartlens_pipeline.data_quality import DataQualityService
from chartlens_pipeline.identity import IdentityOverrides
from chartlens_pipeline.serving import ServingPublisher
from chartlens_pipeline.weekly import WeeklyService

PROJECT = "demo-chartlens"
AUTH = os.environ.get("FIREBASE_AUTH_EMULATOR_HOST")

if os.environ.get("CHARTLENS_REQUIRE_EMULATORS") and not AUTH:
    raise RuntimeError("CHARTLENS_REQUIRE_EMULATORS is set but the emulators are not running")

pytestmark = [
    pytest.mark.emulator,
    pytest.mark.skipif(
        not (AUTH and os.environ.get("FIRESTORE_EMULATOR_HOST")),
        reason="Firebase emulators not running",
    ),
]


def _token(email: str, *, verified: bool) -> tuple[str, str]:
    """(uid, ID token) for a new emulator user, with the email verified if asked."""
    base = f"http://{AUTH}/identitytoolkit.googleapis.com/v1"
    body = {"email": email, "password": "pw-123456", "returnSecureToken": True}
    r = httpx.post(f"{base}/accounts:signUp?key=fake", json=body).json()
    uid = r["localId"]
    if verified:
        httpx.post(
            f"{base}/projects/{PROJECT}/accounts:update",
            json={"localId": uid, "emailVerified": True},
            headers={"Authorization": "Bearer owner"},
        ).raise_for_status()
        r = httpx.post(f"{base}/accounts:signInWithPassword?key=fake", json=body).json()
    return uid, r["idToken"]


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    settings, provider, store, today = build_lake(tmp_path, ex=SESSIONS[17])
    AdjustmentService(
        settings, provider, store, overrides=CorporateActionOverrides(), today=today
    ).run()
    DataQualityService(settings, provider, store, identity_overrides=IdentityOverrides()).run()
    WeeklyService(settings, provider, store).run()
    ServingPublisher(settings, provider, store).run()
    admin = f"boss-{uuid.uuid4().hex[:8]}@example.com"
    api = ChartLensSettings.model_construct(
        api=ApiConfig(firebase_project_id=PROJECT, admin_emails=(admin,))
    )
    app = create_app(api)
    snapshots = SnapshotProvider(store, "NSE")
    app.dependency_overrides.update({settings_dep: lambda: api, snapshots_dep: lambda: snapshots})
    c = TestClient(app)
    c.admin_email = admin  # type: ignore[attr-defined]
    return c


def test_real_tokens_and_the_firestore_allowlist(client: TestClient) -> None:
    admin_uid, admin_token = _token(client.admin_email, verified=True)  # type: ignore[attr-defined]
    admin: dict[str, Any] = {"Authorization": f"Bearer {admin_token}"}
    me = client.get("/api/v1/me", headers=admin).json()
    assert (me["uid"], me["enabled"], me["role"]) == (admin_uid, True, "admin")

    assert client.get("/api/v1/me", headers={"Authorization": "Bearer forged"}).status_code == 401
    # An admin email whose verification is not proven does not become admin.
    _, unverified = _token(f"other-{uuid.uuid4().hex[:6]}@example.com", verified=False)
    pending = {"Authorization": f"Bearer {unverified}"}
    assert client.get("/api/v1/me", headers=pending).status_code == 403

    uids = {u["uid"]: u for u in client.get("/api/v1/admin/users", headers=admin).json()}
    other = next(u for u, v in uids.items() if not v["enabled"])
    client.patch(f"/api/v1/admin/users/{other}", json={"enabled": True}, headers=admin)
    assert client.get("/api/v1/me", headers=pending).json()["enabled"] is True

    sid = client.get("/api/v1/securities", params={"q": "PLAIN"}, headers=admin).json()["results"][
        0
    ]["security_id"]
    put = client.put(
        "/api/v1/me/watchlists/main", json={"name": "Main", "security_ids": [sid]}, headers=admin
    )
    assert put.status_code == 200
    stored = client.get("/api/v1/me/watchlists", headers=admin).json()
    assert [(w["watchlist_id"], w["security_ids"]) for w in stored] == [("main", [sid])]
    assert client.get("/api/v1/me/watchlists", headers=pending).json() == []
