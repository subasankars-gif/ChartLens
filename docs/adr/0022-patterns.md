# ADR-0022: Classical patterns: candidates, validation, confirmation, status

**Status:** Accepted · 2026-10-02 (K5, K8 confirmed by Suba)

Layers G and H of ADR-0019. A pattern is never a visual likeness. It exists only when
measurable rules hold on the **primary confirmed swings** (ADR-0020). Tolerances are in
ATR units (ATR = ATR(14) at the pattern's last key point) or as ratios. Every number below
is a default in `[analysis.patterns]`, covered by `analysis_methodology_hash`. The golden
fixtures pin each rule.

## Pipeline

1. **Candidate generation (broad).** Every swing sequence of the right shape is a
   candidate, e.g. every L–H–L for a double bottom. Boundary patterns use runs of swing
   highs and lows. Breakout structures use zones and pattern levels.
2. **Geometric validation.** The pattern's rule table below. A candidate that fails any
   rule is dropped, and its failure reason is kept for the near-miss tests.
3. **Context validation.** Prior trend (structure regime and the move into the pattern),
   position relative to S/R zones, volume behaviour inside the pattern, volatility
   contraction, Fibonacci depth. Context only changes confidence components. It never
   creates or deletes a pattern.
4. **Confirmation.** The common breakout rule below, on **complete** bars only.
5. **Status.** The state machine below, dated by the complete bars that trigger each
   transition.

**Known when:** a pattern exists from **`known_at` = the latest `known_at` of its key
swings**. It never appears earlier, even though its geometry starts earlier on the chart.

## Common rules

- **Breakout (confirmation):**
  - a complete bar closes beyond the confirmation level (a line's value at that bar) by
    ≥ `breakout_atr` (0.25) × ATR;
  - its body points in the breakout direction (close vs open);
  - the previous complete close was not already beyond the level.
  - An intrabar excursion never confirms.
- **Status machine:**

| Status | Rule |
|---|---|
| FORMING | Geometry valid; no confirmation yet |
| CONFIRMED | The breakout rule is met (`provisional` if that bar closed on a special session, until the next regular complete week) |
| FAILED | After CONFIRMED, a complete close back beyond the confirmation level by ≥ `breakout_atr` × ATR within `fail_window` (8) bars |
| COMPLETED | After CONFIRMED, a complete bar's high (low) reaches the measured-move zone |
| INVALIDATED | Before confirmation: a complete close beyond the invalidation level, or no confirmation within `max_wait_bars` (26) of `known_at`, or the pattern's lines (e.g. a triangle apex) expire |

  - FAILED, COMPLETED and INVALIDATED are terminal.
  - Each transition is appended to `status_history` with its bar date. A forming week
    never causes a transition.
- **Measured-move zone (K8):** the classical projection of the pattern height from the
  confirmation level, ± `mm_zone_atr` (0.5) × ATR. It is the definition of COMPLETED,
  not a forecast. The UI calls it "measured-move zone", never a "target".
- **Clutter control:**
  - candidates of the same type whose key points overlap: the one with the higher
    confidence is kept (ties go to the earlier `known_at`);
  - at most `max_forming_per_type` (2) FORMING patterns per type;
  - terminal patterns are reported for `report_window_bars` (52) after their last
    transition.

## Rule table (bullish shown; bearish mirrors)

Notation: H = swing high, L = swing low; bars(a, b) = bars between swings; height = the
pattern's vertical size.

| Pattern | Geometry | Confirm / invalidate |
|---|---|---|
| **Double bottom** / top | L1, H, L2 consecutive. \|L1 − L2\| ≤ `eq_tol_atr` (0.5) × ATR. bars(L1, L2) ∈ [4, 60]. H − max(L1, L2) ≥ `min_height_atr` (2.0) × ATR. Context: a decline into L1 ≥ 2 × ATR | Close > H (the neckline) / close < min(L1, L2) |
| **Triple bottom** / top | L1, H1, L2, H2, L3: every pair of lows within `eq_tol_atr`. bars(L1, L3) ∈ [8, 90]. min(H1, H2) − max(lows) ≥ `min_height_atr` × ATR | Close > max(H1, H2) / close < min(lows) |
| **Inverse H&S** / H&S | L1 (LS), H1, L2 (head), H2, L3 (RS). Head below both shoulders by ≥ `head_prominence_atr` (1.0) × ATR. \|LS − RS\| ≤ `shoulder_tol_atr` (1.5) × ATR. bars(LS→head) / bars(head→RS) ∈ [0.4, 2.5]. Neckline = the line through H1 and H2, with \|slope\| ≤ `neckline_max_slope_atr` (0.25) × ATR per bar. Context: a prior decline | Close > the neckline's value / close < the head |
| **Rounding bottom** / top | Between rims H_a and H_b (\|H_a − H_b\| ≤ 2 × ATR), ≥ 20 bars. A quadratic fit of closes has a > 0 and R² ≥ 0.6, with its vertex in the middle 50 % of the span. Depth ≥ 3 × ATR. No primary swing low below the vertex region by more than `eq_tol_atr` | Close > max(rims) / close < the fitted minimum − `eq_tol_atr` × ATR |
| **V bottom** / top | H0, L, then recovery: H0 − L ≥ `v_move_atr` (4.0) × ATR within ≤ 8 bars, and no other primary swing between H0 and L | Close > L + 0.618 × (H0 − L) / close < L. Completed at H0 |
| **Rectangle** | ≥ 2 swing highs and ≥ 2 swing lows (≥ 4 touches), alternating, highs within `band_tol_atr` (0.75) × ATR of each other, lows likewise. Height ≥ 2 × ATR. Direction is NEUTRAL until the breakout | A close beyond either boundary; the direction is the side broken. Expires after `max_wait_bars` |
| **Ascending / Descending / Symmetrical triangle** | Lines fitted to ≥ 2 swing highs and ≥ 2 swing lows (≥ 4 touches; two points: exact; three or more: least squares). Every touch within `fit_tol_atr` (0.5) × ATR of its line; no complete close outside both lines inside the span. Flat = \|slope\| ≤ `flat_slope_atr` (0.02) × ATR per bar. Ascending: upper flat, lower rising. Descending: lower flat, upper falling. Symmetrical: upper falling, lower rising, slope magnitudes within ×3 of each other. Converging: end width ≤ 0.75 × start width, with the apex ahead within 52 bars | Close beyond the line in the pattern's direction (symmetrical: either side). A close beyond the other line invalidates it (and is reported as a breakout event). Passing the apex expires it |
| **Rising / Falling wedge** | Both lines slope the same way (\|slope\| > flat) and converge as above. Rising wedge: bearish; falling wedge: bullish | Close beyond the line opposite the slope / close beyond the other line |
| **Channel** | Two parallel lines (slope difference ≤ `parallel_tol_atr` (0.01) × ATR per bar), not flat, ≥ 2 touches on each. CONFIRMED once it holds ≥ `channel_confirm_touches` (5) touches with price inside | A close outside either line ends it: INVALIDATED, with a breakout event on that side |
| **Flag** | Pole: a primary leg ≥ `pole_atr` (3.0) × ATR within ≤ 6 bars. Then 3–12 bars contained by two near-parallel lines sloping against the pole or flat. The retrace is ≤ 0.5 of the pole | Close > the upper line / close < the lower line, or the retrace exceeds 0.5. Measured move = pole height from the breakout level |
| **Pennant** | The same pole. Then 3–12 bars of converging lines (a small symmetrical triangle) | As flag |
| **Cup & handle** / inverse | Cup: rims H_a and H_b within 2 × ATR, 7–65 bars apart, depth ≥ 3 × ATR and ≤ 50 % of the rim price, quadratic fit a > 0 with R² ≥ 0.5. Handle: after H_b, a pullback of 1–10 bars, ≤ 0.5 × the cup depth, holding above the cup's midpoint | Close > the handle high (or H_b) / close < the handle low, or below the cup midpoint |
| **Breakout / Breakdown** | A level from an S/R zone edge or a pattern's confirmation level | The common breakout rule |
| **False breakout** | After a breakout, a complete close back inside within `false_window` (3) bars | (event) |
| **Retest** | After a breakout, within `retest_window` (10) bars, a complete bar's low comes within `retest_tol_atr` (0.5) × ATR of the level and closes on the breakout side | (event) |
| **Failed retest** | During the retest window, a complete close back through the level by ≥ `breakout_atr` × ATR | (event) |

Every pattern in the table also needs the context its definition implies: a reversal
pattern needs a prior move into it of ≥ 2 × ATR in the opposite direction. Without that
context the candidate is kept with a lower context component, not discarded. Only
geometry decides existence.

## Confidence (definition fit, not probability)

`confidence` answers one question: **how fully does the observed structure satisfy this
pattern's definition?**

- Each pattern defines components, each in [0, 1]:
  - **geometry:** e.g. similarity of the lows, height, symmetry, fit residuals, touches;
  - **context:** prior trend, zone alignment, volume behaviour, volatility contraction.
- confidence = round(100 × Σ wᵢcᵢ / Σ wᵢ). The weights are in configuration; geometry
  weighs at least 60 %.
- The response always carries the components next to the number.
- It is never called a probability, never shown as "x % chance", never turned into a
  recommendation, and never computed from outcomes. **Historical statistics** are a
  placeholder (`historical_stats: null`) until a later milestone, so future outcomes
  cannot leak into confidence.

## Pattern record

- **Identity:** pattern_id (deterministic: type, segment and the key points' bar dates),
  security_id, timeframe, continuity_segment_id, pattern_type, direction (BULLISH,
  BEARISH or NEUTRAL).
- **Status:** status, status_history, `known_at`, start_date, end_date (the last key
  point, or the terminal transition).
- **Confidence** and its components.
- **Levels:**
  - key_points: labelled (e.g. LS, HEAD, RS, NECK_1), each with `bar_date` and the
    bar's exact price;
  - lines: labelled, with two endpoints (date, value);
  - confirmation_level, invalidation_level;
  - measured_move_zone: low and high.
- **Evidence:** geometry (rule values measured, e.g. the low difference in ATR); then
  volume, structure, momentum and Fibonacci evidence, each a list of references to F
  events or zones.
- historical_stats: null.
- **Explanation:** structured facts plus text produced from them by templates:

  > "Double bottom: lows of 412.50 (12 Jan 2026) and 418.20 (13 Jul 2026), 0.3 ATR
  > apart. The intervening high at 486.00 is the neckline. FORMING: no weekly close above
  > 486.00 + 0.25 ATR yet."

## Testing (mandatory)

**Golden fixtures**, small and readable weekly series, one per pattern:

- double bottom and top;
- triple top and bottom;
- head and shoulders, normal and inverse;
- rounding top and bottom;
- V top and bottom;
- rectangle;
- ascending, descending and symmetrical triangle;
- rising and falling wedge;
- channel;
- flag and pennant;
- cup and handle, normal and inverse;
- breakout, false breakout, retest and failed retest.

**Cases per pattern:**

- valid;
- a near miss on each geometric rule;
- invalid geometry;
- confirmation;
- failure;
- completion;
- invalidation;
- a continuity break inside the pattern (not analysable);
- a forming final week that would confirm (it must not);
- `as_of` the bar before confirmation, which must not be CONFIRMED;
- `as_of` the confirmation bar, which must be CONFIRMED.

Plus prefix stability (ADR-0020) over every fixture and over random series, and real-data
golden outputs for a reviewed set of securities.
