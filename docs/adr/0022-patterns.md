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
  - the measured-move zone (§5);
  - the breakout bar's frozen volume evidence, `breakout_bar_volume` (added by the
    5b-A amendment in §18.4).

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

## 14. Phase 5b-C: confidence (first proposal; direction approved, aggregation superseded by §15)

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

## 15. 5b-C decisions and the aggregation methodology (approved 2026-10-06; see §17)

### 15.1 Decisions (Suba, 2026-10-06)

| Question | Decision |
|---|---|
| 5b-B amendments | **Fully approved.** PRESENT requires a divergence that is live (FORMING or CONFIRMED) on the pattern's known date. Applicability is decided per pattern, not per family |
| Neutral patterns | **Shape-only is allowed.** "Confidence measures definition fit, not completeness of available evidence." Never manufacture a large contribution because one component happens to be the only one applicable, and never normalise upward because fewer components apply |
| Prior-trend reference | **Locked:** the structure state at the first defining swing, recorded as `prior_structure_state` with its as-of date. The recognition-date state is never called the prior trend |
| Prior-move cap | **Kept at 4 ATR.** It is not tuned to the NSE sample. Saturation, if it shows, is a finding about discriminatory power, not a reason to move the goalposts |
| Double counting | **Principle:** a measurable fact may contribute to the score through one component only. The same observation never gets independent weight because it has two names. The V drop and the flag or pennant pole are shape, so `prior_move` is N/A for them |
| Name | **`definition_fit`**, internally and in the API. `confidence` is not exposed. UI: "Definition fit · 87/100", with the tooltip: "Measures how closely the formation matches ChartLens's defined geometry and applicable technical evidence. It is not a probability of breakout or success." |
| Inputs | **Absolute rule:** the score comes from the frozen geometry and the context snapshot only. Prohibited: breakout outcome, terminal status, measured-move result, future bars, future volume, future divergence, future S/R changes, historical success statistics, similar-pattern outcomes |
| Audit | Each component exposes component, status, score, inputs, evidence_refs and methodology_version |
| Before code | The aggregation rule is reviewed first: why each weight exists, how N/A is handled, how double counting is prevented |

### 15.2 Aggregation methodology (proposal)

**Premise.** No weight in this score can be derived from data without using outcomes,
and outcomes are prohibited. So the weights are declared methodology, and the design
keeps the number of declared constants to one. Everything else follows from rules:

1. **One declared constant: shape is primary.** Shape carries **2/3** of the score
   when every component applies: it weighs twice all the evidence together. Shape is
   the pattern; the hard geometric rules have already decided that it exists, and the
   graded shape criteria say how cleanly. (This is ADR §5's approved 0.65, made a
   statable ratio.)
2. **Equal weights below that, because nothing justifies ranking them.** Without
   outcomes there is no basis for ranking volume above divergence, or one shape
   criterion above another. Equal weighting is the assumption-free choice:
   - **Shape** = the mean of the family's §7 shape criteria (each criterion once;
     functional forms as §5).
   - **Evidence** has three aspects of equal weight, **1/9 each**: *Prerequisite*,
     *Volume* and *Supporting*. Each aspect's leaves share its weight equally.

| Aspect (1/9 each) | Leaf (nominal weight) | Measures |
|---|---|---|
| Prerequisite | `prior_structure` (1/18) | The structure state at the first defining swing: regime and persistence |
| Prerequisite | `prior_move` (1/18) | The size of the move into the first defining swing, min(1, ATR ÷ 4) |
| Volume | `volume_behaviour` (1/9) | The family's volume characteristic from §7 |
| Supporting | `divergence` (1/18) | PRESENT 1, ABSENT 0 |
| Supporting | `level_alignment` (1/18) | 1 if a level known by `known_at`, playing the pattern's role then, is near the base or top |

3. **N/A weight returns to shape, never to other evidence.** This is the answer to
   "don't normalise upward". An applicable component always carries exactly its
   nominal weight, however many others are N/A. So:
   - when nothing but shape applies, `definition_fit` = shape (Suba's rectangle at
     87);
   - no evidence component ever gains influence because a neighbour is absent;
   - there is no renormalisation inside an aspect either. If divergence is N/A,
     level alignment stays at 1/18, and divergence's 1/18 goes to shape.

   Each component records its nominal and effective weight, so the shift to shape is
   visible. The effective weights sum to 1 (tested).
4. **Two kinds of "not applicable", and one "not available".**
   - **`NOT_APPLICABLE` / `NOT_IN_DEFINITION`:** the family's §7 row does not list
     the component. **§7 is the formal definition**, and a component outside a
     family's row never scores for that family. 5b-C adds none. For example, V and
     cup & handle do not list divergence, so it is N/A for them even though context
     records it.
   - **`NOT_APPLICABLE` / `NOT_OBSERVABLE`:** the definition lists it, but the
     methodology cannot see it for this pattern. Examples: divergence for fine-swing
     geometry (§13); `prior_move` where the move is shape (V drop, flag and pennant
     pole); prerequisite components for neutral patterns.
   - **`UNAVAILABLE`:** it applies and could be observed, but the history is
     insufficient (no structure state yet at the first swing because of warm-up; no
     bars before the first swing). It **scores 0 and keeps its weight**. A required
     criterion that cannot be shown is not met; dropping it would raise the score of
     patterns with less history, which is normalising upward by the back door. (This
     is the M3 principle: never turn an unobserved fact into an assumed one.)
5. **Double counting is prevented by construction and by test.**
   - Each leaf declares the facts it reads (for example
     `context.prior_move.decline_into_atr`, `geometry.measures.drop_atr`). A test
     asserts that no fact key feeds two scored leaves of one pattern.
   - The prior-move window ends before the first defining swing, and shape
     measurements start at it. The bar ranges are disjoint.
   - `prior_structure` and `prior_move` read the same price history but measure
     different properties: regime and persistence versus magnitude. They are
     correlated but not the same observation. So they are kept as two leaves inside
     **one** aspect, and together they weigh what one aspect weighs.
   - Removed for the same reason: `fib_depth` (retracement depth is shape for flags
     and handles); `prior_move` where it is shape; contraction presence (§13, not
     discriminating).
6. **`prior_structure` is ordinal, so it is scored by evenly spaced ranks.** The
   score encodes only the order. It replaces §5's table (0.25 for the opposite trend
   becomes 0; RANGE 0.5 becomes 1/3).

