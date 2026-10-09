# M8 pre-production readiness review

**Status:** Report for review · 2026-10-08. **Decisions 2026-10-09 (§5):** A6 = a
`main`-only GitHub Environment (merge blocked until re-checked); B8 accepted as a
pre-merge evidence gap; B9 correction accepted. A6 protection implemented on `m8`; its
re-check awaits the settings only you can apply (§5.3).

Original report follows. Read-only against the locked
[M8 completion gate](m8-completion-gate.md): D5 and Gate 4's before-merge items.

Nothing was changed in production. Specifically:
- no merge and no deployment;
- no workflow dispatched or re-run;
- no GitHub or GCP setting changed;
- no object in the lake read or written.

The cross-version checks (§2) ran on a synthetic local lake. They used the m8 code and
a worktree of `main` (b3473b7, the code production runs today).

One documentation regression was found and fixed (§4.5). It was my own wording in the
gate, and it had turned PR #16's Python CI red.

## Verdict

**NOT READY TO MERGE: one BLOCKER (A6).** Old code can still publish a schema-2
snapshot after cut-over, through re-runs of pre-merge workflow runs and dispatches on
branches that carry pre-merge code. This was demonstrated locally. Every other hard
check passes. Two evidence gaps can only close in production (B8). The rollback path
needs correcting (B9).

| Area | PASS | BLOCKER | EVIDENCE GAP | FOLLOW-UP / NON-BLOCKING |
|---|---|---|---|---|
| A. D5 operational split-brain | A1–A5, A8 | **A6** | — | A4 note, A5 note, A7 |
| B. Compatibility and capacity | B1–B7 | — | B8 | B4 note, B9, B10 |
| C. Gate 4 before-merge documentation | C1–C3 | — | — | C4 (listed for the final stage) |

## 1. D5: operational split-brain

The requirement is evidence that no active production path can still publish schema 2
after cut-over. "The code appears capable" is not enough.

### A1. Deployment workflows run from `main`: PASS

- `deploy-api.yml` and `deploy-web.yml` trigger on push to `main` (path-filtered) and on
  manual dispatch.
- They are identical on `main` and `m8` (no diff), so the merge changes what they deploy,
  not how.
- `m8` is 69 commits ahead of `main` and 0 behind, so the merge is a fast-forward of
  content with no conflicts.

### A2. Refresh dispatch resolves to `main`: PASS

- The API dispatches `production-refresh.yml` with `ref = api.github_ref`, whose default
  is `"main"` (`core/config.py:93`).
- `deploy-api.yml` sets no `CHARTLENS_API__GITHUB_REF`, so production uses the default.
- The GitHub App credential is unchanged.

### A3. The scheduled refresh is the default branch's workflow: PASS

- GitHub runs `schedule` triggers only from the default branch, and the repository's
  default branch is `main`.
- After the merge, `main`'s `production-refresh.yml` runs `chartlens-jobs daily`, the
  seven-stage job with ANALYSIS and schema-4 publication.
- On `main` today it runs `chartlens-pipeline daily`, the six-stage schema-2 job.

### A4. The one-off Pipeline job cannot publish after the merge: PASS

- On `m8`, `chartlens-pipeline publish-serving` refuses without the versions the job
  layer supplies. Run on a lake copy, it printed: `Refusing to run: --analysis-version is
  required (ADR-0026)`.
- `pipeline-job.yml` passes no versions. Its `publish-serving` option therefore refuses
  instead of publishing.
- No other `chartlens-pipeline` command moves the pointer (`ServingPublisher` has one
  production caller, `publish-serving`). `refresh-security` is a dry-run stub.
- *Note (non-blocking):* after the merge, the one-off `publish-serving` option is
  unusable by design. Remove the option, or leave it refusing.

### A5. Probe and verify workflows on `main` after the merge: PASS for the pointer

- **Write to GCS:** `gcs-bootstrap.yml` (backfill) and `lake-seed.yml` (rsync of seed
  data, adjust, data-quality).
  - They trigger only on push to `probe/gcs-bootstrap` and `probe/lake-seed`. Neither
    branch exists.
  - Neither builds or publishes a serving snapshot.
- **Local only:** `m3`/`m4`/`m5`/`m6-verify` run on the runner's disk
  (`CHARTLENS_STORAGE__BACKEND: local`). `m5`/`m6-verify` would now also fail at their
  local `publish-serving`, per A4.
- *Note (non-blocking):* these are obsolete M2–M6 writers. Retiring them would shrink
  the set of workflows that can write to the bucket.

### A6. Old code can still publish schema 2 after cut-over: **BLOCKER**

