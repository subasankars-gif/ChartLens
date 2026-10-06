# ADR-0022: Classical patterns: candidates, geometry, identity, status

**Status:** Accepted · 2026-10-02. This rewrite replaces the first version accepted
with K5 and K8. Suba reviewed the rule table on 2026-10-02 and approved it with the
amendments recorded in §8.

These are layers G (candidates) and H (validation and status) of ADR-0019. A pattern is
never a visual likeness. It exists only when the measurable rules below hold on swings
and bars that were known at the time.

Every number is a default in `[analysis.patterns]`, covered by
`analysis_methodology_hash`. Golden fixtures pin each rule.

## 1. Principles

1. **Broad candidates, then definitions.** Every swing sequence of the right shape
   becomes a candidate. A candidate is dropped only when a **geometric** rule of its
   definition fails, and the failed rule is kept as a diagnostic for the near-miss tests
   and for later historical validation. Nothing is dropped for looking unattractive.
   Context never creates or deletes a pattern: it only changes confidence components.
2. **Consume, never re-derive.** Patterns read:
   - swings (the primary swings, plus one configured finer sensitivity for flags,
     pennants and handles; see §6);
   - structure's trend history;
   - levels with their role histories (existence, not the current zones);
   - Fibonacci, divergence, volume and volatility evidence;
   - the indicator series.

   Patterns never find a pivot, label a swing or judge a break of structure. Breakout
   events (§5) are derived from level role changes and pattern confirmations; they are
   never judged twice.
3. **Causal.** Every pattern has `known_at`. Every status change is dated by the
   complete bar that caused it. A pattern is prefix-stable like every other layer. The
   forming week never causes a transition. A non-regular-session close makes the
   transition `provisional` (ADR-0015).
4. **Existence, not relevance** (ADR-0021). The engine keeps every pattern that exists.
   Which patterns form the current picture is a separate, configured relevance rule
   (§4), never a serving decision.

## 2. Identity and evolution

- **Identity** is fixed by the pattern's **defining swings**, never by the date it was
  detected:

  `pattern_id = security + segment + pattern_type + ordered defining swing ids`

  It is written as `{segment}:PAT:{TYPE}:{date₁}:{date₂}:…`, using the defining swings'
  bar dates.
- **No new pattern every week.** A pattern is created once, at its `known_at`. From then
  on the same object evolves:
  - status entries are appended;
  - touches are added (the later swings within the tolerance of its lines or levels,
    each with its own `known_at`);
  - evidence references are added.
- **Geometry is fixed when the pattern is known.** Levels and lines are computed from
  the defining swings only, and later touches never refit them. So the confirmation and
  invalidation levels a pattern is judged against never move after the fact, and a run
  as of any T sees exactly the geometry the full run shows.
- **A different shape is a different pattern.** When later swings suggest different
  geometry, that is a different candidate with different defining swings. §3 decides
  whether it is a separate formation or the same one.
- **The same formation is never reported twice.** When a new candidate of the same type
  and direction becomes known, it is the same formation as an existing pattern when
  both of these hold:
  - the existing pattern is still FORMING (or became known on the same bar);
  - every defining swing of the new candidate is either a defining swing of the
    existing pattern or a touch of it known by then, and at least two are shared.

  The new candidate is then not created; its swings are recorded as touches of the
  existing pattern. The decision uses only what is known at the new candidate's
  `known_at` (first known wins). It is causal, like trendline de-duplication.

## 3. Status machine

```text
            ┌── CONFIRMED ──┬── FAILED      (within fail_window)
FORMING ────┤               ├── COMPLETED   (within completion_window)
            │               └── (stays CONFIRMED: a historical fact)
            ├── INVALIDATED
            └── EXPIRED
```

| Transition | Rule | Meaning |
|---|---|---|
| → FORMING | At `known_at`, the geometry is valid | The definition is met; no confirmation yet |
| FORMING → CONFIRMED | The pattern's confirmation rule (the common breakout rule, §5, unless stated) on a complete bar | The breakout the definition requires happened |
| FORMING → INVALIDATED | A complete close beyond the pattern's invalidation level, or the opposite boundary breaking (line patterns) | The geometric or structural premise was explicitly broken |
| FORMING → EXPIRED | Still FORMING after the pattern's own `max_wait_bars` from `known_at`, or its geometry running out (a triangle's or wedge's apex) | The permitted observation window ended without confirmation. **Not** proof the pattern was wrong |
| CONFIRMED → FAILED | A complete close back beyond the confirmation level by ≥ `breakout_atr` × ATR within `fail_window` bars | The breakout did not hold |
| CONFIRMED → COMPLETED | A complete bar's high (low) reaches the measured-move zone within `completion_window` bars | The measured move happened |

