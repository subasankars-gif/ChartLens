"""Enforce the package dependency rule (docs/adr/0001-technology-stack.md).

    core  ←  engine
      ↑
    pipeline
      ↑
    backend (api) → engine, pipeline, core

* core imports no other ChartLens package and no I/O client.
* engine imports only core — never pipeline, api, or any I/O client.
* pipeline never imports engine or api.

Checked statically from the source, so a violation fails CI even if the
offending code path is never executed by a test.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

IO_CLIENTS = {"google", "firebase_admin", "requests", "httpx", "urllib3", "boto3", "fastapi"}

RULES: dict[str, set[str]] = {
    "core/src/chartlens_core": {
        "chartlens_engine",
        "chartlens_pipeline",
        "chartlens_api",
        *IO_CLIENTS,
    },
    "engine/src/chartlens_engine": {"chartlens_pipeline", "chartlens_api", *IO_CLIENTS},
    "pipeline/src/chartlens_pipeline": {"chartlens_engine", "chartlens_api"},
}


def _imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    return roots


@pytest.mark.parametrize("package_dir", sorted(RULES))
def test_package_respects_layer_boundaries(package_dir: str) -> None:
    forbidden = RULES[package_dir]
    violations = {
        str(py.relative_to(ROOT)): sorted(_imported_roots(py) & forbidden)
        for py in (ROOT / package_dir).rglob("*.py")
    }
    violations = {k: v for k, v in violations.items() if v}
    assert not violations, f"layer violations: {violations}"


def test_engine_never_reads_the_clock() -> None:
    """as_of is always passed in; the engine must not ask what day it is."""
    offenders = [
        str(py.relative_to(ROOT))
        for py in (ROOT / "engine/src/chartlens_engine").rglob("*.py")
        if any(
            token in py.read_text()
            for token in ("datetime.now", "date.today", "utc_now", "time.time")
        )
    ]
    assert not offenders, f"engine modules reading the clock: {offenders}"
