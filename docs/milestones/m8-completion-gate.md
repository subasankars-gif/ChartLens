# M8 completion gate

**Status:** PROPOSED · 2026-10-08 · for Suba's review. Design only: no code, workflow,
deployment or version change follows from this document until it is approved.

Phase 6 is complete (6a–6e closed, ADR-0024). This document defines what else M8 needs
before it is complete, so that no "next phase" in an ADR becomes a merge requirement by
default.

## The principle (Suba, 2026-10-08)

> M8 is complete when the committed 6a–6e architecture is operational end-to-end in the
> production refresh path, its required milestone documentation is complete, and all
> explicitly deferred investigations/follow-ups are recorded as non-blocking. Features
> not included in the approved M8 completion scope do not block the milestone.

**No-blocker criterion:** no open investigation, follow-up or known limitation may
prevent the system from satisfying the committed M8 behaviour. Open items are allowed;
they must be recorded as non-blocking.

## Gate 1: Analytical scope (satisfied)

| Phase | Scope | Closed |
|---|---|---|
| 6a | orchestrator, `TechnicalAnalysis`, canonical serialization | 2026-10-07 (e869159) |
| 6b | job layer and the ANALYSIS stage (ADR-0025) | 2026-10-07 (be2309c) |
| 6c | schema-3 publication and the analysis API (ADR-0026) | 2026-10-07 (6d64ce9) |
| 6d | chart layers (ADR-0027) | 2026-10-07 (88854c6) |
| 6e | explanations, schema 4 (ADR-0028) | 2026-10-07 (aa2b672) |

The engine layers they compose (ADR-0020 to ADR-0022, Phases 1–5) were closed before 6a.

## Gate 2: Channels (decision needed, D1)

Suba's position: channels are post-M8, unless the original M8 charter promised them.
The repository record on that condition:

- The M8 charter (README milestone table) is "Weekly technical analysis & pattern
  engine (ADR-0019–0023)".
- Channels were in that scope from the start: first as a pattern in ADR-0022, then moved
  to the levels layer on 2026-10-02 (ADR-0022 §7.9). **ADR-0021 §D holds an accepted
  channel design** (candidate, known_at, ACTIVE / BROKEN_UP / BROKEN_DOWN, de-duplication,
  relevance), marked "not yet built".
- No M8 document gives channels a delivery commitment. ADR-0021 §D says they are built
  "when the levels layer is next extended, outside Phase 5". ADR-0024 §8 says "in their
  own levels phase with their own review". Phase 6's delivery table (ADR-0024) omits
  them, and ADR-0028 lists them as "Not in 6e".

So the charter **accepted a channel design** but **never committed to delivering it in
M8**. That is closer to a promise than "ADR-0024 says next" alone, and it is your
decision which weighs more.

- **Option A (inside M8):** channels ADR or amendment, review, implementation, real-NSE
  validation, then production through Gate 3. A new `analysis_version` (levels analyzer)
  and, if explained, new explanation templates and a new `explain_version`.
- **Option B (post-M8, the proposed default):** channels are the first post-M8
  analytical phase, in the levels layer. Two clarifications follow, so "next" never reads
  as "required":
  - ADR-0024 §8: channels are post-M8.
  - ADR-0021 §D: an accepted design, deferred beyond M8 and not built. It is re-reviewed
    when its phase starts, against the rules frozen since (known_at visibility, 6d
    placement, 6e claims, Investigation 0001).

## Gate 3: Production validation (blocking)

At least one **normal live production refresh**, not a probe or rehearsal, exercises the
whole chain:

```
NSE input → INGEST → CORPORATE_ACTIONS → ADJUSTMENT → DATA_QUALITY → WEEKLY
  → ANALYSIS (documents, event datasets, explanations, manifests)
  → PUBLISH_SERVING (schema 4, atomic pointer) → API → chart
```

**Evidence required:**

| # | Evidence | How it is shown |
|---|---|---|
| 3.1 | The seven stages run and are recorded correctly | Firestore run record, System page, workflow log |
| 3.2 | Schema-4 publication is atomic | snapshot schema 4; pointer moved by compare-and-swap after checks 1–12 pass |
| 3.3 | Analysis and explanation manifests agree | snapshot check 10 / 11 results; the explanations' `analysis_set_hash` equals the analysis manifest's |
| 3.4 | The API reads the published snapshot | `/system/status` and `/analysis` name the live meta_version |
| 3.5 | `/chart` reads the same snapshot | every component of a `/chart` response names that meta_version |
| 3.6 | No schema-2 production path remains active | the deployed API and the refresh's code are the m8 code; no schema-2 publish after cut-over (see D2) |
| 3.7 | Reuse works | see D4: a same-data rerun gives UNCHANGED with every analysis and explanation reused |
| 3.8 | The old snapshot stays safe on failure | see D3 |
| 3.9 | The live product shows it | chartlenslab.web.app: search, security page, weekly chart with layers and "What the chart shows" on real data (this also completes the outstanding M6 real-data checks) |

