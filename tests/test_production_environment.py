"""M8 completion gate, A6: only `main`'s current workflows may reach the live lake.

The live storage target and the GCP identities (`GCS_BUCKET`, `GCP_WIF_PROVIDER`,
`GCP_PIPELINE_SA`, `GCP_DEPLOY_SA`) live in the `production` GitHub Environment, whose
deployment branches are `main` only, and the GCP identities accept only that
environment's OIDC subject (scripts/gcp_setup_m8.sh). These tests pin the repository
side: which workflows name the environment, that nothing else does, that the protected
variables are read only where an environment's variables are certainly available (steps),
and that no tracked code or configuration names the bucket, so no path can reach it
without the environment.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
ENVIRONMENT = "production"
PROTECTED = {"production-refresh.yml", "pipeline-job.yml", "deploy-api.yml", "deploy-web.yml"}
PROTECTED_VARS = ("GCS_BUCKET", "GCP_WIF_PROVIDER", "GCP_PIPELINE_SA", "GCP_DEPLOY_SA")
BUCKET = "chartlens-lake-13934-data"
OIDC_SUBJECT = "repo:subasankars-gif/ChartLens:environment:production"
# Human-run setup scripts and documentation may name the bucket; code and workflows never.
BUCKET_ALLOWED_IN = {
    "README.md",
    "scripts/gcp_setup_m5.sh",
    "tests/test_production_environment.py",
}


def _load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    return data


def _environment(job: dict[str, Any]) -> str | None:
    env = job.get("environment")
    if isinstance(env, dict):
        name: str | None = env.get("name")
        return name
    return env


def _all_workflows() -> list[str]:
    return sorted(p.name for p in WORKFLOWS.glob("*.yml"))


def test_every_job_of_the_protected_workflows_runs_in_the_production_environment() -> None:
    for name in PROTECTED:
        jobs = _load(name)["jobs"]
        assert jobs, name
        for job_id, job in jobs.items():
            assert _environment(job) == ENVIRONMENT, f"{name}:{job_id}"


def test_no_other_workflow_names_the_production_environment() -> None:
    for name in set(_all_workflows()) - PROTECTED:
        for job_id, job in (_load(name).get("jobs") or {}).items():
            assert _environment(job) != ENVIRONMENT, f"{name}:{job_id}"


def test_protected_variables_are_read_only_inside_steps() -> None:
    """Workflow-level `env` never sees an environment's variables, and a job-level `if`
    is not relied on to: a job gated on them could be skipped silently."""
    for name in PROTECTED:
        wf = _load(name)
        top = yaml.safe_dump(wf.get("env") or {})
        for var in PROTECTED_VARS:
            assert f"vars.{var}" not in top, f"{name}: workflow env reads {var}"
        for job_id, job in wf["jobs"].items():
            gate = str(job.get("if") or "") + yaml.safe_dump(job.get("env") or {})
            for var in PROTECTED_VARS:
                assert f"vars.{var}" not in gate, f"{name}:{job_id} reads {var} outside steps"


def test_the_refresh_fails_loudly_without_its_storage_target() -> None:
    steps = _load("production-refresh.yml")["jobs"]["refresh"]["steps"]
    first = steps[0]
    assert first["env"]["BUCKET"] == "${{ vars.GCS_BUCKET }}"
    assert "exit 1" in first["run"] and "CHARTLENS_STORAGE__GCS_BUCKET" in first["run"]


def test_every_gcp_sign_in_takes_its_identity_from_the_protected_variables() -> None:
    """Outside the environment these are empty, so the sign-in cannot even be attempted
    with a real identity; the GCP binding refuses any other subject regardless."""
    for name in _all_workflows():
        for job in (_load(name).get("jobs") or {}).values():
            for step in job.get("steps") or []:
                if str(step.get("uses", "")).startswith("google-github-actions/auth"):
                    with_ = step.get("with") or {}
                    assert with_.get("workload_identity_provider") == "${{ vars.GCP_WIF_PROVIDER }}"
                    assert with_.get("service_account") in {
                        "${{ vars.GCP_PIPELINE_SA }}",
                        "${{ vars.GCP_DEPLOY_SA }}",
                    }, name
                    assert "credentials_json" not in with_, name


def test_no_tracked_code_or_configuration_names_the_live_bucket() -> None:
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.split()
    offenders = {
        path
        for path in tracked
        if (ROOT / path).is_file()
        and BUCKET in (ROOT / path).read_text(encoding="utf-8", errors="ignore")
    }
    # Test fixtures may use the bucket name as inert text (e.g. a redacted error message).
    offenders = {p for p in offenders if not p.endswith("/test_production.py")}
    assert offenders <= BUCKET_ALLOWED_IN, offenders - BUCKET_ALLOWED_IN


def test_the_setup_script_binds_the_identities_to_the_environment_subject() -> None:
    script = (ROOT / "scripts" / "gcp_setup_m8.sh").read_text(encoding="utf-8")
    assert OIDC_SUBJECT in script
    assert "chartlens-pipeline@" in script and "chartlens-deployer@" in script
