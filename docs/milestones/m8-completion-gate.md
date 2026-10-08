# M8 completion gate

**Status:** **LOCKED** · 2026-10-08. Approved by Suba with amendments: D1 = Option B
(channels are post-M8); D2 = merge, then validate. Proposed the same day (5424239). This
document decides what M8 needs to be complete. It does not itself start any work.

Phase 6 is complete (6a–6e closed, ADR-0024). This gate defines what else M8 needs, so
that no "next phase" in an ADR becomes a merge or completion requirement by default.

## The principle

> M8 is complete when the committed 6a–6e architecture is operational end-to-end in the
> production refresh path, its required milestone documentation is complete, and all
> explicitly deferred investigations/follow-ups are recorded as non-blocking. Features
> not included in the approved M8 completion scope do not block the milestone. Merging
> the implementation into main is a prerequisite for, but does not itself constitute,
> M8 completion.

**No-blocker criterion.** No open investigation, follow-up or known limitation may
prevent the system from satisfying the committed M8 behaviour. Open items are allowed;
they must be recorded as non-blocking (Gate 5).

**Governance rule (general, not only for channels).** The existence of an accepted design
does not constitute a delivery commitment unless the milestone scope or an accepted phase
gate explicitly requires its implementation.

## Gate 1: Analytical scope (satisfied)

| Phase | Scope | Closed |
|---|---|---|
| 6a | orchestrator, `TechnicalAnalysis`, canonical serialization | 2026-10-07 (e869159) |
| 6b | job layer and the ANALYSIS stage (ADR-0025) | 2026-10-07 (be2309c) |
| 6c | schema-3 publication and the analysis API (ADR-0026) | 2026-10-07 (6d64ce9) |
| 6d | chart layers (ADR-0027) | 2026-10-07 (88854c6) |
| 6e | explanations, schema 4 (ADR-0028) | 2026-10-07 (aa2b672) |

The engine layers they compose (ADR-0020 to ADR-0022, Phases 1–5) were closed before 6a.

## Gate 2: Channels (decided: post-M8)

**D1, approved: Option B.** Channels are an accepted architectural design (ADR-0021 §D)
but were never an explicit M8 delivery commitment. The charter is "Weekly technical
analysis & pattern engine (ADR-0019–0023)".
- Channels moved from patterns to levels on 2026-10-02 and were marked "not yet built".
- ADR-0024's Phase 6 delivery table omits them.
- ADR-0024 §8 gives them their own levels phase and their own review.

They become the **first post-M8 analytical phase**, in the levels layer, with their own
design review before implementation. ADR-0024 §8 and ADR-0021 §D state this.

## Gate 3: Production validation (blocking)

At least one **normal live production refresh**, not a probe or rehearsal, exercises the
whole chain:

```
NSE input → INGEST → CORPORATE_ACTIONS → ADJUSTMENT → DATA_QUALITY → WEEKLY
  → ANALYSIS (documents, event datasets, explanations, manifests)
  → PUBLISH_SERVING (schema 4, atomic pointer) → API → chart
```

| # | Evidence | How it is shown |
|---|---|---|
| 3.1 | The seven stages run and are recorded correctly | Firestore run record, System page, workflow log |
| 3.2 | Schema-4 publication is atomic | snapshot schema 4; pointer moved by compare-and-swap after checks 1–12 pass |
| 3.3 | Analysis and explanation manifests agree | check 10/11 results; the explanations' `analysis_set_hash` equals the analysis manifest's |
| 3.4 | The API reads the published snapshot | `/system/status` and `/analysis` name the live meta_version |
| 3.5 | `/chart` reads the same snapshot | every component of a `/chart` response names that meta_version |
| 3.6 | **No schema-2 production publishing path remains active after cut-over** | the deployed API and the refresh run the merged code; no schema-2 publish after cut-over. Backward-compatible schema-2 *reading* is permitted only for retained historical snapshots and rollback; it is not a violation |
| 3.7 | Reuse | **A same-input rerun after the first live schema-4 refresh demonstrates complete reuse and UNCHANGED publication** (every analysis and explanation reused). Separately, the normal refresh's reused-security count is recorded (D4) |
| 3.8 | Failure safety | **Production validation observes the already-proven failure-safety mechanism; it does not intentionally induce a production failure** (D3) |
| 3.9 | The live product shows it | chartlenslab.web.app: search, security page, weekly chart with layers and "What the chart shows" on real data (this also completes the outstanding M6 real-data checks) |