Two routes run pre-merge code after the merge:

1. **Re-runs of pre-merge runs.** A GitHub re-run executes the original commit and
   workflow file, and re-runs are allowed for 30 days. The refresh workflow is designed to
   treat a re-run as a new run ("Re-running a scheduled or manual workflow records a new
   run").
   - Re-runnable today: `production-refresh` runs #1–#6 (all on b3473b7, 2026-10-02 to
     10-07).
   - Also `pipeline-job` runs #1–#5. Its `publish-serving` option at that code publishes
     schema 2.
   - Every further pre-merge scheduled run extends the window.
2. **Dispatch on a branch carrying pre-merge code.** Five Dependabot branches
   (`dependabot/github_actions/…`) contain `main`'s `production-refresh.yml` and
   `pipeline-job.yml`. "Run workflow" on one of them runs the six-stage schema-2 chain,
   until Dependabot rebases or the PRs are closed.

**Authentication does not stop either route:**
- The repository's setup script binds the deployer identity to the repository-wide
  principal set (`attribute.repository/…`), not to a branch.
- The pipeline identity authenticated from a non-`main` branch in practice: every
  `probe/pattern-stats` run used `GCP_PIPELINE_SA`.
- Its binding itself predates the setup scripts and could not be read from here.
- A WIF condition on `refs/heads/main` would not stop route 1: a re-run of an old `main`
  run still has ref `main`.

**Demonstrated** (local synthetic lake; `main`'s publisher, then `m8`'s):

| Step | Pointer | Merged API `/chart` |
|---|---|---|
| merged code publishes | schema 4, `meta-738c75c1d895` | bars + analysis + explanations |
| **old code publishes (re-run / old-ref dispatch)** | **schema 2, `meta-6182506093d2`** | **bars only, `no_analysis_in_snapshot`** |
| next merged-code refresh | schema 4 again | restored |

What the old publish does:
- It writes the pointer unconditionally. `main`'s publisher has no compare-and-swap; the
  `m8` publisher's CAS cannot protect against a writer that does not use it.
- It deletes nothing of schema 4. Its clean-up only knows `v=` folders and weekly copies;
  the analysis, event and explanation objects were untouched.
- It records a PUBLISHED schema-2 snapshot in Firestore.

The harm is a silent, temporary regression of the live product to bars-only, together
with a schema-2 publication after cut-over. That is exactly what 3.6 forbids.

What already holds:
- No two writers run at once: every writer workflow on every branch shares the
  repository-wide `pipeline-writes` concurrency group.
- Scheduled runs and API-dispatched runs are safe (A2, A3).

**Options. I have not done any of them; each needs your decision:**

- **Option 1 (recommended): make the bucket reachable only from `main`'s current
  workflows.**
  - Create a GitHub Environment (e.g. `production`) whose deployment branches are `main`
    only.
  - Declare `environment: production` in the merged writer and deploy workflows. This is
    a small workflow change on `m8`.
  - After the merge, move `GCS_BUCKET` (at least) from repository variables into that
    environment.
  - Effect: old workflow files declare no environment, so they see no bucket.
    - Old `production-refresh` is skipped by its own `if: vars.GCS_BUCKET != ''`.
    - Old `pipeline-job` falls back to the runner's disk.
    - A dispatch of the new workflows on any other branch is refused by the environment
      rule.
  - Ordering: merge first, while repository variables still exist, so nothing breaks
    mid-way. Move the variable before the manual first refresh. That is a new step
    between cut-over steps 5 and 6.
  - It needs a GitHub settings change that only you can make; this session's GitHub
    access cannot read or write environments or variables.
- **Option 2: procedural only.**
  - Close the five Dependabot PRs before the merge (Dependabot deletes their branches;
    reopen later).
  - Adopt a rule: never re-run a pre-cut-over run.
  - This leaves a human-discipline path for at least 30 days. It does not meet the hard
    check as written, so it would need an explicit D5 amendment accepting the residual
    risk.
- **Option 3 (not recommended): a new pointer key for schema ≥ 3.** Old code would then
  publish to a pointer nobody reads. It changes the storage contract (ADR-0016/0026) and
  the reader, and complicates rollback.

### A7. Deployment ordering at the merge: FOLLOW-UP (non-blocking)

- One push to `main` starts `deploy-api` and `deploy-web` concurrently. Recent runs took
  1–2 min each; the first M8 API image builds new dependency layers and may take longer.
- So the new frontend can be live for a few minutes before the new API. During that time
  the security page calls `/chart` on the old API, gets 404, and shows an error. It
  recovers by itself.
