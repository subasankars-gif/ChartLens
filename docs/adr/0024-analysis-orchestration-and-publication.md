# ADR-0024: Analysis orchestration and publication

**Status:** Accepted · 2026-10-07. Decisions 1–4 and amendments A–G by Suba. Phase 6a
**closed** 2026-10-07: refinements R1–R5 and the storage decision (§4.1) approved.
**Phase 6 complete** 2026-10-08 (Suba): 6a–6e closed (6b ADR-0025, 6c ADR-0026, 6d
ADR-0027, 6e ADR-0028). Investigation 0001 is closed; its follow-ups 0001-A/B/C are
independent read-only investigations, not Phase 6 defects, and do not gate M8.
Amends ADR-0001, ADR-0018, ADR-0019 and ADR-0023 where stated. The ANALYSIS stage's
design is ADR-0025.

The analytical layers are closed: indicators through breakout events (ADR-0020 to
ADR-0022). This ADR is the contract between them and serving: how one security's
analysis is composed, computed in the tracked run, addressed, published atomically and
served.

## Decisions

| # | Question | Decision |
|---|---|---|
| 1 | Who owns ANALYSIS | A new job layer, `chartlens_jobs`. ANALYSIS is **not** in the pipeline; it is a tracked job stage |
| 2 | Universe | The analytical universe produced by the weekly-build manifest for that run, after the locked analytical-universe and data-quality rules (currently 3,191; never hard-coded) |
| 3 | Timestamps or snapshot version in the result | No. The result is deterministic analytical content; the serving snapshot is the publication and provenance envelope |
| 4 | Event retrieval | Explicit predicates (source type, date range, security, event type) and paging. No ranking, scoring, "best events", fit ordering or relevance-based hidden selection |
| — | Orchestrator recalculates layers | No (amendment A) |
| — | Canonical serialization | Required (amendment C) |
| — | Whole-universe failure | Fails publication (amendment F) |
| — | Pointer movement | Last, only after complete validation (amendment E) |
| — | Event datasets | Separate immutable Parquet datasets (amendment D) |
| — | Recompute check | Kept, on a deterministic sample (amendment G) |

## The invariants

> **The orchestrator composes authoritative outputs; it does not reinterpret them.**

> **The orchestrator may assemble references to layer outputs but may not transform,
> score, reinterpret, filter, or recalculate those outputs.** (Amendment A.)

The orchestrator is an assembly boundary, not another analytical layer. It runs the
layers in their fixed order, passes each the typed results it consumes, and assembles
them. Every section of the result is, unchanged, the output of the layer that owns it.

```text
weekly build (pipeline) ──► ANALYSIS stage (chartlens_jobs)
                               │  per security: analyze_security() ─ pure, engine
                               ▼
                    document + 2 event datasets (content-addressed)
                               │  analysis manifest (complete or nothing)
                               ▼
                    PUBLISH_SERVING: validate ─► write ─► validate ─► move pointer last
                               ▼
                    API reads only what the pointer names ─► chart / scanner
```

## 1. Dependency graph and execution graph

**Packages (amends ADR-0001):**

```text
core ◄── engine          core ◄── pipeline
            ▲                        ▲
            └──────── jobs ──────────┘
                        ▲
                     backend (serving/API)
```

- `engine` is pure analytical computation; `pipeline` is ingestion, build and
  persistence mechanics; `jobs` orchestrates across both; `backend` serves.
- `chartlens_jobs` may import `engine` and `pipeline`. **Neither `engine` nor
  `pipeline` may import `chartlens_jobs`**, and `pipeline` still never imports `engine`.
  The layer-boundary test gains these rules when the package is created (6b).
- The pipeline exposes the mechanics to read weekly inputs and write outputs; the
  ANALYSIS stage composes them with the engine, so it belongs to the job layer.