| Bullish reversal (needs a prior downtrend) | Score |
|---|---|
| STRONG_DOWNTREND, WEAKENING_DOWNTREND | 1 |
| TRANSITION in a DOWN regime (pending UP: the downtrend is being challenged) | 2/3 |
| RANGE; TRANSITION in an UP regime | 1/3 |
| STRONG_UPTREND, WEAKENING_UPTREND | 0 |
| No state yet | `UNAVAILABLE` (0) |

   Bearish reversals mirror the table. Continuations use it with the trend directions
   swapped: a bullish continuation needs a prior uptrend.
7. **Global constants only.** The 2/3, the three aspects and the leaf split are the
   same for every family. Per-pattern weight overrides are removed from §5, so that
   per-family tuning cannot creep in. Which leaves apply varies by family (§7) and by
   instance (observability); the weights do not.

**Formula.** `definition_fit = round(100 × Σ effective_weight_i × score_i)`, where:

- shape's effective weight = 2/3 + Σ nominal weights of the N/A leaves;
- each applicable or `UNAVAILABLE` leaf has its nominal weight.

**Worked examples.**

- *Rectangle*, shape 0.87. The prerequisite leaves are N/A (neutral), and divergence
  and level alignment are N/A (not in its definition).
  - Volume N/A: fit = 87.
  - Volume applicable: shape weighs 8/9, so fit = 77 when volume scores 0 and 88 when
    it scores 1.
  - Volume can move a rectangle by at most 11 points, not 35.
- *Double bottom*, shape 0.80, with prior structure STRONG_DOWNTREND (1), prior move
  3.6 ATR (0.9), volume lower at L2 (1), divergence ABSENT (0) and a level near the
  lows (1):
  `2/3·0.80 + 1/18·1 + 1/18·0.9 + 1/9·1 + 1/18·0 + 1/18·1` = 0.806, so **81**.
- *Bull flag*, shape 0.70, prior structure uptrend (1), volume not drying up (0).
  Prior move (pole), divergence (fine swings) and level (not in its definition) are
  N/A, so shape weighs 5/6:
  `5/6·0.70 + 1/18·1 + 1/9·0` = 0.639, so **64**.

**Component record:**

- `component`, `aspect` (SHAPE / PREREQUISITE / VOLUME / SUPPORTING);
- `status` (APPLICABLE / NOT_APPLICABLE / UNAVAILABLE) and `reason`;
- `score` (`null` unless APPLICABLE; 0 when UNAVAILABLE);
- `nominal_weight` and `effective_weight`;
- `inputs` (fact key → value) and `evidence_refs`;
- `methodology_version`.

`DefinitionFit` holds `value` (0–100), `fit_version`, `shape_score` and the
components.

**Context v3 facts needed** (all known by `known_at`):

- `prior_structure_state`, `prior_structure_regime` and `prior_structure_pending`,
  as of the first defining swing's bar, plus that as-of date and the state's `since`;
- the volume SMA at **every** defining swing (H&S compares L2 and L3; rounding uses
  the vertex), and RVOL at the V low.

**Enforcement:**

- `score(geometry, context, config)` is a pure function. Its module may not import the
  lifecycle or read `status_history`, and it receives no bars or layers (a boundary
  test, like the layer-boundary tests).
- Replay: equal to a run as of `known_at`.
- Outcome independence: equal when later bars flip the outcome (COMPLETED ↔ FAILED).
- The effective weights sum to 1, and N/A never changes another component's
  weight.
- No fact key feeds two leaves.
- Hand-computed golden values: one pattern per family, plus the worked examples above.

**Real-data report** (planned, with no outcome cross-tabulation):

- the distribution per family;
- how often fit is shape-only, and the mean effective shape weight;
- how often each leaf is UNAVAILABLE;
- a robustness check: the rank correlation of fits when the shape share is 0.6 or 0.75
  instead of 2/3. This is reported to show the ranking does not hinge on the constant;
  it is not used to choose the constant.

**Questions for review:**

1. **Rectangle and symmetrical-triangle volume.** §7 lists diminishing volume for
   them, and classical definitions of consolidations include it. Under rule 3 it
   moves the fit by at most 11 points. Keep it applicable, or make neutral patterns
   strictly shape-only?
2. **`UNAVAILABLE` scores 0.** Agree, or would you rather show these patterns without
   a fit value?
3. **Three equal aspects**, and the 2/3 shape share as the single declared constant.
   Agree?

### 15.3 Approval (Suba, 2026-10-06)

**Approved for implementation** with these answers to §15.2's questions:

1. Rectangle and symmetrical-triangle volume stays applicable. It is part of the
   approved definition, and it now moves the fit by at most 11 points. "Does this
   formation satisfy the ChartLens definition as specified?", not "is volume
   predictive?".
2. `UNAVAILABLE` scores 0 and keeps its weight, so a young pattern never scores
   higher because it had fewer chances to fail a test. The three states are explicit
   in the API:
   - `NOT_IN_DEFINITION`: nominal 0, actual 0;
   - `NOT_APPLICABLE`: nominal w, actual 0, score `null`;
   - `UNAVAILABLE`: nominal w, actual w, score 0.
3. Shape 2/3 and the equal-weight hierarchy are approved. The aspects are fit aspects,
   not prerequisites: the pattern engine has already established the pattern.

**Locked:**

- shape = 2/3;
- the rest follows the declared equal-weight hierarchy;
- N/A weight returns to shape and is never redistributed to evidence;
- UNAVAILABLE scores 0 and keeps its weight;
- NOT_IN_DEFINITION and NOT_OBSERVABLE do not participate;
- no component double counts a fact;
- no per-family weight tuning;
- no outcome data anywhere in the scorer or in the choice of weights;
- the canonical name is `definition_fit`;
- definition fit is not a probability of success;
- the sensitivity analysis is diagnostic, never optimisation. If 0.60, 2/3 and 0.75
  rank materially differently, that is reported as a finding.

## 16. Phase 5b-C implementation (2026-10-06)

**Context v3** (`context_version` 3). All of these are known by `known_at`:

- `prior_structure`: structure's state, regime, pending direction and `since`, as of
  the first defining swing's bar (`as_of`). That swing is not confirmed yet on that
  day, so the state comes only from swings known before the formation began.
- `volume_at`: the volume SMA and RVOL at every key point, plus `VERTEX` for rounding
  patterns.
- `levels_near_base`: levels near the pattern's base, with their role that day.
- `boundary_touches_at_known`: the defining swings plus the touches known by
  `known_at`, for rectangles, triangles and wedges. It is counted with its own
  `known_at` cut-off, never from the lifecycle's FORMING window, so a breakout on the
  recognition bar cannot change it.
