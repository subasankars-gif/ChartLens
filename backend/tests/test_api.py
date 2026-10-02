"""API end to end over a real (small) lake: authentication, the allowlist, version-bound
reads from the serving snapshot, watchlists and admin (ADR-0016)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fakes import FakeVerifier, MemoryAppState
from fastapi.testclient import TestClient
from test_adjust import SESSIONS, build_lake

from chartlens_api.appstate import User
from chartlens_api.deps import appstate_dep, settings_dep, snapshots_dep, verifier_dep
from chartlens_api.lake import SnapshotProvider
from chartlens_api.main import create_app
from chartlens_core.config import ApiConfig, ChartLensSettings
from chartlens_pipeline.adjust import AdjustmentService, CorporateActionOverrides
from chartlens_pipeline.data_quality import DataQualityService
from chartlens_pipeline.identity import IdentityOverrides
from chartlens_pipeline.serving import ServingPublisher
from chartlens_pipeline.storage import DataLakeLayout, LocalObjectStore
from chartlens_pipeline.weekly import WeeklyService

WED = SESSIONS[17]
ADMIN = {"Authorization": "Bearer admin-uid:boss@example.com"}
STRANGER = {"Authorization": "Bearer someone:someone@example.com"}


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def env(tmp_path: Path) -> dict[str, Any]:
    settings, provider, store, today = build_lake(tmp_path, ex=WED)
    AdjustmentService(
        settings, provider, store, overrides=CorporateActionOverrides(), today=today
    ).run()
    DataQualityService(settings, provider, store, identity_overrides=IdentityOverrides()).run()
    WeeklyService(settings, provider, store).run()
    ServingPublisher(settings, provider, store).run()
    api_settings = ChartLensSettings.model_construct(
        api=ApiConfig(firebase_project_id="demo-test", admin_emails=("boss@example.com",))
    )
    clock = Clock()
    snapshots = SnapshotProvider(store, "NSE", refresh_seconds=60, clock=clock)
    appstate = MemoryAppState()
    app = create_app(api_settings)
    app.dependency_overrides.update(
        {
            settings_dep: lambda: api_settings,
            verifier_dep: FakeVerifier,
            appstate_dep: lambda: appstate,
            snapshots_dep: lambda: snapshots,
        }
    )
    return {
        "client": TestClient(app),
        "store": store,
        "appstate": appstate,
        "clock": clock,
        "pipeline": (settings, provider, store),
    }


def sid(client: TestClient, symbol: str) -> str:
    r = client.get("/api/v1/securities", params={"q": symbol}, headers=ADMIN)
    return next(s["security_id"] for s in r.json()["results"] if s["symbol"] == symbol)


# ----------------------------------------------------------------------------- auth


def test_every_route_but_health_needs_a_signed_in_allowlisted_user(env: dict[str, Any]) -> None:
    client: TestClient = env["client"]
    health = client.get("/api/v1/health").json()
    assert health["serving"]["data_as_of"] == SESSIONS[-1].isoformat()  # versions only
    for path in ("/api/v1/securities?q=SPLITCO", "/api/v1/system/status", "/api/v1/me"):
        assert client.get(path).status_code == 401
        assert client.get(path, headers={"Authorization": "Bearer junk"}).status_code == 401
        r = client.get(path, headers=STRANGER)
        assert (r.status_code, r.json()["detail"]) == (403, "access pending approval")
    # The stranger is now recorded as pending; nothing more.
    assert env["appstate"].users["someone"].enabled is False


def test_admin_bootstrap_needs_a_verified_email(env: dict[str, Any]) -> None:
    client: TestClient = env["client"]
    unverified = {"Authorization": "Bearer unverified:x-uid:boss@example.com"}
    assert client.get("/api/v1/me", headers=unverified).status_code == 403
    me = client.get("/api/v1/me", headers=ADMIN).json()
    assert (me["enabled"], me["role"]) == (True, "admin")


def test_admin_enables_a_pending_user_and_cannot_lock_themselves_out(env: dict[str, Any]) -> None:
    client: TestClient = env["client"]
    client.get("/api/v1/me", headers=STRANGER)
    assert client.get("/api/v1/admin/users", headers=STRANGER).status_code == 403
    users = client.get("/api/v1/admin/users", headers=ADMIN).json()
    assert {u["uid"] for u in users} == {"admin-uid", "someone"}
    r = client.patch("/api/v1/admin/users/someone", json={"enabled": True}, headers=ADMIN)
    assert r.json()["enabled"] is True
    assert client.get("/api/v1/me", headers=STRANGER).json()["role"] == "user"
    assert client.get("/api/v1/admin/users", headers=STRANGER).status_code == 403  # not admin
    r = client.patch("/api/v1/admin/users/admin-uid", json={"enabled": False}, headers=ADMIN)
    assert r.status_code == 409


def test_protected_routes_refuse_to_run_without_firebase_configured(tmp_path: Path) -> None:
    settings = ChartLensSettings.model_construct()
    app = create_app(settings)
    app.dependency_overrides[settings_dep] = lambda: settings
    r = TestClient(app).get("/api/v1/me", headers=ADMIN)
    assert (r.status_code, r.json()["detail"]) == (503, "authentication not configured")


# ----------------------------------------------------------------------------- data


def test_search_detail_and_data_quality_name_their_snapshot(env: dict[str, Any]) -> None:
    client: TestClient = env["client"]
    r = client.get("/api/v1/securities", params={"q": "demerco"}, headers=ADMIN).json()
    meta = r["meta_version"]
    (hit,) = r["results"]
    assert (hit["symbol"], hit["usable_from"], hit["instrument_type"]) == (
        "DEMERCO",
        WED.isoformat(),
        "EQUITY_SHARE",
    )
    detail = client.get(f"/api/v1/securities/{hit['security_id']}", headers=ADMIN).json()
    assert detail["meta_version"] == meta and len(detail["segments"]) == 2
    assert {i["identifier_type"] for i in detail["identifiers"]} >= {"SYMBOL", "ISIN"}
    dq = client.get(f"/api/v1/securities/{hit['security_id']}/data-quality", headers=ADMIN).json()
    assert dq["meta_version"] == meta
    assert "UNQUANTIFIED_ACTION" in {f["code"] for f in dq["findings"]}
    assert client.get("/api/v1/securities/SEC-nope", headers=ADMIN).status_code == 404

    status = client.get("/api/v1/system/status", headers=ADMIN).json()
    assert status["meta_version"] == meta and status["data_as_of"] == SESSIONS[-1].isoformat()
    assert set(status["versions"]) >= {"weekly_version", "data_version", "dq_version"}


def test_weekly_serves_stored_bars_as_exact_decimals(env: dict[str, Any]) -> None:
    client: TestClient = env["client"]
    demer = sid(client, "DEMERCO")
    valid = client.get(f"/api/v1/securities/{demer}/weekly", headers=ADMIN).json()
    full = client.get(
        f"/api/v1/securities/{demer}/weekly", params={"segments": "all"}, headers=ADMIN
    ).json()
    assert len(valid["segment_ids"]) == 2 and len(full["bars"]) > len(valid["bars"])
    assert {b["continuity_segment_id"] for b in valid["bars"]} == {valid["current_segment_id"]}
    first = valid["bars"][0]
    assert (first["first_session_date"], first["open"], first["partial_reason"]) == (
        WED.isoformat(),
        "70.000000",  # exact decimal text, never a float
        "CONTINUITY_BREAK",
    )
    assert valid["as_of"] == SESSIONS[-1].isoformat()
    # The API never builds point-in-time bars, and says so rather than ignoring the ask.
    r = client.get(f"/api/v1/securities/{demer}/weekly?as_of=2024-01-10", headers=ADMIN)
    assert r.status_code == 400 and "ADR-0016" in r.json()["detail"]


def test_a_republished_lake_is_never_mixed_with_the_served_snapshot(env: dict[str, Any]) -> None:
    client: TestClient = env["client"]
    store: LocalObjectStore = env["store"]
    demer = sid(client, "DEMERCO")
    before = client.get("/api/v1/system/status", headers=ADMIN).json()["meta_version"]
    # A later run rewrites a weekly file in place, but has not published a snapshot yet:
    key = DataLakeLayout.curated_weekly_key("NSE", demer)
    original = store.get(key)
    store.put(key, original + b"x")
    r = client.get(f"/api/v1/securities/{demer}/weekly", headers=ADMIN)
    assert r.status_code == 503  # refused, rather than served against the old snapshot
    # The run finishes: weekly republished (content unchanged here) and a new pointer.
    store.put(key, original)
    manifest_key = DataLakeLayout.serving_manifest_key("NSE")
    manifest = json.loads(store.get(manifest_key))
    store.put(
        manifest_key, json.dumps({**manifest, "generated_at": "2024-02-21T00:00:00+00:00"}).encode()
    )
    assert client.get(f"/api/v1/securities/{demer}/weekly", headers=ADMIN).status_code == 200
    assert client.get("/api/v1/system/status", headers=ADMIN).json()["meta_version"] == before


def test_a_new_snapshot_is_picked_up_after_the_refresh_interval(env: dict[str, Any]) -> None:
    client: TestClient = env["client"]
    store: LocalObjectStore = env["store"]
    old = client.get("/api/v1/system/status", headers=ADMIN).json()["meta_version"]
    key = DataLakeLayout.serving_manifest_key("NSE")
    manifest = json.loads(store.get(key))
    store.put(key, json.dumps({**manifest, "meta_version": "meta-next"}).encode())
    assert client.get("/api/v1/system/status", headers=ADMIN).json()["meta_version"] == old
    env["clock"].t += 61
    assert client.get("/api/v1/system/status", headers=ADMIN).json()["meta_version"] == "meta-next"
    # A pointer to files that do not verify is not served: the last good snapshot stays.
    store.put(
        key,
        json.dumps(
            {
                **manifest,
                "meta_version": "meta-broken",
                "files": {
                    **manifest["files"],
                    "securities": {**manifest["files"]["securities"], "sha256": "0" * 64},
                },
            }
        ).encode(),
    )
    env["clock"].t += 61
    assert client.get("/api/v1/system/status", headers=ADMIN).json()["meta_version"] == "meta-next"


# ----------------------------------------------------------------------------- watchlists


def test_watchlists_are_per_user_and_validated_against_the_snapshot(env: dict[str, Any]) -> None:
    client: TestClient = env["client"]
    split, plain = sid(client, "SPLITCO"), sid(client, "PLAIN")
    r = client.put(
        "/api/v1/me/watchlists/core",
        json={"name": "  Core   ideas ", "security_ids": [split, plain, split]},
        headers=ADMIN,
    )
    assert r.status_code == 200 and r.json()["security_ids"] == [split, plain]
    assert r.json()["name"] == "Core ideas"
    assert [
        w["watchlist_id"] for w in client.get("/api/v1/me/watchlists", headers=ADMIN).json()
    ] == ["core"]
    bad = client.put(
        "/api/v1/me/watchlists/core", json={"name": "x", "security_ids": ["SEC-x"]}, headers=ADMIN
    )
    assert bad.status_code == 422
    assert (
        client.put(
            "/api/v1/me/watchlists/Bad_Id", json={"name": "x", "security_ids": []}, headers=ADMIN
        ).status_code
        == 422
    )
    env["appstate"].create_user(User(uid="other", enabled=True))
    assert (
        client.get(
            "/api/v1/me/watchlists", headers={"Authorization": "Bearer other:o@x.com"}
        ).json()
        == []
    )
    assert client.delete("/api/v1/me/watchlists/core", headers=ADMIN).status_code == 204
    assert client.delete("/api/v1/me/watchlists/core", headers=ADMIN).status_code == 404


def test_large_responses_are_compressed(env: dict[str, Any]) -> None:
    client: TestClient = env["client"]
    r = client.get(
        f"/api/v1/securities/{sid(client, 'SPLITCO')}/weekly",
        params={"segments": "all"},
        headers={**ADMIN, "Accept-Encoding": "gzip"},
    )
    assert r.status_code == 200 and r.headers.get("content-encoding") == "gzip"