**One authoritative orchestrator:** `chartlens_engine.analysis.analyze_security(bars,
context, config, inputs) -> TechnicalAnalysis`. It has no I/O, no clock and no
environment (ADR-0019 rule 4). It receives everything explicitly (the bars of one
segment, the context, the settings and the input provenance); it discovers nothing.

| Step | Section | Consumes (passed explicitly) |
|---|---|---|
| A | `indicators` | bars |
| B | `swings` | A |
| C | `structure` | A, B |
| E | `fibonacci` | A, B |
| D | `levels` | A, B, C, E |
| F | `evidence.divergence` | A, B, C |
| F | `evidence.volume` | A, B, C, D |
| F | `evidence.volatility` | A |
| F | `evidence.candles` | A, C |
| G–H | `patterns` (candidates, lifecycle, context, definition fit) | A, B, C, D, E, divergence, volatility |
| — | `relevance` | A, C, patterns |
| — | `breakout_events` | A, D, patterns |

**Rules:**

- **Only the orchestrator instantiates an `Analyzer`** in production code. No layer
  constructs or calls another layer (static test over `engine`, `pipeline` and
  `backend`; tests and scripts may compose layers).
- **Every step goes through `run_analyzer`,** which stays the single enforcement point
  for the bar contract, `as_of` and comparability (ADR-0015).
- **The order is fixed and the output deterministic:** the same bars, context, settings
  and inputs give byte-identical canonical output (§4), in any process, under any hash
  seed and in any processing order (tested).
- **The assembly contains no analytics.** A static test holds the orchestrator, the
  result model and the serializer to: no run-time numeric import, no arithmetic, no
  ordering comparison, no filtering comprehension, and no call that sorts, selects,
  aggregates or rewrites (`sorted`, `min`, `max`, `sum`, `round`, `abs`, `filter`,
  `model_copy`). Channel geometry, for example, can only come from the levels layer (§8).
- **The composition is tested to equal the layers:** each section equals the result of
  running its layer alone, and `provenance` is checked against every analyzer's
  constructor.
- The test-only chain (`engine/tests/analysis_chain.py`) remains for unit tests that
  place swings by hand or keep rejected-candidate diagnostics; a test asserts that,
  without those, it is exactly the orchestrator's composition.

## 2. The complete security result (`TechnicalAnalysis`)

One document per security, for its **current valid segment** (K2), as of the last bar
given:

| Block | Contents | Source |
|---|---|---|
| `identity` | `security_id`, exchange, timeframe, `continuity_segment_id`, first bar date, `as_of` (the last bar), `state_date` (the last complete bar), bar count, whether a forming week was present | the context and the indicators layer |
| `inputs` | the per-security weekly file's SHA-256, the weekly schema and builder versions, `usable_from` | the job layer, recorded verbatim |
| `versions` | document schema, canonical serialization and event schema versions; `analysis_version`; `analysis_methodology_hash`; the data `methodology_hash`; the engine version; every analyzer's and component's name and version | the code and the settings |
| `indicators`, `swings`, `structure`, `fibonacci`, `levels`, `evidence` (divergence, volume, volatility, candles), `patterns`, `relevance` | the layer results, unchanged | the layers |
| `breakout_events` | the layer's provenance and, per dataset, its schema version, row count and content hash. The events are stored in the event datasets (§6) | the breakout-event layer |
| `current` | references only (below) | the layers' own notions of current |
| `provenance` | per section: the analyzer, its version and the sections it consumed | the orchestrator's composition |

**`current` (amendment B).** `current` is a serving-oriented projection of existing
analytical objects, not an analytical layer. It holds only references, each taken from
the owning layer's own notion of current, and each resolving to an object in the same
document (tested):

- `trend_since`: the `structure.trend` entry of `trend_history`;
- `zone_ids`: `levels.zones` (the levels layer keeps current zones only);
- `active_trendline_ids`: `levels.active_trendlines`;
- `fibonacci_ids`: `fibonacci.current()`, the latest structure per sensitivity;
- `included_pattern_ids`: `relevance.included_as_of(state_date)`.

