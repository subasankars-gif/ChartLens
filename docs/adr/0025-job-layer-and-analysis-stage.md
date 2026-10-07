# ADR-0025: The job layer and the ANALYSIS stage

**Status:** Accepted · 2026-10-07. Decisions 1–6 approved by Suba with clarifications,
which are written into the contract below (§3, the frozen part). Builds on ADR-0024,
corrects one of its assumptions (§1) and amends its §6 (event file naming).

ADR-0024 fixed what an analysis is, how it is addressed and how it is published. This
ADR fixes how the tracked run produces the analyses: when an existing result may be
reused, what establishes that, and how the stage proves its manifest covers exactly the
analytical universe before anything can be published.

## Decisions

| # | Decision |
|---|---|
| 1 | Input identity is `bars_sha256`, the content of the bars the engine receives, computed by the orchestrator. The weekly file hash is physical provenance, in the manifest |
| 2 | The reuse key is a versioned dependency fingerprint, including the numeric runtime versions; those versions are in the key only, never in the document |
| 3 | The universe is `analytical_universe` and not NOT_USABLE, including the 616 inactive securities |
| 4 | Artifacts are written straight into the content-addressed serving store; no staging copy |
| 5 | Event files are named by their content hash; their byte hash is recorded in the manifest (amends ADR-0024 §6) |
| 6 | Canonical JSON moves to `chartlens_core`, generic and free of analytical logic |

## 1. Physical and logical input identity

**Finding.** Every row of a weekly file carries the run's provenance columns (`as_of`,
`weekly_version`, `data_version`, `adjustment_version`, `identity_version`), and
`weekly_version` changes with every session. So every weekly file's bytes change on
every run, including the 616 inactive securities whose bars never change. ADR-0024 §4
assumed otherwise. Keyed on the file hash, reuse would only hit on same-day reruns, and
every document address would change daily with nothing analytical changed.

**The split** (the same one R2 made for events):

```text
PHYSICAL PROVENANCE
weekly file ──► weekly_file_sha256 ──► stale-input verification (manifest entry)
                       │
                       ▼
LOGICAL PROVENANCE
exact bars ──► bars_sha256 ─────────┐
recorded inputs ────────────────────┤
analysis version ───────────────────┤
format versions ────────────────────┼──► reuse key ──► reuse │ compute
runtime versions ───────────────────┘                         ▼
                                                  immutable result ──► snapshot manifest
```

> **`bars_sha256` is the SHA-256 of the canonical serialized representation of the
> exact bar sequence supplied to the analytical engine for that security and segment.**

- It is computed inside `analyze_security` from the frame it was given
  (`chartlens_engine.analysis.bars_content_hash`), never accepted from the caller, so
  no caller can claim "these bars have hash X" when the engine received something else.
  The job layer calls the same function to build the reuse key and the stage checks the
  two agree.
- The frame is the one `to_bar_frame` builds for the current segment (all its columns,
  in row order); the encoding is canonical JSON with dates as ISO text, versioned with
  the document schema.
- The weekly file hash stays in the weekly manifest and in the analysis manifest entry,
  for stale-input verification. It is never part of the document or the reuse key.
- 6a change: `AnalysisInputs.weekly_file_sha256` is removed; the document's `inputs`
  carry the orchestrator-computed `bars_sha256`. No analytical value changes.

## 2. Packages and the run driver

`chartlens_jobs` (new workspace member `jobs/`) imports `core`, `engine` and `pipeline`.
Neither `engine` nor `pipeline` imports it, and the API does not either: serving never
computes. The layer-boundary test gains these rules.

```text
core      domain models and contracts, canonical serialization, hashing utilities
engine    analytical computation            (→ core)
pipeline  ingestion, build and storage      (→ core)
jobs      orchestration                     (→ engine, pipeline, core)
backend   serving                           (→ pipeline's serving reader, core)
```

| Moves into `chartlens_jobs` | From |
|---|---|
| `ProductionRunner` (stage sequence, run record) | `chartlens_pipeline.production` |
| `daily` and `run-finalize`, as `chartlens-jobs daily` / `run-finalize` | `chartlens_pipeline.cli` |
| the ANALYSIS stage, and a standalone `chartlens-jobs analysis` | new |

- The pipeline keeps every stage command unchanged. The production workflow syncs and
  calls `chartlens-jobs`; the API's dispatch, the run lock and the Firestore records are
  unchanged.