- A rounding pattern's base is now its fitted extreme (the bowl's vertex), where §7
  places its level alignment and volume. Before v3 it was its invalidation level.

**Scorer** (`patterns/fit.py`, `fit_version` 1):

- `score(family, pattern_type, direction, geometry_measures, geometry_widths,
  context, config)` reads no bars, no layers and no status.
- The analyzer computes the fit before it attaches any lifecycle event.
- A test pins the module's imports and the function's signature.

**Fact registry.** Every input key is registered with the underlying fact it measures,
and aliases share one fact (for example, the V drop and the bowl depth are both
`move.inside_pattern`). An unregistered input is an error. Every score checks that no
fact feeds two participating components. `prior_move` is `NOT_APPLICABLE /
MOVE_IS_SHAPE` exactly when the move §7 names for it is a fact the shape already
scores: the V drop, the flag and pennant pole, and the rounding bowl.

**Readings of §7 made in implementation** (flagged for review):

- **Rounding `prior_move`.** §7.4 defines it as the "decline into the vertex region,
  from H_a". That is the bowl's depth, which is a shape criterion, so it is
  `NOT_APPLICABLE / MOVE_IS_SHAPE` by the double-counting rule.
- **Wedge "slope agreement".** §7.8 does not define it. It is implemented as the §5
  margin of the shallower line's slope against the flat threshold: how clearly both
  lines slope the wedge's way.
- **Boundary "touches".** §7.6–7.8 count touches known by `known_at`. Implemented as
  written: min(1, (n − 3) ÷ 3), with n = defining swings + touches known by
  `known_at`. Touches arrive after recognition, so n is almost always 4 and the
  criterion is almost always 1/3. See the review report.
- **Pennant convergence.** Read from the frozen lines' widths at the pole end and the
  last flag swing (the geometry stores no width measures for pennants).

**Statuses and reasons** in use:

| Status | Reasons |
|---|---|
| `NOT_IN_DEFINITION` | `NEUTRAL_PATTERN`, `NOT_IN_FAMILY_DEFINITION` |
| `NOT_APPLICABLE` | `MOVE_IS_SHAPE`, `FINE_SWING_GEOMETRY`, `NEUTRAL_DIRECTION`, `NO_DEFINING_SWING_OF_TYPE` |
| `UNAVAILABLE` | `INSUFFICIENT_HISTORY` |

**Config.**

- `[analysis.patterns] shape_share = 2/3` replaces `geometry_weight`.
- Per-pattern weight overrides no longer exist.
- `[analysis.patterns.v] capitulation_rvol = 1.5` is §7.5's RVOL threshold.
- `PatternAnalyzer` is now version 4.

**Real-NSE diagnostics** (snapshot meta-a89cf1fbcd05; 3,191 analytical securities;
26,289 patterns; descriptive only, with no outcome used):

- **Lifecycle unchanged.** Every lifecycle path is identical to the 5b-B run.
- **Distribution.** Definition fit p10 / median / p90 = 40 / 58 / 75. Family medians
  range from 44 (V bottom) to 71 (falling wedge).
- **Sensitivity** of the ranking to a shape share of 0.60 or 0.75 instead of 2/3:
  - Spearman 0.988–0.991 overall, and ≥ 0.977 in every family;
  - top-10 % overlap 87–90 % overall, and 88–97 % by family;
  - value changes of at most 6–8 points.

  The ranking does not hinge on the constant. This was not used to choose it.
- **`UNAVAILABLE`.** Volume only, 0–2 % of a family. Prior structure and prior move
  never.
- **The binary leaves discriminate.** Volume behaviour, divergence and level alignment
  are each about 40–55 % true.
- **The prior move saturates as expected.** Half to two thirds of reversals reach the
  4-ATR cap. Kept, per §15.1.
- **Finding, the boundary "touches" criterion is a constant.** It is exactly 1/3 for
  100 % of rectangles, triangles and wedges, because touches arrive after recognition.
  It measures nothing, and it lowers those families' fit by about 7–16 points (median),
  which distorts comparison across families. Within a family the ranking is unaffected.
  Raised for review.
- **Finding, wedge shape saturates.** Without touches, a wedge's convergence and slope
  agreement are 1.0 for most wedges, so its fit is driven by evidence. The "slope
  agreement" reading (§16 above) is part of this. Raised for review.
- **Timing.** The pattern stage is about 16 ms per security, up from 11.3 ms (context
  v3 and the fit).

## 17. 5b-C review and closure (Suba, 2026-10-06)

**Decisions:**

- **Boundary touches** are `NOT_APPLICABLE / TOUCHES_AFTER_KNOWN_AT` in the
  definition fit (fit version 2). This is a temporal-observability problem, not a
  deficiency in those patterns. The touch count is still recorded in the context and
  the component's inputs. Touch detection, the touches and the geometry are unchanged.
  Shape is the mean of its observable criteria.
- **Wedge saturation** is a finding only, not a definition review: "wedge slope
  agreement has low variance under the current flat-slope threshold." The threshold is
  not tuned to the NSE distribution. If later analysis shows that wedges consistently
  discriminate poorly on geometry, that gets its own geometry-definition review.
- **The three readings of §7 are accepted:**
  - the rounding prior move is not applicable (`NOT_APPLICABLE / MOVE_IS_SHAPE`,
    which does not imply insufficient history);
  - wedge slope agreement is the shallower line's slope against the flat threshold;
  - pennant convergence comes from the frozen lines' widths.
- **Robustness is not correctness.** The sensitivity results show that the ranking is
  robust to reasonable changes of the declared constant. They do not show that the
  weighting is correct, and they are never read that way.
- **Nothing else is added to the definition fit at this stage.** Not Fibonacci depth,
  not contraction presence, not finer-swing divergence, not outcome-derived statistics.
  Each would be a separate methodology decision later.
- **Next phase: relevance**, with the same discipline. Relevance is an annotation
  explaining why a pattern deserves attention. It never modifies geometry, lifecycle or
  definition fit.

**Final real-NSE diagnostics (fit v2;** snapshot meta-a89cf1fbcd05; 3,191
securities; descriptive, no outcomes**):**

- **Unchanged.** 26,289 patterns; candidate counts and every lifecycle path are
  identical to the 5b-B run.
