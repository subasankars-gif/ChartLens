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
`time_balance` [0.4, 2.5], `neckline_max_slope_atr` 0.25, `min_height_atr` 2.0,
`max_span` 104, `max_wait_bars` 26.

| Field | Rule |
|---|---|
| Swing sequence | Five consecutive primary swings: L1 (left shoulder), H1, L2 (head), H2, L3 (right shoulder) |
| Candidate geometry | Head below both shoulders; neckline through H1 and H2 |
| Tolerances | P(L2) ≤ min(P(L1), P(L3)) − `head_prominence_atr` × ATR_D; \|P(L1) − P(L3)\| ≤ `shoulder_tol_atr` × ATR_D; \|neckline slope\| ≤ `neckline_max_slope_atr` × ATR_D per bar |
| Separation | bars(L1, L2) / bars(L2, L3) ∈ `time_balance`; bars(L1, L3) ≤ `max_span` |
| Height | Neckline at b(L2) − P(L2) ≥ `min_height_atr` × ATR_D |
| Context | `prior_move` (decline into L1); `trend_context` (bullish reversal); `level_alignment` at the head; `volume_behaviour` = 1 if the volume SMA at L3 < at L2; `divergence` on L2 or L3 |
| Confirmation | Common breakout above the neckline's value at the bar |
| Invalidation | Complete close < P(L2) (the head) |
| Expiry | `max_wait_bars` after `known_at` |
| Measured-move zone | Neckline at the breakout bar + (neckline at b(L2) − P(L2)) ± `mm_zone_atr` × ATR_pre |
| Identity | INVERSE_HEAD_SHOULDERS + (L1, H1, L2, H2, L3) |
| `known_at` | k(L3) |
| Status transitions | Common |
| Confidence | Geometry: `margin(prominence_atr, head_prominence)`, `closeness(|L1 − L3|, shoulder_tol)`, `balance(time ratio, time_balance)`, `closeness(|slope|, max_slope)`. Context: the five above |
| Overlap | Shares its five swings with a triple-bottom window only when the head is within `eq_tol`, and the head-prominence rule makes that impossible. Coexists with the double bottoms inside it; relevance prefers the H&S |

### 7.4 Rounding bottom / rounding top

`[analysis.patterns.rounding]`: `rim_tol_atr` 2.0, `min_span` 20, `max_span` 156,
`min_r2` 0.6, `vertex_window` [0.25, 0.75], `min_depth_atr` 3.0, `low_tol_atr` 0.5,
`max_wait_bars` 26.

| Field | Rule |
|---|---|
| Swing sequence | Two primary swing highs H_a and H_b (rims), not necessarily consecutive. Every primary swing high between them is below min(P(H_a), P(H_b)) |
| Candidate geometry | Closes over b(H_a)…b(H_b) fitted by least squares to c = a·x² + b·x + c₀, with a > 0 |
| Tolerances | \|P(H_a) − P(H_b)\| ≤ `rim_tol_atr` × ATR_D; R² ≥ `min_r2`; the vertex lies in `vertex_window` of the span; no primary swing low between the rims is below the fitted minimum by more than `low_tol_atr` × ATR_D |
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
| Confidence | Geometry: R² scaled from `min_r2` to 1 → [0, 1]; `closeness(|H_a − H_b|, rim_tol)`; `balance(vertex position ÷ 0.5, vertex_window)`; `margin(depth_atr, min_depth)`. Context: as listed |
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
`converge_ratio` 0.75, `apex_max_bars` 52, `fit_tol_atr` 0.5, `min_span` 8,
`max_span` 104, `max_wait_bars` 52.

| Field | Rule |
|---|---|
| Swing sequence | Four consecutive alternating primary swings (two highs, two lows) |
| Candidate geometry | Upper line through the two highs; lower line through the two lows (exact two-point lines) |
| Tolerances | Slopes s_u and s_l per bar; "flat" means \|s\| ≤ `flat_slope_atr` × ATR_D. **Ascending:** upper flat, lower rising. **Descending:** lower flat, upper falling. **Symmetrical:** upper falling, lower rising, with \|s_u\| / \|s_l\| ∈ [1/`symmetry_ratio`, `symmetry_ratio`]. **Converging:** width at the last defining bar ≤ `converge_ratio` × width at the first, and the apex is within `apex_max_bars` after the last defining bar. No complete close outside either line by ≥ `breakout_atr` × ATR_pre inside the span |
| Separation | span ∈ [`min_span`, `max_span`] |
| Height | Width at the first defining bar ≥ 2 × ATR_D |
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
`apex_max_bars`, `fit_tol_atr`, `min_span`, `max_span`), `max_wait_bars` 52.

| Field | Rule |
|---|---|
| Swing sequence | Four consecutive alternating primary swings |
| Candidate geometry | Both lines slope the same way, both non-flat, converging (as for triangles) |
| Tolerances | As for triangles. Rising wedge: s_u > 0, s_l > 0, s_l > s_u (converging). Falling wedge: mirrored |
| Separation | span ∈ [`min_span`, `max_span`] |
| Height | Width at the first defining bar ≥ 2 × ATR_D |
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
| Swing sequence | Rims H_a and H_b: primary swing highs, with every primary high between them below min(rims). Handle low L_h: the first swing low after H_b at `fine_sensitivity` |
| Candidate geometry | Cup = a quadratic fit of closes between the rims, with a > 0. Handle = the pullback from H_b to L_h |
| Tolerances | \|P(H_a) − P(H_b)\| ≤ `rim_tol_atr` × ATR_D; R² ≥ `min_r2`; depth ≥ `min_depth_atr` × ATR_D and ≤ `max_depth_ratio` × min(rims); handle depth P(H_b) − P(L_h) ≤ `handle_max_ratio` × cup depth; P(L_h) > cup midpoint |
| Separation | bars(H_a, H_b) ∈ [`cup_min_bars`, `cup_max_bars`]; bars(H_b, L_h) ∈ [`handle_min_bars`, `handle_max_bars`] |
| Height | Cup depth (above) |
| Context | `prior_move` up into H_a (continuation); `trend_context` (bullish continuation); `volume_behaviour` = 1 if the volume SMA at the cup bottom < at H_a; `fib_depth` of the handle |
| Confirmation | Common breakout above P(H_b) |
| Invalidation | A complete close < P(L_h), or < the cup midpoint |
| Expiry | `max_wait_bars` after `known_at` |
| Measured-move zone | P(H_b) + cup depth ± `mm_zone_atr` × ATR_pre |
| Identity | CUP_HANDLE + (H_a, H_b, L_h) |
| `known_at` | max(k(H_b), k(L_h)) |
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
