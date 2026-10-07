"""Enforce the package dependency rule (docs/adr/0001-technology-stack.md, as amended
by ADR-0024 and ADR-0025).

    core ◄── engine          core ◄── pipeline
                ▲                        ▲
                └──────── jobs ──────────┘
    backend (api) → pipeline's serving reader, core

* core imports no other ChartLens package and no I/O client.
* engine imports only core — never pipeline, jobs, api, or any I/O client.
* pipeline never imports engine, jobs or api.
* jobs may import engine and pipeline; nothing imports jobs.
* the api never imports jobs (serving never computes).

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
        "chartlens_jobs",
        "chartlens_api",
        *IO_CLIENTS,
    },
    "engine/src/chartlens_engine": {
        "chartlens_pipeline",
        "chartlens_jobs",
        "chartlens_api",
        *IO_CLIENTS,
    },
    "pipeline/src/chartlens_pipeline": {"chartlens_engine", "chartlens_jobs", "chartlens_api"},
    "jobs/src/chartlens_jobs": {"chartlens_api"},
    "backend/src/chartlens_api": {"chartlens_jobs"},
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


LATER_LAYERS = ("fibonacci", "levels", "evidence", "patterns", "breakouts", "analysis")
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
            if n.startswith("_") and not n.startswith("__")
        ]
        assert not private, f"{py.name}: {private}"
        for forbidden in (*METHOD_NAMES, "fractal(", "zigzag(", '"BOS"', '"CHoCH"'):
            assert forbidden not in text, f"{py.name}: {forbidden}"


# ----------------------------------------------------------------------------- ADR-0024

PRODUCTION = ("engine/src", "pipeline/src", "jobs/src", "backend/src")
ORCHESTRATOR = "engine/src/chartlens_engine/analysis"
ASSEMBLY = ("orchestrator.py", "model.py", "serialize.py")
"""The modules that assemble layer outputs. ``canonical.py`` and ``versions.py`` encode
and hash bytes; they read no analytical value."""
NUMERIC = {"numpy", "pandas", "math", "statistics", "decimal", "fractions", "scipy", "cmath"}
ARITHMETIC = (
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Pow,
    ast.MatMult,
)
ORDERING = (ast.Lt, ast.LtE, ast.Gt, ast.GtE)
SELECTING = {"sorted", "sort", "min", "max", "sum", "round", "abs", "filter", "model_copy"}


def _calls(tree: ast.AST) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name):
                names.append(f.id)
            elif isinstance(f, ast.Attribute):
                names.append(f.attr)
    return names


def test_only_the_orchestrator_instantiates_analyzers() -> None:
    """ADR-0024 §1: no layer constructs or calls another layer, and no production code
    composes layers except ``chartlens_engine.analysis`` (tests and scripts may)."""
    offenders: list[str] = []
    for root in PRODUCTION:
        for py in (ROOT / root).rglob("*.py"):
            rel = str(py.relative_to(ROOT))
            if rel.startswith(ORCHESTRATOR):
                continue
            tree = ast.parse(py.read_text(), filename=rel)
            offenders += [f"{rel}: {n}()" for n in _calls(tree) if n.endswith("Analyzer")]
    assert not offenders, offenders
    orchestrator = ast.parse((ROOT / ORCHESTRATOR / "orchestrator.py").read_text())
    assert sum(n.endswith("Analyzer") for n in _calls(orchestrator)) == 12


@pytest.mark.parametrize("module", ASSEMBLY)
def test_the_orchestrator_contains_no_analytics(module: str) -> None:
    """Amendment A: assembly may reference layer outputs but never transform, score,
    reinterpret, filter or recalculate them. So these modules import no numeric library
    at run time, do no arithmetic, make no ordering comparison, filter no comprehension,
    and call nothing that sorts, selects, aggregates or rewrites a layer's object."""
    path = ROOT / ORCHESTRATOR / module
    tree = ast.parse(path.read_text(), filename=str(path))
    type_checking = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.If) and isinstance(n.test, ast.Name) and n.test.id == "TYPE_CHECKING"
    ]
    guarded = {id(x) for block in type_checking for x in ast.walk(block)}
    runtime_imports = {
        name.split(".")[0]
        for n in ast.walk(tree)
        if id(n) not in guarded
        for name in (
            [a.name for a in n.names]
            if isinstance(n, ast.Import)
            else [n.module or ""]
            if isinstance(n, ast.ImportFrom)
            else []
        )
    }
    assert not runtime_imports & NUMERIC, runtime_imports & NUMERIC
    arithmetic = [
        ast.dump(n)
        for n in ast.walk(tree)
        if (isinstance(n, ast.BinOp) and isinstance(n.op, ARITHMETIC))
        or isinstance(n, ast.AugAssign)
    ]
    assert not arithmetic, arithmetic
    ordering = [
        ast.dump(n)
        for n in ast.walk(tree)
        if isinstance(n, ast.Compare) and any(isinstance(op, ORDERING) for op in n.ops)
    ]
    assert not ordering, ordering
    filtered = [
        ast.dump(g) for n in ast.walk(tree) if isinstance(n, ast.comprehension) for g in n.ifs
    ]
    assert not filtered, filtered
    assert not set(_calls(tree)) & SELECTING, set(_calls(tree)) & SELECTING