- **Definition fit**, p10 / p25 / median / p75 / p90 = 40 / 49 / 59 / 70 / 80.
- **Statuses:**
  - prior move: not applicable because it is shape for 8,602 patterns, not in the
    definition for 3,330 (neutral);
  - touches: not applicable for 5,391 (all boundary patterns);
  - divergence: applicable for 10,744, not in the definition for 15,545;
  - volume: unavailable for 235 (0.9 %);
  - prior structure and prior move: never unavailable.
- **Sensitivity** (shape share 0.60 / 0.75 vs 2/3):
  - Spearman 0.993 / 0.990 overall, and ≥ 0.978 in every family;
  - top-10 % overlap 93 % / 90 % overall, and 89–97 % by family;
  - top-10 % overlap among the patterns FORMING now 92 % / 90 %;
  - values change by at most 6.2 / 7.8 points.
- **Effect of the touches change** (medians):
  - rectangle 57 → 64; triangles 59–61 → 69–73; wedges 66–71 → 80–85;
  - every other family unchanged.
- **Known limitation (recorded, not changed).** Without the constant, triangle and
  wedge shape is dominated by criteria that saturate. Triangle convergence averages
  0.97, wedge convergence 0.96, wedge slope agreement 0.90, and the falling-wedge
  median shape is 1.0. These families now have the highest fits (the falling wedge
  has the highest p10, 72). Definition fit is therefore comparable **within** a family.
  Across families it is not like-for-like, and relevance should not treat it as such.
- **Timing.** The pattern stage is about 16.8 ms per security.

**Phase 5b-C: CLOSED.**

## 18. Definition fit across families; relevance (approved §18.3; closed §18.5)

### 18.1 Product and API rule (Suba, 2026-10-06)

> **`definition_fit` is an ordinal measure of conformity to the definition within a
> pattern family. It is not a universal quality score across pattern families.**

Each family has its own shape criteria, so a 1.0 on wedge convergence is not
equivalent to a 1.0 on double-bottom low equality. A falling wedge at 85 and a double
bottom at 67 do not mean the wedge is better.

- **Within a family**, `definition_fit` may be sorted and compared.
- **Across families**, it is displayed but never used to rank. No scanner view offers
  "highest definition fit" across families. The API documents the rule and offers no
  cross-family sort on it (ADR-0023 is amended when serving is built).
- **Relevance** never turns it into a hidden universal ranking.

### 18.2 What relevance is

Relevance answers one question: **why should this pattern appear in the user's
attention view?** It never answers "how likely is this pattern to work?". It replaces
§4's relevance rules once accepted. §4's existence rules are unchanged.

```text
Pattern (unchanged by relevance)        PatternRelevance (a separate annotation)
  geometry, lifecycle, context,           pattern_id, pattern_known_at
  definition_fit                          history: append-only entries
                                            effective_date (= relevance_known_at)
                                            included, reason, contained_by
                                            tags, evidence_refs, provisional
```

**Principles:**

1. **An annotation.** It never modifies pattern identity, geometry, lifecycle,
   confirmation, context or definition fit. It lives in its own list. A run with or
   without relevance produces identical `Pattern` objects (tested).
2. **Rules with explicit reasons, never a score.** Inclusion is decided by the
   lifecycle stage, recency, containment and a cap, each a named rule. There is no
   relevance number, no weighted sum, and no count of tags.
