# ADR-0026: Schema-3 publication and the analysis API

**Status:** Proposed · 2026-10-07. For review before any code (phase 6c). It builds on
ADR-0024 (§5 publication, §7 API) and ADR-0025, and supersedes the storage and API
parts of ADR-0023 where stated.

6b produces immutable candidate artifacts and a complete analysis manifest. 6c decides
what becomes live, and how the API serves it.

## The invariants

> **No incomplete analysis set can become live.**

> **The publication schema is a serving envelope around already-authoritative
> artifacts, never a transformation layer.** Publication copies, verifies and points;
> it never re-encodes, reshapes or derives analytical content.

> **The API serves authoritative published results; it does not recompute,
> reinterpret, rank or modify them.**

```text
engine → TechnicalAnalysis → canonical immutable object → analysis manifest
       → schema-3 snapshot (envelope: pins the manifest by hash) → API (selects, never shapes)
```

## Part 1: Publication

### 1.1 What the schema-3 live snapshot is

Everything schema 2 has, unchanged, plus the analysis set:

| Part | Where | Status |
|---|---|---|
| Snapshot metadata files (securities, identifiers, segments, findings) | `v={meta_version}/*.parquet` | as schema 2 |
| Weekly copies | `weekly/{sha256}.parquet` | as schema 2 |
| **The analysis manifest, verbatim** | `v={meta_version}/analysis_manifest.json` | new: a byte-for-byte copy of the ANALYSIS stage's canonical manifest |
| Analysis documents | `analysis/{document_sha256}.json.gz` | written by ANALYSIS, named by the copied manifest |
| Event files | `events/{dataset}/{content_sha256}.parquet` | written by ANALYSIS, named by the copied manifest |
| The snapshot manifest | `v={meta_version}/manifest.json`, then `_manifest.json` (the pointer) | schema 3 |

**Why a verbatim copy** (decision 1). The ANALYSIS stage's manifest at
`curated/analysis/…/_manifest.json` is overwritten by the next run, so the snapshot
needs its own immutable copy. Copying the canonical bytes, rather than re-listing the
entries inside the snapshot manifest, keeps exactly one representation of the analysis
set: the snapshot pins it by hash and adds nothing to it.

### 1.2 What the snapshot manifest adds

```json
"schema_version": 3,
"analysis": {
  "manifest_key": "…/v={meta_version}/analysis_manifest.json",
  "manifest_sha256": "<SHA-256 of the copied bytes>",
  "analysis_version": "analysis-…",
  "analysis_methodology_hash": "…",
  "universe_sha256": "…",
  "analysis_set_hash": "…",
  "securities": 3193
}
```

The summary fields are repeated from the analysis manifest so the API's status route can
answer without reading it; publication checks they equal the manifest's. `meta_version`
hashes them together with everything schema 2 hashes, so a different analysis set is a
different snapshot, and the same one gives the same `meta_version` (rerun → UNCHANGED).

**References, in one place each:**

- weekly inputs: the snapshot's `weekly_files` (`security_id → sha256`), as schema 2;
- universe: the analysis manifest's `universe` and `universe_sha256`;
- analysis objects and event objects: the analysis manifest's entries (document hash;
  per dataset the content hash, physical hash, row count);
- the link between them: each entry's `weekly_file_sha256`, which must equal the
  snapshot's `weekly_files` value for that security.

### 1.3 Independent verification, before anything is written

Publication trusts nothing the ANALYSIS stage claims; it re-derives or re-reads each
fact. Any failure raises `PublicationFailed`, and the pointer never moves.

| # | Check | How |
|---|---|---|
| 1 | Inputs in step | the analysis manifest's `weekly_version`, `dq_version`, `as_of` and data `methodology_hash` equal the weekly manifest's (as schema 2 checks weekly against data quality) |
| 2 | Methodology | `analysis_methodology_hash` equals the settings' (`AnalysisConfig.methodology_hash()`, computable in the pipeline); `analysis_version` equals the expected one, which the job layer supplies (decision 4) |
| 3 | Manifest integrity | the manifest parses; its `analysis_set_hash` recomputes from its entries |
| 4 | Universe set | re-derived from the published data-quality status with `analysis_universe` (pipeline, rule version 1); must equal the manifest's `universe` exactly, and `universe_sha256` must recompute from it |
| 5 | Coverage | entry ids equal the universe, unique, in order |
| 6 | Inputs per security | each entry's `weekly_file_sha256` equals the snapshot's `weekly_files`; its `continuity_segment_id` equals the security's current segment in the snapshot's own securities table |
| 7 | Documents | the object exists; it decompresses to bytes whose SHA-256 is its address; the document's own identity, segment, `bars_sha256`, `analysis_version` and event content hashes equal its entry's |
| 8 | Events, logical | each file decodes; its identifying metadata and rows hash to its address (`dataset_content_hash`); its row count equals the entry's |
| 9 | Events, physical | the SHA-256 of its bytes equals the entry's `physical_sha256` |