- `Stage` gains `ANALYSIS` between `WEEKLY` and `PUBLISH_SERVING`. Six-stage records
  written before this still read; stage lookups tolerate a missing stage; the System
  page labels it "Analysis".
- `chartlens_pipeline.analysis_store` owns the persistence mechanics PUBLISH_SERVING must
  also validate in 6c, without the engine or the job layer: the artifact layout, the
  gzip document codec, the event Parquet codec, the analysis manifest model and the
  universe rule.
- `chartlens_core.canonical` holds the canonical encoder and content-hash helpers. It
  stays generic: no analytical or business logic enters `core`. The engine re-exports it.

## 3. The dependency fingerprint and reuse eligibility (frozen)

### 3.1 The rule

> **Anything whose change could make an existing analytical result invalid participates
> in the reuse key. Operational metadata that does not affect the analysis does not.**

### 3.2 The fingerprint (`reuse_key_version` 1)

`analysis_reuse_key = SHA-256(canonical(DependencyFingerprint))`, where
`DependencyFingerprint` holds:

| Field | Source |
|---|---|
| `reuse_key_version` | `"1"` |
| `context` | the whole `AnalysisContext` dump: `security_id`, timeframe, `continuity_segment_id`, `as_of`, the data `methodology_hash` |
| `bars_sha256` | `bars_content_hash` of the frame the engine receives |
| `inputs` | the whole `AnalysisInputs` dump: exchange, weekly schema and builder versions, `usable_from` |
| `analysis_version` | engine, analyzer and component versions, `analysis_methodology_hash` |
| `formats` | document schema, canonical serialization and event schema versions |
| `runtime` | Python minor version; installed numpy, pandas and pydantic versions |

- **Inputs only, never the result.** The fingerprint is built from the authoritative
  inputs, before analysis: inputs → fingerprint → reuse lookup → analysis →
  `TechnicalAnalysis`. It is never derived from a `TechnicalAnalysis` or any output, so
  there is no cycle (result → key → result). "Whole model dumps" means the
  `AnalysisContext` and `AnalysisInputs` models, so a field added to either can never be
  left out (tested: perturbing any field changes the key).
- **Explicitly excluded:** run or job id, execution and publication timestamps, snapshot
  id, the weekly physical file hash, storage paths, worker id, processing order, gzip
  bytes, Parquet physical encoding.
- **Runtime versions are execution-environment provenance:** in the key, never in the
  document. Same inputs, methodology and runtime → eligible for reuse; an upgraded numpy
  → a different key → recompute, with the document uncontaminated by environment
  metadata. **Any numerical or runtime dependency later found able to affect output
  must be added to `runtime`, with a `reuse_key_version` bump.**

### 3.3 Reuse eligibility is independent of physical artifact identity

> **A physical weekly artifact may change without invalidating an analysis result. An
> analysis result is reusable when its analytical dependency fingerprint is unchanged
> and the current physical manifest still proves that the artifact supplying those
> inputs is the expected artifact.**

Concretely, a security's previous result is reused only when all hold:

1. its weekly file's bytes match the **current** weekly manifest's hash (the physical
   proof);
2. the fingerprint built from the bars read from that file equals the previous entry's
   key;
3. every artifact the previous entry names still exists in the store.

Otherwise it is computed. **The index is the latest complete analysis manifest** (§5).

What reuse saves: a normal daily run computes about 2,577 active securities (the
forming week changes their bars) and reuses the 616 inactive ones; a same-day rerun
reuses all 3,193.

### 3.4 The recompute check (amendment G), evidence not proof

It proves: **a deterministic sample of reused results remains byte-identical when
recomputed from current inputs.** It cannot prove an unsampled security was unaffected.

- **Locked** (stage verification methodology, recorded in the manifest, not a tunable
  setting and outside `analysis_version`): sample size **32**;
  `sample_selection_version` 1.
- **Selection:** the reused securities with the smallest SHA-256 of
  `weekly_version | security_id`; all reused securities if fewer than 32. Chosen from
  the reused set after all keys are known, so independent of processing order.
- **Comparison is exact:** the stored document is read, decompressed and compared with
  the fresh canonical bytes; the stored event files are decoded and their canonical
  rows compared with the fresh rows; and therefore the cached content hashes equal the
  fresh ones. Never rounded or API values.
- Any mismatch fails the stage and names the securities. A manual
  `chartlens-jobs analysis --no-reuse` recomputes everything, for recovery after a fix;
  it is never part of the scheduled run.