## Gate 4: Documentation (blocking, in two stages)

This is split so the M8 report, which needs production evidence, never has to exist
before the merge that produces that evidence.

**Required before merge** (part of the pre-production readiness review):
- the M4 §61 report exists;
- the status of the M5 and M7 §61 reports is known;
- the structure of the M8 §61 report is prepared;
- inconsistencies in the project status, ADR index and README are identified.

**Required before declaring M8 complete:**
- M5, M7 and M8 §61 reports final;
- project status final;
- ADR index final;
- README final (the status line still says "Milestone 7"; the M8 row still says
  "In progress: indicators, swings, structure");
- PR #16 title, description and checklist final (they still say "phase 1: indicators").

## Gate 5: Non-blocking register

Each item is recorded as not preventing the committed M8 behaviour.

| Item | Status | M8 effect |
|---|---|---|
| Investigation 0001 | **closed (accepted)**; not an unresolved defect | none; follow-ons below are non-blocking |
| 0001-A price-domain semantics | open, read-only | non-blocking |
| 0001-B triangle geometry | open, read-only | non-blocking |
| 0001-C drawable line extent | open, read-only | non-blocking |
| ADR-0027 §10.1 spans across missing weeks | open | non-blocking |
| Channels | post-M8 (D1) | non-blocking |
| Old default Hosting origins in the API CORS list (deploy-api.yml `CORS_ORIGINS`) | remove after the chartlenslab checklist | non-blocking |
| Probe branches `probe/pattern-stats`, `probe-results/pattern-stats` | still exist; this session cannot delete them | non-blocking cleanup |
| Dependabot Actions PRs | not reviewed | non-blocking |
| Scheduler lateness (about 7 h once) | watching | non-blocking |
| Pre-open overrides (NSE circulars) | none filed | non-blocking (M3 data item) |

**Explicitly not M8 requirements:**
- closing 0001-A/B/C, and 0001 is not reopened because production deployment happens;
- resolving missing-week span semantics;
- implementing channels;
- a production failure injection;
- deleting the probe branches;
- reviewing the Dependabot PRs;
- resolving scheduler lateness;
- filing NSE pre-open overrides.

## Decisions

**D1. Channels: APPROVED, Option B (post-M8).** See Gate 2.

**D2. Merge and production sequencing: APPROVED.** The previous rule, "PR #16 is not
merged until M8 is complete", is **replaced**. It could not be satisfied together with
Gate 3, because the production path runs from `main`:
- the API and the frontend deploy on push to `main`;
- the API dispatches refreshes on `ref = main`;
- GitHub runs the 20:15 IST schedule from the default branch.

> Merging PR #16 does not declare M8 complete. It begins the final production-validation
> portion of M8. M8 is declared complete only after Gate 3 and Gate 4 pass.

The review before the merge is the **M8 pre-production readiness review**. It is never
called "M8 complete" or "M8 final review".

Cut-over procedure:

1. M8 pre-production readiness review passes (D5 and Gate 4's before-merge items),
   including the A6 re-check (readiness §5.3; its sequencing is your decision).
2. Merge PR #16.
3. Confirm the API deployment is healthy.
4. Confirm the frontend deployment is healthy.
5. Confirm the production refresh workflow resolves to the merged code on `main`.
6. Confirm no scheduled schema-2 publisher can run.
7. Trigger the first production refresh **manually**. This removes the timing race with
   the 20:15 IST schedule, which becomes the next ordinary run, not the cut-over
   mechanism.
8. Wait for all seven stages.
9. Validate the schema-4 publication (3.1–3.3, 3.6, 3.8).
10. Validate the API and chart against the live snapshot (3.4, 3.5, 3.9).
11. Allow the normal schedule to continue.
12. Run the same-data reuse validation (3.7).
13. Complete the documentation (Gate 4, final stage).
14. Declare **M8 COMPLETE**.

**D3. Failure safety: observed, not induced.**
- *Already proven* in 6b/6c, by tests and by rehearsals against the real lake:
  - checks run before the pointer moves;
  - a failure before the pointer moves leaves the old live snapshot;
  - publication is atomic;
  - compare-and-swap protects the live pointer.
- *Observed in production* (Gate 3):
  - the previous snapshot is retained;
  - the new snapshot was published through the normal atomic path;
  - the live pointer references the new snapshot;
  - the previous snapshot remains readable.
- No production failure is injected. The code has no fault switch, and one would add
  risk without proving anything new.

**D4. Reuse: two tests, two claims.**
- *Normal refresh:* new bars lead to correct selective recomputation and reuse of
  unchanged securities. Its reused-security count is recorded. It is low on a normal
  weekday, because the forming week changes for nearly every traded security; that is
  correct, not a reuse failure.
- *Immediate same-data rerun:* identical bars give identical reuse keys, 100% analysis
  reuse, 100% explanation reuse, and UNCHANGED publication. This is the 3.7 requirement.

**D5. Pre-merge deployment readiness: checklist.**

*Operational split-brain (hard checks).* The main risk at cut-over is no longer the
analytical code. It is a new `main` publishing schema 4 while an old path publishes
schema 2.
- the deployment workflows (deploy-api, deploy-web) run from `main`;
- the refresh dispatch (API `github_ref`) resolves to `main`;
- the scheduled refresh is the default branch's workflow, which after the merge is the
  merged code;
- no old workflow can publish schema 2 after cut-over;
- no stale workflow file on `main` can overwrite the schema-4 snapshot, including the
  one-off Pipeline job workflow and any probe or verify workflow that publishes.

*Compatibility and capacity.*
- The merged API reads schema 2 and schema 4, so it can deploy before the first schema-4
  publish. Reading schema 2 is for retained snapshots and rollback only (3.6).
- The API deploy completes before the first schema-4 refresh. Cut-over step 7 makes that
  refresh a manual one.
- Production ANALYSIS time and lake growth. Rehearsal figures: about 5 min first run,
  2 to 4 min explanations and publish; about 0.9 GB documents and events per full
  regeneration, plus explanations.
- No new secret. One IAM change, from A6: the pipeline and deployer identities are
  re-bound to the `production` environment's OIDC subject. Otherwise the jobs service
  account writes the lake and the API service account reads it, as today.
- Rollback path (B9, accepted 2026-10-09): recorded and technically bounded, **not
  rehearsed**.

  | Situation | Required action |
  |---|---|
  | Bad schema-4 live snapshot, compatible API | Re-point the live pointer to a retained compatible snapshot (by hand; no tool) |
  | Need the schema-2 data snapshot | It must still exist: it is gone after the second schema-4 publish; afterwards republish schema 2 with the old code |
  | Old API revision required | Deploy a compatible old revision as well |
  | Only data is wrong | No automatic code rollback; the merged API reads schema 2 |
  | Seven-stage run records | The old API cannot fully operate against them (run history fails) |

*Main-only protection (A6, decided 2026-10-09).* Only workflows executing from `main` may
possess the live-publication storage target and credentials.
- The `production` GitHub Environment (deployment branches: `main`) holds `GCS_BUCKET`,
  `GCP_WIF_PROVIDER`, `GCP_PIPELINE_SA` and `GCP_DEPLOY_SA`.
- The four production workflows name it.
- The pipeline and deployer identities accept only the environment's OIDC subject
  (`scripts/gcp_setup_m8.sh`).
- Evidence is in readiness review §5.3.

*Evidence gap accepted (B8, 2026-10-09):* the real-GCS write paths are not demonstrated
before the merge. Gate 3 gives the first live evidence and is never recorded as a
pre-merge PASS.

## Next

- The **M8 pre-production readiness review** (D5 plus Gate 4's before-merge items) is the
  next M8 step. It is read-only and reported for review before any merge.
- A channels ADR is post-M8 work. It is not a prerequisite for M8 and may be prepared
  independently.
