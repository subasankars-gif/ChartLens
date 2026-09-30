# ADR-0001: Technology stack and repository layout

**Status:** Accepted · 2026-09-30

## Decision

| Layer | Choice |
|---|---|
| Technical engine | Python 3.12 package `chartlens_engine` — NumPy, pandas, Pydantic v2; no I/O |
| Indicators | Implemented in NumPy/pandas; TA-Lib only as a dev-time test oracle, never a runtime dependency |
| Pipeline | Python package `chartlens_pipeline` — providers, raw storage, adjustment, data quality, bar builders |
| Shared primitives | Python package `chartlens_core` — config, domain types, versioning, bar-frame contract, `as_of` guards |
| API | FastAPI (`chartlens_api`) on Cloud Run |
| Application state | Firestore |
| Time series | Parquet on GCS (ADR-0002) |
| Frontend | Next.js + TypeScript + Tailwind, **static export** on Firebase Hosting |
| Charts | TradingView Lightweight Charts (Apache-2.0; TradingView attribution required in the UI) |
| Python tooling | uv workspace, ruff, pyright (strict), pytest, hypothesis, poethepoet tasks |
| Frontend tooling | pnpm, ESLint 9 (Next config), vitest |
| Batch | GitHub Actions (ADR-0007) |

### Package dependency rule

```
core  ←  engine
  ↑
pipeline
  ↑
api  →  engine, pipeline, core
```

* `core` imports no other ChartLens package and no I/O client.
* `engine` imports only `core`; it never reads the clock (`as_of` is always passed in).
* `pipeline` never imports `engine`. Orchestration that needs both (run the engine
  after building bars) lives in the job layer introduced in Milestone 7.

Enforced statically by `tests/test_layer_boundaries.py`.

## Deviations from the specification (§55) and why

* **Patterns live inside the engine package** (`chartlens_engine.patterns`), not at the
  top level. Detectors consume engine types; a separate top-level package would
  import the engine and be imported by it.
* **`core` is its own package.** Both the pipeline and the engine need the bar-frame
  contract and `as_of` guards, and the pipeline must not depend on the engine.
* **`data/` became `pipeline/`**, so the name cannot be confused with data files.
* **Subpackages are created when their milestone lands**, not as empty placeholders.
* **Static export instead of server-rendered Next.js.** Firebase Hosting serves plain
  files; every dynamic value comes from the API. No SSR runtime to host or secure.

## Consequences

* The engine can be tested, benchmarked and backtested without any cloud account.
* Windows development works via uv and poe tasks (no `make`). Some compiled wheels
  (notably PyArrow, needed from Milestone 2) may not exist for Windows on ARM64;
  Docker or WSL2 is the fallback there.
* ESLint is pinned to 9.x: `eslint-plugin-react`, pulled in by Next's config, crashes on
  ESLint 10 as of this writing.
