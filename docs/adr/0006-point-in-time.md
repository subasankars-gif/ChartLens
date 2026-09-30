# ADR-0006: Point-in-time (`as_of`) enforcement

**Status:** Accepted · 2026-09-30

## Context

Phase 6 (historical validation) is only meaningful if nothing computed "as of"
a date used information from after it (spec §45–46). Retrofitting this into an
engine built without it would mean a rewrite.

## Decision

1. **Every analysis takes an explicit `as_of`.** `AnalysisContext` carries it; the
   engine never reads the clock (enforced by `tests/test_layer_boundaries.py`).
2. **One place cuts history:** `chartlens_core.asof.slice_as_of`, called when data is
   loaded for a run.
3. **Everything downstream checks, never trims:** `ensure_as_of` raises
   `AsOfViolation` on any row dated after `as_of`. Silently dropping future rows
   would hide the caller's bug.
4. **Analyzers cannot bypass the check.** They are invoked through
   `chartlens_engine.interfaces.run_analyzer`, which validates the bar frame and
   enforces `as_of` before the analyzer receives any data.
5. **Confirmation only on closed bars:** bars with `is_complete = false` never decide
   a confirmation, breakout or status change (ADR-0004).

## What the guard does not catch

`ensure_as_of` stops future *rows*. It cannot see future information baked into
past rows. Known cases and their handling:

| Case | Handling |
|---|---|
| A stored weekly bar completed after `as_of` | Historical runs rebuild weekly bars from daily bars `<= as_of` |
| Back-adjusted prices embed later corporate actions | Absolute-price filters use raw prices (ADR-0005) |
| Security master includes securities listed after `as_of` | Universe for a historical run is filtered by listing/first-session date `<= as_of` |
| Statistics computed over periods that include the test date | Phase 6 uses walk-forward windows; defined in its own ADR |