- Old browser tabs against the new API are fine: `/weekly` and search are unchanged (B1).
- Cut-over steps 3–4 already check both before the first refresh. Suggestion: merge at a
  quiet hour.

### A8. Manual first refresh: PASS

- Either route works and dispatches on `main`: the System page's Refresh (API) or "Run
  workflow" on `main`.
- Runs are serialized by `pipeline-writes`, so a late schedule cannot overlap the manual
  run.
- The 20:15 IST schedule has fired hours late (03:07, 01:19 and 01:38 IST on the last three). A
  morning IST merge leaves the manual run well clear of it.

## 2. Compatibility and capacity

### B1. The merged API reads schema 2: PASS

- **Unit tests:** `test_a_schema_2_snapshot_says_it_has_no_analysis` and
  `test_a_schema_2_chart_is_bars_with_the_reason`.
- **Cross-version:** the snapshot was published by `main`'s own publisher, then read by
  the merged API.
  - search, security and `/weekly` returned 200;
  - `/chart` returned 200 with bars and `analysis_status: no_analysis_in_snapshot`;
  - `/analysis` and `/explanations` returned an explicit 404 `no_analysis_in_snapshot`;
  - `/system/status` returned 200.
- **Frontend:** it shows "The current data snapshot carries no analysis yet: the chart
  shows weekly bars only."
- This read is allowed under 3.6: it is for the retained snapshot and rollback, not a
  publishing path.

### B2. First schema-4 publish over a schema-2 pointer: PASS

In the cross-version run, the merged code published schema 4 over `main`'s schema-2
pointer, and every endpoint then served analysis and explanations from the same
meta_version.

### B3. The previous API revision reads a schema-4 snapshot: PASS

`main`'s reader served search, security and `/weekly` from the merged code's schema-4
snapshot. The older reader ignores the analysis and explanation blocks.

### B4. Firestore records: PASS, with a rollback note

- **Merged code reads old records.** The merged code reads the six-stage runs written
  before ANALYSIS existed (`core/tests/test_runs.py`). A run queued by the old API gains
  ANALYSIS when it starts.
- **The old revision on new snapshot records.** It parses schema-4 snapshot records
  (checked).
- **The old revision on new run records: it cannot parse seven-stage run records.**
  - `Input should be 'INGEST', …` at `stages[5].stage`.
  - `main`'s run listing validates every document in one comprehension
    (`pipeline/runs.py:607`), so a single seven-stage run breaks the whole run history
    on a rolled-back API revision.
  - This feeds into B9.

### B5. Builds: PASS

- At 10c0880, every CI job passed: Python (1,049 tests), Frontend, the
  Firebase-emulator API tests, the end-to-end suite, and Docker builds of all three
  targets (`api`, `pipeline`, `jobs`).
- Every later commit is documentation only.
- The current head's CI is in §4.5.

### B6. Run time: PASS (rehearsal evidence)

- Today's chain takes about 37 min.
- M8 adds ANALYSIS (459 s computed and explained on 4 CPUs, the 6e real-NSE rehearsal)
  and schema-4 publication (242 s first, 12,772 objects verified). That gives about
  49 min.
- The job timeout is 350 min, and the run store's lost-run threshold is 6 h.
- Production's first ANALYSIS computes everything: the rehearsals wrote nothing to the
  lake, so there is nothing to reuse.

### B7. Lake growth is bounded: PASS (code and tests)

- **Per full regeneration:** documents 707 MB and events 164 MB, compressed (6b), plus
  explanations (mean 26.7 KB each uncompressed, about 85 MB uncompressed).
- **Clean-up after each publish** keeps only what the live and previous snapshots and the
  latest staged sets name, across `analysis/`, `events/` and `explanations/`
  (`serving.py:_remove_unreferenced`; schema-3/4 clean-up tests).
- **Steady state:** about 2–3 regenerations, roughly 2–3 GB.
- A normal weekday regenerates nearly everything, because the forming week changes.
  That is churn, not growth.
- *Note:* `main`'s clean-up does not know the analysis prefixes. A rollback to old code
  leaves analysis objects orphaned but harmless.

### B8. IAM and the real-GCS paths: PASS as recorded; **EVIDENCE GAP**

**As recorded in the repository (PASS):**
- The API runs as `chartlens-api` with `roles/storage.objectViewer` on the whole bucket,
  which covers the new prefixes.
- The pipeline identity has bucket `objectUser` (as recorded in the refresh workflow's
  header and ADR-0018), which covers the new prefixes, deletes and generation
  preconditions.
- No new secret, no new environment variable, and the deploy environment is unchanged.

