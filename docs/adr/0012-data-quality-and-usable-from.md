# ADR-0012: Data quality and `usable_from`

**Status:** Accepted · 2026-10-01

## Definition

> **`usable_from`**: the earliest date from which a security's technical history is
> considered reliable under the current methodology.

It is the first session on or after the **latest continuity break**, or the first
session if there is no break. The technical engine (Milestone 4 onwards) must never
compute a pattern, indicator window or swing structure that spans a continuity break.
A later `analysis_eligible_from` may add minimum-history rules on top of this.

## Findings

Each finding records:
- `security_id`, which is null for market-wide findings;
- `start` and `end` dates;
- a dimension: `SOURCE`, `IDENTITY`, `PRICE`, `CORPORATE_ACTION` or `CALENDAR`;
- a severity: `INFO`, `WARN` or `FAIL`;
- a code;
- `breaks_continuity`;
- a detail;
- evidence (a pointer to the record, event or file).

Findings are stored in `metadata/data_quality/nse/findings.parquet`.

| Code | Severity | Breaks continuity |
|---|---|---|
| `UNQUANTIFIED_ACTION`, `CONFLICTING_ACTION_RECORDS` | WARN | **Yes**, at the first session on/after the ex-date |
| `FACTOR_REJECTED_BY_PRICE` | WARN | Yes, if a gap larger than tolerance remains |
| `REVIEWED_LINK_PRICE_BREAK` (ADR-0013) | WARN | **Yes**, at the first session under the linked ISIN |
| `TRADING_GAP`: more than `max_trading_gap_sessions` (65) expected sessions without a trade | WARN | **Yes**: there is no price discovery across the gap |
| `FACTOR_SUSPECT`, `UNRECOGNISED_ACTION` | WARN | No |
| `UNEXPLAINED_MOVE`: \|adjusted close-to-close\| > `max_unexplained_move` (25%) with no corporate action or cash ex-date at that session | WARN | No. A large move may be real, and it is never inferred to be an artefact |
| `MISSING_SESSIONS`: share > `max_missing_session_ratio` | WARN | No |
| `SESSION_QUARANTINED`, `SESSION_FAILED`, `SESSION_MISSING` (market) | WARN | No; they count for every security trading through them |
| `ACTION_UNRESOLVED`, `ACTION_CONFLICT` (market, price-relevant records that are not attached) | WARN | No; this is a review queue |
| `FACTOR_APPLIED`, `FACTOR_APPLIED_WITHIN_NOISE`, `NO_ADJUSTMENT_NEEDED`, `ACTION_PENDING`, `SYMBOL_CHANGE`, `ISIN_CHANGE`, `DERIVED_CALENDAR` | INFO | No |
| `ADJUSTED_FILE_HASH_MISMATCH` | FAIL | — |

## Status (point-in-time)

`chartlens_core.quality.status(first, last, findings, as_of)` is a pure function
available to the engine. Only findings dated on or before `as_of` count.

* `NOT_USABLE`: there is a FAIL finding, or there is no session on or after
  `usable_from`.
* `USABLE_WITH_WARNINGS`: there is a non-breaking WARN that concerns the usable window.
  Warnings before `usable_from` are history and do not count.
* `USABLE`: otherwise.

`status.parquet` stores the latest view, as of the data end, with one row per
security. The findings table answers the question as of any earlier date.

## Inputs and versioning

Data quality reads only stored artefacts:
- the published adjusted manifest and files (with hashes checked);
- the events and actions tables;
- the ingestion manifests;
- the calendar;
- the identifier history;
- the reviewed identity overrides.

`dq_version` = sha of the engine version, the `data_quality` config, the
`adjustment_version`, the calendar version and the identity-overrides hash.

## Consequences

* Securities with a demerger or scheme lose the history before it unless a reviewed
  override documents the effect. This is deliberate: an unexplained observation is
  never converted into an assumed fact.
* `TRADING_GAP` and its 65-session threshold are a methodology choice, made in
  configuration and hashed into the methodology. It is not an inference about cause.
