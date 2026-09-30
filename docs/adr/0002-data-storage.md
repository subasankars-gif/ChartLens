# ADR-0002: GCS data lake (Parquet) + Firestore for application state

**Status:** Accepted · 2026-09-30

## Context

About 2,000 securities × up to 20 years × ~250 sessions ≈ 10 million daily rows.
Firestore is unsuitable as the primary time-series store (spec §7). The data is
written in batches and read either per security (charts) or in bulk (scans,
backtests).

## Decision

**Object storage (GCS in production, local filesystem in development) is the
canonical store for all market data**, as Parquet, behind the `ObjectStore`
interface. **Firestore holds application state only.**

```
raw/{exchange}/{source_dataset}/{YYYY}/{YYYY-MM-DD}__{sha12}__{filename}
raw/{exchange}/{source_dataset}/snapshots/{fetched}__{sha12}__{filename}
curated/daily/{exchange}/{security_id}.parquet      ← superseded by ADR-0010 (per-date files)
curated/weekly/{exchange}/{security_id}.parquet
metadata/security_master/{exchange}/…
metadata/calendars/{exchange}/…
metadata/adjustments/{exchange}/…
```

All keys are built in one place: `chartlens_pipeline.storage.DataLakeLayout`.

### Raw data is immutable

* Raw files are stored **exactly as downloaded** (original bytes, original format),
  written with `put_immutable`: identical bytes again is a no-op, different bytes
  under an existing key is an error.
* The content hash is part of the key. If an exchange re-issues a file for the same
  date, both versions are kept side by side.
* Everything under `curated/` and `metadata/` is derived and can be rebuilt from `raw/`.

This refines the flow agreed at kickoff ("raw immutable Parquet"): the immutable
layer is the *original file*, and parsed Parquet is the first derived layer. That way
a parser bug is fixed by re-parsing stored bytes, with no dependence on the exchange
still serving the old file.

### Firestore collections

| Collection | Holds | Milestone |
|---|---|---|
| `securities` | Security master for search and display | M5 |
| `data_quality` | Status per security/timeframe | M5 |
| `jobs` | Job tracking (spec §42) | M7 |
| `system_config` | Last data update, active versions | M5 |
| `users`, `watchlists` | Per-user state (multi-user ready, ADR-0001) | Phase 7+ |
| `analysis`, `patterns`, … | Engine results | Phase 2+ |

The frontend never reads Firestore directly; it goes through the API. This keeps
Firestore security rules trivial and the frontend independent of storage (spec §57 rule 16).

## Consequences

* Serving a weekly chart reads one small per-security Parquet object; cached by the API.
* No database to run or pay for until it is actually needed. If ad-hoc SQL becomes
  necessary, DuckDB can query the lake directly; a relational store can be added
  later behind the same repository interfaces.
