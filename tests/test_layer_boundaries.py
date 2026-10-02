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


# ----------------------------------------------------------------------------- ADR-0016

API_DIR = "backend/src/chartlens_api"
API_ALLOWED_PIPELINE = {
    "chartlens_pipeline.serving",
    "chartlens_pipeline.storage",
    "chartlens_pipeline.runs",  # operational run state and snapshot history (ADR-0018)
}
API_FORBIDDEN_CORE = {"chartlens_core.adjustment", "chartlens_core.quality"}
API_ALLOWED_FROM_CORE_WEEKLY = {"WeeklyBar"}


def _from_imports(path: Path) -> list[tuple[str, list[str]]]:
    tree = ast.parse(path.read_text(), filename=str(path))
    return [
        (node.module, [a.name for a in node.names])
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0
    ]


def test_api_is_a_read_only_presentation_layer() -> None:
    """The API never calculates analysis, builds weekly bars, adjusts prices or writes
    market data: it may read the serving snapshot and the object store, nothing else
    from the pipeline, and nothing from the engine but its version (ADR-0016)."""
    violations: list[str] = []
    for py in (ROOT / API_DIR).rglob("*.py"):
        rel = str(py.relative_to(ROOT))
        for module, names in _from_imports(py):
            if module.startswith("chartlens_pipeline") and module not in API_ALLOWED_PIPELINE:
                violations.append(f"{rel}: from {module}")
            if module.startswith("chartlens_engine"):
                violations.append(f"{rel}: from {module}")
            if module in API_FORBIDDEN_CORE:
                violations.append(f"{rel}: from {module}")
            if module == "chartlens_core.weekly" and set(names) - API_ALLOWED_FROM_CORE_WEEKLY:
                violations.append(f"{rel}: from {module} import {names}")
    assert not violations, violations


def test_run_state_is_operational_metadata_only() -> None:
    """The API may write run state (ADR-0018), but that module touches no market data:
    it imports nothing from ChartLens but the run model."""
    runs = ROOT / "pipeline/src/chartlens_pipeline/runs.py"
    chartlens = {m for m, _ in _from_imports(runs) if m.startswith("chartlens")}
    assert chartlens == {"chartlens_core.runs"}, chartlens


LOOK_AHEAD_IDIOMS = ("shift(-", "center=True", "bfill", "backfill", "[::-1]")


def test_engine_has_no_look_ahead_idioms() -> None:
    """ADR-0019/0020: no backward shift, centred window, back-fill or reversed scan in the
    engine. Causality is proved by tests; this keeps the obvious shortcuts out of review."""
    offenders = [
        f"{py.relative_to(ROOT)}: {idiom}"
        for py in (ROOT / "engine/src/chartlens_engine").rglob("*.py")
        for idiom in LOOK_AHEAD_IDIOMS
        if idiom in py.read_text()
    ]
    assert not offenders, offenders


def test_structure_consumes_swings_and_never_finds_pivots() -> None:
    """ADR-0020 §C: market structure reads the primary confirmed swings; it never imports
    the swing methods or names a method itself."""
    for py in (ROOT / "engine/src/chartlens_engine/structure").rglob("*.py"):
        text = py.read_text()
        modules = {m for m, _ in _from_imports(py)}
        assert "chartlens_engine.swings.methods" not in modules, py
        for forbidden in ('"ATR"', '"INTERMEDIATE"', "fractal(", "zigzag("):
            assert forbidden not in text, f"{py.name}: {forbidden}"


LATER_LAYERS = ("fibonacci", "levels", "evidence")
METHOD_NAMES = ('"ATR"', '"FRACTAL"', '"PERCENT"', '"ZIGZAG"', '"INTERMEDIATE"', '"MAJOR"')


@pytest.mark.parametrize("layer", LATER_LAYERS)
def test_later_layers_consume_structure_and_never_rederive_it(layer: str) -> None:
    """ADR-0021 Phase 4 rules: levels, Fibonacci and evidence read the primary swings and
    structure's output. They never find pivots, label swings or judge a break of
    structure themselves, and never name a swing method or sensitivity."""
    for py in (ROOT / "engine/src/chartlens_engine" / layer).rglob("*.py"):
        text = py.read_text()
        imported = _from_imports(py)
        modules = {m for m, _ in imported}
        assert "chartlens_engine.swings.methods" not in modules, py
        private = [
            n
            for m, names in imported
            if m.startswith("chartlens_engine")
            for n in names
            if n.startswith("_")
        ]
        assert not private, f"{py.name}: {private}"
        for forbidden in (*METHOD_NAMES, "fractal(", "zigzag(", '"BOS"', '"CHoCH"'):
            assert forbidden not in text, f"{py.name}: {forbidden}"
