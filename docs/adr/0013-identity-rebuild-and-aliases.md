# ADR-0013: Identity rebuild, aliases and reviewed identity links

**Status:** Accepted · 2026-10-01 · Extends ADR-0009

## Problem

Identity inputs change over time. They are the identity and universe configuration,
the reviewed overrides in `config/identity/nse.toml`, and NSE's symbol-change snapshot.
These inputs are pinned to the security master in `identity_state.json`, and
incremental ingestion refuses to run on changed inputs (`IdentityInputsChanged`, exit
4). A changed input must therefore have a way to take effect.

Universe fields that only select what is analysed — `analytical_instrument_types`
(ADR-0012) — are not identity inputs and are excluded from the fingerprint
(`NON_IDENTITY_UNIVERSE_FIELDS`). Adding that field on 2026-10-02 changed the config
fingerprint (45d29f6c9df5 → 87be61584755) and refused the first `daily` run on `main`,
although no row's security could change. With the exclusion the fingerprint is
45d29f6c9df5 again, matching the lake. A test requires every `UniverseConfig` field to be
classified as one or the other.

## Decision

`chartlens-pipeline identity-rebuild` re-resolves **every stored session from its
original bytes** with the current inputs. It starts from an empty master, adopts the
latest stored symbol-change snapshot, rewrites the canonical daily files and manifests,
and saves the master with the new pinned inputs. Raw data is never touched, and the
rebuild is deterministic.

* **Crash safety.** The identity state is first marked `rebuild_in_progress`.
  Ingestion refuses to run on that mark, so canonical rows can never mix two identity
  states. The mark clears when the new master is saved. If the rebuild was
  interrupted, re-running it recovers.
* **Immutable IDs.** An ID the new master no longer contains is never reused. It is
  recorded in `metadata/security_master/nse/aliases.parquet` as
  `retired_security_id → security_id`:
  - when the retired ID's ISINs (or, failing that, its symbols at its own dates) lead
    to exactly one new security, the alias points to it;
  - otherwise the retired ID gets `AMBIGUOUS` or `NO_SUCCESSOR`, with its candidates
    listed.

  Earlier aliases follow a chain to the latest survivor.
* **Audit record.** Each rebuild writes `rebuilds/{id}.json`. It holds the old and new
  inputs, the counts, the aliases, the merges, the new IDs and any errors.

## Reviewed links that join identity but not prices

The override fingerprint pinned as an identity input covers the *decisions*: links,
distinct ISINs and statuses. Editing evidence text or comments does not require a
rebuild. Data quality quotes the whole file, so it versions by the file hash.

A `[[link_isin]]` entry may declare `price_continuity = "break"`. The link makes the
two ISINs one security, so the history, the watch-lists and the identity are
continuous. But the prices across the change are **not** comparable, so data quality
starts `usable_from` at the first session under the linked ISIN
(`REVIEWED_LINK_PRICE_BREAK`). No price factor is created by an identity link.

**3i Infotech**, decided 2026-10-01:

| Field | Value |
|---|---|
| Old identifiers | `3IINFOTECH` / `INE748C01020`, last traded 2021-08-27 |
| Feed record | **None**: the NSE corporate-action feed (2006–2026) has no record for this change, so no factor can exist |
| New identifiers | `3IINFOLTD` / `INE748C01038`, first traded 2021-10-22 |
| Link | Approved |
| Price factor | None (the broker's 1/10 figure is not used) |
| Continuity | Hard discontinuity |
| `usable_from` | 2021-10-22 |

## Consequences

* Approving an identity override is a two-step change: edit the TOML, then run
  `identity-rebuild` followed by `adjust` and `data-quality`. A full rebuild re-parses
  about 5,100 sessions. That is minutes to tens of minutes, against about 3 hours for
  the first download.
* Downstream references (watch-lists, saved analyses) resolve retired IDs through the
  alias table.
