# Investigations

An investigation is a read-only study of authoritative ChartLens outputs, made to inform
a methodology decision. It changes nothing: no analytical behaviour, stored value,
published artifact or version. Its report ends at a review; any change it leads to is
a separate, explicitly approved methodology decision (normally an ADR or amendment,
with a new `analysis_version`).

## Process rules

1. An investigation reads the authoritative analysis documents and datasets, never a
   derived presentation of them (e.g. explanations), as its analytical source. (Suba's
   safeguard for Investigation 0001.)
2. **No investigation may use the existence or frequency of an undesirable output as the
   criterion for changing the methodology.** The output is evidence that something
   deserves review; it is not evidence that the definition is wrong. (Suba, 2026-10-08,
   Investigation 0001 §8.)
3. An investigation has no authority to modify the engine. No code or version change
   occurs until an investigation produces an explicitly approved methodology decision.

## Index

| # | Investigation | Status |
|---|---|---|
| [0001](0001-negative-price-levels.md) | Analytical price-level outputs outside the valid market-price domain | Closed (accepted), 2026-10-08 |
| 0001-A | Price-domain semantics — where domain status belongs (object, value, or rendering/serving) | Open, not started |
| 0001-B | Triangle geometry — the intended triangle definition, population comparison | Open, not started |
| 0001-C | Drawable line extent — geometry ≠ drawable extent ≠ confirmation evaluation domain | Open, not started |