No new derived state enters it: no value, rank, score or signal (no `current_score`, no
`current_signal`). A test fixes its fields to dates and id lists.

**No execution or publication facts (decision 3).** The result never contains an
execution timestamp, job or run id, publishing timestamp, serving snapshot id
(`meta_version`), processing order or runner identity (tested over every key). It does
contain the analytical provenance needed to interpret it: `analysis_version`, engine
and analyzer versions, the data methodology hash, the analysis methodology hash, the
weekly input hash and versions, and the security and segment identity.

> **Analysis result** = deterministic analytical content.
> **Serving snapshot** = publication and provenance envelope.

Two independent runs over identical inputs therefore produce the same result address.
**Amends ADR-0019:** the `meta_version` a result was published in, `weekly_version`
and the publication time are recorded in the snapshot (manifest and Firestore record)
and added to API responses; they are not in the result.

## 3. The ANALYSIS stage (job layer)

**Amends ADR-0019:** the production run is orchestrated by the job layer; ANALYSIS is a
tracked job stage that consumes the weekly-build output and invokes the pure engine
orchestrator. `chartlens_jobs` owns the tracked production run driver (the stage
sequence and the Firestore run record) and calls the pipeline's stages unchanged.

**Universe (decision 2).** The analytical universe produced by the weekly-build manifest
for that run, after applying the locked analytical-universe and data-quality rules. It
is never hard-coded; it legitimately changes with listings, delistings, identity
mappings, data-quality rules, `usable_from` and segments. The job layer derives it once,
from the published metadata of that weekly version, and gives the orchestrator one
security at a time. Securities outside it have no analysis, and the API says so.

**The stage, per run:**

- **Input:** the weekly manifest of this run (`weekly_version`, `as_of`, each security's
  file SHA-256), the published security metadata, and the `[analysis]` settings.
- **Per security:** read its weekly file and verify its hash; select the current
  segment's bars; call `analyze_security`; serialize (§4); write the document and the
  two event files as create-only blobs (§5, §6).
- **Reuse:** the key is (`security_id`, `continuity_segment_id`, the weekly file's
  SHA-256, `analysis_version`, the document schema, canonical serialization and event
  schema versions). A key already in the analysis index reuses its stored hashes without
  recomputing.
- **Recompute check (amendment G).** Every run recomputes a sample of reused securities
  and requires the stored hashes exactly, or the stage fails. The sample is
  deterministic, never a runtime random draw: the securities with the smallest
  SHA-256 of (`weekly_version`, `security_id`), so it is reproducible for a run and
  rotates across runs. Its size is a setting.
- **Output: the analysis manifest,** written last:
  - `weekly_version`, `analysis_version`, `analysis_methodology_hash`;
  - **the exact universe membership** and its hash (the SHA-256 of the canonical,
    sorted list of `security_id`s: a set, so sorting is its canonical form);
  - one entry per security: the result key, the document hash and the two event-file
    hashes;
  - `analysis_set_hash` (the SHA-256 of the entries, canonical, by `security_id`).
- **Failure (amendment F).** Any security failing to read, analyse, serialize or write
  fails the stage. No manifest is written, PUBLISH_SERVING does not run, and the live
  snapshot (bars and analysis) is untouched. There is no partial publication: never
  "3,190 of 3,191". Partial computation may exist only in a failed job's private
  workspace, never as a serving snapshot. A failure is an engine or data bug to fix,
  not something to publish around.
- **Rerun.** The same weekly version and settings give the same document hashes, the
  same `analysis_set_hash` and the same `meta_version`; publication then records
  `snapshot_outcome = UNCHANGED`, as today.
- **Parallelism:** a process pool over securities. Results are keyed by `security_id`,
  so processing order never shows.

## 4. Content addressing (amendment C)

> **Content addresses are computed from a canonical, versioned serialization with
> deterministic field ordering, numeric representation, null handling and collection
> ordering.**

`canonical_serialization_version` 1:

- UTF-8 JSON, no insignificant whitespace;
- **mappings** (order-irrelevant by definition): keys are strings, sorted by code point;
- **sequences keep their order.** Semantically ordered analytical objects are never
  sorted merely to make hashing deterministic: the layer that produced a list owns its
  order. Only order-irrelevant collections are canonicalized (mappings; the universe
  membership set);
- **numbers:** integers in decimal; a float as its shortest round-trip representation,
  so the stored value is exact (R1); negative zero keeps its sign (R5); NaN and
  infinities are refused (a missing value is null);
- `null`, `true`, `false`; strings with JSON escapes, non-ASCII kept as UTF-8;
- anything else (a raw date, a set, a NumPy scalar) is refused rather than converted.

The encoder is pinned; a test compares it with an independent reference encoder written
from these rules, on real documents.

**What the document address covers:** the inputs (the weekly file SHA-256, not
`weekly_version`, which changes every day even when a security's bars do not; the
segment; `as_of`), the methodology (`analysis_version`, which includes
`analysis_methodology_hash` and every analyzer and component version; the data
`methodology_hash`), every analytical value, and every event dataset's content hash
(R2). **What it never covers:** timestamps, run ids, `meta_version`, hosts or library
build strings, processing order.

The same key gives the same bytes and the same hash. Writes are create-only, so an
existing blob is left as it is.

### 4.1 Storage form (approved at the 6a checkpoint)

> **One security's analysis is one immutable, self-contained analytical object.**

- The complete document is stored as **one gzip-compressed artifact**, addressed by the
  **SHA-256 of the uncompressed canonical bytes**, never of the compressed bytes.
- **Compression is non-semantic.** Gzip metadata never influences identity, and the
  stored bytes are deterministic (no timestamp, no file name: `mtime=0`). A different
  compression implementation may change the physical bytes; it can never change the
  address. A regression test compresses canonical bytes through the production write
  path and checks the address is the uncompressed hash, and a reader always verifies
  the decompressed bytes against the address.
- **The unit is the per-security object, not the snapshot.** The snapshot manifest maps
  `security_id → analysis content hash` and stays small. The full set's size (5.55 GB
  uncompressed, about 1 GB compressed, on 3,193 securities) is a measurement, not a
  serving constraint.
- **Section splitting is not done now.** It would add addresses, manifest entries,
  validation, cross-section consistency rules, retrieval logic, partial-snapshot risk
  and garbage-collection complexity. If serving measurements ever demand it, the
  manifest can map a security to section manifests instead, and the API (with
  `layers=`) can make that transition invisible without changing the analytical
  contract.

## 5. Publication (serving schema 3), atomic (amendment E)

> **The live pointer is never changed unless every validation step succeeds.**

```text
Build ─► Validate ─► Write immutable artifacts ─► Validate written artifacts
      ─► Write complete manifest ─► Validate manifest ─► MOVE POINTER
```

1. **Validate the inputs.** The weekly, data-quality and adjustment versions agree (as
   today). The analysis manifest exists, names this run's `weekly_version`, matches the
   settings' `analysis_version`, and covers exactly the analytical universe. Every
   entry's input hash equals that security's file hash in the weekly manifest, so no
   analysis of stale bars is ever published.
2. **Write the immutable artifacts** (the weekly copies, as today).
3. **Validate the written artifacts.** Every analysis document and event file exists and
   its bytes hash to its name; every event file's rows reproduce the content hash its
   document records. A missing or corrupt blob stops publication.
4. **Write the complete manifest** under `v={meta_version}/`: the metadata, plus
   `analysis_files` (per security, the document and the two event files) and the
   `analysis_version`, `analysis_methodology_hash`, universe hash and
   `analysis_set_hash`. `meta_version` hashes all of it, so a changed analysis is a new
   snapshot.
5. **Validate the manifest** (read back, hash, completeness), and stage the Firestore
   record.
6. **Move the pointer**, last. Then mark the record PUBLISHED.

