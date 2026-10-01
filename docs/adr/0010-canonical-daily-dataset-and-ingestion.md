# ADR-0010: Canonical daily dataset and ingestion

**Status:** Accepted · 2026-09-30 · Supersedes the `curated/daily` line of ADR-0002

## Canonical daily bar (schema version 1)

| Column | Type | Notes |
|---|---|---|
| `exchange` | string | |
| `security_id` | string | ADR-0009 |
| `trading_date` | date32 | |
| `symbol`, `series`, `isin` | string | As published that day (audit); `isin` null before ~2011 |
| `open`, `high`, `low`, `close`, `prev_close` | **decimal128(18, 4)** | Exact |
| `volume` | int64 | |
| `traded_value` | decimal128(24, 4) | |
| `trades` | int64 | Null before ~2011 |
| `source_id`, `source_file_hash` | string | SHA-256 of the exact source bytes |
| `source_file_date` | date32 | |
| `parser_version` | string | e.g. `nse_bhavcopy_v1` |
| `ingested_at` | timestamp[us, UTC] | |

**Prices are exact decimals, never floats.** Values with more than 4 decimal places
are quarantined, not rounded. Float conversion happens only at the engine boundary
(Milestone 4), where the bar-frame contract requires it.

The schema is explicit: tables are built with a declared Arrow schema, never inferred.
It is versioned: `chartlens.schema_version` is stored in the Parquet metadata, and
readers verify both schema and version. Any change bumps the version.

Uniqueness key: `(exchange, security_id, trading_date)`. The resolver guarantees at most
one row per key; a would-be second row is quarantined.

## Layout

```
curated/daily/exchange=NSE/year=2024/2024-01-10.parquet      one file per session
quarantine/daily/exchange=NSE/year=2024/2024-01-10.parquet   rejected rows, same key
quarantine/conflicts/exchange=NSE/2024-01-10.json            source-version conflicts
metadata/ingestion/nse/2024/2024-01-10.json                  per-date manifest
metadata/security_master/nse/{securities,identifier_history}.parquet
raw/nse/bhavcopy/2024/2024-01-10__{sha12}__{file}             original bytes (+ .meta.json)
raw/nse/_by_hash/{sha256}.json                                hash → raw key
```

**Why per-date files, partitioned by exchange and year?**
* The unit of ingestion, idempotency and conflict handling is one session. Replacing
  one small file makes reprocessing a date atomic.
* Cross-sectional reads (scanner, data quality) read one file.
* Hive-style `exchange=`/`year=` directories let Arrow/DuckDB prune by exchange and
  year, at about 250 files per year.

ADR-0002's `curated/daily/{exchange}/{security_id}.parquet` was **rejected** because
daily ingestion would rewrite ~2,000 objects per session.

Per-security history reads (charts, weekly builder) over 5,000 small files will need
compaction into per-year files sorted by `security_id`. That is added in Milestone 4,
when its consumer exists and can be measured.

## Ingestion protocol (per session)

1. Plan the action:
   * `SKIP`: the manifest is current (same parser version, schema version, identity
     fingerprint and source versions, and no identity-pending rows).
   * `PROCESS_STORED`: re-parse stored bytes, no download.
   * `DOWNLOAD`: fetch the file.
2. Download: the original bytes are stored immutably and their hash indexed.
   Identical bytes are a no-op; different bytes for the same date are a **new
   version, kept alongside**.
3. Parse the selected version. Reading re-verifies its SHA-256.
4. If several versions exist, parse all of them and compare. Any difference writes a
   conflict report and a warning. **Selection rule: the most recently downloaded
   version**, recorded in the manifest. It is explicit and auditable, never silent.
5. Resolve identities, then write, in commit order:
   1. security master
   2. canonical file
   3. quarantine file
   4. **manifest last** (the commit marker)

   A crash before the manifest means the date is simply processed again, with
   identical results.

An expected session whose file is not published gets a `NOT_PUBLISHED` manifest with
`dq_condition = EXPECTED_SESSION_NOT_PUBLISHED`. **No bar is manufactured.** Recent
not-published dates (7 days) are re-checked on the next run, since files can appear late.

Backfills process sessions **newest first** (identity anchoring, ADR-0009). One failing
date is recorded (`FAILED` manifest, error in the summary) and never stops the run.

## Quarantined sessions (added 2026-10-01, review finding H2)

A session counts as ingested only if the parser accepted it as a whole. A manifest gets
status `QUARANTINED`, with a `dq_condition`, when either:

* the parser rejects more than `data_quality.max_session_quarantine_ratio` (5%) of the
  in-scope rows (`ABNORMAL_QUARANTINE_RATIO`); or
* the file has no rows in the configured series (`NO_IN_SCOPE_ROWS`).

Such a session:
* counts as a run error, so the CLI exits 2;
* still writes its valid rows and its quarantine file;
* is reprocessed from stored bytes on every run, so a parser fix heals it with no
  re-download.

Previously, 2020-07-13 (100% rejected) was recorded as `INGESTED` with 0 errors.
Identity-pending rows do not count towards the ratio; they are expected while pre-ISIN
history is anchored.

## Quarantine

Nothing is discarded. Quarantined rows keep the source hash, parser version, row
number, reason, detail and the **verbatim source line**.

* Structural reasons: `MALFORMED_ROW`, `MISSING_FIELD`, `INVALID_NUMBER`,
  `PRICE_PRECISION`, `INVALID_DATE`, `DATE_MISMATCH`, `NON_POSITIVE_PRICE`,
  `NEGATIVE_VOLUME`, `INVALID_OHLC`, `INVALID_ISIN`, `DUPLICATE_ROW`.
* Identity reasons: `UNRESOLVED_IDENTITY`, `AMBIGUOUS_IDENTITY`, `IDENTITY_CONFLICT`,
  `DUPLICATE_SECURITY_DATE`.

`chartlens-pipeline quarantine-report` summarises them for review. Rows outside the
universe (other series) are counted, not quarantined; they remain in the raw file.

## Scale (20-year run on a GitHub-hosted runner, 2026-09-30)

| Measure | Value |
|---|---|
| Sessions | 5,144 (2006-01-02 → 2026-09-29) |
| Rows read | 9,431,333 |
| Rows accepted | 8,313,728 |
| Rows outside the universe | 1,115,946 |
| Securities | 4,055 |
| Lake size | about 1.0 GB (raw zips + canonical + master) |
| First run | 3.2 h, network-bound at a 0.5 s politeness delay |
| Second run | 90 s, every date skipped as already ingested |

## Observability

* Every date logs `ingest.date` with `job_id`, `trading_date`, `source_file`,
  `source_hash`, `parser_version`, `rows_read`, `rows_accepted`, `rows_rejected` and
  `duration`. The logs are JSON on runners and Cloud Run.
* Counters (`files_downloaded`, `rows_quarantined`, `securities_created`,
  `unresolved_identifiers`, `duplicate_rows`, `source_conflicts`, …) appear in the run
  summary and the JSON report, ready for Milestone 7 job tracking.