- **Ordering within one bar:**
  - before confirmation, confirmation and invalidation are judged first, then expiry;
  - after confirmation, if a bar both reaches the zone and closes back through the
    level, it is FAILED (the close is the bar's final state).
- **First judgement (same-bar rule).** On the `known_at` bar, the engine evaluates the
  newly created pattern against all geometry and confirmation levels that are computable
  from information available by the close of that bar. It never uses anything from a
  later bar.
  - For many patterns the confirmation level itself only becomes computable when the
    last defining swing becomes known, so it cannot be "known before" that bar.
  - A pattern whose close at `known_at` is already beyond its confirmation level (by the
    buffer) is CONFIRMED on that bar, with reason `RECOGNISED_AFTER_BREAKOUT`.
  - The invariant is `confirmation_date ≥ known_at`, never earlier.
- **Terminal statuses:** INVALIDATED, EXPIRED, FAILED and COMPLETED. A CONFIRMED pattern
  that neither fails nor completes within its windows stays CONFIRMED, as a historical
  fact. How recent it is, is relevance.
- **Each status entry** carries the status, the date, `provisional`, a reason code, the
  measured values (the close, the level, the buffer, the ATR) and evidence references.
  The CONFIRMED entry also carries:
  - the breakout bar's RVOL, classified by the volume layer's thresholds as
    confirmation or contradiction;
  - the measured-move zone (§5).

## 4. Overlap and relevance

**Existence** keeps every pattern that passes geometry, except the same-formation
duplicates of §2. Different types that share swings coexist; for example a double
bottom L1–H1–L2 inside a triple bottom L1–H1–L2–H2–L3. That is a fact about the chart,
not a conflict.

**Relevance** (the current picture as of the state date) is configured and hashed
methodology, applied by the engine:

1. **Recent:**
   - FORMING patterns;
   - patterns whose last transition is within `report_window_bars` (52);
   - CONFIRMED patterns within their completion window.
2. **Contained:** a pattern whose defining swings are all defining swings of a more
   complex relevant pattern of the same direction is marked `contained_by` and not
   listed separately. For example, a double bottom inside a triple bottom, or a triangle
   inside a larger triangle.
3. **Capped:** at most `max_forming_per_type` (2) FORMING patterns per type, highest
   confidence first, ties to the later `known_at`.

The relevant set is published as references into the full list, which stays available.

- **Relevance never modifies a pattern.** It is a separate list of annotations, one per
  pattern:

  ```text
  relevance: pattern_id, included (true/false), reason (RECENT | CONTAINED | CAPPED | AGED_OUT), contained_by
  ```

- `contained_by` lives only in that annotation, never on the pattern object. The
  pattern itself, with its status history and geometry, is the same whether or not it
  is relevant.

## 5. Common definitions

**Notation.**

- The defining swings are named per pattern: L for a swing low, H for a swing high.
  P(x) is a swing's price, b(x) its bar index in the segment, and k(x) its `known_at`.
- bars(x, y) = b(y) − b(x).
- **ATR_D** is ATR(14) at the bar of the last defining swing. Geometry tolerances use
  it, so the geometry is fixed at `known_at`.
- **ATR_pre(t)** is ATR(14) at bar t − 1. Breakout buffers and failure thresholds at
  bar t use it, following the Phase 4 decision that an event's own range never sets its
  own threshold.
- A line's value at bar t is the straight line through two defining swings, advancing
  per bar of the segment.

**Common breakout rule** (confirmation unless stated). On a complete bar t, all of the
following hold:

- close[t] is beyond the confirmation level (or the line's value at t) by ≥
  `breakout_atr` (0.25) × ATR_pre(t);
- the bar's body points in the breakout direction (close vs open);
- close[t−1] was not already beyond the level (except for `RECOGNISED_AFTER_BREAKOUT`,
  §3).

An intrabar excursion never confirms.

**Measured-move zone (K8).**

- The pattern's height projected from the confirmation level (a line's value at the
  breakout bar), ± `mm_zone_atr` (0.5) × ATR_pre at the breakout bar.
- It is fixed in the CONFIRMED entry.
- While a pattern is FORMING, only `measured_move_height` (a fact) is published.
- It defines COMPLETED. It is not a forecast. The UI calls it "measured-move zone",
  never "target".

**Context components** (confidence only, each in [0, 1], all known by `known_at`):

| Component | Definition |
|---|---|
| `prior_move` | The move into the first defining swing, against the pattern's direction for reversals and with it for continuations. Measured over `context_lookback` (26) bars before it, as min(1, move ÷ (2 × `context_move_atr` (2.0) × ATR at that swing)). Example: bullish reversal = (highest close in the lookback − P(first)) |
| `trend_context` | Structure's trend state as of `known_at` (`state_as_of`), mapped per pattern family (table below) |
| `level_alignment` | 1 if a Level known by `known_at`, playing the pattern's role then (support for a bullish base, resistance for a bearish top), lies within `level_tol_atr` (0.5) × ATR_D of the pattern's base or top; else 0 |
| `volume_behaviour` | Pattern-specific, from the volume SMA at defining swing bars (stated per pattern) |
| `volatility_contraction` | 1 if a volatility contraction episode (ADR-0021) starts inside the pattern's span and is known by `known_at`; else 0 |
| `divergence` | 1 if a divergence in the pattern's direction, known by `known_at`, has a defining swing as its second swing; else 0 |
| `fib_depth` | For retracement shapes: 1 if the retracement is ≤ 0.5 of the prior leg, 0.5 if ≤ 0.618, else 0 |

`trend_context` mapping:

| Family | 1.0 | 0.75 | 0.5 | 0.25 |
|---|---|---|---|---|
| Bullish reversal | STRONG / WEAKENING_DOWNTREND | TRANSITION with pending UP | RANGE | uptrend states |
| Bullish continuation | STRONG_UPTREND | WEAKENING_UPTREND | RANGE / TRANSITION | downtrend states |
| Neutral (rectangle, symmetrical triangle) | 0.5 always: reported, not scored | | | |

Bearish patterns mirror these.

**Geometry component shapes** (each in [0, 1]):

- `closeness(d, tol) = 1 − d/tol`: 1 when exact, 0 at the tolerance.
- `margin(x, min) = min(1, x / (2·min))`: 0.5 at the minimum, 1 at twice it.
- `balance(r, lo, hi) = 1 − |ln r| / ln(bound)`: 1 when r = 1, 0 at the bound on r's
  side.

**Confidence (definition fit, not probability).**

- confidence = round(100 × (g·mean(geometry) + (1 − g)·mean(context))), with g =
  `geometry_weight` (0.65, ≥ 0.6).
- Equal weights within each group by default. Per-pattern weights are configurable.
- It is computed once, at `known_at`, from information known then. Geometry is fixed,
  so confidence is too.
- Evidence that arrives later (the breakout volume, a later divergence) is attached to
  status entries, never folded back into confidence.
- It is never called a probability, never computed from outcomes, and never a
  recommendation. `historical_stats` stays `null`.

**Parameters are pattern-specific.**

- `[analysis.patterns]` holds the common defaults: `breakout_atr`, `fail_window` (8),
  `completion_window` (52), `mm_zone_atr`, `context_lookback`, `context_move_atr`,
  `level_tol_atr`, `geometry_weight`, `max_forming_per_type` and `report_window_bars`.
- `[analysis.patterns.<type>]` holds each pattern's own parameters, **including its own
  `max_wait_bars`**, and may override any common default.

**Breakout events** (not patterns: no confidence, no measured move):

| Event | Rule | `known_at` |
|---|---|---|
| BREAKOUT / BREAKDOWN | A Level's role change (ADR-0021: SUPPORT → RESISTANCE is a breakdown), or a pattern's CONFIRMED entry. Recorded with the close's distance in ATR, the body direction and the RVOL | the bar |
| FALSE_BREAKOUT | The role flips back (or the pattern FAILS) within `false_window` (3) bars, with no RETEST before it | the flip-back bar |
| RETEST | Within `retest_window` (10) bars after the breakout and before any flip back, the first complete bar whose low (high) comes within `retest_tol_atr` (0.5) × ATR_pre of the level and closes on the breakout side | that bar |
| FAILED_RETEST | A flip back after a RETEST, within `retest_window` | the flip-back bar |

- **Identity:** the level or pattern id, plus the breakout bar date.
- These events read role histories and pattern statuses. They never judge a break
  themselves.

## 6. Swings used

- Every pattern uses the **primary** confirmed swings (ATR / INTERMEDIATE by default),
  except where stated below.
- Flags, pennants and cup handles are short formations, 3–12 bars. A pullback in a
  flag is usually smaller than the primary method's 3-ATR reversal, so the primary
  swings never see the flag's turns. These three therefore use the swing layer's
  `fine_sensitivity` of the primary method (default MICRO: a 1-ATR reversal).
- Patterns never detect pivots themselves (§1.2). The sensitivity is configuration
  (`[analysis.patterns] fine_sensitivity`, default MICRO), and no method is named in
  code.

## 7. Rule table

Bullish forms are shown. Bearish forms mirror every price comparison (H ↔ L, above ↔
below). Each pattern lists its parameters and defaults.

### 7.1 Double bottom / double top

`[analysis.patterns.double]`: `eq_tol_atr` 0.5, `min_sep` 4, `max_sep` 60,
`min_height_atr` 2.0, `max_wait_bars` 26.

| Field | Rule |
|---|---|
| Swing sequence | Three **consecutive** primary swings: L1, H, L2 (no primary swing between them) |
| Candidate geometry | Two lows separated by one peak |
| Tolerances | \|P(L1) − P(L2)\| ≤ `eq_tol_atr` × ATR_D |
| Separation | bars(L1, L2) ∈ [`min_sep`, `max_sep`] |
| Height | P(H) − max(P(L1), P(L2)) ≥ `min_height_atr` × ATR_D |
| Context | `prior_move` (decline into L1); `trend_context` (bullish reversal); `level_alignment` at min(L1, L2); `volume_behaviour` = 1 if the volume SMA at L2 < at L1; `divergence` on L2 |
| Confirmation | Common breakout above the neckline P(H) |
| Invalidation | Complete close < min(P(L1), P(L2)) |
| Expiry | `max_wait_bars` after `known_at` |
| Measured-move zone | P(H) + (P(H) − min(P(L1), P(L2))) ± `mm_zone_atr` × ATR_pre |
| Identity | DOUBLE_BOTTOM + (L1, H, L2) |
| `known_at` | k(L2) (the latest of the three) |
| Status transitions | Common (§3) |
| Confidence | Geometry: `closeness(|L1 − L2|, tol)`, `margin(height_atr, min_height)`. Context: the five above |
| Overlap | Same-formation duplicates cannot arise (the swings are consecutive). Coexists with a triple bottom or inverse H&S that contains it; relevance marks it `contained_by` |

### 7.2 Triple bottom / triple top

`[analysis.patterns.triple]`: `eq_tol_atr` 0.5, `min_sep` 8, `max_sep` 90,
`min_height_atr` 2.0, `max_wait_bars` 26.

| Field | Rule |
|---|---|
| Swing sequence | Five consecutive primary swings: L1, H1, L2, H2, L3 |
| Candidate geometry | Three lows at about the same price, with two peaks between them |
| Tolerances | max(lows) − min(lows) ≤ `eq_tol_atr` × ATR_D |
| Separation | bars(L1, L3) ∈ [`min_sep`, `max_sep`] |
| Height | min(P(H1), P(H2)) − max(lows) ≥ `min_height_atr` × ATR_D |
| Context | As for the double bottom; `volume_behaviour` = 1 if the volume SMA at L3 < at L1 |
| Confirmation | Common breakout above max(P(H1), P(H2)) |
| Invalidation | Complete close < min(lows) |
| Expiry | `max_wait_bars` after `known_at` |
| Measured-move zone | max(H1, H2) + (max(H1, H2) − min(lows)) ± `mm_zone_atr` × ATR_pre |
| Identity | TRIPLE_BOTTOM + (L1, H1, L2, H2, L3) |
| `known_at` | k(L3) |
| Status transitions | Common |
| Confidence | Geometry: `closeness(spread of lows, tol)`, `margin(height_atr, min_height)`, `closeness(|H1 − H2|, 2 × tol)` (similar peaks). Context: as the double bottom |
| Overlap | Contains up to two double bottoms; relevance prefers the triple. Two triples sharing swings are consecutive windows (L1..L3 and L2..L4); they are separate formations unless §2's touch rule makes them one |

### 7.3 Inverse head and shoulders / head and shoulders

`[analysis.patterns.head_shoulders]`: `head_prominence_atr` 1.0, `shoulder_tol_atr` 1.5,
`time_balance_min` 0.4, `time_balance_max` 2.5, `neckline_max_slope_atr` 0.25, `min_height_atr` 2.0,
`max_span` 104, `max_wait_bars` 26.

| Field | Rule |
|---|---|
| Swing sequence | Five consecutive primary swings: L1 (left shoulder), H1, L2 (head), H2, L3 (right shoulder) |
| Candidate geometry | Head below both shoulders; neckline through H1 and H2 |
| Tolerances | P(L2) ≤ min(P(L1), P(L3)) − `head_prominence_atr` × ATR_D; \|P(L1) − P(L3)\| ≤ `shoulder_tol_atr` × ATR_D; \|neckline slope\| ≤ `neckline_max_slope_atr` × ATR_D per bar |
| Separation | bars(L1, L2) / bars(L2, L3) ∈ [`time_balance_min`, `time_balance_max`]; bars(L1, L3) ≤ `max_span` |
| Height | Neckline at b(L2) − P(L2) ≥ `min_height_atr` × ATR_D |
| Context | `prior_move` (decline into L1); `trend_context` (bullish reversal); `level_alignment` at the head; `volume_behaviour` = 1 if the volume SMA at L3 < at L2; `divergence` on L2 or L3 |
| Confirmation | Common breakout above the neckline's value at the bar |
| Invalidation | Complete close < P(L2) (the head) |
| Expiry | `max_wait_bars` after `known_at` |
| Measured-move zone | Neckline at the breakout bar + (neckline at b(L2) − P(L2)) ± `mm_zone_atr` × ATR_pre |
| Identity | INVERSE_HEAD_SHOULDERS + (L1, H1, L2, H2, L3) |
| `known_at` | k(L3) |
| Status transitions | Common |
| Confidence | Geometry: `margin(prominence_atr, head_prominence)`, `closeness(|L1 − L3|, shoulder_tol)`, `balance(time ratio, [time_balance_min, time_balance_max])`, `closeness(|slope|, max_slope)`. Context: the five above |
| Overlap | Shares its five swings with a triple-bottom window only when the head is within `eq_tol`, and the head-prominence rule makes that impossible. Coexists with the double bottoms inside it; relevance prefers the H&S |

### 7.4 Rounding bottom / rounding top

`[analysis.patterns.rounding]`: `rim_tol_atr` 2.0, `min_span` 20, `max_span` 156,
`min_r2` 0.6, `vertex_min` 0.25, `vertex_max` 0.75, `min_depth_atr` 3.0, `low_tol_atr` 0.5,
`max_wait_bars` 26.

| Field | Rule |
|---|---|
| Swing sequence | Two primary swing highs H_a and H_b (rims), not necessarily consecutive. Every primary swing high between them is below min(P(H_a), P(H_b)) |
| Candidate geometry | Closes over b(H_a)…b(H_b) fitted by least squares to c = a·x² + b·x + c₀, with a > 0 |
| Tolerances | \|P(H_a) − P(H_b)\| ≤ `rim_tol_atr` × ATR_D; R² ≥ `min_r2`; the vertex lies in [`vertex_min`, `vertex_max`] of the span; no primary swing low between the rims has a **close** below the fitted curve **at its own bar** by more than `low_tol_atr` × ATR_D (closes against a curve fitted to closes: one price basis; geometry version 2) |
| Separation | bars(H_a, H_b) ∈ [`min_span`, `max_span`] |
| Height | Depth = min(rims) − fitted minimum ≥ `min_depth_atr` × ATR_D |
| Context | `prior_move` (decline into the vertex region, from H_a); `trend_context` (bullish reversal); `level_alignment` at the fitted minimum; `volume_behaviour` = 1 if the volume SMA at the vertex bar < at both rims; `volatility_contraction` |
| Confirmation | Common breakout above max(rims) |
| Invalidation | Complete close < fitted minimum − `low_tol_atr` × ATR_D |
| Expiry | `max_wait_bars` after `known_at` |
| Measured-move zone | max(rims) + depth ± `mm_zone_atr` × ATR_pre |
| Identity | ROUNDING_BOTTOM + (H_a, H_b) |
| `known_at` | k(H_b) (the fit uses only closes up to b(H_b), and b(H_b) ≤ k(H_b)) |
| Status transitions | Common |
| Confidence | Geometry: R² scaled from `min_r2` to 1 → [0, 1]; `closeness(|H_a − H_b|, rim_tol)`; `balance(vertex position ÷ 0.5, [vertex_min, vertex_max] ÷ 0.5)`; `margin(depth_atr, min_depth)`. Context: as listed |
| Overlap | Pairs sharing H_a with a later H_b are different formations. Relevance keeps the latest-known one per H_a. A cup & handle with the same rims coexists; relevance prefers the cup & handle |

The candidate count is O(highs × highs within `max_span`): about 10–30 pairs per
security.

### 7.5 V bottom / V top

`[analysis.patterns.v]`: `v_move_atr` 4.0, `max_drop_bars` 8, `recovery_ratio` 0.618,
`max_wait_bars` 8.

| Field | Rule |
|---|---|
| Swing sequence | Two consecutive primary swings: H0, L |
| Candidate geometry | A fast, deep drop with no intermediate primary swing |
| Tolerances | P(H0) − P(L) ≥ `v_move_atr` × ATR_D |
| Separation | bars(H0, L) ≤ `max_drop_bars` |
| Context | `prior_move` (the drop itself is the move; scored as `margin(drop_atr, v_move)`); `trend_context` (bullish reversal); `volume_behaviour` = 1 if RVOL at b(L) ≥ `rvol_expansion` (capitulation volume); `level_alignment` at L |
| Confirmation | Common breakout above P(L) + `recovery_ratio` × (P(H0) − P(L)) |
| Invalidation | Complete close < P(L) |
| Expiry | `max_wait_bars` (8) after `known_at`: a V is fast by definition |
| Measured-move zone | P(H0) ± `mm_zone_atr` × ATR_pre (a full retrace) |
| Identity | V_BOTTOM + (H0, L) |
| `known_at` | k(L) |
| Status transitions | Common |
| Confidence | Geometry: `margin(drop_atr, v_move)`, speed = 1 − (bars ÷ (`max_drop_bars` + 1)). Context: as listed |
| Overlap | Can be the H–L leg of other patterns; coexists |

### 7.6 Rectangle

`[analysis.patterns.rectangle]`: `band_tol_atr` 0.75, `min_height_atr` 2.0, `min_span` 8,
`max_span` 104, `max_wait_bars` 52.

| Field | Rule |
|---|---|
| Swing sequence | Four consecutive primary swings, alternating: H1, L1, H2, L2 or L1, H1, L2, H2 |
| Candidate geometry | Two flat boundaries: upper = max(P(H1), P(H2)), lower = min(P(L1), P(L2)) |
| Tolerances | \|P(H1) − P(H2)\| ≤ `band_tol_atr` × ATR_D; \|P(L1) − P(L2)\| ≤ `band_tol_atr` × ATR_D; no complete close outside either boundary by ≥ `breakout_atr` × ATR_pre between the first defining bar and `known_at` |
| Separation | span (first to last defining bar) ∈ [`min_span`, `max_span`] |
| Height | upper − lower ≥ `min_height_atr` × ATR_D |
| Context | `prior_move` reported in its own direction (it decides nothing: a rectangle is NEUTRAL); `trend_context` = 0.5 (neutral); `volume_behaviour` = 1 if the volume SMA at the last defining swing < at the first; `volatility_contraction` |
| Confirmation | Common breakout beyond either boundary. Direction = the side broken |
| Invalidation | None before confirmation: either side confirms |
| Expiry | `max_wait_bars` after `known_at` |
| Measured-move zone | The broken boundary ± height, ± `mm_zone_atr` × ATR_pre |
| Identity | RECTANGLE + the four defining swings |
| `known_at` | k(last defining swing) |
| Status transitions | Common, with no INVALIDATED. Direction is NEUTRAL until CONFIRMED |
| Confidence | Geometry: `closeness(|H1 − H2|, band_tol)`, `closeness(|L1 − L2|, band_tol)`, `margin(height_atr, min_height)`, touches = min(1, (touch count − 3) ÷ 3), counting touches known by `known_at` only. Context: as listed |
| Overlap | Later four-swing windows that share ≥ 2 swings and whose other swings fall within `band_tol` of the boundaries are the same formation (§2) and become touches. A window that breaks the band is a separate formation |

### 7.7 Triangles: ascending, descending, symmetrical

`[analysis.patterns.triangle]`: `flat_slope_atr` 0.02, `symmetry_ratio` 3.0,
`converge_ratio` 0.75, `apex_max_bars` 52, `fit_tol_atr` 0.5, `min_height_atr` 2.0,
`min_span` 8, `max_span` 104, `max_wait_bars` 52.

| Field | Rule |
|---|---|
| Swing sequence | Four consecutive alternating primary swings (two highs, two lows) |
| Candidate geometry | Upper line through the two highs; lower line through the two lows (exact two-point lines) |
| Tolerances | Slopes s_u and s_l per bar; "flat" means \|s\| ≤ `flat_slope_atr` × ATR_D. **Ascending:** upper flat, lower rising. **Descending:** lower flat, upper falling. **Symmetrical:** upper falling, lower rising, with \|s_u\| / \|s_l\| ∈ [1/`symmetry_ratio`, `symmetry_ratio`]. **Converging:** width at the last defining bar ≤ `converge_ratio` × width at the first, and the apex is within `apex_max_bars` after the last defining bar. No complete close outside either line by ≥ `breakout_atr` × ATR_pre inside the span |
| Separation | span ∈ [`min_span`, `max_span`] |
| Height | Width at the first defining bar ≥ `min_height_atr` × ATR_D |
| Context | Ascending: `prior_move` up into it (continuation), `trend_context` (bullish continuation). Descending: mirrored. Symmetrical: `prior_move` in its own direction, reported; `trend_context` = 0.5. All three: `volume_behaviour` (lower volume SMA at the last defining swing than at the first), `volatility_contraction` |
| Confirmation | Ascending: common breakout above the upper line's value. Descending: below the lower line. Symmetrical: either side, and the direction is set at confirmation |
| Invalidation | Ascending: a complete close below the lower line by ≥ `breakout_atr` × ATR_pre (also recorded as a BREAKDOWN event). Descending: mirrored. Symmetrical: none (either side confirms) |
| Expiry | The earlier of `max_wait_bars` after `known_at` and the apex bar (reason `APEX_REACHED`) |
| Measured-move zone | The broken line's value at the breakout bar ± width at the first defining bar, ± `mm_zone_atr` × ATR_pre |
| Identity | TRIANGLE_{ASC\|DESC\|SYM} + the four defining swings. The type is fixed at creation |
| `known_at` | k(last defining swing) |
| Status transitions | Common, with expiry including `APEX_REACHED` |
| Confidence | Geometry: `closeness` of each flat slope to 0 (or slope symmetry for symmetrical), convergence = `margin(1 − end/start width, 1 − converge_ratio)`, touches known by `known_at`. Context: as listed |
| Overlap | Triangle types are mutually exclusive by slope, and rectangles (both flat) and wedges (same sign) are exclusive too. Later windows join a formation as touches when within `fit_tol_atr` of its lines (§2) |

### 7.8 Rising wedge / falling wedge

`[analysis.patterns.wedge]`: as the triangle (`flat_slope_atr`, `converge_ratio`,
`apex_max_bars`, `fit_tol_atr`, `min_height_atr`, `min_span`, `max_span`; no symmetry
ratio), `max_wait_bars` 52. Triangles and wedges are evaluated independently on the same
four-swing windows.

| Field | Rule |
|---|---|
| Swing sequence | Four consecutive alternating primary swings |
| Candidate geometry | Both lines slope the same way, both non-flat, converging (as for triangles) |
| Tolerances | As for triangles. Rising wedge: s_u > 0, s_l > 0, s_l > s_u (converging). Falling wedge: mirrored |
| Separation | span ∈ [`min_span`, `max_span`] |
| Height | Width at the first defining bar ≥ `min_height_atr` × ATR_D |
| Context | A rising wedge is a bearish pattern: `prior_move` up into it, `trend_context` (bearish reversal), `volume_behaviour` (lower volume SMA at the end), `divergence` on the last high. Falling wedge: mirrored |
| Confirmation | Rising: common breakout below the lower line. Falling: above the upper line |
| Invalidation | A complete close beyond the opposite line by ≥ `breakout_atr` × ATR_pre |
| Expiry | The earlier of `max_wait_bars` and the apex |
| Measured-move zone | The broken line's value ± width at the first defining bar, ± `mm_zone_atr` × ATR_pre |
| Identity | WEDGE_{RISING\|FALLING} + the four defining swings |
| `known_at` | k(last defining swing) |
| Status transitions | Common |
| Confidence | Geometry: convergence, slope agreement, touches known by `known_at`. Context: as listed |
| Overlap | As for triangles |

### 7.9 Channel: moved to the levels layer

A channel is a container: two parallel trendlines, not a projection. It does not fit
FAILED or COMPLETED, and it has no measured move. It is a levels object (ADR-0021: an
ACTIVE / BROKEN channel built from the trendline machinery), and its breaks feed the
same breakout events as any level. The pattern engine does not detect channels.

### 7.10 Bull flag / bear flag

`[analysis.patterns.flag]`: `pole_atr` 3.0, `pole_max_bars` 6, `flag_min_bars` 3,
`flag_max_bars` 12, `parallel_tol_atr` 0.05, `max_retrace` 0.5, `flat_slope_atr` 0.02,
`fit_tol_atr` 0.5. Swings are at `[analysis.patterns] fine_sensitivity`. `max_wait_bars`
is derived: `flag_max_bars` from the pole's end.

| Field | Rule |
|---|---|
| Swing sequence | At `fine_sensitivity` of the primary method: L0 (pole start), H1 (pole end), then L2, H2, L3 consecutively |
| Candidate geometry | Pole = the leg L0 → H1. Flag = an upper line through H1 and H2 and a lower line through L2 and L3, near-parallel, sloping against the pole or flat |
| Tolerances | P(H1) − P(L0) ≥ `pole_atr` × ATR_D; \|s_u − s_l\| ≤ `parallel_tol_atr` × ATR_D; s_u ≤ `flat_slope_atr` × ATR_D and s_l ≤ `flat_slope_atr` × ATR_D; retrace = (P(H1) − min(P(L2), P(L3))) ÷ pole ≤ `max_retrace` |
| Separation | bars(L0, H1) ≤ `pole_max_bars`; bars(H1, L3) ∈ [`flag_min_bars`, `flag_max_bars`] |
| Height | The pole (above) |
| Context | `prior_move` = the pole; `trend_context` (bullish continuation); `volume_behaviour` = 1 if the volume SMA at L3 < at H1 (volume dries up in the flag); `fib_depth` of the retrace |
| Confirmation | Common breakout above the upper line's value |
| Invalidation | A complete close below the lower line by ≥ `breakout_atr` × ATR_pre, or below P(H1) − `max_retrace` × pole |
| Expiry | No confirmation by bar b(H1) + `flag_max_bars` |
| Measured-move zone | Upper line at the breakout bar + pole ± `mm_zone_atr` × ATR_pre |
| Identity | BULL_FLAG + (L0, H1, L2, H2, L3) at the fine sensitivity |
| `known_at` | k(L3) |
| Status transitions | Common |
| Confidence | Geometry: `margin(pole_atr, pole_atr min)`, `closeness(|s_u − s_l|, parallel_tol)`, `closeness(retrace, max_retrace)`. Context: as listed |
| Overlap | Windows sharing the same pole (L0, H1) are one formation (§2: the pole swings are shared and later swings are touches) |

### 7.11 Pennant

`[analysis.patterns.pennant]`: as the flag, plus `converge_ratio` 0.75.

| Field | Rule |
|---|---|
| Swing sequence | As the flag |
| Candidate geometry | The same pole. Then converging lines: upper falling, lower rising (a small symmetrical triangle) |
| Tolerances | As the flag's pole and retrace. Lines converge: width at b(L3) ≤ `converge_ratio` × width at b(H1) |
| Separation | As the flag |
| Context, confirmation, invalidation, expiry, measured-move zone | As the flag |
| Identity | PENNANT + the five swings |
| `known_at` | k(L3) |
| Confidence | Geometry: pole margin, convergence, retrace. Context: as the flag |
| Overlap | The same five swings cannot form both a flag and a pennant: near-parallel and converging are exclusive at these tolerances. A test pins this |

### 7.12 Cup and handle / inverse

`[analysis.patterns.cup_handle]`: `rim_tol_atr` 2.0, `cup_min_bars` 7, `cup_max_bars` 65,
`min_depth_atr` 3.0, `max_depth_ratio` 0.5, `min_r2` 0.5, `handle_min_bars` 1,
`handle_max_bars` 10, `handle_max_ratio` 0.5, `max_wait_bars` 13.

| Field | Rule |
|---|---|
| Swing sequence | **Geometry version 2 (5b-A review).** Left rim H_a: a primary swing high. Cup low L_c: the next primary swing, a low. Right rim H_b: the **first** swing high at `fine_sensitivity` after L_c that returns to the left rim's zone (P(H_b) ≥ P(H_a) − `rim_tol_atr` × ATR at H_b's bar), within `cup_max_bars` of H_a. Every primary high between the rims is below min(rims). Handle low L_h: the first swing low after H_b at `fine_sensitivity` |
| Candidate geometry | Cup = a quadratic fit of closes between the rims, with a > 0. Handle = the pullback from H_b to L_h |
| Tolerances | \|P(H_a) − P(H_b)\| ≤ `rim_tol_atr` × ATR_D; R² ≥ `min_r2`; depth ≥ `min_depth_atr` × ATR_D and ≤ `max_depth_ratio` × min(rims); handle depth P(H_b) − P(L_h) ≤ `handle_max_ratio` × cup depth; P(L_h) > cup midpoint |
| Separation | bars(H_a, H_b) ∈ [`cup_min_bars`, `cup_max_bars`]; bars(H_b, L_h) ∈ [`handle_min_bars`, `handle_max_bars`] |
| Height | Cup depth (above) |
| Context | `prior_move` up into H_a (continuation); `trend_context` (bullish continuation); `volume_behaviour` = 1 if the volume SMA at the cup bottom < at H_a; `fib_depth` of the handle |
| Confirmation | Common breakout above P(H_b) |
| Invalidation | A complete close < P(L_h). The handle must also be ≤ `handle_max_ratio` of the cup depth and above the cup midpoint to exist at all (geometry) |
| Expiry | `max_wait_bars` after `known_at` |
| Measured-move zone | P(H_b) + cup depth ± `mm_zone_atr` × ATR_pre |
| Identity | CUP_HANDLE + (H_a, L_c, H_b, L_h) |
| `known_at` | The latest of the four, in practice k(L_h): the cup and handle is known once its handle low is |
| Status transitions | Common |
| Confidence | Geometry: R² scaled, `closeness(|H_a − H_b|, rim_tol)`, `margin(depth_atr, min_depth)`, `closeness(handle ratio, handle_max_ratio)`. Context: as listed |
| Overlap | Coexists with the rounding bottom on the same rims. Relevance prefers the cup & handle |