If anything fails before the pointer moves, **the old snapshot remains live**. No cleanup
of the old snapshot is part of the transaction; removing blobs no live or previous
snapshot names stays a separate, best-effort step after publication (as today).

**Never a mixture:** bars and analysis are named by the same manifest, and the API
swaps whole snapshots (ADR-0016). An analysis blob is served only if its hash matches
the live manifest; otherwise the API reloads the pointer once, then answers 503, as for
weekly bars.

## 6. The event datasets (amendment D)

| Item | Decision (with ADR-0022 §19.4) |
|---|---|
| Datasets | `pattern_breakouts` and `level_breakouts`: separate schemas, files and validation; never merged |
| Storage | One Parquet file per security per dataset, immutable: `curated/serving/exchange={EX}/events/{dataset}/{event_content_sha256}.parquet`. *Amended by ADR-0025:* named by the content hash of its identifying metadata and rows (logical identity); the manifest also records the SHA-256 of its bytes (physical integrity) |
| Security partitioning | Logical, through the manifest (`security_id` → file hash). A path partitioned by `security_id=` would mean rewriting files in place, which schema 2 forbids |
| Rows | One per event, as the layer produced it, in the layer's order: by `bar_date`, then `event_key` (R3) |
| Content hash | The SHA-256 of the canonical bytes of the identifying metadata and the rows (§4, ADR-0025 §5). The document records it (R2); publication recomputes it from the file |
| File metadata | dataset, event schema version, event methodology version (`breakouts-N`), source methodology version (`patterns-N` / `levels-N`), `analysis_version`, `security_id`, `continuity_segment_id`, the input weekly file hash, the content hash |
| Writer | pinned options: explicit schema and column order, zstd, one row group. The bytes are physical only (ADR-0025), so a writer version may change them |

So an event-data change necessarily changes the containing document's address, while a
serving republish does not; and a different Parquet writer changes neither file names
nor document addresses, only the recorded byte hashes.

## 7. API contract (amends ADR-0023)

- **The published snapshot only.** The API reads the pointer and the blobs it names,
  never lists GCS, never computes, and refuses `?as_of` with 400 (K3).
- **`GET /api/v1/securities/{id}/analysis?layers=…`:** the document, with
  `meta_version`, `weekly_version` and the publication time added from the snapshot.
  - The layers are those of ADR-0023 plus `relevance` and `current`.
  - Sections are labelled current (as of `state_date`) or history (objects with
    `known_at` and status histories), so the two are never confused.
  - Numbers are formatted for display here, per K7 (R1).
- **`GET /api/v1/securities/{id}/breakout-events?source=pattern|level&from=&to=&type=&page=`
  (decision 4):** rows of the published event files that match explicit predicates
  (source type, date range, security, event type), in the stored order, paged.
  Filtering by explicit predicates is retrieval, not selection in the analytical sense.
  No ranking, scoring, "best events", fit ordering or relevance-based hidden selection.
  Level events can number over 1,300 for one security, so paging is required.
- **Definition fit (ADR-0022 §18.1):** no endpoint sorts or ranks by `definition_fit`
  across families; no `relevance_score` or `fit_rank` field exists; patterns come in
  the engine's order; the wording is "definition fit", never "confidence" (ADR-0023's
  "Confidence (definition fit)" label is replaced).
- **Provenance on every response:** `as_of`, `meta_version`, `analysis_version`,
  `analysis_methodology_hash`, the data `methodology_hash`, `weekly_version`, the
  engine and analyzer versions.

## 8. Channels

They remain a levels-layer object (ADR-0022 §7.9, ADR-0021), built in their own levels
phase with their own review. Orchestration and publication only carry what the layers
return: no geometry is fitted, extended or invented outside the layer, and the
assembly's no-analytics test (§1) enforces it. When channels exist, they appear inside
the `levels` section and change `analysis_version` through the levels analyzer's
version.