**Depth (decision 2).** Checks 7–9 read every object whose address is **not** named by
the live snapshot: today, about 2,577 documents and 5,154 event files a day (~700 MB).
An address the live snapshot already names was fully verified when that snapshot was
published, and is immutable (create-only writes), so it is checked for existence only.
The alternative, verifying all 3,193 every time, costs more reading, and 6c will measure
both on real data.

### 1.4 Missing or corrupted objects

- **At publication:** publication fails before anything is written, naming the
  securities and objects. The old snapshot stays live; the run is FAILED at
  PUBLISH_SERVING.
- **Recovery must not loop.** Today a corrupted object at an address would be reused by
  the next ANALYSIS run (reuse checks existence) and fail publication again. **6b
  amendment (decision 3):** a reuse candidate's artifacts are verified logically
  (decompress or decode, hash) before reuse; a security whose artifact fails is
  computed, and the corrupt object (one whose content does not match its own address,
  so no valid snapshot can be served from it) is deleted and rewritten with the correct
  content. The run record counts repairs. Nothing else is ever deleted by ANALYSIS.

### 1.5 The sequence, and the exact moment the pointer moves

```text
1. Validate inputs              weekly ↔ data quality ↔ adjustment (schema 2), checks 1–6
2. Verify artifacts             checks 7–9 (parallel reads)
3. Copy weekly files            as schema 2, then verify every copy exists
4. Write v={meta_version}/      metadata files, analysis_manifest.json (verbatim), manifest.json
5. Verify what was written      read each back; its SHA-256 equals the manifest's
6. Stage the snapshot record    Firestore: STAGED, with the analysis summary
7. MOVE THE POINTER             write _manifest.json — the only step that changes what is live
8. Mark PUBLISHED               best effort; a failure is corrected by the next publication
9. Clean up (separate)          best effort, after the pointer moved (§1.6)
```

The pointer moves at step 7 and only if steps 1–6 all succeeded. A failure at any step
before 7 leaves the previous snapshot live; no step before 7 removes or changes anything
the previous snapshot names.

- **Schema 3 is mandatory once 6c ships** (decision 5). Publication refuses to publish
  without an analysis manifest for this weekly version: a snapshot with bars but no
  analysis would make analysis vanish from the API. The standalone
  `chartlens-pipeline publish-serving` therefore needs the ANALYSIS stage to have run;
  the tracked run always does.

### 1.6 Clean-up (garbage collection), a separate concern

After the pointer moves, objects named by none of these are removed (best effort, as
schema 2 removes weekly copies): the new live snapshot, the previous snapshot (the API
may still hold it for a minute), and the latest analysis manifest (so tomorrow's reuse
still finds its artifacts). Orphans from failed runs go here. A failed clean-up never
fails a publication.

### 1.7 The snapshot record

The Firestore snapshot record and `/system/status` gain the analysis summary
(`analysis_version`, `analysis_methodology_hash`, `universe_sha256`,
`analysis_set_hash`, securities analysed). Run facts (computed, reused, repaired,
validation sample) stay in the run record.

## Part 2: The API

### 2.1 What the API may expose

