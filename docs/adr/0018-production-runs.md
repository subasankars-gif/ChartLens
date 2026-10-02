# ADR-0018: Production runs: tracked refresh, immutable serving, snapshot history

**Status:** Accepted · 2026-10-02 (decisions confirmed by Suba the same day)

M7 turns the daily chain into a controlled, observable production refresh. It adds no
analysis and changes no methodology: ingestion (M2), adjustment (M3), weekly bars (M4),
the read-only API (M5) and the chart (M6) behave exactly as before.

## Decisions

| # | Area | Decision |
|---|---|---|
| 1 | **Job ownership** | GitHub Actions runs the pipeline (ADR-0007). Cloud Run only authenticates, authorizes, records the run, starts the workflow and reports status. It never runs a stage. |
| 2 | **Run state** | Firestore holds durable run state: `runs/{run_id}`, with its stages inside the run document. |
| 3 | **Snapshot history** | Firestore holds `snapshots/{meta_version}`. GCS stays the only source of analytical data. |
| 4 | **Publication** | Only a run whose every stage succeeded may move the serving pointer. The API serves weekly bars only from immutable, content-hashed copies, so a failed or interrupted run cannot touch the live snapshot. |
| 5 | **Concurrency** | At most one active production run: a Firestore transaction on `ops/active_run`, plus the GitHub concurrency group `pipeline-writes`. |
| 6 | **Triggers** | An admin through the API (`POST /api/v1/refresh/daily`), a manual dispatch in GitHub, and the existing weekday schedule (20:15 IST), recorded as `requested_by = "schedule"`. No new scheduler. |
| 7 | **GitHub credential** | A GitHub App installed only on this repository, permission *Actions: write*. Its private key is in Secret Manager, readable only by `chartlens-api`. Never sent to the browser. |
| 8 | **Lost runs** | A run with no progress for 6 hours (longer than the job's timeout) becomes FAILED and releases the lock. |

## Run model

```
runs/{run_id}
  run_id, job_type = DAILY, trigger (api | schedule | manual),
  requested_by ("schedule", "github:<actor>" or the admin's email), requested_by_uid,
  requested_at, started_at, completed_at, heartbeat_at,
  status (QUEUED | RUNNING | SUCCEEDED | FAILED | CANCELLED), current_stage,
  data_as_of, weekly_version, serving_version,
  snapshot_outcome (PUBLISHED | UNCHANGED | NOT_PUBLISHED), error_summary,
  github_run_id, github_run_attempt,
  stages: [ {stage, status, started_at, completed_at, duration_seconds,
             records_processed, version, error_summary, details}, … ]
ops/active_run         {run_id, acquired_at}       the lock
ops/last_successful_run {run_id, completed_at}
```

Stages run in a fixed order: `INGEST → CORPORATE_ACTIONS → ADJUSTMENT → DATA_QUALITY →
WEEKLY → PUBLISH_SERVING`. Each one calls the existing CLI command in the same process,
with the same exit codes. Stages use the run statuses as well:

- a stage not yet started is QUEUED;
- stages left after a failure become CANCELLED.

**Why stages live inside the run document.** There are six stages, written by one
process. Keeping them in the run document means one read for the run page and one atomic
write per update. Subcollections would add reads and give nothing in return.

**Run IDs.**

- An API refresh gets `run-<UTC timestamp>-<random>`. The API passes it to the workflow
  as an input, and the workflow writes its own GitHub run ID into the record when it
  starts. This link does not depend on GitHub's dispatch call returning an ID.
- Scheduled and manual runs get `gh-<github_run_id>-<attempt>`, and the workflow creates
  the record itself.

**Lifecycle.**

- An API refresh:
  - In one transaction, the API takes the lock and creates the run as QUEUED. If a run
    is already active, it answers `409` with that run's ID.
  - It then dispatches the workflow. If the dispatch fails, the run becomes FAILED and
    the lock is released at once.
  - The workflow's `daily` step moves the run from QUEUED to RUNNING and runs the stages.
  - It ends the run as SUCCEEDED, or as FAILED at the stage that failed, and releases
    the lock.
- A scheduled or manual run creates its record as RUNNING and takes the lock in the same
  transaction.
  - GitHub keeps only one pending run in a concurrency group, so a newer pending run
    cancels an older one before it starts. To cover this, a starting run that finds the
    lock held by a QUEUED run cancels that run with the note "superseded by `<run_id>`"
    and takes over.
  - A RUNNING holder within its lease means another run is genuinely active, and the
    new run refuses to start.
- A final workflow step that runs whatever happened marks the run if the process never
  got to record its end (a killed runner, a cancelled workflow, a failed setup step).
  It only touches a run that belongs to the same GitHub run, so a duplicate or superseded
  workflow cannot overwrite someone else's record.
- Lost runs:
  - A QUEUED run is measured from `requested_at`, a RUNNING run from its last
    `heartbeat_at`.
  - After 6 hours either becomes FAILED ("lost") and releases the lock.
  - Both the API, before taking the lock and when reading runs, and the pipeline, before
    starting, apply this rule.
- Re-running a finished workflow in GitHub does not reopen its run. The run is closed,
  so the new attempt refuses to start; start a new refresh instead.

**Failure is visible where it happened.**

- `error_summary` is one short classified line, such as "ADJUSTMENT: hard requirement
  failed; adjusted dataset not published (exit 5)". It never contains stack traces or
  environment values.
- The GitHub run link, for the full log, is shown to admins only.

## Immutable serving (correction to ADR-0016)

ADR-0016 versioned the snapshot metadata, but the API read weekly bars from the
per-security files that every run rewrites in place, checking each against a hash. A
rewritten file made the API answer 503 for that security until publication: briefly in
every daily run, and for as long as it took if publication failed. That broke "a failed
run leaves the live snapshot untouched".

From serving schema 2, publication goes in this order:

1. Validate the inputs, which are the existing checks: the weekly, data-quality and
   adjustment versions must agree.
2. Copy each weekly file the snapshot refers to into
   `curated/serving/exchange={EX}/weekly/{sha256}.parquet`.
   - A file is copied only if that content is not stored yet. The write is create-only
     and must match the hash in the weekly manifest; otherwise publication stops.
3. Check that every referenced copy exists.
4. Write the versioned metadata files and a copy of the manifest under `v={meta_version}/`.
5. Record the snapshot in Firestore as STAGED.
6. Move the pointer (`_manifest.json`), written last.
7. Mark the snapshot PUBLISHED.
8. Remove version folders and weekly copies that neither the new snapshot nor the one
   before it refers to. The API may still hold the previous snapshot for up to a minute.

The API reads weekly bars only from the content-hashed copies, and still verifies each
hash on read.

- A schema-1 snapshot (published before M7) is still read from the curated path, with
  its hash check, until the first schema-2 publication replaces it.
- If the pipeline produced exactly the snapshot already live, nothing is written and the
  run records `snapshot_outcome = UNCHANGED`, a successful run with no new snapshot.
- If publication fails at any step before the pointer moves, the run is FAILED at
  PUBLISH_SERVING, `serving_version` stays empty, and the previous snapshot stays live.

Whether a snapshot is live is never stored: it is read from the pointer. If a snapshot
record says STAGED but is the pointer's version, it is live. This happens only if the
process died between steps 6 and 7.

```
snapshots/{meta_version}
  snapshot_id, exchange, schema_version, status (STAGED | PUBLISHED), staged_at,
  published_at, run_id, data_as_of, versions {weekly_version, data_version,
  adjustment_version, identity_version, dq_version, calendar_version, methodology_hash},
  counts {securities, analytical, weekly_bars}
```

## API

- `POST /api/v1/refresh/daily`, admins only:
  - `202 {run_id, status: "QUEUED"}`;
  - `409` if a run is active;
  - `503` if refresh is not configured;
  - `502` if GitHub refused the dispatch.
- `GET /api/v1/jobs?limit=&before=` and `GET /api/v1/jobs/{run_id}`, any approved user.
  Requester email and GitHub run details are shown to admins only.
- `GET /api/v1/system/snapshots?limit=` returns snapshot history.
- `GET /api/v1/system/operations`, any approved user, returns:
  - the API status and the live snapshot (null before the first snapshot);
  - the active run, the last run and the last successful run;
  - whether refresh is configured and whether this viewer may start one.
- `GET /api/v1/system/status` is unchanged (ADR-0016). It still answers `503` until a
  snapshot exists, so M5 and M6 clients see no difference.

Runs are not per-user resources. Any approved user may read them, and nothing in them is
secret, so reading another user's run ID is not an IDOR. Run IDs are validated against a
strict pattern.

The API still never discovers snapshots by listing GCS: the pointer decides. Snapshot
history is metadata about past publications, never a way to choose what to serve.

## What this rules out

- Cloud Run running pipeline stages, or a request that waits for a run to finish.
- A second scheduler (Cloud Scheduler, Pub/Sub, cron on Cloud Run).
- Live refresh status over WebSockets. The operations page polls every 5 seconds while a
  run is active.
- Serving any weekly file that a later, unpublished run could rewrite.
