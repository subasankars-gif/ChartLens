# ADR-0009: Security identity resolution

**Status:** Accepted · 2026-09-30 · Extends ADR-0003

## Principle

A false identity mapping is worse than missing data. Every rule below either
follows evidence or refuses: an unresolvable row is quarantined, never guessed.

## Model

* `securities`: one row per `security_id`, holding current ISIN, symbol and series,
  name, `identity_basis`, `listing_status`, and first/last seen.
* `identifier_history`: `(security_id, type ∈ {ISIN, SYMBOL, SERIES}, value,
  valid_from, valid_to, evidence, source_id)`.
  * Spans are **observed**: the first and last date the exchange published that
    identifier for that security.
  * They extend across gaps of at most `identity.max_symbol_gap_days` (45); a longer
    gap starts a new span.
  * Spans merge only with spans backed by the **same evidence**, so an inferred
    extension never looks observed.
* `security_id = "SEC-" + uuid5(exchange : basis : key)`. The key is the ISIN that
  created the security, or `symbol:date` for pre-ISIN securities. It is never
  reassigned.

## Rules

For rows with an ISIN:

1. **Known ISIN** → that security. A new symbol is a symbol change (new SYMBOL span);
   a new series (EQ ↔ BE) is a new SERIES span. Neither creates a security.
2. **Override** `link_isin` → the security holding the target ISIN. If the target is
   unknown yet, the row waits (quarantine) rather than guessing.
3. **Same-issuer rule** (NSE policy): an Indian ISIN's first 9 characters identify the
   issuer and security type; re-issues (e.g. after a face-value split) change only
   the serial. A new ISIN links to an existing security only when:
   * the issuer and type match, **and**
   * symbol evidence connects them: the same symbol within the gap, or an NSE
     symbol-change notice between the two symbols dated within the gap.

   Several candidates → `IDENTITY_CONFLICT`.
4. **Otherwise a new security**, unless another security holds this symbol on this
   same day (a contradiction) → `IDENTITY_CONFLICT`.

For rows **without an ISIN** (NSE legacy files before ~2011-2015):

5. The single security holding this symbol within the gap → that security
   (evidence `SYMBOL_CONTINUITY`).
6. Else an NSE symbol-change notice links it to the single holder of the other symbol.
7. Else a new security with `identity_basis = SYMBOL_CONTINUITY`. This is a weaker
   identity, flagged in the master so consumers can treat it accordingly.

Several candidates → `AMBIGUOUS_IDENTITY`.

**Anchoring.** A no-ISIN row is resolved only when every session after it, up to the
next ISIN-bearing session, has been ingested. Otherwise it is quarantined as
`UNRESOLVED_IDENTITY` and retried (`reprocess-pending`, or any later backfill).
Without this, backfilling the pre-ISIN era first would create a second security for
every company, and IDs can never be merged afterwards. Backfills process dates
**newest first**, so a full-range run anchors in one pass.

**Per-day checks.**
* Two rows resolving to one security → all of them `DUPLICATE_SECURITY_DATE`.
* One symbol claimed by two securities → all of them `IDENTITY_CONFLICT`.
* Neither case is ever resolved by picking one.

**Real example** (NSE samples): 3i Infotech traded as `3IINFOTECH` / `INE748C01020`
(2015–2020) and later as `3IINFOLTD` / `INE748C01038`. Both identifiers changed.
The same-issuer rule links them only if NSE's symbol-change list connects the two
symbols near the change date; without that notice they remain two securities.
Continuity is lost, but nothing is falsely merged.

## EQ / BE

Series is trading information, recorded as SERIES spans. A security moving EQ → BE → EQ
keeps one `security_id`. The same ISIN in both series on one day is a duplicate and is
quarantined.

## Listing status

| Status | Meaning |
|---|---|
| `ACTIVE` | Traded within `active_within_sessions` (20) sessions of the latest ingested session |
| `INACTIVE` | Not traded recently |
| `DELISTED` | Only from evidence: a reviewed override now, exchange delisting data from Milestone 3 |
| `UNKNOWN` | Not yet evaluated |

Absence from a file never implies delisting.

## Overrides

`config/identity/nse.toml` (versioned, reviewed) supports three entries:

* `link_isin`: two ISINs are the same security.
* `distinct_isin`: never auto-link this ISIN.
* `status`: a listing status with an effective date.

The overrides file's hash is part of every ingestion manifest. Changing it makes
affected dates reprocess.

## Determinism

Resolution is a pure function of master state, the day's rows, notices, overrides and
config. Rows are processed in a fixed order, so file row order does not matter. A
property test checks the invariants over arbitrary histories in either direction:
every row assigned or quarantined, one row per security per day, one security per
symbol per day, `valid_from ≤ valid_to`, and one security per ISIN.