## 8. Review decisions (Suba, 2026-10-02)

| Question | Decision |
|---|---|
| Fixed geometry | **Approved.** Defining swings set the geometry; later observations are touches, and nothing is refitted |
| `fine_sensitivity` = MICRO for flags, pennants and handles | **Approved**, as configuration. Patterns consume the swing layer; they never compute pivots |
| ATR for pattern breakouts | **ATR_pre** for pattern confirmation, failure and retest thresholds |
| Change BOS/CHoCH or level role changes to ATR_pre | **No.** The approved Phase 3/4 methodology stays as it is. A structure or level break and a pattern confirmation are deliberately different concepts |
| Channels | **Moved to the levels layer** (§7.9). Not part of the pattern engine |
| `RECOGNISED_AFTER_BREAKOUT` | **Approved**, with the same-bar wording of §3: `confirmation_date ≥ known_at` |
| EXPIRED; pattern-specific `max_wait_bars` | **Approved** |
| Identity and evolution | **Approved**: deterministic identity, first known wins, no refits |
| Relevance and containment | **Kept separate from existence**: an annotation, never a change to the pattern (§4) |
| Historical statistics | Remain `null` |
| Confidence | Definition fit only, never probability |

**Phase 5 is built in steps.** The first checkpoint (5a) is candidate generation,
geometric validation, identity, same-formation handling and FORMING patterns, with
candidate counts, overlap behaviour, identity stability and timings reported. Context,
confirmation and the rest of the status machine follow after that review.

