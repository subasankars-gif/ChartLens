# ADR-0003: Immutable internal security IDs with identifier history

**Status:** Accepted · 2026-09-30

## Context

Neither ticker symbols nor ISINs are stable identifiers in India:

* symbols change (renames, restructurings);
* a face-value split typically issues a **new ISIN**;
* securities move between series (EQ ↔ BE) without becoming different securities.

Keying history on any of these would break a chart or a backtest at exactly the
moments that matter most.

## Decision

* Every security gets an internal, immutable `security_id`, assigned once by the
  security master and never reused.
* An identifier-history table maps external identifiers to it over time:

  ```
  security_id | id_type (SYMBOL | ISIN | SERIES) | value | valid_from | valid_to
  ```

* Lookups by symbol or ISIN are always *as of a date*.
* The security master is built from the exchange's own daily files, so securities
  that were later delisted are included automatically (no survivorship bias).
* Universe for V1: series **EQ and BE**. A security whose series changes remains one
  security.

## Consequences

* Linking two identifiers to one security (a rename, a new ISIN after a split) needs
  evidence: a corporate-action record or a manual override. Unlinked cases are
  surfaced by data quality rather than guessed.
* The engine only ever sees `security_id`.
