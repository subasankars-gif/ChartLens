"""The production frontend URL is `https://chartlenslab.web.app` (ADR-0017).

It is the Hosting site `chartlenslab` in the project `chartlens-lake-13934`. These tests
read the committed deployment configuration, so a change that moves the site, widens CORS
or makes the old default domain canonical again fails CI before it can deploy.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest
from fakes import FakeVerifier, MemoryAppState
from fastapi.testclient import TestClient

from chartlens_api.deps import appstate_dep, settings_dep, verifier_dep
from chartlens_api.main import create_app
from chartlens_core.config import ApiConfig, ChartLensSettings

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"

PRODUCT_URL = "https://chartlenslab.web.app"
PROJECT_ID = "chartlens-lake-13934"
OLD_URL = f"{PROJECT_ID}.web.app"
# Kept only for the migration: the API still accepts the old origin until the new site is
# verified end to end, and the docs say so. Nowhere else may name it.
OLD_URL_ALLOWED_IN = {".github/workflows/deploy-api.yml", "README.md", "docs/adr/0017-frontend.md"}


def _workflow(name: str) -> str:
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def _env_value(workflow: str, key: str) -> str:
    (value,) = re.findall(rf"^\s+{key}:\s*(.+?)\s*$", workflow, flags=re.MULTILINE)
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        value = value[1:-1]  # one layer of YAML quoting
    return value


def production_cors_origins() -> list[str]:
    origins = json.loads(_env_value(_workflow("deploy-api.yml"), "CORS_ORIGINS"))
    assert isinstance(origins, list)
    return [str(o) for o in origins]


# ----------------------------------------------------------------------------- hosting


def test_hosting_targets_the_chartlenslab_site_with_the_static_export() -> None:
    hosting = json.loads((ROOT / "firebase.json").read_text(encoding="utf-8"))["hosting"]
    assert hosting["site"] == "chartlenslab"
    assert hosting["public"] == "frontend/out"  # the static export, unchanged


def test_web_deploy_is_keyless_and_points_at_the_product_url() -> None:
    web = _workflow("deploy-web.yml")
    assert _env_value(web, "SITE_URL") == PRODUCT_URL
    assert _env_value(web, "NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN") == "chartlenslab.web.app"
    assert 'site="$SITE_URL"' in web  # the smoke test checks the product URL
    # The project is unchanged; the site comes from firebase.json.
    assert '--only hosting --project "${{ vars.GCP_PROJECT_ID }}"' in web
    # Keyless: GitHub OIDC through Workload Identity Federation, never a key file.
    assert "workload_identity_provider: ${{ vars.GCP_WIF_PROVIDER }}" in web
    assert "id-token: write" in web
    assert "credentials_json" not in web and "FIREBASE_TOKEN" not in web


def test_production_cors_is_explicit_and_leads_with_the_product_url() -> None:
    origins = production_cors_origins()
    assert origins[0] == PRODUCT_URL
    assert all(o.startswith("https://") and "*" not in o for o in origins)
    # The rest are the project's default Hosting domains, kept only during the migration.
    assert set(origins[1:]) <= {f"https://{OLD_URL}", f"https://{PROJECT_ID}.firebaseapp.com"}


def test_no_code_path_treats_the_old_hosting_url_as_canonical() -> None:
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.split()
    offenders = {
        path
        for path in tracked
        if (ROOT / path).is_file()
        and OLD_URL in (ROOT / path).read_text(encoding="utf-8", errors="ignore")
    }
    offenders.discard("tests/test_production_hosting.py")
    assert offenders <= OLD_URL_ALLOWED_IN, offenders - OLD_URL_ALLOWED_IN
    # The frontend names no Hosting URL at all: it takes the API and auth domain at build time.
    assert not {
        p
        for p in tracked
        if p.startswith("frontend/src/")
        and ".web.app" in Path(ROOT / p).read_text(encoding="utf-8", errors="ignore")
    }


# ----------------------------------------------------------------------------- the API


@pytest.fixture
def client() -> TestClient:
    settings = ChartLensSettings.model_construct(
        api=ApiConfig(
            cors_origins=tuple(production_cors_origins()),
            firebase_project_id="demo-test",
            admin_emails=("boss@example.com",),
        )
    )
    appstate = MemoryAppState()
    app = create_app(settings)
    app.dependency_overrides.update(
        {
            settings_dep: lambda: settings,
            verifier_dep: FakeVerifier,
            appstate_dep: lambda: appstate,
        }
    )
    return TestClient(app)


def _preflight(client: TestClient, origin: str) -> dict[str, str]:
    r = client.options(
        "/api/v1/me",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    return {"status": str(r.status_code), **{k.lower(): v for k, v in r.headers.items()}}


def test_the_api_accepts_the_product_origin(client: TestClient) -> None:
    pre = _preflight(client, PRODUCT_URL)
    assert pre["status"] == "200"
    assert pre["access-control-allow-origin"] == PRODUCT_URL
    assert "authorization" in pre["access-control-allow-headers"].lower()


@pytest.mark.parametrize(
    "origin",
    [
        "https://evil.example.com",
        "https://chartlenslab.web.app.evil.example.com",
        "http://chartlenslab.web.app",  # not https
        "https://other-site.web.app",
    ],
)
def test_the_api_refuses_other_origins(client: TestClient, origin: str) -> None:
    pre = _preflight(client, origin)
    assert pre["status"] == "400"
    assert "access-control-allow-origin" not in pre
    r = client.get("/api/v1/health", headers={"Origin": origin})
    assert "access-control-allow-origin" not in r.headers


def test_cors_changes_nothing_about_authentication(client: TestClient) -> None:
    origin = {"Origin": PRODUCT_URL}
    for path in ("/api/v1/me", "/api/v1/securities?q=X", "/api/v1/admin/users"):
        r = client.get(path, headers=origin)
        assert r.status_code == 401  # an allowed origin is not a credential
        assert r.headers["access-control-allow-origin"] == PRODUCT_URL  # the UI can read why
        bad = client.get(path, headers={**origin, "Authorization": "Bearer junk"})
        assert bad.status_code == 401
    pending = {**origin, "Authorization": "Bearer someone:someone@example.com"}
    r = client.get("/api/v1/me", headers=pending)
    assert (r.status_code, r.json()["detail"]) == (403, "access pending approval")
    admin = {**origin, "Authorization": "Bearer admin-uid:boss@example.com"}
    assert client.get("/api/v1/me", headers=admin).json()["role"] == "admin"
    assert client.get("/api/v1/admin/users", headers=pending).status_code == 403
