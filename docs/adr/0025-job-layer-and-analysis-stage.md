# ADR-0025: The job layer and the ANALYSIS stage

**Status:** Proposed · 2026-10-07. For review before any code (phase 6b). It builds on
ADR-0024 (accepted), corrects one of its assumptions (§1) and refines §6.

ADR-0024 fixed what an analysis is, how it is addressed and how it is published. This
ADR fixes how the tracked run produces the analyses: when an existing result may be
reused, what establishes that, and how the stage proves its manifest covers exactly
the analytical universe before anything can be published.

## 1. A correction: the weekly file hash is not a stable input identity

ADR-0024 §4 assumed that a security's weekly file SHA-256 changes only when its bars do
("not `weekly_version`, which changes every day even when a security's bars do not").
**That is false.** Every row of a weekly file carries the run's provenance columns
(`as_of`, `weekly_version`, `data_version`, `adjustment_version`, `identity_version`),
and `weekly_version` changes with every new session. So **every weekly file's bytes
change on every run**, including the 616 inactive securities whose bars never change.

Consequences if left as built in 6a:

- reuse keyed on the file hash would only ever hit on a same-day rerun;
- every document records the file hash, so all 3,193 document addresses would change
  every day even where nothing analytical changed, contradicting "unchanged analysis
  gives the same address" (ADR-0024 §4).

**Proposed fix (decision 1):** the analysis's input identity is the **content of the
bars the engine analyses**, not the file they were read from.

- `bars_sha256` = the canonical SHA-256 of the exact bar frame passed to
  `analyze_security`: every column `to_bar_frame` produces, for the current segment,
  in row order, with dates as ISO text (encoding versioned with the document schema).
- It is computed by an engine function (`chartlens_engine.analysis.bars_content_hash`)
  and recorded by the orchestrator itself in the document, so the recorded hash is by
  construction the hash of what was analysed; the caller cannot misstate it.
- The **weekly file SHA-256 moves to the analysis manifest entry**: physical
  provenance (which file the bars were read from), used for the stale-input checks. The
  same logical/physical split Suba approved for events in R2.
- 6a change: `AnalysisInputs.weekly_file_sha256` is replaced by the
  orchestrator-computed `bars_sha256`; the event-file metadata follows. No analytical
  value changes.

This is the "weekly input hash" of ADR-0024 decision 3, made faithful to its intent.

## 2. Packages and the run driver

`chartlens_jobs` (new workspace member `jobs/`) imports `core`, `engine` and `pipeline`.
Neither `engine` nor `pipeline` imports it, and neither does `backend` (the API never
computes). The layer-boundary test gains these three rules.

| Moves into `chartlens_jobs` | From |
|---|---|
| `ProductionRunner` (stage sequence, run record) | `chartlens_pipeline.production` |
| `daily` and `run-finalize` commands, as `chartlens-jobs daily` / `run-finalize` | `chartlens_pipeline.cli` |
| the ANALYSIS stage and a standalone `chartlens-jobs analysis` command | new |

The pipeline keeps every stage command (`ingest-daily`, `weekly`, `publish-serving`, …)
unchanged. The production workflow syncs `chartlens-jobs` and calls it; the API's
dispatch path, the run lock and the Firestore records are unchanged.

`Stage` gains `ANALYSIS` between `WEEKLY` and `PUBLISH_SERVING` (seven stages). Older
run records keep their six stages and must still read: stage lookups tolerate a missing
stage, and the System page labels the new one "Analysis".

Persistence mechanics stay in the pipeline (ADR-0001: pipeline = ingestion, build and
persistence): a new `chartlens_pipeline.analysis_store` owns the artifact layout, the
gzip document codec, the event Parquet codec, the analysis manifest model, and the
universe rule (§4), because PUBLISH_SERVING (pipeline) must validate all of them in 6c
without importing the engine or the job layer.

**Canonical JSON moves to `chartlens_core.canonical`** (decision 6). The publisher must
recompute event content hashes and the universe hash, and the pipeline may not import
the engine. The encoder is a shared primitive, like versioning; the engine re-exports
it, and nothing about it changes.

## 3. Dependency fingerprints and reuse

A result may be reused only when **everything its bytes depend on is unchanged**. The
document is a function of:

| Dependency | Pinned by |
|---|---|
| the bars analysed | `bars_sha256` (in the document) |
| the context (security, timeframe, segment, `as_of`, data `methodology_hash`) | the document's `identity` and `versions` |
| the recorded inputs (exchange, weekly schema and builder versions, `usable_from`) | the document's `inputs` |
| the engine code and the `[analysis]` settings | `analysis_version` (engine, analyzer and component versions, `analysis_methodology_hash`) |
| the stored form | document schema, canonical serialization and event schema versions |
| the numeric runtime | the **runtime fingerprint**: Python minor version and the installed numpy, pandas and pydantic versions |

