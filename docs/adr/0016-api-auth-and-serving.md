# ADR-0016: The API: a read-only presentation layer, private access, version-bound serving

**Status:** Accepted · 2026-10-02 (decisions confirmed by Suba the same day)

## Invariant

> **API = read-only presentation and access layer.** GCS → readers → API → client.

The API never:

- calculates indicators;
- detects swings or patterns;
- calculates trends;
- builds weekly bars;
- applies corporate-action adjustment;
- writes market data.

It reads published data products and their metadata. The only thing it writes is
application state in Firestore: users and watchlists. Any calculation belongs to the
batch pipeline or, later, the engine, both deterministic and versioned.

This rule is enforced statically by `tests/test_layer_boundaries.py::test_api_is_a_read_only_presentation_layer`:

- From the pipeline, `chartlens_api` may import only `chartlens_pipeline.serving` and
  `chartlens_pipeline.storage`.
- It may import nothing from `chartlens_engine` (only its version string).
- It may not import `chartlens_core.adjustment` or `chartlens_core.quality`.
- From `chartlens_core.weekly`, it may import only the `WeeklyBar` type.

**Consequence for `as_of`:** a point-in-time weekly view of an earlier date requires
building bars and applying an as-of adjustment. That is computation, so the API refuses
`?as_of=` with **400** rather than ignoring it, so no client can believe it received a
point-in-time view. Such views will come from a batch-built point-in-time dataset (the
"cached point-in-time layer" noted in the M4 report). `WeeklyReader`'s point-in-time path
remains available to batch jobs and the engine.

## Decisions (confirmed 2026-10-02)

| # | Decision |
|---|---|
| 1 | **Firestore holds application state only**: `users` (the allowlist) and `users/{uid}/watchlists`. The lake stays the single source of truth for securities, identity and data quality, so Firestore never becomes a second lake |
| 2 | **Private.** Google sign-in through Firebase Authentication, plus an **admin-managed allowlist** in Firestore |
| 3 | **Firebase is added to the existing project `chartlens-lake-13934`** |
| 4 | **URLs are keyed by the immutable `security_id`.** Symbols change; symbols are found through search |
| 5 | **Cloud Run, minimum instances 0**, deployed keylessly from GitHub on merge to `main`. It runs as `chartlens-api`, which can only **read** the lake (`roles/storage.objectViewer`) and use Firestore (`roles/datastore.user`). Deployment uses a separate `chartlens-deployer` identity |
| 3/5 region | **`us-central1` for Cloud Run, Firestore and Artifact Registry.** The lake bucket is in `us-central1`, so the whole server-side data path stays in one region. The Firestore location is permanent. The region is a repository variable, `GCP_REGION` |

Mumbai was considered and rejected. It would put the browser hop at about 20 ms instead
of about 250 ms. However, it moves the API's lake reads across regions, and the
in-memory caches hide that hop only for data already loaded. The decision favours one
region for simplicity. If ChartLens later serves many users in India, a regional
frontend or API can be added without changing this contract.

## Clarification 1: version-bound metadata

The API never mixes metadata from different lake versions. The daily pipeline rewrites
the security master, data-quality tables and weekly files in place, so the **last step
of the daily chain publishes a serving snapshot** (`chartlens-pipeline publish-serving`):

```
curated/serving/exchange=NSE/v={meta_version}/securities.parquet
                                             /identifiers.parquet
                                             /segments.parquet
                                             /findings.parquet
curated/serving/exchange=NSE/_manifest.json        (written last: the pointer)
```

**What the publisher checks and records:**

- It refuses to publish unless the weekly, adjusted and data-quality versions agree.
- The pointer manifest records every component version:
  - `weekly_version`, `data_version`, `adjustment_version`, `identity_version`,
    `dq_version`, `calendar_version` and `methodology_hash`;
  - the SHA-256 of each snapshot file;
  - the SHA-256 of every per-security weekly file the snapshot refers to.
- `meta_version` hashes all of these. A snapshot directory never changes once written.
- The current and previous versions are kept.

**What the API does:**

- It caches **one** snapshot in memory, keyed by `(exchange, meta_version)`, and swaps it
  whole.