**Clarified 2026-10-08 (M8 completion gate, D1):** channels are **post-M8**. They are
the first post-M8 analytical phase, in the levels layer, with their own design review
before implementation. "Next phase" here never meant an M8 requirement: an accepted
design does not constitute a delivery commitment unless the milestone scope or an
accepted phase gate explicitly requires its implementation
(docs/milestones/m8-completion-gate.md).

## 9. The explanation layer (`chartlens_engine.explain`)

- **A pure function of one `TechnicalAnalysis`.** It returns a separate `explanations`
  section and never modifies the analysis (tested: the analysis is identical with and
  without it).
- **Structured claims only:** `claim_type`, a deterministic template text, the values
  it quotes and `evidence_refs`. Every reference must resolve to an object in the same
  document, and every number it quotes must equal that object's value (tested).
- **No LLM in this phase.** If a language model is ever used, it may only rephrase
  existing claims. A validator rejects any output that introduces a number, name,
  object or judgement not present in the claims. An LLM never produces an analytical
  fact (project rule).
- **Wording rules:** no "probability", "target", "buy" or "sell"; "measured-move
  zone"; "definition fit". Each is a test.

## Delivery, in reviewable phases

| Phase | Scope | Checkpoint evidence |
|---|---|---|
| 6a | `analysis` orchestrator, `TechnicalAnalysis`, canonical serialization | byte determinism (processes, hash seeds, order); composition equals the layers; lossless round trip; the static tests; the real-NSE fingerprint unchanged; real-NSE size and timing |
| 6b | `chartlens_jobs` and the ANALYSIS stage (documents, event files, manifest, universe, reuse, deterministic recompute sample, failure) | a full run against a test lake; a forced failure leaves the live snapshot untouched; rerun gives UNCHANGED; real-NSE timing |
| 6c | Publisher schema 3 and the API endpoints | a crash before the pointer moves leaves the old snapshot live; the API serves only pointer-named blobs; `?as_of` 400; no cross-family sort |
| 6d | Chart layers (ADR-0023 visual rules) | screenshots on real data |
| 6e | `explain` | every claim resolves; wording tests |

Channels follow as their own levels-layer phase.

**Status 2026-10-08:** all five phases closed; Phase 6 is complete for the scope above.
Carried forward, not gating: ADR-0027 §10.1 (spans across missing weekly observations)
and Investigations 0001-A/B/C (docs/investigations).

## Phase 6a refinements (approved at the 6a checkpoint)

- **R1. Stored numbers are lossless; K7 is display.** The document stores each float
  exactly (shortest round-trip text). ADR-0023 K7 (a bar's exact decimal text, 4-decimal
  derived values) is applied by the API when it formats a response, not in the stored
  bytes. Rounding inside the hashed form would lose information and make the address
  depend on a display rule.
- **R2. The document pins events by content, the manifest by bytes.** The document
  records each event dataset's canonical content hash (computed in the engine, no
  Parquet library), and the job layer's manifest records each file's byte hash. The
  document's address therefore does not depend on Parquet writer versions, and
  publication validates both.
- **R3. The breakout layer orders both datasets.** Pattern breakout events are now
  ordered by the layer by (`bar_date`, `event_key`), as level events already were, so
  the stored order is the layer's own and the serializer never sorts. No event's
  content changes.
- **R4. Production keeps no rejection diagnostics.** The orchestrator runs the pattern
  analyzer without `diagnostics`, so `patterns.rejections` is empty in stored documents;
  the per-family candidate counts remain.
- **R5. Negative zero keeps its sign.** It is a distinct value, the representation is
  lossless, and normalizing it would need a second pass over every document. Normalizing
  it later would be an explicit canonical-serialization version change, never a silent
  one.

Suba's notes on approval: R1 keeps a UI formatting rule from ever changing analytical
identity. R2 gives two independent guarantees: the event content hash is the logical
identity the document depends on; the Parquet byte hash in the manifest is physical
integrity. R4: candidate diagnostics belong to development and statistics outputs.