**Evidence gaps:**
1. The actual GCP bindings could not be read from here; the above comes from the setup
   scripts and ADR-0016.
2. Two production paths have never run against real GCS:
   - the pointer compare-and-swap (`GcsObjectStore.swap`, generation-match);
   - writes by the pipeline identity to the analysis, event and explanation prefixes.

   Every rehearsal was read-only towards the lake. `put_immutable` (create-only,
   generation 0) is already proven in production through the weekly copies.

Both gaps fail safe: an error in either happens before the pointer moves, so the
schema-2 snapshot stays live. They close at cut-over step 9 (3.2). I suggest accepting
them as Gate 3 evidence rather than adding a pre-merge write test. A pre-merge test
would write to the production bucket.

### B9. Rollback path: FOLLOW-UP (D5's recorded path needs correcting)

D5 recorded: "re-point to the retained schema-2 snapshot and redeploy the previous
revision". The review found three corrections:

1. **Data rollback needs no code rollback.**
   - The merged API reads schema 2 (B1). Rolling back data means writing the retained
     schema-2 version manifest (`v=<meta>/…`) back to the pointer.
   - There is no tool for this; it would be done by hand with `gcloud storage`.
2. **The retained snapshot exists only until the second schema-4 publish.**
   - Clean-up keeps live and previous only. With the cut-over procedure, the manual
     refresh and that evening's scheduled run remove it, within hours.
   - After that, data rollback means republishing schema 2 with the old code, which
     rebuilds it deterministically from the weekly data.
3. **Rolling back the API revision degrades run history.**
   - The old revision reads schema-4 data (B3).
   - It fails on any seven-stage run record (B4), so the System page's run history
     breaks until those records are gone or the merged API is back.
   - The frontend rolls back separately (a previous Firebase Hosting release).

None of this is rehearsed.

### B10. Runner image change: NON-BLOCKING note

CI annotates: "the ubuntu-latest label will migrate to Ubuntu 26 beginning October 19,
2026". Python, uv and dependencies are pinned, and the reuse fingerprint carries the
Python, numpy, pandas and pydantic versions, not the OS. Gate 3 evidence gathered either
side of that date is still valid; I'm noting it in case the first run changes image.

## 3. Gate 4: before-merge documentation

### C1. M4 §61 exists: PASS

"ChartLens — Milestone 4 §61 report (weekly data product)", a Claude Doc dated
2026-10-02. The M3 report also exists (2026-10-01).

### C2. M5 and M7 report status: ESTABLISHED

**Neither is written.** No draft exists among the reports or the repository. Both are
required before M8 is declared complete.

### C3. M8 §61 structure: PREPARED (Appendix)

It mirrors the M3/M4 reports, plus a Gate 3 section.

### C4. Inconsistencies identified (fixed in the final documentation stage, not now)

| Where | Now | Should say |
|---|---|---|
| README status line | "Phase 1 · Milestone 7 (production operations…)" | Milestone 8 state |
| README milestone table, row 8 | "In progress: indicators, swings, structure" | final M8 state |
| README "Production refresh and run history" | "GitHub Actions runs the six stages" | seven (ANALYSIS) |
| PR #16 title | "M8: weekly technical analysis (draft, phase 1: indicators)" | final scope |
| PR #16 description | "**Phase 1 only** so far…" | final scope and the gate checklist |
| ADR index, 0023 row | "API and K7 to be amended by 0026" | amended by 0026 (accepted) |
| ADR-0023 header | no note of the 0026 amendments (K7, API) | amendment note |
| ADR index, 0025 and 0026 rows | "Accepted" with no phase closure | "6b closed" / "6c closed", like 0024, 0027, 0028 |
| Project status doc | live-production facts end at the 6a probe snapshot | updated at Gate 3 |

### C5. CI regression from the gate document: FOUND AND FIXED

The locked gate named the old Hosting URL in its non-blocking table. The guard test
`test_no_code_path_treats_the_old_hosting_url_as_canonical` forbids that URL in tracked
files, so PR #16's Python CI was red on 5424239 and ee2d187. 34e3776 names the origins
by where they are configured instead. The guard test passes locally and the full suite
is otherwise green (1,048 passed, 1 failed before the fix). CI on 34e3776 is green
(§4.5).

## 4. What this review leaves for you

1. **A6:** choose Option 1, 2 or 3. Option 1 needs a small workflow change on `m8` (your
   approval) and a GitHub Environment you create. Then this review re-checks A6 and
   reports again. Nothing else should change before the merge.
2. **B8:** accept the two real-GCS paths as Gate 3 evidence (they fail safe), or ask for
   another route.
