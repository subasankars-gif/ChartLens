from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from chartlens_api.deps import settings_dep
from chartlens_api.main import create_app
from chartlens_core.config import ChartLensSettings


@pytest.fixture
def client() -> TestClient:
    settings = ChartLensSettings.model_construct()
    app = create_app(settings)
    app.dependency_overrides[settings_dep] = lambda: settings
    return TestClient(app)


def test_health_reports_versions_and_provenance(client: TestClient) -> None:
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["exchange"] == "NSE"
    assert len(body["methodology_hash"]) == 12
    assert set(body["versions"]) == {"api", "core", "engine", "pipeline"}


def test_routes_are_versioned(client: TestClient) -> None:
    assert client.get("/health").status_code == 404
    assert client.get("/api/v1/openapi.json").status_code == 200


def test_cors_allows_configured_frontend_origin(client: TestClient) -> None:
    response = client.get("/api/v1/health", headers={"Origin": "http://localhost:3000"})
    assert response.headers.get("access-control-allow-origin") == "http://localhost:3000"