## Gate 4: Documentation (blocking)

- M4 §61 report: exists.
- M5, M7 and M8 §61 reports: written and finalised.
- Project status shows the final M8 state.
- ADR index consistent: statuses, and any D1 clarifications.
- README: the status line (still "Milestone 7") and the M8 row (still "In progress:
  indicators, swings, structure") updated.
- PR #16: title and description (still "phase 1: indicators") match the final scope and
  this gate's checklist.

## Gate 5: Non-blocking register

Each item is recorded as not preventing the committed M8 behaviour.

| Item | Status | M8 effect |
|---|---|---|
| Investigation 0001 | closed (accepted) | none |
| 0001-A price-domain semantics | open, read-only | non-blocking |
| 0001-B triangle geometry | open, read-only | non-blocking |
| 0001-C drawable line extent | open, read-only | non-blocking |
| ADR-0027 §10.1 spans across missing weeks | open | non-blocking |
| Channels | D1 | blocking only under Option A |
| Old CORS origins (`chartlens-lake-13934.web.app`, `.firebaseapp.com`) | to remove after the chartlenslab checklist | non-blocking (proposed) |
| Probe branches `probe/pattern-stats`, `probe-results/pattern-stats` | still exist; this session cannot delete them | non-blocking cleanup |
| Dependabot Actions PRs | not reviewed | non-blocking |
| Scheduler lateness (about 7 h once) | watching | non-blocking |
| Pre-open overrides (NSE circulars) | none filed | non-blocking (M3 data item) |

## Decisions needed

**D1. Channels.** Option A or B (Gate 2).

**D2. Merge versus production validation: a sequencing conflict.** Today's rule is "PR #16
is not merged until M8 is complete". But the production path runs from `main`:

- the API deploys on push to `main` (deploy-api.yml);
- the frontend deploys on push to `main` (deploy-web.yml);
- the API dispatches refreshes on `ref = main` (`github_ref` default);
- GitHub runs the 20:15 IST schedule from the default branch's workflow.

So a "normal live refresh" of the schema-4 path cannot happen until the m8 code is on
`main`. Running it from the branch instead (manual deploys and dispatches on `m8`) would
not be a normal refresh. Worse, the scheduled run would still be main's schema-2 code,
publishing over the schema-4 snapshot unless the schedule were suspended.

Proposed: separate **merge** from **completion**.
1. Pre-merge review: Gate 1, plus Gate 2 under Option A, plus a deployment readiness
   check (D5).
2. Merge PR #16 and deploy. The API and frontend deploy from `main`; the next refresh
   publishes schema 4.
3. Gate 3 evidence from the first live schema-4 refreshes.
4. Gate 4 reports.
5. M8 declared complete.

Under this reading, merging PR #16 is the start of Gate 3, not the end of M8.

**D3. Failure safety in production (3.8).** Failure safety is already proven by tests
(a crash before the pointer moves leaves the old snapshot live) and by the 6b/6c
rehearsals against the real lake. Proposed: Gate 3 checks it passively. After cut-over,
the previous (schema-2) snapshot is retained and readable, and the first schema-4 publish
replaced the pointer only by compare-and-swap. No deliberate production failure is
injected; the code has no fault switch, and adding one would be new code. If you want a
live failure demonstration, that is a separate, explicit decision.

**D4. Reuse in production (3.7).** A normal weekday refresh changes the forming week of
nearly every traded security, so its bars change and little is reused. That is correct
behaviour, not a reuse failure. Proposed: reuse is evidenced by a manual refresh on the
same data straight after a scheduled one. Expected: snapshot UNCHANGED, all analyses and
explanations reused. Also by the number reused on the first normal day (securities with
no new trade).

**D5. Deployment readiness check (before merge).** Proposed contents:
- The m8 API reads both schema 2 and schema 4. The reader accepts schemas below 4, with
  analysis and explanations absent, so the API can deploy before the first schema-4
  publish.
- The API deploy completes before the next scheduled refresh. Merge in the IST morning,
  or the cut-over refresh is a manual one after the deploy.
- Production ANALYSIS time and lake growth. Rehearsal figures: about 5 min first run, 2 to
  4 min explanations and publish; about 0.9 GB documents and events, plus explanations,
  per full regeneration.
- No secret or IAM change: the jobs service account already writes the lake; the API
  service account reads it.
- A rollback path: re-point to the retained schema-2 snapshot and redeploy the previous
  revision. Recorded, not rehearsed, unless you want it rehearsed.

## After approval

- **Option B and the D2 sequence:** clarify ADR-0024 §8 and ADR-0021 §D; run the D5
  readiness check and report it; then stop for your merge decision.
- **Option A:** a channels ADR (design only) comes first, against this boundary.
