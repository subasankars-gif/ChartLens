# ADR-0011: Corporate actions, exact factors and the adjusted analytical dataset

**Status:** Accepted · 2026-10-01 · Supersedes the method details of ADR-0005 (its policy stands)

## Source

NSE's JSON corporate-actions API
(`/api/corporates-corporateActions?index=equities&from_date=DD-MM-YYYY&to_date=…`),
fetched in **calendar-month ex-date windows** from 2006. Probing established three things.
It answers GitHub-hosted runners. It covers ex-dates back to 2006, with sparse early
years. Each record carries `symbol`, `series`, `isin`, `comp`, free-text `subject`,
`exDate`, `recDate`, `faceVal` and `caBroadcastDate`.

* Each window's response is stored **immutably as the original bytes**
  (`raw/nse/corporate_actions/…`). A changed response is a new version, and old ones
  are kept. Windows ending within 120 days of today are always re-fetched, because
  announcements for recent ex-dates keep changing. Older windows are fetched once
  unless `--refetch` is given.
* Every later stage reads only stored versions. The latest version of each window is
  used, and records are de-duplicated by the hash of their JSON. The whole chain is
  therefore reproducible without the network.

Facts that shaped the design:

* **`faceVal` is the *current* face value**, not the value at the ex-date. The face
  value in force before an event is reconstructed backwards: undo every later
  split/consolidation from today's value. If an unquantified capital change follows
  the event, that face value is *unknown*.
* **Bhavcopy `PREVCLOSE` is not adjusted on ex-dates.** Validation therefore compares
  the last close before the ex-date with the first open on or after it, never
  PREVCLOSE.

## Interpretation (exchange grammar → neutral model)

`providers/nse/ca_subjects.py` (`nse_ca_subject_v1`) turns `subject` text into
components: SPLIT, CONSOLIDATION, BONUS and RIGHTS. It was built from the full feed
(41,293 records, 828 distinct price-relevant subjects), not from memory. Every record
gets one class:

| Class | Meaning | Adjusted? |
|---|---|---|
| `EQUITY_ADJUSTMENT` | Quantified split, consolidation, equity bonus, or rights with a price | Yes, after validation |
| `UNQUANTIFIED` | Demerger, scheme, amalgamation, capital reduction, non-equity bonus, unpriced or composite rights | **Never**: hard discontinuity |
| `CASH_DISTRIBUTION` | Dividends, interest | No (ADR-0005) |
| `NO_PRICE_EFFECT` | Meetings, buybacks, record dates | No |
| `UNRECOGNISED` | Nothing could be read | No; surfaced for review |

The precedence rules are these. If an unquantified term appears anywhere, it wins.
"Spl" counts as a split only when it is followed by face values from → to; otherwise
it is a special dividend. Wording that is price-relevant but unreadable is
`UNRECOGNISED`, never guessed.

## Resolution to a security

ISIN is tried first. A symbol is used only when the record's ISIN is unknown, and it
must be held by exactly one security within the symbol gap on the ex-date. If the ISIN
and the symbol disagree, the result is `CONFLICT`, and nothing is attached. Series
outside the universe are `OUT_OF_UNIVERSE`.

There is one evidence-based exception, `STALE_ISIN`. NSE's feed sometimes carries an
issuer's *earlier* ISIN. This happens like `faceVal`, which is always the current value.
Examples are SUMEETINDS (split 2025-10-03) and TATAMTRDVR (rights 2015). Such a record
belongs to the live security when all three of these hold:
- the ISIN's own security was not trading within the symbol gap of the ex-date;
- exactly one security held the symbol then;
- that security has an ISIN of the same issuer under the exchange's identity policy.

Price validation still applies.

## Exact factors

A factor multiplies prices dated **before** the ex-date. Volumes are multiplied by
1/factor. All factors are `Fraction`s:

| Component | Factor |
|---|---|
| Split / consolidation | fv_to / fv_from |
| Bonus a:b (a new for b held) | b / (a + b) |
| Rights a:b at price P, cum close C | TERP / C, with TERP = (b·C + a·P)/(a + b). P is the issue price, or the face value at the time plus the premium. If P ≥ C the factor is 1. |

Components sharing an ex-date multiply together. Records that state *different* terms
for the same security and ex-date are `CONFLICTING_RECORDS`: no factor is applied and
the date is a hard break.

**Adjusted value** = raw × cumulative fraction, rounded **once**, half-even, using
exact integer arithmetic. Prices are rounded to 6 dp and volumes to 4 dp. Nothing is
rounded twice. The as-of rule is that analysis as of T applies only events with
`ex_date ≤ T`. Events after the latest ingested session are `PENDING`.

## Validation: required for every applied adjustment

Let `raw` = ln(first open on/after ex-date ÷ last close before it), and
`residual = raw − ln(factor)`. The tolerance is
`max(0.15, 5 × robust σ)`, where σ is the MAD of the 250 previous overnight gaps
(point-in-time; other event sessions are excluded).