- Every `api.snapshot_refresh_seconds` (60 s) it reads the pointer. A new version is
  loaded and **hash-verified before** it replaces the old one; one that does not verify is
  never served, and the last good snapshot stays.
- A weekly file is served only if its hash matches the snapshot. If a newer run has
  rewritten it, the API reloads the pointer once. If the file still does not match, it
  answers **503 "retry shortly"**, never a mixture.
- Weekly bars are cached by `(exchange, meta_version, security_id)`.

Every data response names the `meta_version` it came from. `GET /system/status` returns
the snapshot's `meta_version`, all component versions, its `as_of`, counts and when it
was generated and loaded.

## Clarification 2: authentication ≠ authorization

```
Firebase ID token → verify (Firebase Admin SDK) → uid → users/{uid} in Firestore
                  → enabled? → access          (admin routes: role == admin)
```

- **Every route except `/health`** requires a valid token **and** an enabled user.
- **Missing or invalid token:** 401.
- **Valid sign-in, not enabled:** 403 "access pending approval". The first sign-in
  records the user as pending (`enabled = false`), so an admin can enable them later
  through `PATCH /admin/users/{uid}`.
- **Bootstrap:** an email listed in `api.admin_emails` (the repository variable
  `API_ADMIN_EMAILS`) becomes an enabled admin on first sign-in, but only when the token
  says the email is **verified**.
- **No lock-out:** an admin cannot disable or demote themselves.
- **Auth not configured:** without `api.firebase_project_id`, protected routes answer
  503 "authentication not configured". The API never runs open.

Token verification and Firestore access are interfaces (`TokenVerifier`, `AppState`).
Unit tests use in-memory fakes; CI's `api-emulators` job runs the real Firebase code
paths against the Firebase Auth and Firestore emulators.

Cloud Run allows unauthenticated *invocation* (`--allow-unauthenticated`) because the
browser calls it directly; authentication is enforced by the API itself on every route
but `/health`.

## API contract (`/api/v1`)

| Route | Auth | Returns |
|---|---|---|
| `GET /health` | none | Versions, methodology hash, and the serving snapshot's `meta_version` and `as_of` (versions only, no market data; `null` until one can be read) |
| `GET /securities?q=&universe=analytical\|all&limit=` | user | Matches on current or past symbol, ISIN or name, best first |
| `GET /securities/{security_id}` | user | Identity and its history, instrument type, analytical flag, status, `usable_from`, segments |
| `GET /securities/{security_id}/weekly?segments=valid\|all` | user | Stored weekly bars: the valid segment (default) or all of them, with flags and versions. `as_of` → 400 |
| `GET /securities/{security_id}/data-quality` | user | Status and findings |
| `GET /system/status` | user | The serving snapshot and its versions |
| `GET /me` | user | The caller's user record |
| `GET /me/watchlists`, `PUT/DELETE /me/watchlists/{id}` | user | Watchlists (20 per user, 200 securities each). Ids are validated against the snapshot |
| `GET /admin/users`, `PATCH /admin/users/{uid}` | admin | The allowlist |

Responses over 1 KB are gzip-compressed. A full 20-year weekly history is about 0.5 MB of
JSON before compression. Prices and volumes are **exact decimal strings** (for example `"1269.375000"`), never
binary floats. The OpenAPI document is at `/api/v1/openapi.json` and the docs at
`/api/v1/docs`.

## Operations

- **Daily chain:**

  ```
  ingest-daily → corporate-action feed → adjust → data-quality → weekly → publish-serving
  ```
- **Deployment:** `.github/workflows/deploy-api.yml` builds the `api` image into
  Artifact Registry (`{region}-docker.pkg.dev/{project}/chartlens/api:{sha}`), deploys
  `chartlens-api`, and smoke-tests it. `/health` must answer, and unauthenticated
  `/system/status` must return 401. The job is skipped until the setup variables exist.
- **Cloud Run sizing:** min 0 / max 3 instances, 1 vCPU, 1 GiB, concurrency 40. The
  snapshot loads on the first request after a cold start.
- **One-time setup:** `scripts/gcp_setup_m5.sh`, plus enabling Firebase and Google
  sign-in in the console.
