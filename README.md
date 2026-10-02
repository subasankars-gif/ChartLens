# ChartLens

**See the structure. Read the trend.**

A technical-analysis platform that reads price and volume charts and reports what
they show — structure, trend, patterns, and the conditions that would confirm or
invalidate each scenario. Deterministic, explainable, point-in-time correct. No
fundamentals, no news, no buy/sell calls.

> **Status: Phase 1 · Milestone 5 (API, authentication, serving).**
> ChartLens downloads NSE bhavcopies and the corporate-action feed and stores the original
> bytes immutably. It resolves every row to a stable internal security and writes
> exact-decimal canonical daily bars. From these it derives an adjusted analytical dataset:
> exact factors, each validated against prices, published only when no adjustment
> creates or worsens a discontinuity. Each security gets a data-quality status, a
> `usable_from` date and its continuity segments. Weekly bars are built from the adjusted
> series, never across a continuity break, and can be read point-in-time as of any date.
> A private, read-only API on Cloud Run serves a version-bound snapshot of all of it to
> signed-in, allowlisted users (ADR-0016).

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

# Milestone 3: corporate actions → adjusted dataset → data quality (no network after fetch)
uv run chartlens-pipeline corporate-actions fetch                     # 2006 → 90 days ahead
uv run chartlens-pipeline corporate-actions summary                   # classes, unrecognised subjects
uv run chartlens-pipeline adjust --report-file adjust.json            # exit 5 = not published
uv run chartlens-pipeline data-quality                                # findings, status, usable_from
uv run chartlens-pipeline adjustment-report --symbol HDFCBANK         # every event + evidence
uv run chartlens-pipeline security-quality --symbol TATACOMM          # status, usable_from, findings
uv run chartlens-pipeline identity-rebuild                            # after changing config/identity/

# Milestone 4: weekly bars (ADR-0014)
uv run chartlens-pipeline weekly                                      # per-security files + scan dataset
uv run chartlens-pipeline weekly-bars --symbol RELIANCE               # the valid segment, latest bars
uv run chartlens-pipeline weekly-bars --symbol RELIANCE --as-of 2015-06-30 --all-segments

# Milestone 5: serving snapshot + API (ADR-0016)
uv run chartlens-pipeline publish-serving                             # version-bound snapshot for the API
uv run poe api                                                        # API on :8080 (needs Firebase config)
```

Reviewed decisions live in version-controlled files:
- `config/identity/nse.toml` holds identity links, for example 3i Infotech. Changing
  it requires an `identity-rebuild` (ADR-0013).
- `config/corporate_actions/nse.toml` holds factors taken from primary documents and
  suppressed feed records (ADR-0011).

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

Market data belongs in GCS (ADR-0002). The flow is: NSE → runner → GCS raw (immutable)
→ GCS curated → adjusted → weekly. `.github/workflows/pipeline-job.yml` switches to
GCS automatically once these repository **variables** exist (Settings → Secrets and
variables → Actions → Variables):

| Variable | Example |
|---|---|
| `GCS_BUCKET` | `chartlens-lake-13934-data` |
| `GCP_WIF_PROVIDER` | `projects/<number>/locations/global/workloadIdentityPools/github/providers/chartlens-repo` |
| `GCP_PIPELINE_SA` | `chartlens-pipeline@<project>.iam.gserviceaccount.com` |

Authentication is keyless, through Workload Identity Federation restricted to this
repository. The service account has `roles/storage.objectUser` on the bucket only.

The lake was populated on 2026-10-01/02 (2006 → 2026-09-30, 5,145 sessions), and the
weekday schedule (20:15 IST) runs the `daily` chain: `ingest-daily` → corporate-action
feed → `adjust` → `data-quality` → `weekly` → `publish-serving`. Without `--trade-date`, `ingest-daily` catches up every
expected session since the latest ingested one (and re-checks recently unpublished
dates), so a missed or failed run leaves no hole; the next run fills it. On an empty
lake it refuses: the first load is an explicit `backfill`.

## API deployment

The API (ADR-0016) is a private, read-only presentation layer on Cloud Run in
`us-central1`, next to the lake. Every route but `/api/v1/health` needs a Firebase
Google sign-in **and** an enabled user in the Firestore allowlist. Contract:
`/api/v1/docs`.

One-time setup:
1. In the [Firebase console](https://console.firebase.google.com), add Firebase to the
   existing project `chartlens-lake-13934`, then enable **Authentication → Google**.
2. In Cloud Shell, run `scripts/gcp_setup_m5.sh`. It sets up Firestore, Artifact Registry,
   the read-only `chartlens-api` identity and the keyless `chartlens-deployer`.
3. Add the repository variables `GCP_PROJECT_ID`, `GCP_REGION`, `GCP_DEPLOY_SA`,
   `GCP_API_SA` and `API_ADMIN_EMAILS` (comma-separated). The emails listed become
   admins on their first verified sign-in. Everyone else waits as pending until an admin
   enables them (`PATCH /api/v1/admin/users/{uid}`).

Every merge to `main` that touches the API then deploys it
(`.github/workflows/deploy-api.yml`) and smoke-tests it.

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
| 3 | Corporate actions, adjustment factors, data quality → canonical daily dataset | ✅ |
| 4 | Weekly builder + property tests | ✅ |
| 5 | API: securities, weekly bars, data quality; Firestore; auth | In review |
| 6 | Frontend: search, weekly chart, last update, data-quality badge | |
| 7 | Jobs: daily incremental, backfill, single-security refresh, job tracking | |

## Key decisions

See [`docs/adr`](docs/adr/README.md). In short: NSE bhavcopy archives + NSE
corporate actions as the source; 20-year target history; EQ + BE series; Parquet on
GCS as the canonical store; Firestore for application state only; GitHub-hosted
runners behind a CLI; immutable internal security IDs resolved ISIN-first with
evidence-only linking; exact-decimal prices; calendars as versioned data; weekly bars
by ISO week, split at continuity breaks, point-in-time as of any date; a read-only API
over a version-bound serving snapshot, private behind Google sign-in and an allowlist; splits, bonuses and rights adjusted, dividends not; `as_of` enforced
everywhere.
