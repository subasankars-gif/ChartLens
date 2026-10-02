# ADR-0005: Corporate-action price adjustment policy

**Status:** Accepted · 2026-09-30 · Method details (formulas, validation, storage, the
`OTHER` class) superseded by [ADR-0011](0011-corporate-actions-and-adjustment.md); the
policy below stands

## Decision

* **Raw prices are stored and never modified.** Adjusted series are derived:
  `adjusted = raw × cumulative_factor(security, date)`, with volume divided by the
  same factor.
* Factors are computed from parsed corporate actions plus a manual override table,
  and stored under `metadata/adjustments/` with an `adjustment_version`.
* Defaults (in `config/chartlens.toml`, part of the methodology hash):

| Action | Adjusted by default | Method |
|---|---|---|
| Split | Yes | new face value ÷ old face value (₹10 → ₹2 gives 0.2) |
| Bonus | Yes | a new shares per b held → b ÷ (a + b) (1:1 gives 0.5) |
| Rights | Yes | Theoretical ex-rights price (TERP) factor |
| Dividend | **No** | Charts show prices as traded |

* The engine operates on adjusted prices. Adjusted and raw series are never mixed in
  one computation (spec §11).
* Corporate-action descriptions are free text at the source. The original text is
  kept on every parsed record; anything the parser cannot interpret is recorded as
  `OTHER` and surfaced by data quality, never guessed.

## Point-in-time caveat

Back-adjusted history embeds corporate actions that happened *later*. Ratios,
returns and indicators are unaffected, but **absolute-price conditions are not**:
a backtest filter such as "price > ₹20" evaluated on adjusted prices uses future
information. Such filters must use raw prices. (Turnover, price × volume, is
unaffected by split/bonus adjustment.)

## Consequences

* A new split or bonus changes the adjusted history of that security, so its derived
  data and analysis are recomputed and the `data_version` changes.
* Price jumps that no corporate action explains are flagged
  (`data_quality.max_unexplained_move`) rather than silently absorbed.