3. **B9:** accept the corrected rollback description into D5.
4. **Non-blocking follow-ups** to record in Gate 5: A4 (unusable one-off
   publish-serving), A5 (obsolete writer workflows), A7 (deployment ordering window),
   B10.

### 4.5 CI on the current head

All seven CI checks pass on 34e3776 (the gate wording fix): Python, Frontend, the
Firebase-emulator API tests, the end-to-end suite, and the three Docker targets. PR #16 is
green again. This report adds documentation only.

## 5. Decisions (Suba, 2026-10-09) and the A6 implementation

### 5.1 B8: accepted as an evidence gap, not a merge blocker

Recorded as: *not demonstrated against real Cloud Storage before the merge; production
validation provides the first live evidence.* B8 is **not** upgraded to PASS here.

At Gate 3, verify explicitly:
- actual GCS object writes;
- actual manifest and object validation;
- the actual compare-and-swap pointer update;
- retention of the live and previous snapshots;
- the API and chart reading the resulting live snapshot.

### 5.2 B9: correction accepted

The rollback record (gate D5) now distinguishes these cases:

| Situation | Required action |
|---|---|
| Bad schema-4 live snapshot, compatible API | Re-point the live pointer to a retained compatible snapshot |
| Need the schema-2 data snapshot | It must still exist; retention is not indefinite (gone after the second schema-4 publish) |
| Old API revision required | Deploy a compatible old revision as well |
| Only data is wrong | No automatic code rollback |
| Seven-stage run records | The old API cannot fully operate against them (run history fails) |

The rollback path is **recorded and technically bounded, not rehearsed.**

### 5.3 A6: implementation and the focused re-check

**Invariant:** only workflows executing from `main` may possess the live-publication
storage target and credentials. It is enforced at two independent layers, so that it does
not rest on a variable merely being absent.

**Layer 1: GitHub.** Implemented on `m8`.
- `production-refresh`, `pipeline-job`, `deploy-api` and `deploy-web` declare
  `environment: production`. No other workflow does.
- The four target and credential variables (`GCS_BUCKET`, `GCP_WIF_PROVIDER`,
  `GCP_PIPELINE_SA`, `GCP_DEPLOY_SA`) move from repository variables into that
  environment, whose deployment branches are `main` only.
- A workflow file that does not name the environment sees none of them:
  - old commits' re-runs;
  - Dependabot branches;
  - probe and verify workflows.
- A workflow that names it on another branch is refused by GitHub before it runs.
- The protected variables are read only inside steps, never in a job-level `if` or
  `env`. A job gated on an environment variable could otherwise be skipped silently.
- The refresh's first step fails loudly if no bucket is provided.
- `pipeline-job` decides GCS or local inside a step.

**Layer 2: GCP.** Script `scripts/gcp_setup_m8.sh`, which you run.
- The pipeline and deployer identities accept exactly one OIDC subject:
  `repo:subasankars-gif@288858503/ChartLens@1398125563:environment:production`. GitHub issues that subject
  only to jobs the environment admits.
- The repository-wide principal set is removed, so a workflow that hard-codes the
  provider and account names still cannot impersonate either identity.
- This covers point 7: no alternate configuration bypasses the protection.

**Static evidence (PASS).** `tests/test_production_environment.py`, 7 tests:
- every job of the four workflows names `production`, and nothing else does;
- the protected variables are never read in workflow-level `env`, job-level `if` or job
  `env`;
- the refresh fails without its bucket;
- every GCP sign-in takes its identity from the protected variables, and none uses a key;
- no tracked code, workflow or configuration names the live bucket;
- the setup script binds both identities to the environment subject.

The full suite passes: 1,056 tests. The README's storage section is updated.

**Settings only you can apply.** This session cannot read or write GitHub environments
or variables, or GCP IAM.
- **S1.** Create Environment `production`, with deployment branches set to `main` only.
  Add the four variables with their current values.
- **S2.** `bash scripts/gcp_setup_m8.sh before-merge`. This adds the environment-subject
  binding. It is harmless today, because the current `main` does not use that subject.

**Sequencing, a decision for you.** The negative checks (points 3, 4, 6, 7) need
enforcement switched on: the repository-level variables removed and `after-merge` run.
Today's `main` workflows do not name the environment, so switching it on stops
production's refresh until the merge. Two orderings are possible:

