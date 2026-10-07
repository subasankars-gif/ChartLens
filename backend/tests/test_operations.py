"""Operations API (ADR-0018): admin-only refresh, run status, snapshot history — over a
real small lake, an in-memory run store and a fake GitHub dispatcher."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fakes import FakeVerifier, MemoryAppState, publish_with_analysis
from fastapi.testclient import TestClient
from test_adjust import SESSIONS, build_lake

from chartlens_api.appstate import User
from chartlens_api.deps import (
    appstate_dep,
    dispatcher_dep,
    runs_dep,
    settings_dep,
    snapshots_dep,
    verifier_dep,
)
from chartlens_api.github import DispatchFailed
from chartlens_api.lake import SnapshotProvider
from chartlens_api.main import create_app
from chartlens_core.config import ApiConfig, ChartLensSettings
from chartlens_core.runs import RunRecord, RunStatus, Stage, start_run, start_stage
from chartlens_pipeline.adjust import AdjustmentService, CorporateActionOverrides
from chartlens_pipeline.data_quality import DataQualityService
from chartlens_pipeline.identity import IdentityOverrides
from chartlens_pipeline.runs import MemoryRunStore
from chartlens_pipeline.storage import LocalObjectStore
from chartlens_pipeline.weekly import WeeklyService

ADMIN = {"Authorization": "Bearer admin-uid:boss@example.com"}
USER = {"Authorization": "Bearer reader:reader@example.com"}
PENDING = {"Authorization": "Bearer someone:someone@example.com"}
REPO = "subasankars-gif/ChartLens"


class FakeDispatcher:
    def __init__(self) -> None:
        self.dispatched: list[str] = []
        self.fail: str | None = None

    def dispatch(self, run_id: str) -> None:
        if self.fail:
            raise DispatchFailed(self.fail)
        self.dispatched.append(run_id)


def make_client(
    store: LocalObjectStore, runs: MemoryRunStore, dispatcher: FakeDispatcher | None
) -> TestClient:
    settings = ChartLensSettings.model_construct(
        api=ApiConfig(
            firebase_project_id="demo-test",
            admin_emails=("boss@example.com",),
            github_repository=REPO,
        )
    )
    appstate = MemoryAppState()
    appstate.create_user(User(uid="reader", email="reader@example.com", enabled=True))
    snapshots = SnapshotProvider(store, "NSE")
    app = create_app(settings)
    app.dependency_overrides.update(
        {
            settings_dep: lambda: settings,
            verifier_dep: FakeVerifier,
            appstate_dep: lambda: appstate,
            snapshots_dep: lambda: snapshots,
            runs_dep: lambda: runs,
            dispatcher_dep: lambda: dispatcher,
        }
    )
    return TestClient(app)


@pytest.fixture
def env(tmp_path: Path) -> dict[str, Any]:
    settings, provider, store, today = build_lake(tmp_path, ex=SESSIONS[17])
    AdjustmentService(
        settings, provider, store, overrides=CorporateActionOverrides(), today=today
    ).run()
    DataQualityService(settings, provider, store, identity_overrides=IdentityOverrides()).run()
    WeeklyService(settings, provider, store).run()
    runs = MemoryRunStore()
    published = publish_with_analysis(settings, provider, store, history=runs, run_id="gh-1-1")
    dispatcher = FakeDispatcher()
    return {
        "client": make_client(store, runs, dispatcher),
        "runs": runs,
        "dispatcher": dispatcher,
        "store": store,
        "meta": published["meta_version"],
        "tmp": tmp_path,
    }


ROUTES = [
    ("POST", "/api/v1/refresh/daily"),
    ("GET", "/api/v1/jobs"),
    ("GET", "/api/v1/jobs/run-x"),
    ("POST", "/api/v1/jobs/run-x/cancel"),
    ("GET", "/api/v1/system/operations"),
    ("GET", "/api/v1/system/snapshots"),
]


# ----------------------------------------------------------------------------- authorization


@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_operations_need_a_signed_in_approved_user(
    env: dict[str, Any], method: str, path: str
) -> None:
    client: TestClient = env["client"]
    assert client.request(method, path).status_code == 401
    assert client.request(method, path, headers={"Authorization": "Bearer x"}).status_code == 401
    r = client.request(method, path, headers=PENDING)
    assert (r.status_code, r.json()["detail"]) == (403, "access pending approval")
    assert env["dispatcher"].dispatched == [] and env["runs"].runs == {}


def test_only_an_admin_can_start_a_refresh(env: dict[str, Any]) -> None:
    client: TestClient = env["client"]
    r = client.post("/api/v1/refresh/daily", headers=USER)
    assert (r.status_code, r.json()["detail"]) == (403, "admin only")
    assert env["dispatcher"].dispatched == []
    r = client.post("/api/v1/refresh/daily", headers=ADMIN)
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "QUEUED" and env["dispatcher"].dispatched == [body["run_id"]]
    run = env["runs"].get(body["run_id"])
    assert run is not None
    assert (run.trigger, run.requested_by, run.requested_by_uid, run.status) == (
        "api",
        "boss@example.com",
        "admin-uid",
        RunStatus.QUEUED,
    )


def test_a_second_refresh_is_refused_while_one_is_active(env: dict[str, Any]) -> None:
    client: TestClient = env["client"]
    first = client.post("/api/v1/refresh/daily", headers=ADMIN).json()["run_id"]
    r = client.post("/api/v1/refresh/daily", headers=ADMIN)
    assert r.status_code == 409
    assert (r.json()["run_id"], r.json()["status"]) == (first, "QUEUED")
    assert env["dispatcher"].dispatched == [first]
    # Started by the workflow: still exactly one.
    env["runs"].claim(first, datetime.now(UTC), github_run_id="5", github_run_attempt=1)
    assert client.post("/api/v1/refresh/daily", headers=ADMIN).status_code == 409


def test_a_refused_dispatch_fails_the_run_and_frees_the_lock(env: dict[str, Any]) -> None:
    client: TestClient = env["client"]
    env["dispatcher"].fail = "GitHub answered 422"
    r = client.post("/api/v1/refresh/daily", headers=ADMIN)
    assert r.status_code == 502
    run = env["runs"].get(r.json()["run_id"])
    assert run is not None and run.status == RunStatus.FAILED
    assert run.error_summary == "the workflow could not be started: GitHub answered 422"
    env["dispatcher"].fail = None
    assert client.post("/api/v1/refresh/daily", headers=ADMIN).status_code == 202


def test_refresh_without_a_github_app_is_unavailable(env: dict[str, Any]) -> None:
    client = make_client(env["store"], env["runs"], None)
    r = client.post("/api/v1/refresh/daily", headers=ADMIN)
    assert (r.status_code, r.json()["detail"]) == (503, "refresh is not configured")
    ops = client.get("/api/v1/system/operations", headers=ADMIN).json()
    assert (ops["refresh_configured"], ops["can_refresh"]) == (False, False)


# ----------------------------------------------------------------------------- reading runs


def test_a_run_shows_its_stages_and_hides_admin_details_from_users(env: dict[str, Any]) -> None:
    client: TestClient = env["client"]
    run_id = client.post("/api/v1/refresh/daily", headers=ADMIN).json()["run_id"]
    now = datetime.now(UTC)
    run = env["runs"].claim(run_id, now, github_run_id="4242", github_run_attempt=1)
    env["runs"].save(start_stage(run, Stage.INGEST, now))

    admin = client.get(f"/api/v1/jobs/{run_id}", headers=ADMIN).json()
    assert (admin["status"], admin["current_stage"]) == ("RUNNING", "INGEST")
    assert admin["requested_by"] == "boss@example.com"
    assert admin["github_run_url"] == f"https://github.com/{REPO}/actions/runs/4242"
    assert [s["stage"] for s in admin["stages"]] == [s.value for s in Stage]
    assert admin["stages"][0]["status"] == "RUNNING" and admin["stages"][1]["status"] == "QUEUED"

    user = client.get(f"/api/v1/jobs/{run_id}", headers=USER).json()
    assert user["requested_by"] == "an administrator"
    assert user["github_run_id"] is None and user["github_run_url"] is None
    assert user["stages"] == admin["stages"]


def test_unknown_and_malformed_run_ids(env: dict[str, Any]) -> None:
    client: TestClient = env["client"]
    assert client.get("/api/v1/jobs/run-nope", headers=USER).status_code == 404
    assert client.get("/api/v1/jobs/..%2Fx", headers=USER).status_code in (404, 422)
    assert client.get(f"/api/v1/jobs/{'x' * 81}", headers=USER).status_code == 422


def test_recent_runs_page_newest_first(env: dict[str, Any]) -> None:
    client: TestClient = env["client"]
    runs: MemoryRunStore = env["runs"]
    base = datetime.now(UTC) - timedelta(hours=1)
    for i in range(5):
        t = base + timedelta(minutes=i)
        runs.create(
            RunRecord(run_id=f"run-{i}", trigger="api", requested_by="b@x.com", requested_at=t), t
        )
        runs.end_active(f"run-{i}", t, RunStatus.FAILED, "x")
    page = client.get("/api/v1/jobs", params={"limit": 3}, headers=USER).json()
    assert [r["run_id"] for r in page["runs"]] == ["run-4", "run-3", "run-2"]
    older = client.get(
        "/api/v1/jobs", params={"limit": 3, "before": page["next_before"]}, headers=USER
    ).json()
    assert [r["run_id"] for r in older["runs"]] == ["run-1", "run-0"]
    assert older["next_before"] is None
    assert client.get("/api/v1/jobs", params={"limit": 500}, headers=USER).status_code == 422


def test_a_lost_run_is_shown_failed_and_does_not_block_a_refresh(env: dict[str, Any]) -> None:
    client: TestClient = env["client"]
    runs: MemoryRunStore = env["runs"]
    old = datetime.now(UTC) - timedelta(hours=7)
    stuck = start_run(
        RunRecord(run_id="gh-9-1", trigger="schedule", requested_by="schedule", requested_at=old),
        old,
        github_run_id="9",
        github_run_attempt=1,
    )
    runs.create(stuck, old)
    shown = client.get("/api/v1/jobs/gh-9-1", headers=USER).json()
    assert shown["status"] == "FAILED" and shown["error_summary"].startswith("lost")
    stored = runs.get("gh-9-1")
    assert stored is not None and stored.status == RunStatus.RUNNING  # reads never write
    assert client.post("/api/v1/refresh/daily", headers=ADMIN).status_code == 202
    stored = runs.get("gh-9-1")
    assert stored is not None and stored.status == RunStatus.FAILED


# ----------------------------------------------------------------------------- status, history


def test_operations_status_names_the_live_snapshot_and_runs(env: dict[str, Any]) -> None:
    client: TestClient = env["client"]
    ops = client.get("/api/v1/system/operations", headers=USER).json()
    assert ops["api_status"] == "ok" and ops["serving"]["meta_version"] == env["meta"]
    assert ops["serving"]["data_as_of"] == SESSIONS[-1].isoformat()
    assert "methodology_hash" in ops["serving"]["versions"]
    assert ops["last_run"] is None and ops["active_run"] is None
    assert (ops["refresh_configured"], ops["can_refresh"]) == (True, False)
    run_id = client.post("/api/v1/refresh/daily", headers=ADMIN).json()["run_id"]
    ops = client.get("/api/v1/system/operations", headers=ADMIN).json()
    assert ops["can_refresh"] is True
    assert ops["active_run"]["run_id"] == run_id == ops["last_run"]["run_id"]


def test_operations_status_answers_before_the_first_snapshot(tmp_path: Path) -> None:
    client = make_client(LocalObjectStore(tmp_path / "empty"), MemoryRunStore(), FakeDispatcher())
    ops = client.get("/api/v1/system/operations", headers=ADMIN)
    assert ops.status_code == 200 and ops.json()["serving"] is None


def test_snapshot_history_marks_the_live_one(env: dict[str, Any]) -> None:
    client: TestClient = env["client"]
    history = client.get("/api/v1/system/snapshots", headers=USER).json()
    assert len(history) == 1
    snap = history[0]
    assert (snap["snapshot_id"], snap["live"], snap["status"], snap["run_id"]) == (
        env["meta"],
        True,
        "PUBLISHED",
        "gh-1-1",
    )
    assert snap["schema_version"] == 3 and "weekly_version" in snap["versions"]
    assert snap["analysis"]["securities"] == snap["counts"]["analysed"]
    assert snap["analysis"]["analysis_version"].startswith("analysis-")


def test_no_secret_reaches_a_response(env: dict[str, Any]) -> None:
    secret = "-----BEGIN RSA PRIVATE KEY-----TOPSECRET"
    settings = ChartLensSettings.model_construct(
        api=ApiConfig(
            firebase_project_id="demo-test",
            admin_emails=("boss@example.com",),
            github_repository=REPO,
            github_app_id="123",
            github_app_private_key=secret,  # type: ignore[arg-type]
        )
    )
    client: TestClient = env["client"]
    app = client.app
    app.dependency_overrides[settings_dep] = lambda: settings  # type: ignore[attr-defined]
    del app.dependency_overrides[dispatcher_dep]  # type: ignore[attr-defined]
    for path in ("/api/v1/system/operations", "/api/v1/jobs", "/api/v1/openapi.json"):
        r = client.get(path, headers=ADMIN)
        assert r.status_code == 200 and "TOPSECRET" not in r.text
    assert client.get("/api/v1/system/operations", headers=ADMIN).json()["refresh_configured"]


# ----------------------------------------------------------------------------- review fixes


def test_any_dispatch_error_fails_the_run_and_frees_the_lock(env: dict[str, Any]) -> None:
    class Broken:
        def dispatch(self, run_id: str) -> None:
            raise ValueError("No key could be detected.")  # e.g. a malformed PEM

    client = make_client(env["store"], env["runs"], Broken())  # type: ignore[arg-type]
    r = client.post("/api/v1/refresh/daily", headers=ADMIN)
    assert r.status_code == 502
    run = env["runs"].get(r.json()["run_id"])
    assert run is not None and run.status == RunStatus.FAILED
    assert run.error_summary == "the workflow could not be started: unexpected error (ValueError)"
    assert env["runs"].active() is None


def test_an_admin_cancels_a_queued_run_nobody_started(env: dict[str, Any]) -> None:
    client: TestClient = env["client"]
    run_id = client.post("/api/v1/refresh/daily", headers=ADMIN).json()["run_id"]
    assert client.post(f"/api/v1/jobs/{run_id}/cancel", headers=USER).status_code == 403
    assert client.post(f"/api/v1/jobs/{run_id}/cancel").status_code == 401
    r = client.post(f"/api/v1/jobs/{run_id}/cancel", headers=ADMIN)
    assert r.status_code == 200 and r.json()["status"] == "CANCELLED"
    assert r.json()["error_summary"] == "cancelled by boss@example.com before it started"
    assert client.post(f"/api/v1/jobs/{run_id}/cancel", headers=ADMIN).status_code == 409
    assert client.post("/api/v1/jobs/run-nope/cancel", headers=ADMIN).status_code == 404
    second = client.post("/api/v1/refresh/daily", headers=ADMIN).json()["run_id"]
    env["runs"].claim(second, datetime.now(UTC), github_run_id="8", github_run_attempt=1)
    assert client.post(f"/api/v1/jobs/{second}/cancel", headers=ADMIN).status_code == 409


def test_a_lost_run_no_longer_counts_as_active(env: dict[str, Any]) -> None:
    client: TestClient = env["client"]
    old = datetime.now(UTC) - timedelta(hours=7)
    env["runs"].create(
        RunRecord(run_id="run-old", trigger="api", requested_by="b@x.com", requested_at=old), old
    )
    ops = client.get("/api/v1/system/operations", headers=ADMIN).json()
    assert ops["active_run"] is None
    assert (ops["last_run"]["run_id"], ops["last_run"]["status"]) == ("run-old", "FAILED")


def test_github_usernames_are_for_admins_only(env: dict[str, Any]) -> None:
    client: TestClient = env["client"]
    t = datetime.now(UTC)
    env["runs"].create(
        RunRecord(run_id="gh-5-1", trigger="manual", requested_by="github:octo", requested_at=t), t
    )
    assert client.get("/api/v1/jobs/gh-5-1", headers=ADMIN).json()["requested_by"] == "github:octo"
    shown = client.get("/api/v1/jobs/gh-5-1", headers=USER).json()["requested_by"]
    assert shown == "github:a maintainer"