| Exposed | How | Not a transformation because |
|---|---|---|
| A security's complete analysis | the published document, verbatim | it is the artifact |
| Selected top-level sections | `sections=` names document sections (`identity`, `inputs`, `versions`, `indicators`, `swings`, `structure`, `fibonacci`, `levels`, `evidence`, `patterns`, `relevance`, `breakout_events`, `current`, `provenance`) | it omits whole sections, never reshapes one |
| The current projection | the `current` section: references only | the engine wrote it; the API does not resolve or expand the references |
| History | already inside the document (every object's `known_at` and status history) | no historical recomputation exists; `?as_of` is refused with 400 (K3) |
| Breakout events | rows of the published event files matching explicit predicates, paged | retrieval, not selection (ADR-0024 decision 4) |
| Provenance | the snapshot envelope on every response | it describes, never alters, the content |

**Never:** ranking, scoring, sorting by `definition_fit` or anything else, cross-family
comparison, fit ranks, relevance scores, merging datasets, resolving references into
new objects, filling gaps, deriving a status, or any computation over analytical
values. Section selection is the only shaping, and it is by whole section.

### 2.2 Routes (names proposed, not locked)

**`GET /api/v1/securities/{id}/analysis?sections=…`**

```json
{
  "envelope": {
    "security_id": "…", "meta_version": "meta-…", "published_at": "…",
    "data_as_of": "…", "weekly_version": "wk-…", "methodology_hash": "…",
    "analysis_version": "analysis-…", "analysis_methodology_hash": "…",
    "document_sha256": "…", "sections": ["…"]
  },
  "document": { "<section>": <the stored section, verbatim>, … }
}
```

- The default is the whole document; `sections` narrows it. `document_sha256` lets a
  client cite exactly the object it was served (with every section, the canonical
  encoding of `document` hashes to it).
- `404 unknown_security`; `404 not_analysed` for a security outside the published
  universe (its detail route already shows `analytical_universe` and data-quality
  status, so the API never re-applies the universe rule to explain it);
  `404 no_analysis_in_snapshot` while a schema-2 snapshot is live; `400` for `?as_of`.
- The API reads the published copy of the analysis manifest once per snapshot (3 MB),
  fetches a document on demand, verifies it (decompress, hash equals address) and keeps
  a small cache keyed by `(meta_version, security_id)`. A missing or corrupt object
  triggers one pointer reload, then 503, exactly as weekly bars do.

**`GET /api/v1/securities/{id}/breakout-events?source=pattern|level&from=&to=&direction=&pattern_type=&level_source_type=&cursor=&limit=`**

- `source` is required: the two datasets stay separate (ADR-0022 §19.4); nothing merges
  them.
- Predicates are equality or range tests on stored row fields only: `bar_date` between
  `from` and `to`, `direction`, `pattern_type` (pattern) or `level_source_type`
  (level). There is no `status` filter: a breakout event's status is derived from its
  history and is not a stored field. Filtering on it would mean computing it.
- Rows come verbatim, in the stored order (`bar_date`, `event_key`); `limit` ≤ 500.
- The cursor encodes the dataset's content hash and an offset. If the snapshot changes
  between pages, the content hash no longer matches and the API answers 409 ("the
  snapshot changed; restart"), never a page from a different dataset.

**`GET /api/v1/system/status`** adds the snapshot's analysis summary (§1.7).

### 2.3 Numbers: an amendment to K7 (decision 6)

ADR-0023 K7 asked the API to print bar prices as exact decimal text and round derived
values to 4 decimals. R1 (approved) already moved formatting out of the stored form.
Doing it in the API would need a field-by-field map of which values are bar prices and
which are derived: a second schema of the document, the thing this ADR forbids.

Proposed instead:

- **The API returns stored values exactly** (the canonical JSON numbers).
- **Bar prices keep their exact decimal digits anyway.** A weekly price is a decimal of
  at most 15 significant digits (6 decimal places); converting it to a float and back
  with the shortest round-trip representation returns the same decimal (checked on
  200,000 random 6-decimal prices; 6c adds a real-data test that every swing and key
  point price equals its bar's stored decimal).
- **Rounding derived values to 4 decimals becomes a display rule in the frontend,** next
  to where it draws them, never a change to served values.

### 2.4 Boundaries

The API imports the pipeline's read side only: `serving` (snapshot), `storage`, and the
read functions of `analysis_store`. It never imports the engine or the job layer
(the layer test already forbids both). The frontend's rule is unchanged: if the API
does not return it, the chart does not compute it.

## Delivery and checkpoint evidence (6c)

- Publication: a full schema-3 publish on a test lake; for each check in §1.3, a test
  that breaks it and proves the pointer did not move (missing document, corrupt
  document, corrupt event file, wrong physical hash, extra or missing security, stale
  weekly hash, wrong analysis version, a crash just before step 7); rerun → UNCHANGED;
  clean-up keeps exactly the three sets in §1.6.
- 6b amendment: a corrupt reusable artifact is repaired, not reused.
- API: each route against a published test lake; section selection returns stored
  sections byte-identical to the document's; event predicates and paging; 409 across a
  snapshot change; 404s; 400 for `?as_of`; no route sorts or ranks (static test).
- Real NSE: a publish to a scratch prefix or the rehearsal store (read-only towards the
  live pointer), with verification timing for both depths in decision 2; the
  real-data K7 check.

## Decisions for review

1. **The snapshot pins a verbatim copy of the analysis manifest** under its version
   directory; no second listing of the entries.
2. **Verification depth:** full logical and physical checks for every object not named
   by the live snapshot; existence only for addresses it already names (immutable, and
   verified when first published). The alternative is to verify everything, every time.
3. **6b amendment:** reuse requires the artifacts to verify; a corrupt object is deleted
   and rewritten correctly, so a corruption cannot fail publication run after run.
4. **The job layer supplies the expected `analysis_version`** to publication (the
   pipeline cannot import the engine to compute it); the pipeline checks
   `analysis_methodology_hash` itself.
5. **Schema 3 is mandatory** once 6c ships: no snapshot without a complete analysis set.
6. **K7 amendment:** the API returns stored values exactly; 4-decimal rounding of
   derived values is a frontend display rule.
7. **API shape:** whole-section selection only. Sub-section predicates (for example a
   swing method or sensitivity, which ADR-0023 proposed) are deferred, since filtering
   inside a section is a further shaping decision.
8. **Events API:** `source` required; predicates on stored fields only; no status filter;
   a content-hash-bound cursor.