## 9. Phase 5a implementation notes

- **Windows.** Candidates are windows of consecutive swings in bar order.
  - Rectangles, triangles and wedges each evaluate every four-swing window on their
    own.
  - Flags and pennants each evaluate every five-swing window at the fine sensitivity.
  - Rounding bottoms and cups evaluate every pair of up-swings within their span.
- **Diagnostics.** Each family reports how many candidates were generated, how many
  were valid, how many were same-formation duplicates, and the rejections counted by
  the first rule that failed. The full list of rejected candidates is kept only when the
  analyzer runs with `diagnostics` on (tests and reviews), and it never changes which
  patterns exist.
- **Touch horizon.** A later swing is a touch when it lies within the tolerance and is
  known before the horizon ends. The horizon ends at the earliest of:
  - the first complete close outside the lines by the breakout buffer;
  - the apex (triangles and wedges);
  - the waiting window (for flags and pennants: `flag_max_bars` after the pole's end).
- **"Still FORMING" in 5a.** For the same-formation rule this means "inside that
  horizon". Phase 5b replaces it with the status machine.
- **Rounding-bottom low rule (decided by Suba after 5a).**
  - The first rule compared swing **lows** with the minimum of a curve fitted to
    **closes**. Those are two price series, and a bar's low sits below its close, so
    the rule rejected every bowl: 0 of 3,592 candidates on 60 synthetic series.
  - The rule is now closes-to-closes: each interior swing week's close is compared with
    the fitted curve at that week. That gave 52 valid on the same series.
  - Dropping the rule (408) was rejected, because it would turn the definition into a
    loose shape matcher.
  - Weekly lows stay in the pattern as structural information (key points).
  - The rounding family's `geometry_version` is 2. A regression test proves a valid
    synthetic bowl, whose swing-week low sits well below its close, survives.
- **Geometry versions.** Every geometry records its family's `geometry_version`, which
  is bumped whenever that family's geometric definition changes.
- **Geometry, confirmation and status stay separate.** Geometry is a frozen object
  fixed at `known_at`. Confirmation and status (Phase 5b) are recorded only in status
  entries, so a breakout can never reshape the pattern it confirms.

## 10. Phase 5b-A: confirmation and lifecycle (approved 2026-10-06)

- **RECOGNISED_AFTER_BREAKOUT is a status of its own, not a CONFIRMED reason.**
  - A pattern whose close on its `known_at` bar is already beyond the confirmation
    level by the buffer is RECOGNISED_AFTER_BREAKOUT, never CONFIRMED.
  - CONFIRMED means ChartLens knew the pattern before the breakout bar, which therefore
    lies strictly after `known_at`.
  - Both are post-breakout states, with the same failure and completion rules.
  - Historical statistics must keep them apart.
- **One forward pass, first condition wins.**
  - Before a breakout, each bar from `known_at` checks the breakout rule, then
    invalidation, then expiry.
  - After a breakout, each later bar checks failure and completion. When both happen on
    one bar, FAILED wins.
  - Every event is the first bar that objectively satisfies its condition. Events are
    appended and never edited. At most one breakout event and one terminal event exist,
    and a terminal event is always last.
- **Thresholds use ATR_pre.** Breakout and failure buffers are `breakout_atr` × the ATR
  of the bar *before*. The breakout week's own range never sets its own threshold.
  Horizontal invalidation levels use no buffer; line invalidations use the buffer.
- **The common breakout rule is applied strictly.**
  - A prospective CONFIRMED needs a close beyond the level by the buffer, a body in the
    breakout direction, and a previous close that was not already beyond the level by
    its buffer.
  - So a bar that closes beyond the level with the wrong-coloured body uses up that
    breakout. The next bar is not "fresh", and the pattern stays FORMING until it expires
    or is invalidated.
  - A test pins this. The statistics show how often it happens.
- **Neutral patterns** (rectangle, symmetrical triangle) confirm on either side.
  - The breakout event records the direction.
  - The pattern's own `direction` and geometry are never changed.
- **Each event record** carries `status`, `effective_date`, `known_at`, `reason`,
  `measured_values`, `evidence_refs` and `methodology_version`
  (`patterns-{analyzer}/geometry-{family}`), plus `provisional`.
  - `effective_date` is the complete bar whose data satisfied the condition.
  - `known_at` is the first `as_of` that may see the event. The two are equal for weekly
    bars but are kept separate.
- **The measured move is frozen on the breakout bar.** It records `target_method`
  (LEVEL_PLUS_HEIGHT, or FULL_RETRACE for V patterns), `target_inputs` (level, height,
  direction, ATR_pre, `mm_zone_atr`), `target_low`, `target_high` and
  `target_calculated_at`. No later price ever recalculates it.
- **FORMING drives touches and same-formation.** Touches are swings known while the
  pattern is FORMING. The same-formation rule asks whether the earlier pattern was still
  FORMING. This replaces the 5a "horizon".
- **Historical replay is tested.**
  - A run as of the breakout week reproduces the full run's identity, geometry and
    breakout event, including its measured move.
  - A run as of the week before shows no breakout event: either the pattern is still
    FORMING, or, when it was recognised after the breakout, it does not exist yet.

## 11. 5b-A review amendments (Suba, 2026-10-06)

**Cup and handle, geometry version 2.**

- **Why it changed.** In version 1 the right rim had to be a *primary* swing.
  - A primary swing is confirmed only after a reversal of about 3 ATR.
  - The handle can be at most half the cup depth, and its low was the first fine swing.
  - So by the time the rim was confirmed, price had usually already broken the handle
    low. On real data 392 of 409 were invalidated, typically on the very bar that made
    them known.
- **The rule now.**
  - The right rim is the first **fine** swing high after the cup low that returns to the
    left rim's zone.
  - The left rim and the cup low stay primary.
  - The handle low is still the first fine swing after the right rim. Invalidation is
    still a close below it: the stricter rule is kept, not the cup midpoint.
- **Only this one methodology is implemented.** Regression tests show that:
  - a cup whose right rim exists only at the fine level is known with its handle and is
    FORMING then, not invalidated on recognition;
  - a handle deeper than half the cup is rejected;
  - a later close below the handle low still invalidates the pattern.

**V patterns.**

- RECOGNISED_AFTER_BREAKOUT is an intrinsic recognition limit, not a defect.
  - The V low is a primary swing confirmed after a reversal of about 3 ATR.
  - The V's confirmation level is 0.618 of a drop of at least 4 ATR.
  - So the V is often already beyond its level when it becomes knowable.
- A methodology test pins this. Swing confirmation must not be weakened to raise the
  prospective share.

**Terminal decisions are permanent.** When FAILED and COMPLETED are both true on one
bar, the pattern is FAILED. A test runs as of that bar, the next bar, and the full
history in which price later reaches the zone. All three keep the same FAILED event; no
later bar upgrades FAILED to COMPLETED.

**No success rates yet.** Lifecycle counts are behavioural observations. Success rates
belong to the later historical-validation layer, where the observation window, the
outcome definition, censoring and the methodology version can be controlled.

**Phase 5b-A is closed (Suba, 2026-10-06).** Confirmation, recognition after the
breakout, lifecycle transitions, terminal-state precedence, measured-move freezing,
event immutability and historical replay are implemented and validated. Cup and handle
geometry version 2 resolves the conflict between recognition and lifecycle. No other
pattern family changed. Recognition of V patterns after the breakout remains an
intentional property of structural confirmation.

## 12. Phase 5b-B: context, facts only (2026-10-06)

- **Context is a snapshot as of `known_at`.** It uses the bars up to `known_at` and
  every other layer's objects as they stood then: levels via `as_of(known_at)` with
  their role on that day, the current Fibonacci structures as of `known_at`, structure's
  state as of `known_at`, and divergences and volatility episodes known by then.
  - Nothing from later bars is used.
  - A run as of `known_at` reproduces every context exactly, and changing later bars
    changes none (both are tested).
  - Every cited object was known by `known_at` (tested).
- **Facts, not scores.** Context records measurements. Mapping them to the [0, 1]
  components of §5 and to a confidence is a later step (5b-C), so a change to scoring
  can never be mistaken for a change in what was observed. Context never creates,
  deletes or reshapes a pattern, and never writes geometry or status.
- **What is recorded** (`PatternContext`; `context_version` 2 after the review, §13):

| Part | Facts |
|---|---|
| Prior move | The `context_lookback` (26) bars before the first defining swing: the highest and lowest close with their dates; the rise into and the decline into the first swing, in ATR at that swing's bar; the window's high–low range in ATR (how tight the preceding consolidation was) |
| Structure | Trend state, regime, pending direction and `since`, as of `known_at`; the last BOS/CHoCH known by then; the events inside the pattern's span |
| Levels | Levels known by `known_at` within `level_tol_atr` × ATR_D of any key point, with their role as of `known_at` and their distance. Levels built from the pattern's own defining swings are excluded |
| Volume | Volume SMA at the first and last defining bars; RVOL at the last; volume state and trend at `known_at`; OBV change across the span relative to volume |
| Volatility | ATR% at the first and last defining bars and at `known_at`; contraction episodes starting inside the span and known by `known_at` |
| Divergence | `PRESENT`, `ABSENT` or `NOT_APPLICABLE` (with a reason), and each divergence in the pattern's direction, known by `known_at`, whose second swing is a defining swing, with its status as of `known_at` (§13) |
| Fibonacci | For each current structure as of `known_at`: its identity (id, anchor and counter swings, its own `known_at`), its status then, where the pattern's base sits in the leg (0 = counter swing, 1 = anchor), and the nearest Fibonacci ratio and its distance in ATR |

- **The pattern's base** is its lowest defining low (bullish) or highest defining high
  (bearish). When it has no defining swing of that type (rounding rims), the base is its
  invalidation level. For neutral patterns it is the last defining swing.
- `evidence_refs` lists every object id the context cites.

## 13. 5b-B review decisions (Suba, 2026-10-06)

Phase 5b-B is **approved as built**. These decisions are recorded before 5b-C:

| Context element | Confidence treatment |
|---|---|
| Contraction presence | **No.** Present in 61–100% of patterns, so it barely discriminates. Kept as context evidence only |
| Contraction magnitude and duration | Possible later, as part of a confidence methodology design, not now |
| Finer-swing divergence | **Not now.** A second divergence system (its own sensitivity, pivot matching, separation, qualification, availability) is a methodology layer of its own |
| Divergence for patterns that divergence cannot observe | **`NOT_APPLICABLE`, never `ABSENT`** |
| Trend state at `known_at` | Context only. It is never called the prior trend: the pattern's own move may already have changed it |
| Prior trend | To be derived from the pre-pattern state when confidence needs it |
| Fibonacci existence | **No.** A current structure exists for about 100% of patterns |
| Fibonacci position | A candidate for later confidence, without arbitrary weights now. The structure's identity is kept so its relevance to the pattern can be judged later |

**Implemented (`context_version` 2):**

- **Divergence presence.** `PRESENT` / `ABSENT` / `NOT_APPLICABLE`. Divergence is
  computed on the primary swings only, so it is not applicable when:
  - the pattern is neutral (`NEUTRAL_DIRECTION`);
  - its defining swings of the needed type (lows for bullish, highs for bearish) are all
    fine swings (`FINE_SWING_GEOMETRY`: flags, pennants);
  - it has no defining swing of that type at all (`NO_DEFINING_SWING_OF_TYPE`: a
    rounding pattern's bowl is a curve, not a swing).

  The rule is per pattern, from its swings, not per family. A cup and handle is
  applicable, because its cup low is a primary swing. `PRESENT` means at least one
  qualifying divergence is FORMING or CONFIRMED as of `known_at`. Each divergence is
  listed with its status on that day.
- **Fibonacci identity.** Each Fibonacci entry carries its anchor and counter swing
  ids and its own `known_at`.
- **Trend wording.** `StructureContext` states that it is the structure as of
  `known_at`, not the prior trend.

**Object-level `known_at` contract.** An object is eligible for a pattern's context
only if **the object's own `known_at` ≤ the pattern's `known_at`**. Old observations are
not enough. An object whose bars all precede `known_at`, but which only became knowable
later, is excluded. The contract applies to:

- swings, levels, divergences and Fibonacci structures;
- contraction episodes and structure events and states;
- volume conditions.

Bar-indexed readings are taken at bars ≤ `known_at`. In code every lookup goes through
the object's own `known_at` or an `as_of(day)` projection, never through bar dates
alone.

Tested:

- **Hand fixture.** A swing low from week 22, near both lows, confirmed only at week
  44. The pattern is known at week 41. The context does not cite the resulting level,
  and equals the context of a run without that swing.
- **Random series.** Objects built from bars before a pattern's `known_at` but known
  after it do occur, and none is ever cited.

## 14. Phase 5b-C: confidence (proposal for review, not yet accepted)

> **Confidence is how well the observed formation satisfies the formal pattern
> definition and the supporting technical evidence. It is not the probability of
> success.**

It never reads an outcome. It is never tuned to outcomes. It is never cross-tabulated
with outcomes in this phase.

**Proposed shape.**

1. **A pure function of frozen inputs.** `confidence = score(geometry, context,
   family config)`. The scorer reads no bars, no layers and no status. Geometry is
   frozen at `known_at`, and context is the `known_at` snapshot. So confidence is fixed
   at `known_at` by construction, not by discipline. A boundary test forbids the
   confidence module from importing the lifecycle or reading `status_history`.
2. **Two groups.**
   - `confidence = round(100 × (g·G + (1 − g)·E))`, with g = `geometry_weight` (0.65).
   - G is the mean of the family's geometry components (§7, unchanged).
   - E is the mean of its **applicable** evidence components.
   - Equal weights within each group. Per-pattern weights are configurable.
3. **Not applicable is excluded, never scored.** An evidence component that cannot be
   observed for this pattern is left out of E's mean, and recorded with its reason.
   It is never scored 0, and never 0.5. A flag is not penalised for divergence that the
   methodology cannot see.
4. **Fully explainable.** Each component is recorded with its name, group, value (or
   `null` with a not-applicable reason) and the context or geometry facts it was
   computed from. `confidence_version` is recorded. The LLM may explain these
   components. It never computes or adjusts them.

**Proposed evidence components (replacing §5's list):**

| Component | Proposal | Change from §5 |
|---|---|---|
| `prior_move` | As §5, from `PriorMove`. Not applicable when the prior move is part of the geometry (V drop, flag/pennant pole), and for neutral patterns | §5 counted the V drop and the flag pole twice, once in geometry and once as context |
| `prior_trend` | Structure's state as of the bar of the first defining swing, mapped with §5's table. Not applicable for neutral patterns | Replaces `trend_context`, which used the state at `known_at`. Needs one context fact added: the structure snapshot at the first defining bar (`context_version` 3) |
| `level_alignment` | As §5: a level known by `known_at`, with the pattern's role then, near the base or top | Unchanged |
| `volume_behaviour` | As stated per pattern in §7 | Needs the volume SMA at **every** defining swing as a context fact (H&S compares L2 and L3; rounding uses the vertex), not only the first and last |
| `divergence` | `PRESENT` = 1, `ABSENT` = 0, `NOT_APPLICABLE` excluded | Uses §13's three states |
| `volatility_contraction` | **Removed** (§13) | Removed |
| `fib_depth` | **Removed.** For flags and handles the retracement depth is already a geometry component (`retrace`, `handle_ratio`). Fibonacci position waits for its own design (§13) | Removed |

**Questions for review:**

1. **Neutral patterns** (rectangle, symmetrical triangle). Once contraction is removed
   and `prior_move` / `prior_trend` are not applicable, E is a single binary
   (`volume_behaviour`), and it would move confidence by 35 points. Proposal: neutral
   patterns are scored on geometry only (E reported, not scored). The alternative is to
   add `level_alignment` at both boundaries.
2. **`prior_trend` as of the first defining swing's bar.** The structure state that
   day comes only from swings known by then. The pattern's own swings are not yet
   confirmed, so it is pre-pattern. Is this the reference you want?
3. **Saturation.** `prior_move` reaches 1 at 4 ATR. The real-data median move into
   bottoms is 3.5–4 ATR, and into tops about 5, so most reversals score near 1. Keep the
   definition (a reversal needs something to reverse; it should not grade the size), or
   revisit? I would not tune it to the distribution.
4. **Name.** Keep the field `confidence`, as in the ADR? Or call it `definition_fit`
   in the API and UI, so that no reader takes it for a probability?

**Proposed tests:**

- confidence equals its value from a run as of `known_at`, and from a run whose later
  bars flip the outcome (COMPLETED ↔ FAILED);
- the module cannot import or read the lifecycle;
- every component lies in [0, 1], and the score in 0–100;
- monotonicity per geometry component (closer rims → a higher score; nothing else
  changes);
- not-applicable components never change the score;
- hand-computed golden values for one pattern per family.

**Real-data report (planned):** confidence distributions and component means per family.
No outcome cross-tabulation.

## Testing (mandatory)

**Golden fixtures**, small and readable weekly series, one per pattern and direction:

- double bottom and top;
- triple bottom and top;
- head and shoulders and the inverse;
- rounding bottom and top;
- V bottom and top;
- rectangle;
- the three triangles;
- both wedges;
- bull and bear flag;
- pennant;
- cup and handle and the inverse;
- every breakout event.

**Cases per pattern:**

- valid;
- a near miss on every geometric rule (the diagnostic names the rule);
- confirmation; `RECOGNISED_AFTER_BREAKOUT`;
- invalidation; expiry (each reason); failure; completion; a CONFIRMED pattern that
  stays CONFIRMED;
- `as_of` the bar before confirmation, which must not be CONFIRMED;
- `as_of` the confirmation bar, which must be CONFIRMED;
- a forming final week that would confirm, which must not;
- a special-session close, which must be provisional;
- a continuity break inside the span, which makes no pattern;
- identity: the same formation over many weeks keeps one `pattern_id`;
- overlap: a same-formation duplicate is suppressed (causally), and different types
  coexist;
- relevance: containment and caps.

**Also:**

- **Geometry immutability**, for every pattern:
  - the geometry from a run on the full history equals the geometry from a run on the
    prefix ending at `known_at`;
  - it equals the geometry from runs ending at any later T, as touches arrive.

  This protects the fixed-geometry decision from future refactoring.

- prefix stability (ADR-0020) over every fixture and over random series;
- causal composition: no pattern is known before its defining swings or any evidence it
  cites;
- candidate counts and timings per pattern type in the benchmark;
- real-data golden outputs for a reviewed set of securities.