**The reuse key** is the SHA-256 of the canonical encoding of the complete analysis
request: the `AnalysisContext` and `AnalysisInputs` (whole model dumps, so a field added
to either can never be left out), `bars_sha256`, `analysis_version`, the three format
versions and the runtime fingerprint, under a `reuse_key_version`. A test perturbs each
field and requires the key to change.

- **The runtime fingerprint is in the key, not the document** (decision 2). A numpy or
  pandas upgrade can move the last bit of a float; without it in the key, the first run
  after a lockfile bump would fail the recompute check instead of simply recomputing.
  With it, a dependency upgrade costs one full recompute. It is not in the document,
  per ADR-0024's "no library build strings": if an upgrade does change bytes, the
  address changes because the content did.
- **The index is the latest complete analysis manifest** (§5): each entry carries its
  reuse key and its artifact names. A security is reused when its new key equals the
  entry's key **and** every artifact the entry names still exists. Otherwise it is
  computed.
- **What reuse saves, candidly.** The forming week changes every active security's bars
  each session, so a daily run computes about 2,577 securities and reuses the 616
  inactive ones; a same-day rerun reuses everything. Reuse is for inactive securities,
  reruns and UNCHANGED days, not the daily path.

**Code changes without a version bump** are a bug (ADR-0019). The guards are the golden
tests and the recompute check:

- **Recompute check (amendment G).** Every run recomputes the `k` reused securities with
  the smallest SHA-256 of (`weekly_version`, `security_id`) (all of them if fewer than
  `k` were reused) and requires byte-identical documents and event content. Any
  mismatch fails the stage and names the securities. Deterministic per run, rotating
  across runs, never a runtime random draw. `k` is a jobs setting (default 32).
- The check is evidence, not proof: it catches a missed bump only where the change
  affects a sampled security. A manual `chartlens-jobs analysis --no-reuse` recomputes
  everything; it is for recovery after a fix, never part of the scheduled run.

## 4. The universe, and proving coverage

**The rule (decision 3), `UNIVERSE_RULE_VERSION` 1:** a security is analysed when its
data-quality status row (at the weekly build's `dq_version`) has
`analytical_universe = true` and status `USABLE` or `USABLE_WITH_WARNINGS`.

Live snapshot `meta-c67e19c44cc4` (as of 2026-10-06), probed read-only:

| analytical | status | listing | securities |
|---|---|---|---|
| yes | USABLE | ACTIVE | 2,135 |
| yes | USABLE | INACTIVE | 316 |
| yes | USABLE_WITH_WARNINGS | ACTIVE | 442 |
| yes | USABLE_WITH_WARNINGS | INACTIVE | 300 |
| no | (either) | (either) | 874 |

So the rule gives 3,193 today; no security is NOT_USABLE; and for every analytical
security `usable_from` equals its current segment's start.

- **Inactive securities are included** (616). A delisted security's chart can still be
  opened; its analysis as of its last bar is history, costs nothing after the first run
  (reuse), and the scanner, not the analysis, decides what is current.
- **NOT_USABLE securities are excluded.** A FAIL finding means the history is not
  reliable; the API will say "not analysed: data quality".

**Proving coverage before publication:**

1. The stage derives the universe `U` once, from the data-quality status at the weekly
   manifest's `dq_version` (refusing if they differ), with the rule above.
2. Every member must have a weekly file in the weekly manifest; otherwise the stage
   fails (a data bug, not something to skip).
3. Per security, the stage verifies the file's bytes against the weekly manifest, takes
   the current segment's bars, and checks that segment against the data-quality
   current segment and `usable_from`. Any mismatch fails the stage.
4. The manifest is written only if its entries' `security_id`s **equal `U` exactly**. It
   records the sorted membership, `universe_sha256` (canonical hash of the sorted ids)
   and the rule version.
5. Publication (6c) re-derives `U` independently from the same published inputs and
   requires the same set and hash, and each entry's weekly file hash to equal the
   weekly manifest's. Recomputing the set from the inputs is what turns "the manifest
   says it covers the universe" into a check.

## 5. Artifacts and the analysis manifest

**Where they are written (decision 4).** Straight into the content-addressed serving
store, create-only:

- `curated/serving/exchange={EX}/analysis/{document_sha256}.json.gz`
- `curated/serving/exchange={EX}/events/{dataset}/{content_sha256}.parquet`

A blob there is inert until a manifest the pointer names refers to it, the property the
weekly copies already rely on. A failed run leaves only unreferenced orphans, which no
snapshot serves (amendment F); publication's clean-up removes them. The alternative, a
staging prefix copied at publication, would download and re-upload about 1 GB every
day for no added guarantee.

**Documents** are gzip-compressed (level 6, `mtime=0`, no file name) and named by the
SHA-256 of the uncompressed canonical bytes (ADR-0024 §4.1). On synthetic documents,
level 6 compresses 5.7× in about 130 ms; level 3 is 5.1× in about 60 ms. The level is
physical only and can change freely.

**Event files are named by their content hash** (decision 5): the canonical hash of
their rows, the same hash the document records, so a pyarrow upgrade never renames
them. The manifest also records each file's byte SHA-256 for integrity (R2's
physical check). ADR-0024 §6 named them by their bytes; this aligns them with documents.