## 4. The universe, and proving coverage

**The rule (`universe_rule_version` 1):** a security is analysed when its data-quality
status row, at the weekly build's `dq_version`, has `analytical_universe = true` and
status `USABLE` or `USABLE_WITH_WARNINGS`.

Live snapshot `meta-c67e19c44cc4` (as of 2026-10-06), probed read-only:

| analytical | status | listing | securities |
|---|---|---|---|
| yes | USABLE | ACTIVE | 2,135 |
| yes | USABLE | INACTIVE | 316 |
| yes | USABLE_WITH_WARNINGS | ACTIVE | 442 |
| yes | USABLE_WITH_WARNINGS | INACTIVE | 300 |
| no | (either) | (either) | 874 |

So 3,193 today, none NOT_USABLE, and `usable_from` equals the current segment's start
for every analytical security.

- **Inactive securities are included.** A delisted security is still a legitimate
  analytical security; excluding it would make the historical dataset depend on today's
  trading status. The scanner, not the analysis, decides what is current.
- **NOT_USABLE securities are excluded**; the API will say "not analysed: data quality".

**Hard failures** (each fails the stage; none is a warning): a missing security, a
duplicate security, an unexpected security (outside the universe), a missing weekly
file, a weekly file hash mismatch, a segment mismatch, a `usable_from` mismatch
(`usable_from` must equal the current segment's start), an analysis failure, a missing
result, a result for a security outside the universe, a `bars_sha256` disagreement
between the job layer and the document.

**The proof:**

1. The stage derives the expected universe `U` once from the data-quality status at the
   weekly manifest's `dq_version` (refusing if they differ).
2. Every member must have a weekly file in the weekly manifest.
3. Per security, the file is verified against the weekly manifest, and its current
   segment against the data-quality current segment and `usable_from`.
4. **The manifest is generated from the completed result set, never from the expected
   list.** The stage collects one result per security that actually finished; the
   manifest's ids are the ids of those results. Then `set(results) == U`, with no
   duplicates, is checked as a set and through the recorded sorted-list hash
   (`universe_sha256`) before the manifest is written. This catches a job that believes
   it processed everything but silently failed to emit one result.
5. Publication (6c) re-derives `U` independently from the same published inputs and
   requires the same set and hash, and each entry's weekly file hash to equal the weekly
   manifest's.

## 5. Artifacts and the analysis manifest

> **An object becomes live only when a successfully published snapshot manifest
> references it.**

**Location:** straight into the content-addressed serving store, create-only:

- `curated/serving/exchange={EX}/analysis/{document_sha256}.json.gz`
- `curated/serving/exchange={EX}/events/{dataset}/{event_content_sha256}.parquet`

A failed run leaves only unreferenced objects, which no snapshot serves (amendment F).
Removing them is a separate garbage-collection concern; it must keep everything the
live snapshot, the previous snapshot and the latest analysis manifest name.

**Writes are idempotent, never overwrites.** Writing an address that already exists is
accepted only if the existing object decodes to the same logical content (a document
decompresses to bytes hashing to its name; an event file decodes to rows hashing to its
name). Then it is left as it is. An object at that address with different logical
content is corruption and fails the stage.

**Documents:** gzip level 6, `mtime=0`, no file name; named by the SHA-256 of the
uncompressed canonical bytes (ADR-0024 §4.1). Level is physical only.

**Event files (amends ADR-0024 §6):** named by `event_content_sha256`, the hash the
document records: the canonical SHA-256 of the dataset's **identifying metadata and its
rows** (`chartlens_core.canonical.dataset_content_hash`; the metadata are amendment D's
fields, without the content hash itself). Logical identity, stable across pyarrow
upgrades. The manifest also records `physical_sha256`, the SHA-256 of the Parquet bytes:
physical integrity, detecting corruption or replacement.

*Found in 6b (the pool-versus-one-process test, intermittently):* hashing the rows alone
gave every security with no events of a dataset the same address, so they shared one
file whose metadata named whichever security wrote first, and the recorded byte hash
depended on the race. The metadata is part of the dataset, so it is part of its
address; two owners' empty datasets are two files.

- Codec: an explicit Arrow schema per dataset (`date32` dates, `map<string, double>`
  measured values, `list<struct>` history, nullable struct `bar_volume`), zstd, one row
  group. Decoding returns exactly the engine's rows: rows → Parquet → rows are equal
  and reproduce the content hash (tested on synthetic and real data).
- File metadata carries amendment D: dataset, event schema version, event and source
  methodology versions, `analysis_version`, security, segment, `bars_sha256`, content
  hash.

**The analysis manifest**, `curated/analysis/exchange={EX}/_manifest.json`, written last
and only when the stage succeeds:

- input versions: `weekly_version`, `dq_version`, `as_of`, data `methodology_hash`;
- `analysis_version`, `analysis_methodology_hash`, format versions, runtime versions,
  `reuse_key_version`, `universe_rule_version`, recompute sample size and
  `sample_selection_version`;
- `universe` (sorted ids) and `universe_sha256`;
- per security: the reuse key, `weekly_file_sha256`, `bars_sha256`, the document hash,
  and per event dataset the content hash, physical hash and row count. Whether a result
  was computed or reused is a run fact: it goes to the run record, not the manifest
  (otherwise a rerun's manifest could not be byte-identical);
- `analysis_set_hash`: the canonical hash of the entries, by `security_id`.

No timestamp, run id or host: the same inputs give a byte-identical manifest. Run facts
go to the run record.

## 6. Execution and failure

- **Per security, in a process pool** (worker count is a `[jobs]` setting, outside
  both methodology hashes): read and verify the weekly file, build the frame, build the
  fingerprint, then reuse or analyse, serialize, compress, encode, write. Workers return
  results; the parent builds the manifest from them. Order never shows.
- **Any failure fails the stage** (amendment F): pending work is cancelled, no manifest
  is written, the previous manifest is untouched, PUBLISH_SERVING does not run, the live
  snapshot stays live. The run record names the stage and the first failing security.
- **Run record:** `records_processed` = universe size; `version` = `analysis_version`;
  details = computed, reused, sample size and mismatches, `universe_sha256` and
  `analysis_set_hash` prefixes, total document MB, seconds.
- Until 6c, PUBLISH_SERVING stays schema 2 and ignores the analysis manifest. Nothing on
  `m8` reaches production before PR #16 merges.

## 7. Checkpoint evidence for 6b

- A full stage run on a local test lake: the manifest covers `U` exactly; every artifact
  decodes to its name; a rerun reuses everything and writes a byte-identical manifest;
  the recompute sample passes.
- One security's bars changed: only it is computed. A weekly file rewritten with new
  provenance but the same bars: reused.
- Forced failures (a corrupted weekly file, an engine exception, a dropped result, a
  recompute mismatch, a conflicting object at an address): the stage fails, no
  manifest, the previous manifest and the live pointer untouched.
- Key completeness and exclusions; the gzip regression test through the production
  write path; the event codec round trip; six-stage records still read; a tracked
  seven-stage run end to end.
- Real NSE: read the live lake, write to a local store on the runner (read-only towards
  the lake): timing, sizes, computed and reused counts, a same-day rerun that reuses
  everything.

## 6b evidence (2026-10-07, at the checkpoint)

- **Tests:** 945 pass (`poe check`), CI green on `aea5460` including the new `jobs`
  image. The stage tests cover a full run, the same-day rerun (all reused, identical
  manifest bytes), a rewritten weekly file with the same bars (reused, new physical
  hash recorded), one changed security (only it computed), `--no-reuse` (same
  manifest), a missing artifact (recomputed), the pool against one process (identical
  manifest), and every hard failure (no manifest written, the previous one untouched).
- **Found by the pool test:** event content hashes over rows alone let empty datasets of
  different securities share one file (§5); fixed by hashing identifying metadata with
  the rows.
- **Real NSE rehearsal** (live lake read-only, artifacts written on the runner; weekly
  `wk-cc82a2aff239`, as of 2026-10-06; 4 CPUs):

| | First run | Same-day rerun |
|---|---|---|
| Securities | 3,193 computed | 3,193 reused |
| Recompute sample | none to check | 32, 0 mismatches |
| Wall time | 286 s | 133 s |
| `analysis_set_hash` | `79eff90b38a2…` | identical |

  Stored: 3,193 documents, 707 MB compressed (5,554 MB canonical, 7.9×); 6,386 event
  files, 164 MB (111 MB level, 53 MB pattern) holding 501,782 level and 15,718 pattern
  events, the same counts as the 6a fingerprint; manifest 3.0 MB. Every hard check
  (weekly hash, current segment, `usable_from` equals the segment start, the segment's
  first bar) held for all 3,193.
