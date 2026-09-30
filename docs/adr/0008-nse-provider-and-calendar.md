# ADR-0008: NSE provider, bhavcopy formats and trading calendar

**Status:** Accepted · 2026-09-30

## Evidence

Established by running `scripts/nse_probe.py` on a GitHub-hosted runner (2026-09-30).
Nothing below is assumed; each point was observed.

| Question | Finding |
|---|---|
| Can hosted runners reach NSE? | Yes: `nsearchives.nseindia.com` serves archive files to GitHub runners. `archives.nseindia.com` returns 403 for older paths; `www.nseindia.com` pages return 403, but its holiday API answered. |
| Legacy bhavcopy | `content/historical/EQUITIES/{YYYY}/{MON}/cm{DD}{MON}{YYYY}bhav.csv.zip`, present 2006-01-02 → 2024-07-05, absent from 2024-07-08. |
| New (UDiFF) bhavcopy | `content/cm/BhavCopy_NSE_CM_0_0_0_{YYYYMMDD}_F_0000.csv.zip`, present from at least 2024-01-19 (absent 2020-03-02). |
| Legacy columns | `SYMBOL…TIMESTAMP` + trailing empty column. **`TOTALTRADES` and `ISIN` are absent in 2006, 2009 and June 2011 files**, present by January 2015. Dates like `2-JAN-2006` (unpadded) and `01-JUN-2011`. Prices with variable decimals (`862`, `51.2`). |
| UDiFF columns | Two header variants seen: `Rsvd01…Rsvd04` + trailing empty column (Jan 2024); `Rsvd1…Rsvd4` (later). Prices with 2 decimals. Close is `ClsPric` (not `SttlmPric`). |
| Missing file | HTTP 404 with an HTML body (e.g. Republic Day 2024-01-26). |
| Special sessions | Files exist for Sat 2024-01-20, Fri 2024-11-01 (Muhurat), Sat 2025-02-01 (Budget). |
| Cross-format agreement | Legacy and UDiFF for 2024-07-05 have identical row counts and identical values for shared rows. |
| Identity evidence | `content/equities/symbolchange.csv` (no header: company, old, new, date). |

## Decision

### Provider contract

`ExchangeProvider` (exchange-neutral, in `providers/base.py`) exposes the trading
calendar, a `DailyBarSource` (download / check_published / parse), an identity
policy and symbol-change evidence. Download and parse are separate: bytes are
stored before parsing, and parsing is a pure function of bytes. Everything NSE-specific
lives under `providers/nse/`; nothing else imports it except the provider registry.

### Source selection per date

Try UDiFF first from `providers.nse.udiff_first_date` (2024-01-01); try legacy up to
`legacy_last_date` (2024-07-05). Outcomes:

* **FOUND:** a valid zip at some location.
* **NOT_PUBLISHED:** every location answered 404. This is a fact about the source.
* **FAILED:** anything else (403, 5xx after retries, timeout, HTML with 200). Nothing
  is concluded, and the date is retried on the next run.

Publication checks always use **GET with body validation, never HEAD**. The first
calendar derivation used HEAD and found that the legacy path answers HEAD with 200
for files that do not exist, which made every day of 2006–2023 look published.

### One parser, two mappings

Format detection reads the **header**, never the filename. Each format is only a
`FormatSpec` (field → column, date format, row filters, known-but-unused columns).
All rows then pass through one common pipeline: field parsing, structural validation,
date checks and duplicate checks. Unknown extra columns are tolerated and reported.
The parser is versioned (`nse_bhavcopy_v1`); a behavioural change bumps the version,
which makes every stored date reprocess from its raw bytes.

### Trading calendar

Calendars are **data**: per year, weekday closures and weekend sessions, each year
labelled with its evidence. A year the calendar does not cover raises an error
(never "assume Monday–Friday").

| Years | Evidence | Source |
|---|---|---|
| 2026 | `official` | NSE holiday-master API (current year only), plus weekend sessions observed in the archive |
| 2006–2025 | `derived` | `chartlens-pipeline calendar-derive`: which files the archive actually serves |

The derivation was validated where an official list exists. For 2026-01-01 → 2026-09-29
the derived weekday closures match NSE's official list exactly (11 of 11, no extras).
It also found a session the official list does not show: **Sunday 2026-02-01 (Union Budget)**.
The derivation refuses implausible years (more than 262 sessions, more than 6 weekend
sessions, or no closures in a full year).

## Limitations

* In `derived` years, a session whose file NSE never published is indistinguishable
  from a holiday. Missing-day detection is fully meaningful only in `official` years.
  Official lists for past years can replace derived years later without code changes.
* Only the capital-market segment (`SGMT = CM`) and universe series (EQ, BE) are ingested.
  Other series are counted, and remain in the raw files.
