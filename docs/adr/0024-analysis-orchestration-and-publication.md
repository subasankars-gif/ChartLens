# ADR-0024: Analysis orchestration and publication

**Status:** Proposed · 2026-10-07. For review before any code. It amends ADR-0018,
ADR-0019 and ADR-0023 where stated.

The analytical layers are closed: indicators through breakout events (ADR-0020 to
ADR-0022). This ADR is the contract between them and serving: how one security's
analysis is composed, computed in the tracked run, addressed, published atomically and
served.

## The invariant

> **The orchestrator composes authoritative outputs; it does not reinterpret them.**

```text
weekly bars (WEEKLY stage) ──► ANALYSIS stage (job layer)
                                  │  per security: analyze_security() ─ pure, engine
                                  ▼
                       TechnicalAnalysis document + 2 event files (content-addressed)
                                  │  analysis manifest (complete or nothing)
                                  ▼
                       PUBLISH_SERVING: validate ─► stage ─► move pointer last
                                  ▼
                       API reads only what the pointer names ─► chart / scanner
```

The orchestrator is not another analytical layer. It runs the layers in their fixed
order, passes each the typed results it consumes, and assembles them. It does not
filter, rank, select, recompute or derive any analytical value. Every section of the
result is, byte for byte, the output of the layer that owns it.

## 1. Execution graph (engine, pure)

**One authoritative orchestrator:** `chartlens_engine.analysis.analyze_security(bars,
context, config) -> TechnicalAnalysis`. It has no I/O, no clock and no environment
(ADR-0019 rule 4).

| Step | Layer | Consumes (passed explicitly) |
|---|---|---|
| A | indicators | bars |
| B | swings | bars, A |
| C | structure | A, B |
| E | fibonacci | A, B |
| D | levels | A, B, C, E |
| F | divergence · volume · volatility · candles | A, B, C, D |
| G–H | patterns (candidates, lifecycle, context, definition fit) | A, B, C, D, E, F |
| — | pattern relevance | A, C, G–H |
| — | breakout events | A, D, G–H |

**Rules:**

- **Only the orchestrator instantiates an `Analyzer`.** No layer constructs or calls
  another layer. A static test asserts that only `chartlens_engine.analysis`
  instantiates `*Analyzer` classes.
- **Every step goes through `run_analyzer`,** which stays the single enforcement point
  for the bar contract, `as_of` and comparability (ADR-0015).
- **The order is fixed, and the output is deterministic:** the same bars and config
  give byte-identical canonical output (§4), whatever the order in which securities
  are processed.
- **The orchestrator contains no analytics.** A static test asserts that it imports no
  numeric library and defines no analytical computation. Channel geometry, for
  example, can only ever come from the levels layer (§8).
