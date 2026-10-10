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
# Immutable subject claims: owner and repository IDs follow the names (recorded from a real
# token by the A6 probe, run 37928441272).
OIDC_SUBJECT = "repo:subasankars-gif@288858503/ChartLens@1398125563:environment:production"
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
    assert f'SUBJECT="{OIDC_SUBJECT}"' in script
    assert "repo:subasankars-gif/ChartLens:" not in script  # the name-only form never matches
    assert "chartlens-pipeline@" in script and "chartlens-deployer@" in script


# ----------------------------------------------------------------------------- R12
# The M5 setup script binds the deployer to the whole repository. After A6 it must refuse
# to run before changing anything, so it can never silently restore that binding.

FAKE_GCLOUD = r"""#!/usr/bin/env bash
# Records every call; answers like gcloud for the identity checks.
echo "$*" >> "$GCLOUD_LOG"
case "$*" in
  "iam service-accounts list "*)
    [ "$FAKE_LIST_FAILS" = 1 ] && exit 1
    if [ "$FAKE_IDENTITIES_EXIST" = 1 ]; then
      printf '%s\n' "${*#*email=}" | cut -d' ' -f1
    fi ;;
  "iam service-accounts get-iam-policy "*)
    [ "$FAKE_POLICY_FAILS" = 1 ] && exit 1
    printf '%s\n' "$FAKE_MEMBERS" ;;
esac
exit 0
"""

ENV_MEMBER = (
    "principal://iam.googleapis.com/projects/1082278531047/locations/global/"
    f"workloadIdentityPools/github/subject/{OIDC_SUBJECT}"
)
REPO_MEMBER = (
    "principalSet://iam.googleapis.com/projects/1082278531047/locations/global/"
    "workloadIdentityPools/github/attribute.repository/subasankars-gif/ChartLens"
)


def _run_m5(
    tmp: Path,
    *,
    identities_exist: bool,
    members: str,
    list_fails: bool = False,
    policy_fails: bool = False,
) -> tuple[int, list[str], str]:
    bin_dir = tmp / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "gcloud"
    fake.write_text(FAKE_GCLOUD, encoding="utf-8")
    fake.chmod(0o755)
    log = tmp / "gcloud.log"
    log.touch()
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "GCLOUD_LOG": str(log),
        "FAKE_IDENTITIES_EXIST": "1" if identities_exist else "0",
        "FAKE_MEMBERS": members,
        "FAKE_LIST_FAILS": "1" if list_fails else "0",
        "FAKE_POLICY_FAILS": "1" if policy_fails else "0",
    }
    proc = subprocess.run(
        ["bash", str(ROOT / "scripts" / "gcp_setup_m5.sh")],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, log.read_text(encoding="utf-8").splitlines(), proc.stderr


def _mutations(calls: list[str]) -> list[str]:
    reads = ("config set project", "iam service-accounts list", "get-iam-policy")
    return [c for c in calls if not any(r in c for r in reads)]


def test_m5_setup_refuses_after_a6_without_changing_anything(tmp_path: Path) -> None:
    code, calls, err = _run_m5(tmp_path, identities_exist=True, members=ENV_MEMBER)
    assert code != 0
    assert "undo that" in err and "gcp_setup_m8.sh" in err
    assert _mutations(calls) == [], calls
    assert not any(REPO_MEMBER in c for c in calls)


def test_m5_setup_refuses_even_mid_cut_over(tmp_path: Path) -> None:
    """Both members present (between before-merge and after-merge): still refused."""
    code, calls, _ = _run_m5(
        tmp_path, identities_exist=True, members=f"{REPO_MEMBER}\n{ENV_MEMBER}"
    )
    assert code != 0 and _mutations(calls) == []


def test_m5_setup_still_works_on_a_project_that_predates_a6(tmp_path: Path) -> None:
    """Historical behaviour preserved: no identities yet, so the full setup runs."""
    code, calls, _ = _run_m5(tmp_path, identities_exist=False, members="")
    assert code == 0
    assert any(f"--member={REPO_MEMBER}" in c for c in calls)


def test_m5_setup_fails_closed_when_existence_cannot_be_checked(tmp_path: Path) -> None:
    """An error is never read as "the identity does not exist" (which would let the
    script re-add the repository-wide binding)."""
    code, calls, err = _run_m5(tmp_path, identities_exist=True, members="", list_fails=True)
    assert code != 0 and "cannot check" in err
    assert _mutations(calls) == [], calls


def test_m5_setup_fails_closed_when_the_policy_cannot_be_read(tmp_path: Path) -> None:
    code, calls, err = _run_m5(tmp_path, identities_exist=True, members="", policy_fails=True)
    assert code != 0 and "cannot read the IAM policy" in err
    assert _mutations(calls) == [], calls