- **(a) Enforce, verify, then merge. Recommended: it satisfies "block merge until A6 is
  re-checked" literally.**
  1. Remove the four repository variables.
  2. Run `after-merge` (despite its name, it works before the merge).
  3. Run the negative checks below.
  4. Merge.

  Cost: production's refresh is paused (each run is skipped) from step 1 until the merge.
  Data stays at the last schema-2 snapshot for those hours. Nothing can publish, so every
  negative check is harmless even if it fails. Don't press Refresh during the pause; the
  run would be skipped and later recorded as lost.
- **(b) Merge, then enforce.** Enforcement and the negative checks happen after the merge,
  before the first schema-4 refresh. Live is still schema 2 then, so a failed check would
  only republish schema 2. The checks are equally harmless, but the merge is not blocked
  by them.

**Negative checks (both orderings).** All are attempted, not inferred:

| # | Check | Expected |
|---|---|---|
| 3 | Re-run a pre-merge `production-refresh` run (e.g. #6, b3473b7) | job skipped: its old `if` sees no `GCS_BUCKET`; no run record written |
| 3′ | Re-run a pre-merge `pipeline-job` run | runs on the runner's disk only; no GCP sign-in step runs |
| 4 | "Run workflow" `production-refresh` on a Dependabot branch | job skipped (no `GCS_BUCKET`) |
| 6 | Probe or verify workflow on a probe branch | no variables; GCP sign-in cannot be attempted |
| 7a | Probe that **hard-codes** the provider and pipeline-account names and signs in from a non-`main` branch | GCP refuses the token: wrong subject |
| 7b | Probe that names `environment: production` from a non-`main` branch | GitHub refuses the job: the branch policy |
| — | Today's `main` `production-refresh` (pre-merge file, ordering (a) only) | job skipped; even `main`'s old file cannot reach the lake |

**Positive checks.** These need the merged code, so they are cut-over steps:
- **(1) Scheduled refresh publishes:** the first scheduled run after the merge.
- **(2) API-dispatched refresh publishes:** cut-over step 7, triggered from the System
  page's Refresh.
- **Deploy identity:** the merge push's `deploy-api` and `deploy-web` succeed under the
  environment subject.

**Point 5** (the one-off Pipeline job's publish refuses) is already PASS (A4).

**Re-check status: repository side PASS; live negative checks PENDING S1, S2 and your
choice of ordering.** I'll report the A6 re-check after the checks run.

Follow-up created by A6 (non-blocking): read-only probes of the real lake, as used for
6a–6e and Investigation 0001, are no longer possible from probe branches. If they are
needed again, they would need their own read-only identity and environment.

## Appendix: M8 §61 report structure (prepared, to be filled at completion)

Following the M3/M4 reports:

1. **Summary.** What M8 delivers, the headline numbers, and the completion verdict
   against the gate.
2. **What was built.** Engine layers (ADR-0020–0022), orchestration and serialization
   (6a), the job layer and ANALYSIS (6b), schema-3/4 publication and the API (6c), chart
   layers (6d), explanations (6e). Packages and layer graph.
3. **Verification.** The checkpoint evidence of each phase; test counts; determinism and
   reuse; the real-NSE fingerprints.
4. **Worked examples.** A security through the chain: bars, analysis document, chart
   layers, explanation claims and their quoted values.
5. **Findings and decisions.** The locked decisions K1–K9 and per phase; Investigation
   0001 and its follow-ups; what was deliberately not built.
6. **First production run (Gate 3).** Evidence 3.1–3.9 from the live cut-over: timings,
   reuse, the publication checks, the API and chart against the live snapshot.
7. **Non-blocking register and next steps.** Gate 5 as closed out; channels as the first
   post-M8 phase.

## 6. Focused A6 re-check (2026-10-09, ordering (a): protection switched on before the merge)

**Settings applied by Suba.**
- S1: the `production` environment exists (deployment branches: `main` only) with the
  four variables, and the repository copies were deleted.
- S2 (`before-merge`) and `after-merge`: Cloud Shell output received. Each identity
  ended with exactly one member, the environment subject. Provider `chartlens-repo` maps
  `google.subject = assertion.sub` and has the attribute condition
  `assertion.repository=='subasankars-gif/ChartLens'`.

**Checks** (run IDs are GitHub Actions runs; evidence comes from job and step
conclusions and check-run annotations, because job logs are not reachable from this
session):

| # | Check | Run | Result | Evidence |
|---|---|---|---|---|
| 3 | Re-run of pre-merge refresh #8 (b3473b7) | 37837070543 attempt 2 | **PASS** | job `refresh` **skipped**: the old file's `if` saw no `GCS_BUCKET` |
| 3′ | Re-run of pre-merge Pipeline job #5 (b3473b7; job `info`) | 37335565816 attempt 2 | **PASS** | "Authenticate to GCP" **skipped**; "Warn when the lake is ephemeral" ran (runner disk only) |
| 4 | Refresh dispatched on `dependabot/github_actions/actions/checkout-7` (1bf4f94) | 37928295865 (#11) | **PASS** | job **skipped**. The first attempt, #9 (37928005526), was cancelled by the shared `pipeline-writes` queue before it was evaluated, so it counts as NOT RUN and was repeated |
| — | Today's `main` refresh dispatched (pre-merge file) | 37928007927 (#10) | **PASS** | job **skipped**: even `main`'s old file cannot reach the lake |
| 6 | Probe without the environment (`probe/a6`) | 37928441272, job `check6-no-environment` | **PASS** | the four variables were empty (lengths 0); the sign-in attempt failed in the action (no provider given) |
| 7a | Probe **hard-coding** the provider and both identities from `probe/a6` | 37928441272, job `check7a-hard-coded-identity` | **PASS (refusal by GCP)** | both sign-ins: `403 Permission 'iam.serviceAccounts.getAccessToken' denied`. Token claims recorded: `sub = repo:subasankars-gif@288858503/ChartLens@1398125563:ref:refs/heads/probe/a6`, `environment = null` |
| 7b | Probe requesting `environment: production` from `probe/a6` | 37928441272, job `check7b-environment-from-other-branch` | **PASS (refusal by GitHub)** | "Branch "probe/a6" is not allowed to deploy to production due to environment protection rules"; the job never started a step |

(First probe run 37928080195 gave the same results, before the claims annotation was
added.)

**Defect found in the A6 implementation: the binding subject was wrong. Fixed in the
repository; the fix must be re-applied in GCP.**
- The recorded claim shows GitHub issues this repository **immutable subject claims**:
  `repo:<owner>@<owner-id>/<repo>@<repo-id>:…`. That is GitHub's default for
  repositories renamed or created after 2026-07-15.
- The binding added by S2 used the name-only form `repo:subasankars-gif/ChartLens:
  environment:production`, which **no token will ever carry**.
- The negative checks are unaffected: a token from another branch is refused either way.
- But after the merge the **production workflows would also be refused**: no deploy and
  no refresh could sign in. Ordering (a) surfaced this before the merge.
- The correct subject is `repo:subasankars-gif@288858503/ChartLens@1398125563:
  environment:production`:
  - the IDs come from the recorded token and the repository API;
  - the environment suffix follows GitHub's documentation, which says the owner and
    repository IDs are always in the `repo` segment for repositories using immutable
    subject claims.
- The script, test and README now use it. `after-merge` now removes every other pool
  member, including the wrong subject.

**Re-check status: negative checks PASS. A6 is NOT yet passed:**
1. The corrected binding must be applied: `before-merge`, then `after-merge`, from the
   updated `m8`.
2. Check 7a must be repeated against the final binding.
3. The exact environment subject has only been derived, not observed. It will be observed
   at the first sign-in from `main` after the merge (the deploys) unless an earlier
   observation is approved: a probe job in a separate, empty scratch environment would
   record the format with no access to anything.

### 6.1 Corrected binding applied and 7a repeated (2026-10-09)

- **Cloud Shell, from 4de8698.**
  - `before-merge` added the immutable-ID subject.
  - `after-merge` removed the wrong name-only subject.
  - Final state: **each identity has exactly one member**,
    `principal://…/workloadIdentityPools/github/subject/repo:subasankars-gif@288858503/ChartLens@1398125563:environment:production`.
- **Probe re-run (37928441272, attempt 2), against the final binding:**

| # | Result | Evidence |
|---|---|---|
| 6 | **PASS** | variables empty; sign-in could not be attempted |
| 7a | **PASS** | pipeline and deployer: `Permission 'iam.serviceAccounts.getAccessToken' denied`; token `sub = repo:subasankars-gif@288858503/ChartLens@1398125563:ref:refs/heads/probe/a6`, `environment = null` |
| 7b | **PASS** | "Branch "probe/a6" is not allowed to deploy to production due to environment protection rules" |

- Checks 3, 3′, 4 and today's-`main` are unaffected by the binding change; they fail
  earlier, at the missing variables. They stand as recorded in §6.

**A6 re-check verdict.** Every attempt from a re-run, another branch or a hard-coded
identity was refused, by GitHub (variables, environment rule) and by GCP (subject
binding). One **evidence gap** remains:
- The `production` environment's token subject has been derived (the probe's recorded ID
  prefix plus GitHub's documented `:environment:<name>` suffix), not observed.
- If the derivation were wrong, the first sign-ins after the merge (deploys, then the
  manual refresh) would be refused. That failure is safe: nothing publishes, and the
  schema-2 snapshot stays live.
- It is closed either by an empty scratch-environment probe before the merge (needs
  Suba's approval), or at cut-over step 3.

### 6.2 Decision on the environment-subject gap (Suba, 2026-10-09): option (b)

**Accepted pre-merge evidence gap, not a passed check.** The `production` environment's
token subject is closed at cut-over, by the first authorized sign-in (the merge push's
deploys), before any schema-4 refresh:

1. Observe the actual OIDC subject from the authorized deployment sign-in.
2. Compare it, character for character, with the Workload Identity binding.
3. If authentication fails: stop the cut-over, correct the binding, retry the deployment.
4. Do not start the schema-4 refresh until the deployment has succeeded and the
   production identity is verified.

No scratch environment was created. The probe needs no further step.

**Pre-merge checks of the live site during the pause.** These check the current
(pre-M8) site, not Gate 3.9; the layers and "What the chart shows" are Gate 3.9 after
deployment:
- search returns real securities;
- a security page opens;
- the weekly chart renders real bars;
- the System page's data date (expected 2026-10-08; the displayed date is recorded
  as shown).

Result (Suba, 2026-10-09 18:59 IST, during the pause):

| Check | Result |
|---|---|
| Search returns real securities (RELIANCE) | **yes** |
| A security page opens | **yes** |
| The weekly chart renders real bars | **yes** |
| System page | "Serving now. Data through 8 Oct 2026". Snapshot `meta-95df3d57e421`, published 9 Oct 00:48 IST; weekly `wk-0069f4822725`; adjustment `adj-1b10a5b3f2e1`; methodology `f9e40bee87d3`; API healthy, 0.1.0 |

Notes:
- **Which run published the live snapshot.** The publication time (00:48 IST = 19:18 UTC
  on 8 Oct) matches refresh **#7** (manual dispatch, finished 19:18 UTC), not #8 as
  assumed earlier.
- **Refresh #8.** The scheduled run started at 01:37 IST, so it found the same data. Its
  first attempt succeeded, which is consistent with an UNCHANGED outcome; the run log is
  not readable from here to confirm.
- **Nothing published during the checks.** The live snapshot predates every A6 check
  (first one 17:37 IST on 9 Oct).
- The last published snapshot stays readable during the pause.

### 6.3 R12 fixed before the merge (Suba, 2026-10-09) and A6 re-confirmed

**The hazard.** The only code path that grants the repository-wide Workload Identity
binding is `scripts/gcp_setup_m5.sh`, its deployer step (`git grep` finds no other).
The README's "API deployment" setup tells you to run it, and it was written to be
re-runnable, so a re-run after A6 would silently restore deploy access for any branch or
old workflow.

**The fix (4128173).**
- A guard runs before the script changes anything (only `gcloud config set project`
  precedes it). For the deployer and the pipeline identity, if the identity exists, it
  reads its `workloadIdentityUser` members.
- The script **exits with an actionable error** if any member is a pool *subject*
  binding (the A6 marker), or if the policy cannot be read.
- On a project that predates A6 (no such binding, or the identities not yet created),
  the script behaves exactly as before.
- No other change to the script.

**Verification.**
- Three behavioural tests with a fake `gcloud` (`tests/test_production_environment.py`):
  - after A6: refuses, makes no mutating call, and never names the repository-wide
    member;
  - mid-cut-over (both members present): refuses, makes no mutating call;
  - pre-A6 project: runs fully, including the original binding.
- The guard reads members with the same `gcloud … get-iam-policy --flatten … --format=
  value(bindings.members)` call as `gcp_setup_m8.sh status`, whose real Cloud Shell output
  (one member per line) is recorded in §6 and §6.1.
- The guard was **not** run live: if it misbehaved, it would re-open the binding it
  exists to protect.
- Full suite: 1,059 passed.

**A6 restrictions re-confirmed after the fix:** probe re-run 37928441272, attempt 3.

| # | Result |
|---|---|
| 6 | **PASS**: no variables; sign-in impossible |
| 7a | **PASS**: pipeline and deployer both refused by GCP with `403 … iam.serviceAccounts.getAccessToken denied` (subject `…:ref:refs/heads/probe/a6`) |
| 7b | **PASS**: GitHub refused the `production` environment for `probe/a6` |

The GCP binding's positive state (exactly one member per identity) was last observed in
Suba's `after-merge` output (§6.1). A fresh `bash scripts/gcp_setup_m8.sh status`
(read-only) re-observes it.