- **Codec:** an explicit Arrow schema per dataset (dates as `date32`, measured values
  as `map<string, double>`, `history` as `list<struct>`, `bar_volume` as a nullable
  struct), zstd, one row group. Decoding returns exactly the engine's rows: a test
  requires rows → Parquet → rows to be equal and to reproduce the content hash, on
  synthetic and real data.
- The file metadata carries amendment D's fields (dataset, schema version, event and
  source methodology versions, `analysis_version`, security, segment, `bars_sha256`,
  content hash).

**The analysis manifest** is `curated/analysis/exchange={EX}/_manifest.json`, written
last and only when the stage succeeds:

- the input versions: `weekly_version`, `dq_version`, `as_of`, data `methodology_hash`;
- `analysis_version`, `analysis_methodology_hash`, the format versions, the runtime
  fingerprint, `reuse_key_version`, `universe_rule_version`;
- `universe` (sorted ids) and `universe_sha256`;
- one entry per security: the reuse key, the weekly file SHA-256, `bars_sha256`, the
  document hash, and per event dataset the content hash, byte hash and row count, plus
  whether it was computed or reused;
- `analysis_set_hash`: the canonical hash of the entries, by `security_id`.

It holds no timestamp, run id or host, so the same inputs give the same manifest bytes
(a rerun's manifest is byte-identical). Run facts go to the run record.

## 6. Execution and failure

- **Per security, in a process pool** (workers = CPU count, a jobs setting): read and
  verify the weekly file, build the frame, hash it, compute the key, then reuse or
  analyse, serialize, compress, encode, write. Workers return entries; the parent
  assembles the manifest. Processing order never shows: entries are keyed by
  `security_id`.
- **Failure fails the stage** (amendment F): a missing or changed weekly file, a segment
  mismatch, an engine exception, a non-canonical value, a write error or a recompute
  mismatch. Pending work is cancelled, **no manifest is written**, the previous manifest
  is untouched, PUBLISH_SERVING does not run, and the live snapshot stays live. The
  run record names the stage and the first security that failed.
- **Run record:** `records_processed` = universe size; `version` = `analysis_version`;
  details = computed, reused, recompute-sample size and mismatches, `universe_sha256`
  and `analysis_set_hash` prefixes, total document MB, seconds.
- **Settings:** a new `[jobs]` section (workers, recompute sample size). It is outside
  both methodology hashes: how the stage runs never changes what it computes.

Until 6c, PUBLISH_SERVING stays schema 2 and ignores the analysis manifest. Nothing on
`m8` reaches production before PR #16 merges, so there is no half-live state.

## 7. Checkpoint evidence for 6b

- A full stage run on a local test lake: the manifest covers `U` exactly; every artifact
  decompresses or decodes to its name; a rerun reuses everything and produces a
  byte-identical manifest; the recompute sample passes.
- Change one security's bars: only it is computed.
- Forced failures (a corrupted weekly file, an engine exception, a recompute mismatch):
  the run fails at ANALYSIS, no manifest, the previous manifest and the live pointer
  untouched.
- Key completeness (every field perturbed); the gzip regression test through the
  production write path; the event codec round trip; the old six-stage run records
  still read.
- A tracked seven-stage run end to end (memory run store).
- Real NSE: the stage reads the live lake and writes to a local store on the runner
  (read-only towards the lake, as every probe so far): timing, sizes, the computed and
  reused counts, and a same-day rerun that reuses everything.

## Decisions for review

1. **Input identity:** `bars_sha256` (the analysed frame's content) in the document; the
   weekly file hash in the manifest entry. A small change to the 6a model (§1).
2. **Reuse key:** the whole analysis request plus a runtime fingerprint (Python, numpy,
   pandas, pydantic versions), the fingerprint in the key only (§3).
3. **Universe:** `analytical_universe` and not NOT_USABLE, **including the 616 inactive
   securities** (§4).
4. **Artifact location:** straight into the content-addressed serving store, rather than
   a staging area copied at publication (§5).
5. **Event files named by content hash**, byte hash in the manifest (refines ADR-0024
   §6).
6. **Canonical JSON moves to `chartlens_core`** so publication can validate without the
   engine (§2).
