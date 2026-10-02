# ADR-0014: The weekly data product, continuity segments and point-in-time weekly bars

**Status:** Accepted · 2026-10-02 (decisions confirmed by Suba the same day)
**Supersedes:** the field names of ADR-0004 (`week_start_date` / `week_end_date`), see below.

## Context

Weekly is the primary analytical timeframe. ADR-0004 fixed how a week is formed; this
record fixes what the weekly data product is, how it meets continuity breaks
(ADR-0012) and `as_of` (ADR-0006), and how it is stored. M4 is only
**adjusted daily → deterministic weekly data**. Indicators, swings, trends, patterns and
scans do not belong here; they consume this contract later.

## Decisions (confirmed 2026-10-02)

| # | Question | Decision |
|---|---|---|
| 1 | A continuity break inside a calendar week | **Split the week** into one bar per continuity segment, both marked `partial_reason = CONTINUITY_BREAK`; neither side is dropped |
| 2 | Adjustment for a historical `as_of = T` | **Point-in-time**: only corporate actions with ex-date ≤ T |
| 3 | Which securities | **All canonical securities**. Analytical eligibility is applied downstream |
| 4 | Storage | **Per-security files** (primary) + a **derived logical scan dataset**, both published through manifests |

## The contract

1. Weekly bars are derived exclusively from the published adjusted daily dataset.
2. Weeks are ISO weeks (Monday–Sunday), labelled by the **last actual session** in the
   bar (`last_session_date`, the bar frame's `bar_date`).
3. open = first session's open; high = max high; low = min low; close = last session's
   close; volume = sum of volumes. Exact decimals, the adjusted dataset's scales.
4. Every bar records `security_id`, `continuity_segment_id`, `iso_year`, `iso_week`,
   `week_start_date` and `week_end_date` (the Monday and Sunday bounds of the ISO week),
   `first_session_date`, `last_session_date`, `trading_days`, `is_complete`,
   `partial_reason`, `special_sessions`, `closes_on_special_session`, `raw_close`, `as_of`,
   `adjustment_version`, `identity_version`, `data_version` and `weekly_version`.
5. A weekly bar never crosses a continuity boundary.
6. A break inside a week produces two partial bars rather than dropping either side.
7. **Technical structure must never cross `continuity_segment_id`.** The bar-frame
   contract enforces it: a frame carrying `continuity_segment_id` must hold exactly one
   value (`validate_bar_frame`, called by `run_analyzer`). No HH/HL relationship,
   swing, trendline, Fibonacci leg, pattern or indicator window spans two segments, and
   a post-break segment inherits nothing from the one before.
8. Incomplete bars are never eligible for confirmed technical signals (ADR-0004/0006).
9. A historical `as_of = T` uses only corporate actions with ex-date ≤ T.
10. Weekly data is generated for every canonical security.
11. Per-security weekly files are the primary query representation.
12. A combined logical scan dataset is generated for scanning. It is derived and
    reproducible, never authoritative: deleting it loses nothing.
13. The scan dataset is published atomically through its manifest.
14. `usable_from` controls analytical eligibility. **It is a point-in-time eligibility
    boundary, not a deletion boundary**: every segment stays in the data.
15. A historical run may use an earlier segment when that segment was the valid one at
    the requested `as_of`.
16. Weekly OHLCV reconciles exactly to its contributing daily bars.
17. Weekend sessions (Budget days, Muhurat) are represented explicitly and flagged
    (`special_sessions`, `closes_on_special_session`), never silently treated as ordinary.

## Continuity segments

A **continuity segment** is a stretch of a security's history with no continuity break
inside it. Segments are produced by data quality, the authority on breaks, as
`metadata/data_quality/{ex}/continuity_segments.parquet` (dq_v4). Each row holds:

- `security_id`
- `continuity_segment_id`
- `segment_start` and `segment_end`
- `sessions`
- `cause`: `FIRST_SESSION`, or the break code(s) joined by `+`

The id is `security_id@segment_start`. It is derived from the continuity regime itself,
not from a builder counter, so:

- a rebuild with the same breaks gives the same ids;
- a new break changes only the segment it splits;
- any engine can ask "are these two bars in the same continuous price segment?" by
  comparing ids, without inferring continuity itself.

The last segment always starts at `usable_from`, and data quality asserts this. Each
segment starts on a session the security actually traded.

## `is_complete`

A bar is complete once the last *scheduled* session of its ISO week, taken from the
trading calendar, is on or before `as_of`. A week whose remaining sessions are holidays
is therefore complete on its last actual session. The data end of 2026-10-01 is an
example: 2 October is Gandhi Jayanti, so that week is complete.

The pre-break side of a split week is complete, because the break closed it.

## The two meanings of `as_of`

ChartLens keeps two boundaries distinct. M4 implements both for weekly bars, and every
later analytical input must respect the first one too.

1. **Data knowledge boundary.** This is what the system could know at T. It covers:
   - daily bars dated ≤ T;
   - continuity breaks (segments) starting ≤ T;
   - a security exists only from its first session;
   - a week is complete only if its last scheduled session is ≤ T.

   Patterns, historical validation, scanner backtests, divergence and trend transitions
   must all respect this boundary for *all* their inputs, not just prices. Otherwise a
   historical chart can look realistic while future information leaks through another
   input.
2. **Price adjustment boundary.** These are the corporate actions that had become
   effective by T: ex-date ≤ T. The adjusted price is computed as follows:

   ```
   adjusted_as_of(T)[d] = raw[d] × Π{ factor : d < ex_date ≤ T }
   ```

   It is recomputed from raw with the exact cumulative fraction and rounded once
   (ADR-0011). At T = the data end it equals the published adjusted columns exactly,
   and the verification checks every security.

## Reading weekly bars

`chartlens_pipeline.weekly.WeeklyReader.load(security_id, as_of)` is the only way
analysis reads weekly bars.

| Requested `as_of` | What is returned |
|---|---|
| none, or ≥ the data's `as_of` | The stored bars (the hash is verified against the manifest). The effective `as_of` is the data's own: nothing later exists |
| earlier than the data | A **point-in-time rebuild** from the published adjusted file. It uses rows ≤ T, the applied factor events with ex-date ≤ T, the segments starting ≤ T, and the calendar. The stored weekly files play no part |

By default only the segment valid at `as_of` is returned, because that is what analysis
may use. `all_segments=True` returns every segment known at `as_of`, for charts and
inspection. `series.frame()` gives the engine bar frame (floats only at this boundary).

Examples:

- **RELIANCE, as of 2015:** the 2006–2015 history, adjusted only for actions up to 2015.
  The 2017 and 2024 bonuses do not apply, and the 2023 demerger break does not exist
  yet.
- **RELIANCE, as of today:** the segment starting 2023-07-20.

## Storage

```
curated/weekly/exchange={EX}/{security_id}.parquet        every segment of one security
curated/weekly/exchange={EX}/_manifest.json               written last; file hashes
curated/weekly_scan/exchange={EX}/v={weekly_version}/part-NNN.parquet
curated/weekly_scan/exchange={EX}/_manifest.json          written last; parts + checksums
```

Both manifests record:

- `weekly_version`, `data_version` (hash of the adjusted manifest), `adjustment_version`,
  `identity_version`, `dq_version`, `calendar_version` and `methodology_hash`;
- the schema and builder versions;
- `as_of`, row and security counts, and the min/max session;
- the source and `generated_at`.

The scan manifest also lists each part's key, row count and SHA-256.

The scan dataset is a **logical dataset of one or more parts**: today one part, at up to
2,000,000 rows per part. A reader goes through the manifest and never assumes a single
file, so storage can be re-partitioned later without changing the analytical contract.

- Parts live under their version, so publishing never overwrites what a reader of the
  previous manifest is reading.
- The current and the previous version are kept, and older ones are pruned. The object
  store gained `delete`, which refuses `raw/`.
- Per-security files are rewritten only when their content changes, like the adjusted
  dataset.

`weekly_version` hashes the builder and schema versions, the weekly methodology, the
`data_version`, the `dq_version` and the calendar version. The same inputs give the same
version and rewrite nothing.

ADR-0010 expected the weekly builder to need per-year compaction of the canonical daily
files. It does not: the builder reads the per-security adjusted files, so no compaction
is added.

## Operations

`chartlens-pipeline weekly` runs as the last step of the `daily` chain:

```
ingest-daily → corporate-action feed → adjust → data-quality → weekly
```

It refuses to run (exit 5) when the adjusted dataset is missing, or when the
data-quality assessment is missing or was made on a different adjustment version.
`chartlens-pipeline weekly-bars --symbol X [--as-of D] [--all-segments]` shows a
security's bars.

## What the guard does not catch (known limits)

- **Identity** is resolved with today's security master. A link approved later, such as
  3i Infotech's, is applied to history. Its price effect is still a break, so no price
  structure spans it.
- **The `UNEXPLAINED_PRICE_DISCONTINUITY` rule** asks whether an accepted corporate-action
  explanation exists using today's feed. A record filed after the session would change
  the answer in hindsight.
- **Factor validation statuses** use only the ex-date open and earlier prices, so they
  are point-in-time.