| Status | Condition | Applied |
|---|---|---|
| `VERIFIED` | \|residual\| < \|raw\| and \|residual\| ≤ tol | Yes |
| `CONSISTENT` | \|ln f\| ≤ tol: the factor is smaller than the stock's normal overnight noise, so prices cannot confirm it; and \|residual\| ≤ min(tol, ln 1.25) | Yes |
| `SUSPECT` | \|residual\| < \|raw\|, \|residual\| > tol | Yes, flagged |
| `REJECTED_BY_PRICE` | \|residual\| ≥ \|raw\|: the factor would not shrink the gap | No |
| `NO_ADJUSTMENT` | factor = 1 | — |
| `NOT_APPLICABLE` | no trades on both sides of the ex-date | — |
| `UNQUANTIFIED` / `CONFLICTING_RECORDS` | no factor | No; hard break |

* `VERIFIED` requires an *informative* factor: \|ln f\| > tol. Small rights issues and
  small bonuses are usually below the noise floor. Rejecting them for failing to shrink
  a gap that is pure noise would drop real, sourced events. They are applied as
  `CONSISTENT` only if the gap after applying them stays within noise and below a large
  gap, so they can never create one. On the first 20-year run this rule moved about 95
  events from `REJECTED_BY_PRICE` to `CONSISTENT`.
* If a rejected, informative factor fits the session before or after the ex-date, the
  event is *reported* as a possible ex-date error. It is **never moved automatically**.
* Factors whose discontinuity falls on the same session (for example, two ex-dates
  with no trade between them) are validated **jointly**. Otherwise each could pass
  alone and the two together would overshoot.
* A rejected factor that leaves a gap larger than the tolerance at its ex-date is a
  continuity break of unknown cause.

The agreed requirements map onto this design as follows:

1. **Source:** an identifiable source, either feed record keys or a reviewed override.
2. **Formula:** a deterministic formula, using `Fraction`s with a single rounding.
3. **Ex-date:** the ex-date is checked against prices.
4. **Reproducibility:** results are reproducible through `adjustment_version`.
5. **Consistency:** the event is consistent with prices, or it is marked `SUSPECT`.
6. **No new discontinuity:** this is a hard requirement (below).
7. **Unquantified events:** these are never adjusted.

## Reviewed overrides: `config/corporate_actions/nse.toml`

* `[[factor]]` gives an ISIN, an ex-date, a factor and evidence, plus `reviewed_by`.
  The factor comes **only from a primary document**. `factor = "1/1"` records a
  reviewed *no price effect*, for example the acquirer in an amalgamation; this removes
  the break. An overridden factor is still validated against prices.
* `[[suppress]]` gives a record key and a reason, and removes a feed record judged
  wrong.
* The file's hash is part of `adjustment_version`. An override whose ISIN does not
  resolve to exactly one security blocks publication.

**No factor is ever derived from a price gap.** An unexplained observation is never
converted into an assumed fact. One example is 3i Infotech (ADR-0013): the broker's
1/10 figure is not used.

## Adjusted dataset and the hard requirement

```
curated/adjusted/exchange=NSE/{security_id}.parquet   raw OHLCV + factors (text) + adj_* +
                                                       next_factor_date + break_before + source hash
curated/adjusted/exchange=NSE/_manifest.json          adjustment_version, data_end, file hashes
metadata/corporate_actions/nse/actions.parquet        every record: class, components, resolution
metadata/adjustments/nse/events.parquet               every decision with inputs, gap, residual, notes
metadata/adjustments/nse/report.json                  counts + market-wide discontinuity report
```

* Raw, unadjusted canonical, adjusted and evidence/override data are each separately
  traceable. The canonical dataset is read and never modified.
* The market-wide discontinuity report counts large overnight gaps, defined as
  \|open / previous close − 1\| > `adjustment.gap_report_threshold` (0.25). It reports:
  - X, the count before adjustment;
  - the count removed by applied actions;
  - Y, the count after adjustment;
  - **new gaps introduced** and **gaps worsened**;
  - what remains, split into unquantified events, rejected or suspect events, cash
    ex-dates, and **unexplained** (Z).
* **The manifest is written only if new = 0, worsened = 0 and every override
  resolves.** Otherwise the report is written, the CLI exits 5, and the previously
  published version stays current.
* `adjustment_version` = sha of these inputs:
  - engine version;
  - parser and grammar versions;
  - adjustment and universe config;
  - override hash;
  - feed source hashes;
  - identity version;
  - schema.

  An unchanged input set rewrites nothing.

`break_before` marks breaks caused by corporate-action events only. The authoritative
`usable_from` comes from data quality (ADR-0012).

## Consequences

* A new or changed feed record creates a new `adjustment_version`, and every
  security's adjusted series is re-derived. This takes minutes; files whose bytes
  are unchanged are not rewritten.
* Unquantified events (demergers, schemes) truncate `usable_from`. Restoring
  continuity needs a reviewed override with primary-document evidence.