- `tests/analysis_chain.py` (today's test-only chain) is replaced by the orchestrator,
  so tests and production run the same composition.

## 2. The complete security result (`TechnicalAnalysis`)

One document per security, for its **current valid segment** (K2), as of the weekly
data's `as_of`:

| Block | Contents | Source |
|---|---|---|
| `identity` | `security_id`, exchange, `continuity_segment_id`, segment start, `as_of` (the last complete bar), whether a forming week was present | the input frame |
| `input` | the per-security weekly file's SHA-256, bar count, `usable_from`, partial and special-session bar counts | passed in by the job layer (data quality stays in the published metadata, referenced by `security_id`) |
| `versions` | `document_schema_version`, `analysis_version`, `analysis_methodology_hash`, the data `methodology_hash`, the engine version and every analyzer's name and version | config and analyzers |
| `indicators`, `swings`, `structure`, `fibonacci`, `levels` (levels, zones, trendlines), `evidence` (divergences, volume, volatility, candles), `patterns` (with context and `definition_fit`), `relevance` | the layer results, unchanged | the layers |
| `events` | for each dataset (`pattern_breakouts`, `level_breakouts`): row count, file SHA-256 and schema version. The events themselves live in the event files (§6) | the job layer |
| `current` | references only, as of `as_of`: the trend state, the active zones, Fibonacci structures and trendlines, the included relevance entries. No values, ranks or scores are created here | the layers' own "current" outputs |

- **No timestamps, run ids or `meta_version`.** A document is created before any
  snapshot exists, and including any of these would break content addressing (§4).
- **Amends ADR-0019:** the `meta_version` a result was published in, and the
  publication time, are recorded in the snapshot (manifest and Firestore record) and
  added to API responses. They are not in the document.

## 3. The ANALYSIS stage (job layer)

**Where it runs.** ADR-0001 forbids the pipeline from importing the engine and puts
"orchestration that needs both" in a job layer. That layer was never created; the M7
run driver lives in `chartlens_pipeline`. Proposed: a new workspace package,
`chartlens_jobs`, which imports `core`, `engine` and `pipeline`.

- It owns the tracked production run driver: the stage sequence and the Firestore run
  record.
- It calls the pipeline's stages (unchanged) and the new ANALYSIS stage.
- `pipeline` still never imports `engine`, and the layer-boundary test gains the jobs
  rule.

**The stage, per run:**

- **Input:**
  - the weekly manifest produced by this run's WEEKLY stage (`weekly_version`, `as_of`,
    each security's file SHA-256);
  - the security master;
  - the `[analysis]` config.
- **Universe:** the analytical universe of that weekly version only (equity shares,
  §61 decision 2). Other securities have no analysis, and the API says so.
- **Per security:**
  - read its current-segment weekly file (and verify its hash);
  - call `analyze_security`;
  - serialise canonically (§4);
  - write the document and the two event files as create-only, content-addressed blobs
    (§5).
- **Reuse:**
  - the result key is (`security_id`, `continuity_segment_id`, the weekly file's
    SHA-256, `analysis_version`);
  - if the analysis index already holds that key, the stored hashes are reused without
    recomputing;
  - a deterministic sample is recomputed on every run, and must reproduce the stored
    hashes exactly, or the stage fails.
- **Output:** an **analysis manifest**, written last:
  - `weekly_version`, `analysis_version`, `analysis_methodology_hash`;
  - one entry per security: the result key, the document hash and the two event-file
    hashes;
  - `analysis_set_hash` (the SHA-256 of the sorted entries).
- **Failure.** Any security failing to read, analyse, serialise or write fails the
  stage. No manifest is written, PUBLISH_SERVING does not run, and the live snapshot
  (bars and analysis) is untouched (the ADR-0018 amendment).
  - **There is no partial result.** The manifest exists only if it covers exactly the
    analytical universe of this weekly version.
  - A failure is an engine or data bug to fix, not something to publish around.
- **Rerun.** The same weekly version and config give the same document hashes, the
  same `analysis_set_hash` and the same `meta_version`. Publication then records
  `snapshot_outcome = UNCHANGED`, as today.
- **Parallelism:** a process pool over securities. The manifest is sorted by
  `security_id`, so processing order never shows.

## 4. Content addressing

**The document hash** is the SHA-256 of the canonical bytes:

- UTF-8 JSON with sorted keys and no insignificant whitespace;
- the numbers per K7: a bar price is its exact decimal text, a derived value is
  rounded to 4 decimals, and null stays null;
- dates in ISO format;
- lists in the layer's own deterministic order. The serialiser never sorts analytical
  lists; it relies on the layers' order, and a test checks the order is stable.

**What the hash covers, and therefore what changes the address:**

- the inputs: the per-security weekly file SHA-256 (not `weekly_version`, which changes
  every day even when a security's bars do not), the segment and `as_of`;
- the methodology: `analysis_version` (which already includes
  `analysis_methodology_hash` and every analyzer version), plus the data
  `methodology_hash`;
- every analytical value and every event-file hash.

**What it never covers:**

- timestamps, run ids, `meta_version`, host or library build strings;
- processing order.

**Unchanged analysis gives the same address.** The same key gives the same bytes and
the same hash. Writes are create-only, so an existing blob is left as it is.

**Event files** (Parquet) are addressed by the SHA-256 of their bytes, written with
pinned writer options:

- the schema and column order are fixed;
- rows are sorted by `(bar_date, event_key)`;
- one row group;
- no creation timestamps in the metadata;
- a fixed `created_by`.

A test writes the same events twice and requires identical bytes. A future pyarrow that
writes different bytes only misses reuse once; it never produces a wrong address.

## 5. Publication (serving schema 3), atomic

The ADR-0018 schema-2 order is extended. Nothing is visible until the pointer moves:

1. **Validate the inputs.**
   - The weekly, data-quality and adjustment versions agree (as today).
   - The analysis manifest exists, its `weekly_version` is this run's, and its
     `analysis_version` matches the config.
   - It covers exactly the analytical universe.
   - Every entry's input hash equals that security's file hash in the weekly manifest,
     so no analysis of stale bars is ever published.
2. **Copy the weekly files** (as today). **Verify every analysis document and event
   file**: it exists and its bytes hash to its name. A missing or corrupt blob stops
   publication.
3. **Write `v={meta_version}/`** with the metadata and the manifest. The manifest now
   includes:
   - `analysis_files`: per security, the document and the two event files;
   - the `analysis_version`, `analysis_methodology_hash` and `analysis_set_hash`.

   `meta_version` hashes all of it, so a changed analysis is a new snapshot.
4. **Stage** the Firestore record.
5. **Move the pointer**, last.
6. **Mark it PUBLISHED; clean up** blobs that neither the new snapshot nor the previous
   one refers to (best effort, as today).

**Never a mixture:**

- Bars and analysis are named by the same manifest, and the API swaps whole snapshots
  (ADR-0016).
- An analysis blob is served only if its hash matches the live manifest; otherwise
  the API reloads the pointer once, then answers 503, as for weekly bars.
- A failed ANALYSIS or PUBLISH stage leaves the previous snapshot entirely live.

## 6. The event datasets

| Item | Decision (with §19.4 of ADR-0022) |
|---|---|
| Datasets | `pattern_breakouts` and `level_breakouts`: separate schemas, files and validation |
| Storage | One Parquet file per security per dataset, content-addressed and immutable: `curated/serving/exchange={EX}/events/{dataset}/{sha256}.parquet` |
| Security partitioning | Logical, through the manifest (`security_id` → file hash). A path partitioned by `security_id=` would mean rewriting files in place, which schema 2 forbids. The file's metadata repeats `security_id` and the segment |
| Schema | One row per event: `event_id` (deterministic, ADR-0022 §19.4), `event_key`, `security_id`, segment, source id, ref and version, direction, `bar_date`, `known_at`, `level_at_break`, `reference_atr`, `retest_band`, the windows, `provisional`, `history` (a list of structs: kind, date, `known_at`, authority, `source_outcome_ref`, measured values, `provisional`), `source_measured_values` (map), `bar_volume` (struct), `methodology_version` |
| File metadata | `chartlens.dataset`, `chartlens.schema_version`, `analysis_version`, `security_id`, `continuity_segment_id`, the input weekly file hash |
| Relationship | The security's document names both file hashes (§2), so a document address pins its events, and the manifest pins both |

## 7. API contract (amends ADR-0023)

- **The published snapshot only.** The API reads the pointer and the blobs it names,
  never lists GCS, never computes, and refuses `?as_of` with 400 (K3).
- **`GET /api/v1/securities/{id}/analysis?layers=…`:** the document, with
  `meta_version` and the publication time added from the snapshot.
  - The layers are those of ADR-0023 plus `relevance` and `current`.
  - The sections are labelled current (as of `as_of`) or history (objects with
    `known_at` and status histories), so the two are never confused.
- **`GET /api/v1/securities/{id}/breakout-events?source=pattern|level&from=&to=&page=`:**
  rows from the published event files, ordered by `(bar_date, event_key)`.
  - Filtering by source and date range, and paging, are retrieval of a published
    dataset, never selection or ranking (question 4).
  - Level events can number 1,353 for one security, so paging is required.
- **Definition fit (ADR-0022 §18.1):**
  - No endpoint sorts or ranks by `definition_fit` across families.
  - No `relevance_score` or `fit_rank` field exists.
  - Patterns come in the engine's order.
  - Wording: "definition fit", never "confidence". (ADR-0023's "Confidence (definition
    fit)" label is replaced.)
- **Provenance on every response:**
  - `as_of`, `meta_version`, `analysis_version`, `analysis_methodology_hash`;
  - the data `methodology_hash`, `weekly_version`;
  - the engine and analyzer versions.

## 8. Channels

They remain a levels-layer object (ADR-0022 §7.9, ADR-0021), built in their own levels
phase with their own review. Orchestration and publication only carry what the layers
return:

- no geometry is fitted, extended or invented outside the layer;
- the orchestrator's no-analytics test (§1) enforces it.

When channels exist, they appear inside the `levels` section and change
`analysis_version` through the levels analyzer's version.

## 9. The explanation layer (`chartlens_engine.explain`)

- **A pure function of one `TechnicalAnalysis`.** It returns a separate `explanations`
  section and never modifies the analysis (tested: the analysis is identical with and
  without it).
- **Structured claims only:** `claim_type`, a deterministic template text, the values
  it quotes and `evidence_refs`. **Every reference must resolve to an object in the
  same document**, and every number it quotes must equal that object's value (tested).
- **No LLM in this phase.** If a language model is ever used, it may only rephrase
  existing claims. A validator rejects any output that introduces a number, name,
  object or judgement not present in the claims. An LLM never produces an analytical
  fact (project rule).
- **Wording rules:** no "probability", "target", "buy" or "sell"; "measured-move
  zone"; "definition fit". Each is a test.

## Delivery, in reviewable phases

| Phase | Scope | Checkpoint evidence |
|---|---|---|
| 6a | `analysis` orchestrator, `TechnicalAnalysis`, canonical serialisation | byte-determinism; order independence; composition equals the layers; the static tests; the real-NSE fingerprint unchanged |
| 6b | `chartlens_jobs` and the ANALYSIS stage (documents, event files, manifest, reuse, failure) | a full run against a test lake; a forced-failure run leaves the live snapshot untouched; rerun gives UNCHANGED; real-NSE timing |
| 6c | Publisher schema 3 and the API endpoints | an atomicity test with a crash before the pointer moves; the API serves only pointer-named blobs; `?as_of` 400; no cross-family sort |
| 6d | Chart layers (ADR-0023 visual rules) | screenshots on real data |
| 6e | `explain` | every claim resolves; wording tests |

Channels follow as their own levels-layer phase.

## Questions for review

1. **The job layer.** Create `chartlens_jobs`, owning the production run driver,
   because ADR-0001 says that is where engine-plus-pipeline orchestration lives?
2. **The universe.** Analyse the analytical universe only (3,191 equity shares)?
3. **Document identity.** Keep `meta_version`, timestamps and `weekly_version` out of
   the document, and add them from the snapshot in API responses (amending ADR-0019's
   "every result records the meta_version")?
4. **Events API retrieval.** Are source and date-range filters plus paging acceptable as
   retrieval, given the Phase 4 rule that serving never re-selects analysis objects?