3. **No definition fit.** The relevance module does not read `definition_fit` (an
   import/name test, like the scorer's). It is shown next to a pattern, never used to
   select one. Ranking within a category is deferred to its own design, as you asked.
4. **Known at the evaluation date, then appended, never rewritten.** At each complete
   bar t, relevance is evaluated from `as_of(t)` projections only:
   - the lifecycle events known by t;
   - the context (a `known_at` snapshot);
   - structure events and levels known by t;
   - the close at t.

   An entry is appended only when (included, reason, contained_by) changes. Each entry
   is dated by the bar that caused it, and `relevance_known_at` is that bar. A pattern
   cannot become relevant in retrospect: a later breakout produces a later entry and
   never edits an earlier one. The forming week never creates an entry. An entry
   caused by a non-regular-session close is `provisional` (ADR-0015).

**Evaluation at bar t** (for patterns with `known_at` ≤ t, in this order):

1. **Stage** (from the lifecycle as of t), giving a candidate reason:

| Lifecycle as of t | Included | Reason |
|---|---|---|
| FORMING, close at t within `approach_atr` × ATR_pre(t) of the confirmation level or line, on the pattern's side (either boundary for a neutral pattern) | yes | `APPROACHING_CONFIRMATION` |
| FORMING, t − `known_at` < `new_pattern_bars` | yes | `NEWLY_RECOGNISED` |
| FORMING | yes | `FORMING` |
| CONFIRMED (or RECOGNISED_AFTER_BREAKOUT), breakout ≤ `recent_event_bars` ago | yes | `BREAKOUT_CONFIRMED` (or `RECOGNISED_AFTER_BREAKOUT`) |
| CONFIRMED, within its `completion_window` | yes | `BREAKOUT_OPEN` |
| CONFIRMED, past its `completion_window` | no | `AGED_OUT` |
| COMPLETED / FAILED / INVALIDATED / EXPIRED | no | that status |

   Rows are checked top to bottom, and the first match wins.
2. **Containment.** An included pattern whose defining swings are all defining swings
   of an included, more complex pattern (more defining swings) of the same direction is
   excluded with reason `CONTAINED` and `contained_by` set to the outermost container.
   Both patterns must be known by t. If the container stops being included, the
   contained pattern is judged on its own from that bar.
3. **Cap.** At most `max_forming_per_type` (2) included FORMING patterns per type. The
   most recently known come first, with ties broken by `pattern_id`. The rest are
   excluded with reason `CAPPED`. **Recency, not fit** (question 2).

**Tags** explain why a pattern may deserve attention. They never affect inclusion or
order, and they are never counted. Each tag is a named fact with its evidence, known
by the entry's date:

| Tag | Fact | Evidence |
|---|---|---|
| `AT_SUPPORT` / `AT_RESISTANCE` | The context has a level with that role near the base (`levels_near_base`) | level ids |
| `STRUCTURE_SHIFT` | A CHoCH in the pattern's direction, known by t, at or after the first defining bar | structure event id |
| `AGAINST_PRIOR_TREND` | Reversal family, and the prior structure is the trend it reverses | prior structure (context) |
| `DIVERGENCE` | The context's divergence is PRESENT | divergence ids |
| `BREAKOUT_VOLUME` | The CONFIRMED event's RVOL classification is confirmation | the status entry |

A test checks that stripping every tag never changes an inclusion or a reason.

**Record.** `PatternRelevance(pattern_id, pattern_known_at, history)`. Each entry
holds:

- `effective_date` (= `relevance_known_at`);
- `included`, `reason`, `contained_by`;
- `tags`, each with its `evidence_refs`;
- `evidence_refs` for the reason: the pattern, the confirmation level or line value
  and the close for APPROACHING; the status entry for lifecycle reasons; the container
  for CONTAINED; the patterns kept for CAPPED;
- `provisional`, `methodology_version`.

`as_of(day)` gives the entry in force on that day. `PatternResult.relevance` holds one
`PatternRelevance` per pattern. Pattern objects are never touched.

**Configuration** (`[analysis.patterns.relevance]`, hashed):

- `new_pattern_bars` = 4;
- `recent_event_bars` = 4;
- `approach_atr` = 1.0 (ATR_pre, as for every event threshold);
- `max_forming_per_type` = 2 (moved from `[analysis.patterns]`);
- `report_window_bars` retired (question 1).

**Tests:**

- replay: the entry in force at T from the full run equals the current entry of a run
  truncated at T;
- the history from a run to T is a prefix of the full history (append-only, never
  rewritten);
- `Pattern` objects are identical with and without relevance;
- the module never reads `definition_fit` or outcome-only fields;
- tags never change inclusion;
- a fixture for every reason: containment (double inside triple), cap order, the
  forming week, a provisional special-session entry, and a pattern that is FORMING,
  then APPROACHING, then BREAKOUT_CONFIRMED, then BREAKOUT_OPEN, then COMPLETED, with
  each entry dated by its own bar.

**Real-data report** (descriptive):

- current reasons per family;
- included patterns per security now;
- entries per pattern, and the transitions between reasons;
- timings.

**Questions for review:**

1. **Terminal patterns** leave the attention view on the terminal bar. They stay in
   the full list and the chart's history layer. I propose this rather than §4's 52-bar
   window. A failed breakout is attention-worthy, but it belongs to the breakout-event
   phase (FALSE_BREAKOUT, FAILED_RETEST), not to pattern relevance. So
   `report_window_bars` is retired.
2. **The cap orders by recency only.** Within a type, definition fit would be a
   legitimate tie-break (same family). But you deferred prioritisation within a
   category, so v1 keeps relevance completely free of fit.
3. **Defaults:** `new_pattern_bars` 4, `recent_event_bars` 4, `approach_atr` 1.0.
   These are declared and would not be tuned to data.
4. **Tags in v1:** the five above, or none until the scanner exists?

### 18.3 Approval and the exact semantics (Suba, 2026-10-06)

**Central invariant.** Changing relevance rules never changes pattern identity,
geometry, lifecycle, definition fit, or the underlying analytical objects. This is the
basis of the replay and regression tests. Relevance runs as its own stage, after the
pattern analyzer, over its finished result.

| Question | Decision |
|---|---|
| Retire the 52-bar window | **Yes** |
| A terminal pattern stays in the attention view on its terminal bar | **No.** Relevance ends when the pattern reaches a terminal lifecycle state, including on that bar. The terminal event stays visible in the history, chart and event views |
| Failed breakouts handled here | **No.** They belong to the future breakout-event layer |
| Cap by recency only | **Yes.** Order by the immutable `known_at`, then `pattern_id`, never processing order. No cross-family `relevance_score` or `fit_rank` in the API |
| `definition_fit` in relevance | **No** |
| 4-bar new window, 4-bar breakout window, 1-ATR approach | **Yes**, as declared methodology with frozen semantics (below) |
| Tags now | **Yes**, as inert explanation |
| Cross-family ranking | **No** |

**Rule hierarchy (locked).** At each complete weekly bar T, with segment bar indices:

1. Determine the lifecycle state known at T (events with `known_at` ≤ T).
2. If terminal: **not included**, reason `TERMINAL_<STATUS>` (`TERMINAL_COMPLETED`,
   `_FAILED`, `_INVALIDATED`, `_EXPIRED`). This is permanent.
3. Else, if a breakout is known and b ≤ T < b + `recent_breakout_bars`: included,
   `BREAKOUT_CONFIRMED` (or `RECOGNISED_AFTER_BREAKOUT`).
4. Else, if a breakout is known and T − b ≤ the pattern's `completion_window`:
   included, `BREAKOUT_OPEN`. Past that window: not included, `AGED_OUT`, permanent.
   (The CONFIRMED status stays as a historical fact; nothing is tracked after the
   window.)
5. Else (FORMING), if |close_T − level_T| ≤ `approaching_confirmation_atr` × ATR_T:
   included, `APPROACHING_CONFIRMATION`.
   - ATR_T is the ATR at the current complete bar, computed from data through T.
   - level_T is the confirmation level, or the confirmation line's value at T.
   - A neutral pattern uses the nearer of its two boundaries.
   - The close, level, ATR and distance are recorded.
6. Else, if k ≤ T < k + `new_pattern_bars` (k = the pattern's `known_at` bar):
   included, `NEWLY_RECOGNISED`.
7. Else: included, `FORMING`.
8. **Containment.** An included pattern contained by an included, more complex pattern
   of the same direction is **not included**, reason `CONTAINED`, with
   `container_pattern_id` (the outermost container; ties broken by `pattern_id`).
9. **Cap.** Among included FORMING-stage patterns (rules 5–7) of one type, keep the
   `max_forming_per_type` most recently known (`known_at` descending, then
   `pattern_id`). The rest are **not included**, reason `FORMING_CAP`, with
   `kept_pattern_ids`.
10. Attach the inert tags.

**Precedence is intentional.** A pattern may satisfy several predicates; only the first
matching reason is emitted. For example, a newly recognised pattern within 1 ATR of
confirmation is `APPROACHING_CONFIRMATION`, the more useful current fact. This is
tested.

**Thresholds.** All relevance thresholds are evaluated with information available at
the relevance bar, and none is tuned from historical outcomes:

- `new_pattern_bars` = 4;
- `recent_breakout_bars` = 4;
- `approaching_confirmation_atr` = 1.0;
- `max_forming_per_type` = 2.

Windows count weekly bars of the segment, not calendar days.

**Entries.** An entry is appended when the inclusion, the reason, the container, the
kept ids or the tag set changes. Tags are recorded as of the entry, so `as_of(day)`
reproduces exactly what was shown that day. A tag change alone appends an entry but can
never change an inclusion or a reason (tested).

**Implementation notes (5b-D):**

- `StructureResult.character_changes()` was added, so relevance never names
  structure's event kinds (the layer-boundary test).
- `BREAKOUT_VOLUME` reads the volume layer's own classification (`volume_state` =
  EXPANSION) at the breakout bar. **Gap found:** §3 says the CONFIRMED entry carries the
  breakout bar's RVOL classification, but 5b-A did not record it. It is raised for
  review and left unchanged here.
- **Edge case recorded:** containment runs before the cap. A contained pattern stays
  `CONTAINED` even on a bar where its container is itself excluded by the cap. The
  container is still FORMING and visible in the full list.

**Real-NSE diagnostics (5b-D;** snapshot meta-a89cf1fbcd05; 3,191 securities;
descriptive**):**

- **The central invariant holds on real data.** Patterns, candidates, every lifecycle
  path and every definition fit are identical to the 5b-C close.
- **The attention view now** holds 1,027 included patterns in 743 securities (23 %).
  The median security has none; p90 has 1 and p95 has 2. By reason:
  - `BREAKOUT_OPEN` 438;
  - `FORMING` 256;
  - `APPROACHING_CONFIRMATION` 158;
  - `NEWLY_RECOGNISED` 85;
  - `BREAKOUT_CONFIRMED` 73;
  - `RECOGNISED_AFTER_BREAKOUT` 17.

  Excluded now: `CONTAINED` 45; `AGED_OUT` 2,416; terminal 22,801.
- **History.** 3.95 entries per pattern on average (median 3, p95 8).
- **Cap.** `FORMING_CAP` occurred 8 times in total and never binds now: at 2 per type
  the cap is rarely reached. `CONTAINED` occurred 3,060 times, 1,929 of them from
  recognition.
- **Observation: `APPROACHING_CONFIRMATION` flickers.** It is an instantaneous
  1-ATR band, so a close oscillating near the level alternates it with
  FORMING / NEWLY_RECOGNISED:
  - APPROACHING → FORMING 7,388 times;
  - FORMING → APPROACHING 8,432 times;
  - APPROACHING → NEWLY_RECOGNISED 3,851 times.

  Each entry is correct and dated. 41 % of patterns are already within 1 ATR of
  confirmation when recognised (the last swing's ~3-ATR reversal often ends near the
  neckline). Recorded, not changed. Hysteresis would be a methodology change.
- **Timing.** The relevance stage takes about 5.2 ms per security; the pattern stage
  about 16.5 ms.

### 18.4 Review decisions after 5b-D (Suba, 2026-10-06)

**1. The approaching-confirmation flicker: accepted; hysteresis deferred.**

- The 1-ATR rule and the append-only history stay unchanged. The flicker is real but
  is not a correctness defect: each entry is the rule evaluated with the information
  available at that weekly close.
- It is not tuned on transition counts. A wider exit band would add a new stateful
  rule and parameter.
- The flicker, and the 41 % of patterns already approaching at recognition, are
  recorded as diagnostics (§18.3).
- When the scanner is designed, assess whether the changes are a real user-experience
  problem. If hysteresis is warranted, propose it as an explicit methodology change,
  with deterministic replay tests.
- **Presentation never rewrites analysis.** The relevance history stays exact even if
  the scanner later shows state changes less prominently.

**2. Breakout-bar volume: amend 5b-A now (an evidence-contract change only).**

ADR §3 said the breakout event carries the breakout bar's RVOL and classification, but
5b-A did not record it. The breakout event (CONFIRMED or RECOGNISED_AFTER_BREAKOUT) now
carries `breakout_bar_volume`:

- `bar_date`, `volume`;
- `baseline_bars`, `baseline_mean_volume` (the bars before the breakout bar; the bar
  is not in its own baseline);
- `rvol`;
- `classification` (EXPANSION / NORMAL / CONTRACTION) and the
  `expansion_threshold` / `contraction_threshold` it used;
- `evidence_refs` (the indicator series at that bar);
- `measurement_version`.

The rules:

- **The name is deliberate.** It is the breakout bar's frozen measurement and never
  shares a field name with any later or current volume classification.
- **Data through the breakout bar only.** It reads the indicator layer's
  `relative_volume` and `volume_state` at that bar and never reclassifies. In warm-up,
  `rvol`, the baseline mean and the classification are `None`.
- **Evidence, never a condition.** No lifecycle transition reads it. Breakout dates,
  transitions, terminal behaviour, measured moves and definition fit are unchanged.
- **Relevance consumes the record.** The `BREAKOUT_VOLUME` tag reads the event's
  recorded classification. The relevance module no longer touches volume indicators,
  and the tag stays explanation only.
- **Versioning and backfill.** `PatternAnalyzer` is version 5, because the event record
  contract changed; the events' `methodology_version` stamp says patterns-5. There is
  no backfill. No pattern event has ever been persisted: the engine exists only on the
  M8 branch, and neither production nor the API runs it. Every event, including this
  field, is recomputed from the point-in-time weekly bars and reproduced exactly by a
  run as of its date (tested). If events are ever persisted, a stored event will
  carry its own `methodology_version` and `measurement_version`, and older records
  will never be filled in with later calculations.

**Regression checks:**

- **Tests:**
  - every breakout event, and only those, carries the record, consistent with the
    indicator series and its parameters;
  - replacing every later bar, volumes included, leaves each earlier breakout event
    identical;
  - replay at the breakout bar reproduces the whole event;
  - nonsense evidence changes no status, date, reason, measured value, measured move,
    context or fit;
  - the relevance tag follows the recorded classification;
  - warm-up gives `None`.
- **Fingerprint** (`scripts/pattern_fingerprint.py`). It hashes every pattern (id,
  geometry, touches, context, definition fit, every event) and every relevance history,
  excluding only the new field, the version stamp and tag evidence refs. Old commit
  (4e08c75) vs new:
  - on synthetic data (300 securities, 2,266 patterns, 1,406 breakouts): **identical**;
  - on the published snapshot meta-a89cf1fbcd05 (3,191 securities, 26,289 patterns,
    15,719 breakout events): **identical**. Every hash matches, so pattern ids,
    geometry, touches, context, definition fit, every lifecycle event (dates,
    reasons, measured values, measured moves) and every relevance history are
    unchanged.
  - 7,240 of the 15,719 breakouts (46 %) were on an EXPANSION bar. That is the
    same classification relevance used before, now read from the record.

### 18.5 Closure (Suba, 2026-10-06)

**Phase 5b-D (relevance): CLOSED.**

- Relevance is a separate, point-in-time, replayable, append-only annotation layer.
  Patterns, geometry, lifecycle and definition fit are untouched.
- Terminal patterns leave attention permanently.
- Containment and the forming cap are explicit exclusions.
- Definition fit is entirely outside relevance, and tags are inert.
- The 1-ATR flicker is documented rather than tuned away. Relevance is not reopened
  for it unless scanner design shows an actual presentation problem.

**The 5b-A amendment is approved as an evidence-contract amendment.**

> **Invariant: an event records what the engine knew at the event bar; downstream
> layers consume the recorded evidence rather than recomputing it.**

**The 46 % high-volume finding is descriptive only.** It does not say that high-volume
breakouts are better, more reliable or more relevant. Any such question is outcome
analysis, and it belongs to later statistics work.

**Tooling note.** The first real-data comparison (probe run 37496732930) failed because
a diagnostic formatting change had dropped the volume-classification keys from the
fingerprint output. Both fingerprint jobs had completed successfully. A direct
comparison of the published fingerprints (probe run 37498737083) then confirmed they
were equal, and the diagnostic script was corrected. This did not affect analytical
outputs.

## 19. Breakout events (approved with changes in §19.1)

§5 sketched breakout events. Since then the evidence invariant (§18.5), the
lifecycle's own FAILED rule and the size of the level layer change some of its
details. This section replaces §5's breakout-event table once accepted.

**Purpose.** For every break of a level and every pattern breakout, record facts about
what followed within a fixed short window: a retest, a false breakout, a failed retest.

- Facts only: no success rates, no scores, no relevance (attention for events is a
  later, separate design).
- Breakout events never modify levels, patterns, pattern relevance or definition fit.
  The §18.4 fingerprint on patterns and relevance must stay identical.

**Sources: judged once, by their own layer.**

| Source | Breakout event created from | Direction |
|---|---|---|
| `PATTERN` | The pattern's breakout event (CONFIRMED or RECOGNISED_AFTER_BREAKOUT, ATR_pre rule) | Its breakout direction: `BREAKOUT` up, `BREAKDOWN` down |
| `LEVEL` | Each non-original role change of a Level (ADR-0021 rule: close beyond price ± `level_break_atr` × ATR, unchanged) | RESISTANCE → SUPPORT is `BREAKOUT`; SUPPORT → RESISTANCE is `BREAKDOWN` |

The two sources keep their own approved rules. The event layer never re-judges a
break, and it never harmonises the thresholds. A pattern breakout through a neckline
and the role change of the level at the same price are two events from two sources.
Grouping them is presentation, not analysis.

**Evidence: recorded at the source (the §18.5 invariant).**

- A pattern breakout already carries its evidence (close, level, buffer, ATR_pre and
  `breakout_bar_volume`).
- **Phase 4 evidence amendment (proposed).** A Level's non-original `RoleChange` would
  also record what the levels layer knew on that bar: `change_bar` (close, open,
  level, ATR, buffer, threshold) and `change_bar_volume`, the same frozen measurement
  as `breakout_bar_volume`, under its own name.
  - The shared model is renamed `BarVolumeEvidence`. The field names stay distinct.
  - It is evidence only. No role change would move. Prefix stability and the levels
    fingerprint would be checked as in §18.4.
- The breakout event copies its source's recorded evidence with a reference to it. It
  never reads indicators to rebuild it.

**Object.** `BreakoutEvent`:

- `event_id = {source_id}:{BREAKOUT|BREAKDOWN}:{bar_date}`;
- `source_type`, `source_id`, `source_event_ref`;
- `direction`, `bar_date`, `known_at` (= `bar_date`);
- `level_at_break`, `evidence` (copied), `provisional`;
- `history`: append-only follow-ups, each dated by the complete bar that caused it,
  with its own measured values and `known_at`.

**Follow-ups.** b is the break bar. The level is constant for a LEVEL; for a PATTERN it
is the confirmation level, or the line's value at t.

| Follow-up | Rule | Judged by |
|---|---|---|
| `RETEST` (non-terminal) | The first complete bar t in (b, b + `retest_window`], before any reversal, whose low (BREAKOUT) or high (BREAKDOWN) comes within `retest_tol_atr` × ATR_pre(t) of the level, or pierces it, and whose close is on the breakout side | The event layer: a new, named rule. Its measured values (low/high, close, level, tolerance) are recorded |
| `FALSE_BREAKOUT` (terminal) | The source reverses before any RETEST, within the source's reversal window. PATTERN: the pattern's own FAILED event (its `fail_window`). LEVEL: the level's next role change back, within `false_window` | The source. The event layer only reads the dated fact |
| `FAILED_RETEST` (terminal) | The source reverses after a RETEST, within `retest_window` | The source |
| `WINDOW_ENDED` (terminal) | None of the above by b + the observation window (the longer of the reversal and retest windows) | A date, not a judgement: "nothing further is tracked" |

- **A reversal after the window** is not a follow-up of this event. For a level it is
  a new role change, and so a new breakout event in the other direction.
- **The forming week** never creates a follow-up.
- **A non-regular-session bar** makes the follow-up `provisional`.

**Configuration** (`[analysis.breakouts]`, hashed and declared, never tuned from
outcomes):

- `retest_window` = 10;
- `retest_tol_atr` = 0.5;
- `false_window` = 3 (levels only).

**Tests:**

- replay (a run as of T gives the same events and histories up to T);
- prefix stability;
- the events never change levels, patterns, relevance or fit (fingerprint);
- the evidence equals the source's record (and is never recomputed);
- fixtures for each follow-up and for the window ending;
- a pattern breakout whose pattern FAILED after a retest becomes `FAILED_RETEST`;
- a level that flips back on the next bar becomes `FALSE_BREAKOUT`;
- special-session and forming-week cases.

**Scale.** On synthetic data (700 weekly bars) there are about 290 level role changes
per security, against about 9 pattern breakouts. Levels flip whenever a close crosses
them by 0.10 ATR, and every swing level stays in existence. Real NSE counts are the
first thing the build measures. Existence keeps them all; attention is the later
event-relevance design.

**Questions for review:**

1. **Level breakouts in scope now**, as events for every role change (existence,
   complete; about 290 per security on synthetic data)? Or pattern breakouts only in
   this phase, with level breakouts after real counts? I recommend both now: the role
   changes already exist as facts, and an event is a view over them plus follow-ups.
2. **Reversal windows.** PATTERN: the pattern's own FAILED (its `fail_window` 8), with
   no second window. LEVEL: `false_window` 3, as in §5. Keep them source-specific (my
   recommendation: never judged twice), or set one event-layer window for both?
3. **`WINDOW_ENDED`** as an explicit terminal record, or leave the event open with no
   further entries?
4. **The Phase 4 evidence amendment** (`RoleChange.change_bar` and `change_bar_volume`,
   evidence only): approve it as part of this phase?

### 19.1 Approval and changes (Suba, 2026-10-06)

**Invariant:**

> A breakout event is derived from an already-authoritative source event. It does not
> reinterpret or modify the source. Its lifecycle observes what happened after a source
> breakout; it never mutates, relabels or feeds back into the source pattern or level
> lifecycle. **The event layer reads the source and never writes to it.**

| Question | Decision |
|---|---|
| Pattern breakouts | Build now: `PatternBreakoutEvent` |
| Level breakouts | Build now: `LevelBreakoutEvent`, an independent source type |
| Merge coincident pattern and level events | **No.** Not even at identical prices |
| Reversal window | **Source-specific.** Pattern: the existing 8-bar pattern failure authority. Level: the 3-bar level reversal. No generic "breakout → wait N bars → decide" |
| Wording | The event layer does not "detect" a false breakout for a pattern. It references and classifies the pattern's own FAILED event; the pattern analyzer stays authoritative |
| `WINDOW_ENDED` | An explicit terminal record. It means exactly: the defined observation period ended without another qualifying event. **It is not success** |
| Terminality | Once `WINDOW_ENDED`, `FALSE_BREAKOUT` or `FAILED_RETEST` occurs, nothing more is appended |
| Phase 4 evidence amendment | Approved now. The level event consumes the frozen record; it never queries or recalculates indicators |
| Retest ATR | **Frozen to the breakout's own ATR**, never the retest bar's. The band is 0.5 × A_breakout. Likewise, the level's break buffer is the one frozen in its role-change record |
| Event modifies source | Never |
| Persistence and API | Not decided until the real-NSE scale report for level events has been reviewed |

### 19.2 Implementation (2026-10-06)

- **`chartlens_engine.bar_evidence`.** `BarVolumeEvidence`, `VolumeSource` and
  `bar_volume_evidence` are one implementation for every event source:
  - patterns: `breakout_bar_volume` (the data is unchanged);
  - levels: `change_bar_volume`.
- **Levels v2 (the Phase 4 amendment).** Each non-original `RoleChange` records
  `change_bar` and `change_bar_volume`.
  - `change_bar` holds: bar date, direction, open, close, level, ATR at the bar (the
    approved Phase 4 rule), `level_break_atr`, buffer, threshold, evidence refs and
    `measurement_version`.
  - Role changes themselves are unchanged (fingerprint).
- **`chartlens_engine.breakouts`.** `BreakoutEventAnalyzer` produces `BreakoutResult`
  with `pattern_events` and `level_events`, kept apart.
  - **Identity:** `{source_id}:{BREAKOUT|BREAKDOWN}:{bar_date}`.
  - **Fields:** `source_event_ref`; `level_at_break`; `reference_atr`, frozen from the
    source record (a pattern: `atr_pre`; a level: `change_bar.atr`); `retest_band`;
    the reversal and retest windows; `observation_bars`; `source_measured_values` and
    `bar_volume`, copied from the source; `history`.
  - **Each follow-up records** its kind, date, `known_at`, `authority`
    (`RETEST_RULE`, `PATTERN_LIFECYCLE`, `LEVEL_ROLE_CHANGE` or `WINDOW`),
    `source_outcome_ref` and its measured values.
- **A pattern's level** is its confirmation level, or the frozen confirmation line's
  value at t (the line the lifecycle broke).
- **Same-bar ordering.** A RETEST on the last bar of the window shares that bar with
  `WINDOW_ENDED`, in that order.
- **A level role change in ATR warm-up** has no reference ATR. That event looks for no
  retest; reversals and the window still apply.
- **The layer never reads indicators** for ATR or volume (tested). It reads complete
  bars for its one rule (RETEST) and the source records for everything else.
- **Configuration** `[analysis.breakouts]`: `retest_window` 10, `retest_tol_atr` 0.5,
  `level_false_window` 3.
  - **Note:** the levels layer itself has no reversal window. Its authority is the
    next role change. The 3 bars are the declared window, carried over from §5, within
    which that reversal counts as a false breakout.

### 19.3 Real-NSE diagnostics (probe run 37504820464; snapshot meta-a89cf1fbcd05; descriptive)

**Sources unchanged.** The fingerprint of 4641497 (before this phase) and bbfa307 is
identical on all 3,191 securities:

- patterns and relevance;
- levels, zones and trendlines (the new evidence fields excluded).

**Authority respected.** Pattern events: FALSE_BREAKOUT 2,097 + FAILED_RETEST 3,644 =
5,741, exactly the pattern lifecycle's FAILED count.

**Scale.** This is the input to the persistence and API decision, which is not taken
here.

| | Level events | Pattern events |
|---|---|---|
| Total | 501,792 | 15,719 |
| Per security: mean / median / p90 / max | 157 / 70 / 450 / 1,353 | 4.9 / 3 / 13 / 30 |
| Serialized JSON | ~869 MB (~1.7 KB each) | ~28 MB |
| Ratio | 32 : 1 | |

The breakout stage takes about 7.1 ms per security.

**Follow-ups.**

| | Level events | Pattern events |
|---|---|---|
| RETEST → WINDOW_ENDED | 165,900 (33.1 %) | 7,694 (48.9 %) |
| RETEST → FAILED_RETEST | 189,016 (37.7 %) | 3,644 (23.2 %) |
| FALSE_BREAKOUT | 107,330 (21.4 %) | 2,097 (13.3 %) |
| WINDOW_ENDED without a retest | 33,177 (6.6 %) | 2,048 (13.0 %) |
| Still within the window (RETESTED / open) | 6,369 | 236 |

**Observations, recorded and not acted on:**

- **A retest follows about 72 % of breaks from either source.** The band (0.5 × the
  breakout's ATR) is wider than the margin by which the break closed beyond the level
  (0.10 ATR for a level, 0.25 ATR_pre for a pattern), so the next bars usually reach
  back into it. The rule is as approved; any change would be a methodology decision.
- **Level breaks reverse within their windows far more often than pattern breakouts**
  (59 % against 37 %). This is consistent with the small 0.10-ATR level buffer. It is
  descriptive only: no inference about which breaks are better.

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
