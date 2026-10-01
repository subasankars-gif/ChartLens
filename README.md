# ChartLens

**See the structure. Read the trend.**

A technical-analysis platform that reads price and volume charts and reports what
they show — structure, trend, patterns, and the conditions that would confirm or
invalidate each scenario. Deterministic, explainable, point-in-time correct. No
fundamentals, no news, no buy/sell calls.

> **Status: Phase 1 · Milestone 2 (NSE ingestion) complete.** ChartLens downloads NSE
> bhavcopies (both historical formats), stores the original bytes immutably, resolves
> every row to a stable internal security, and writes exact-decimal canonical daily bars
> to Parquet. No corporate-action adjustment yet (Milestone 3).

## Repository layout

```
chartlens/
├── core/          chartlens_core      config, domain types, versioning, bar contract, as_of guards
├── engine/        chartlens_engine    technical analysis — pure computation, no I/O, no clock
├── pipeline/      chartlens_pipeline  providers, immutable raw storage, adjustment, data quality, bars
├── backend/       chartlens_api       FastAPI service (Cloud Run)
├── frontend/                          Next.js static site (Firebase Hosting)
├── tests/                             cross-package tests (layer boundaries)
├── config/        chartlens.toml      single source of configuration and methodology parameters
├── docker/        Dockerfile          targets: api, pipeline
├── docs/adr/                          architecture decision records
└── .github/       workflows           CI, pipeline jobs, Dependabot
```

Package dependency rule (enforced by `tests/test_layer_boundaries.py`):
`engine → core`, `pipeline → core`, `api → engine, pipeline, core`. The engine never
imports the pipeline, never touches storage or the network, and never reads the clock.

## Prerequisites

* [uv](https://docs.astral.sh/uv/) ≥ 0.8 (installs Python 3.12 for you)
* Node.js 22 and pnpm 10 (`corepack enable` gives you pnpm)
* Docker (optional, for images and the compose stack)

## Run locally

```bash
# Python: install everything, then run all checks
uv sync
uv run poe check            # ruff lint + format check + pyright (strict) + pytest

# API on http://localhost:8080  (docs at /api/v1/docs)
uv run poe api

# Frontend on http://localhost:3000  (in a second terminal)
cd frontend
cp .env.example .env.local
pnpm install
pnpm dev

# Pipeline CLI (writes to ./.data by default — see "Production storage")
uv run chartlens-pipeline info
uv run chartlens-pipeline backfill --date 2026-09-29 --dry-run        # plan only
uv run chartlens-pipeline backfill --date 2026-09-29                  # one session
uv run chartlens-pipeline backfill --start-date 2026-09-01 --end-date 2026-09-29
uv run chartlens-pipeline security --symbol RELIANCE                  # identity + history
uv run chartlens-pipeline quarantine-report --start-date 2026-09-01 --end-date 2026-09-29
uv run chartlens-pipeline reprocess-pending                           # retry unresolved identities
```

`backfill` flags: `--refetch` downloads again even if stored (catches re-issued files;
a different file for the same date is kept alongside, never overwritten), `--reprocess`
re-parses stored files. Dates are processed newest first. Re-running is safe: already
ingested dates are skipped.

NSE blocks many networks. If your machine cannot reach `nsearchives.nseindia.com`, run
backfills from GitHub Actions (hosted runners can reach it — ADR-0008).

Everything above works on Windows (PowerShell) as well — tasks run through
`poethepoet`, not `make`. If a compiled dependency has no wheel for your platform
(possible on Windows ARM64 once PyArrow arrives in Milestone 2), use Docker or WSL2.

### With Docker

```bash
docker compose up --build                  # API on :8080
docker compose run --rm pipeline info      # any pipeline command
```

## Production storage

Market data belongs in GCS (ADR-0002); the code is ready (`GcsObjectStore`), the cloud
project is not yet created. To switch on:

1. Create a GCS bucket and a service account with object read/write on it.
2. Set up Workload Identity Federation for this repository (no JSON keys) and store
   `GCP_WIF_PROVIDER` / `GCP_PIPELINE_SA` as repository variables.
3. Uncomment the auth step and the schedule in `.github/workflows/pipeline-job.yml`, and
   set `CHARTLENS_STORAGE__BACKEND=gcs`, `CHARTLENS_STORAGE__GCS_BUCKET=<bucket>`.

## Configuration

`config/chartlens.toml` is the single source of truth; environment variables
override it (`CHARTLENS_` prefix, `__` for nesting, e.g.
`CHARTLENS_STORAGE__BACKEND=gcs`).

Settings are split into **infrastructure** (where things run) and **methodology**
(anything that can change a result). Methodology settings are hashed into a
`methodology_hash` stamped on every output, so a silent methodology change is
impossible. Changing one means writing an ADR and recalculating.

## Phase 1 milestones

| # | Milestone | Status |
|---|---|---|
| 1 | Skeleton: repo, config, core contracts, API health, frontend shell, Docker, CI | ✅ |
| 2 | Security master, exchange calendar, NSE bhavcopy provider, immutable raw store, GCS, backfill | ✅ |
| 3 | Corporate actions, adjustment factors, data quality → canonical daily dataset | Next |
| 4 | Weekly builder + property tests | |
| 5 | API: securities, weekly bars, data quality; Firestore; auth | |
| 6 | Frontend: search, weekly chart, last update, data-quality badge | |
| 7 | Jobs: daily incremental, backfill, single-security refresh, job tracking | |

## Key decisions

See [`docs/adr`](docs/adr/README.md). In short: NSE bhavcopy archives + NSE
corporate actions as the source; 20-year target history; EQ + BE series; Parquet on
GCS as the canonical store; Firestore for application state only; GitHub-hosted
runners behind a CLI; immutable internal security IDs resolved ISIN-first with
evidence-only linking; exact-decimal prices; calendars as versioned data; weekly bars
by ISO week; splits, bonuses and rights adjusted, dividends not; `as_of` enforced
everywhere.
