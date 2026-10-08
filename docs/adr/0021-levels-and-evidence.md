# ADR-0021: Support/resistance, Fibonacci, divergence and technical evidence

**Status:** Accepted · 2026-10-02 (K5 candlestick subset confirmed by Suba)

These are layers D–F of ADR-0019. Everything here is **evidence**: measurable facts that
patterns and the later scenario layer may cite. Nothing here is a signal. M8 never
combines evidence into a score, a ranking or a "best setup". All parameters are in
`[analysis]` and covered by `analysis_methodology_hash`. ATR means ATR(14) at the bar
named, so every quantity is known at that bar.

## D. Support and resistance zones (current state at `as_of`)

**Sources**, each one a price with a date:

- **Horizontal:** primary swing highs and lows (confirmed, `known_at ≤ as_of`).
- **Structural:** levels of BOS and CHoCH events.
- **Dynamic:** SMA 50 and SMA 200 at `as_of` (when they exist).
- **Fibonacci:** the 38.2 %, 50 % and 61.8 % levels of active structures (E).
- **Volume:** the price range of complete bars with RVOL ≥ `rvol_expansion` that
  coincide with a swing.

**Zones:**

- Sources are clustered greedily in price order. A source joins a zone when it is within
  `zone_tolerance_atr` (0.5) × ATR[`as_of`] of the zone's current mean.
- A zone spans the min to the max of its sources, widened to at least
  `zone_min_width_atr` (0.25) × ATR.
- A zone is **support** below the last close and **resistance** above it.
- **Touches:** distinct complete bars, after the zone's first source, whose low (for
  support) or high (for resistance) entered the zone, and that closed on the zone's side.
  Several touches in consecutive bars count once.
- Only the `max_zones_per_side` (4) zones nearest the last close are reported on each
  side, so the chart doesn't fill with lines.

**Zone record:**

- zone_id, type (support or resistance), price_low, price_high, segment;
- touch_count, source_types, first_seen (the earliest source's `known_at`), last_tested;
- strength = Σ of weighted components, reported **with** the components:
  - touches (`w_touch` × touch_count);
  - distinct source types (`w_sources`);
  - volume at touches (`w_volume` × max RVOL);
  - recency (`w_recency` × e^(−weeks since last test / `recency_halflife_weeks`)).

**No word label such as "strong" is used:** only the number and its parts.

## E. Fibonacci (current state)

- **Built only from confirmed primary swing legs:**
  - the most recent completed leg at the primary sensitivity;
  - the most recent completed leg at MAJOR sensitivity, when it differs.
- **Anchor:** the leg's start swing. **Counter:** its end swing.
- **Retracements:** 23.6, 38.2, 50.0, 61.8 and 78.6 % of the leg, measured back from
  the counter swing.
- **Extensions:** 127.2, 161.8 and 261.8 %, projected from the anchor in the leg's
  direction.
- **Record:** the anchor and counter swing ids and prices, direction, levels (exact
  arithmetic on the swing prices, output rounded to 4 decimals), segment, `known_at`
  (the counter swing's), and status:
  - ACTIVE: price is inside 0–100 %;
  - BROKEN: a complete close beyond the anchor;
  - EXTENDED: a complete close beyond the counter swing.
- Fibonacci levels are supporting evidence only. They never stand alone as a prediction.

## F. Evidence

### Divergence (events)

- **What is compared:** two consecutive confirmed **primary** swings of the same type,
  4 to 52 bars apart (`divergence_min_bars`, `divergence_max_bars`).
- **Indicators:** RSI(14), the MACD line and OBV, each read at the two swing bars. Both
  values must exist (no warm-up nulls).

| Type | Price (swing lows / highs) | Indicator |
|---|---|---|
| Regular bullish | lower low | higher low |
| Hidden bullish | higher low | lower low |
| Regular bearish | higher high | lower high |
| Hidden bearish | lower high | higher high |

- **Thresholds:**
  - "Higher" and "lower" for price need more than `equal_tolerance_atr` × ATR.
  - For the indicator they need more than `divergence_min_delta`, which depends on the
    indicator:
    - RSI: 2 points;
    - MACD: 0.05 × ATR;
    - OBV: 5 % of the 20-week volume SMA.
- **Record:**
  - divergence_id, indicator, type;
  - price_swing_1 and price_swing_2;
  - indicator_swing_1 and indicator_swing_2 (values at the swing bars);
  - date_start and date_end;
  - **`known_at` = the second swing's `known_at`**;
  - strength: the normalized disagreement of the two slopes, with its components;
  - confirmation: a complete close beyond the intervening opposite swing;
  - invalidation: a complete close beyond the second swing;
  - status_history: FORMING, CONFIRMED or INVALIDATED.

### Volume (events and state)

| Item | Rule |
|---|---|
| Expansion / contraction | The volume state per bar (ADR-0020) |
| Breakout volume confirmation | On a breakout bar (any layer), RVOL ≥ `breakout_rvol_confirm` (1.5) |
| Breakout volume contradiction | On a breakout bar, RVOL < `breakout_rvol_contradict` (0.8) |
| Volume climax | RVOL ≥ `climax_rvol` (2.5) and true range ≥ `climax_range_atr` (2.0) × ATR, on a complete bar |
| Volume divergence | Price makes HH (LL) at consecutive primary swings while the volume SMA20 at the second swing is lower than at the first by more than `volume_divergence_min` (10 %) |

Terms stay neutral: "volume expansion", "volume confirmation", "volume divergence".
There is no "institutional", "smart money" or "accumulation" language: those claims
cannot be measured from OHLCV.

### Volatility (events and state)

| Item | Rule |
|---|---|
| ATR compression | ATR% at `t` ≤ the `compression_percentile` (20th) of ATR% over the previous `volatility_lookback` (52) complete bars of the segment |
| Bollinger contraction | Bandwidth at `t` ≤ the 20th percentile of bandwidth over the lookback |
| Range contraction | True range at `t` is the smallest of the last `nr_window` (7) complete bars (NR7) |
| Expansion after contraction | True range ≥ `expansion_range_atr` (1.5) × ATR within `expansion_window` (4) bars after any contraction above |

Percentiles use only past bars, so they are causal. There is no value until the lookback
is full.

### Candles (events; evidence, never signals)

The subset confirmed in K5, on **complete** bars only. Definitions:

- body = |c − o|; range = h − l; upper shadow = h − max(o, c); lower shadow = min(o, c) − l.
- **Prior direction:** the sign of c[t−1] − c[t−1−`candle_context_bars`] (5).
- A pattern needs range > 0.

| Candle | Rule |
|---|---|
| Doji | body ≤ 0.1 × range |
| Hammer / Hanging man | lower shadow ≥ 2 × body, upper shadow ≤ 0.25 × range, body > 0. Hammer after a decline; hanging man after an advance |
| Inverted hammer / Shooting star | upper shadow ≥ 2 × body, lower shadow ≤ 0.25 × range, body > 0. Inverted hammer after a decline; shooting star after an advance |
| Bullish / Bearish engulfing | Opposite colour to the previous bar, and the body covers the previous body (open beyond its close, close beyond its open) |
| Morning / Evening star | Three bars: a long first body (≥ 0.6 × its range) in the prior direction; a small middle body (≤ 0.3 × the first body); a third bar of the opposite colour closing beyond the first body's midpoint |
| Inside bar | h ≤ previous h and l ≥ previous l |
| Outside bar | h ≥ previous h and l ≤ previous l, with at least one strict |

- The multipliers are configuration (`candle_*`).
- Candles are hidden on the chart by default. They are listed as evidence where a
  pattern or zone cites them.
- Deferred to a later milestone (K5): piercing, dark cloud cover, tweezers, three white
  soldiers / black crows, rising/falling three methods.

## Testing

- Zones:
  - clustering edges (just inside and outside the tolerance);
  - deterministic strength;
  - the nearest-N cap;
  - nothing from another segment.
- Fibonacci: the exact levels from known anchors; direction; status transitions.
- Divergence:
  - one fixture per type;
  - near misses on each threshold;
  - no comparison across segments;
  - the second swing's `known_at` gates visibility.
- Volume, volatility and candles:
  - one fixture per rule, plus near misses;
  - the forming week never produces an event.
- Prefix stability for every event type (ADR-0020).

## Implementation rules (Phase 4)

These were settled while building Phase 4 and confirmed in Suba's Phase 4 review. Where
they sharpen or change the text above, they take precedence.

**Existence and relevance are separate questions.**

- **Existence:** does this technical object exist, given only what was known at that
  time? Levels, trendlines, Fibonacci structures, divergences and every other event
  answer this. They are kept in full, with `known_at` and an append-only history, and
  they are prefix-stable.
- **Relevance:** is the object close, strong or recent enough to be part of the current
  picture? The nearest `max_zones_per_side` zones, the active trendlines and the current
  Fibonacci structures answer this. They are current state as of the state date.
- Relevance selection is analysis methodology: it is configured, hashed into
  `analysis_methodology_hash` and computed by the engine. The serving layer and the
  frontend never re-select, filter or rank analysis objects. They show what the engine
  published, and may only hide or show whole layers.
- There is no age cut-off on existence. A level established hundreds of weeks ago stays
  a candidate. Its age only lowers the recency component of a zone's strength, and the
  nearest-zone selection keeps the output small.

**Order and inputs.**

- **Order:** Fibonacci (E) runs before levels (D), because active Fibonacci levels are
  zone sources. Evidence (F) runs last.
- **Levels and evidence consume, they never re-derive.** They read:
  - the primary confirmed swings;
  - structure's labels and BOS/CHoCH events;
  - the Fibonacci layer;
  - the indicator series.

  They never find a pivot, label a swing, judge a break of structure or name a swing
  method. A layer test enforces this.
- **Complete bars only.** Layers D–F never read the forming week. Their current state
  (zones, active trendlines, current Fibonacci, the volume and volatility state) is as
  of the last complete bar, recorded as `state_date`. Every event is dated by a complete
  bar.

**Causal composition.**

- Every derived object records `depends_on`: the ids of the swings, structure events,
  Fibonacci structures, trendlines or contraction episodes it was built from.
- **A derived object is never `known_at` before any of them.**
- For a zone, `known_at` is the latest `known_at` of its sources and `first_seen` is the
  earliest. A zone that includes a moving average is therefore `known_at` the state date.
- A generic test checks this rule for every object of every layer. A scenario test
  checks it for a swing known on 2026-07-10 and the Fibonacci structure, divergence and
  zone built from it.

**Status histories.**

- An object's first status is judged on the bar that makes it known. Later statuses are
  judged on each complete bar after that.
- The levels compared against were all known by that bar. The rule from structure still
  holds: a level is never broken by the bar that makes it known.
- Each entry is marked `provisional` when its bar closed on a non-regular session.
- Every event type has an `as_of(T)` projection. The prefix-stability tests compare a
  run on the bars up to T with the full run projected to T.

**E. Fibonacci.**

- A leg is two **consecutive confirmed** primary-method swings of opposite type, at the
  primary sensitivity and at each of `extra_sensitivities` (default MAJOR).
- A pending extreme is never an anchor.
- A leg is meaningful when |counter − anchor| ≥ `min_leg_atr` (2.0) × ATR at the counter
  swing's bar.
- `known_at` is the later `known_at` of the two swings.
- Each leg is an event with its own status history. ACTIVE becomes BROKEN or EXTENDED
  on a complete close beyond the anchor or the counter swing. Both are terminal.
- "Current" Fibonacci is the latest structure for each sensitivity that is known by the
  state date.

**D. Levels: role changes.**

- Every primary swing price and every BOS/CHoCH level is a `Level` (existence), known
  from its source's `known_at`.
- **Original role:**
  - a swing low, or a level broken upwards (BOS/CHoCH UP), starts as support;
  - a swing high, or a level broken downwards, starts as resistance.
- **Role changes are explicit:** a support becomes resistance on a complete close below
  it by `level_break_atr` (0.10) × ATR at that bar, and the reverse. Each change is an
  entry in the level's append-only `role_history`, dated by the closing bar.
- A level is never broken by the bar that makes it known.
- So a broken support never goes on looking like an untouched one. Its history stays
  available, and the role reversal is visible for the breakout and retest patterns
  later.

**D. Zones.**

- **Sources:**
  - primary swing prices;
  - structure's BOS/CHoCH levels (the prior breakout levels);
  - `dynamic_sources` (SMA 50 and SMA 200 at the state date);
  - the `fibonacci_ratios` (38.2, 50 and 61.8 %) of the current Fibonacci structures
    that are ACTIVE.
- **Volume (a change from the text above).** A swing whose pivot bar is in volume
  EXPANSION adds the source type VOLUME. Its bar's whole high–low range is not used as a
  zone edge, because on high-volume weeks that range is several ATR wide and would
  swallow nearby zones.
- **Side:** decided by the zone's midpoint against the last complete close, which
  settles a zone that contains the close.
- **Touches:** tests in the zone's **current role**. They are counted only on bars after
  both its first source is known and the last role change of any of its levels
  (`tested_since`).
- **Each zone reports its role changes:** `role_changes` and `role_reversed` (at least
  one level now plays the opposite of its original role), and each source carries its
  level's original role, current role and `role_since`.
- **Strength:** the sum of `w_touch` × touches, `w_sources` × distinct source types,
  `w_volume` × the largest RVOL at a touch, and `w_recency` × e^(−bars since the last
  test / 26).
  - A zone untested in its current role takes its recency from its latest source bar or
    role change, whichever is later.
  - Strength is reported with all its components. It measures how much a level has
    mattered. It is not a probability, a signal or a score of the security.
- **No ATR yet** (warm-up): no zones.

**D. Trendlines (added in Phase 4).**

- **Candidate:** a line through two primary swings of one type, `trendline_min_bars`
  (4) to `trendline_max_bars` (104) bars apart.
  - Support lines use swing lows and must rise; resistance lines use swing highs and
    must fall. Flat levels are zones.
  - The line advances per bar of the segment, not per calendar week.
- **Rejected** when either of these happens:
  - a swing of that type between the anchors (known by then) lies beyond the line by
    more than `trendline_touch_atr` (0.5) × ATR at that swing's bar;
  - a complete close is beyond the line by `trendline_break_atr` (0.10) × ATR before the
    line exists.
- **Validated** by a third swing within the touch tolerance that is known within
  `trendline_max_bars` of the first anchor and before any break.
  - `known_at` is the later of the third touch's `known_at` and the second anchor's.
  - `depends_on` is the three swings.
- **Later touches** are added with their own `known_at`.
- **BROKEN:** the first complete close beyond the line by the buffer. This is terminal.
- **De-duplication is causal.** A line validated later that shares two touches (known by
  then) with an accepted line that is not yet broken is the same line, and is dropped.
- **Reported:** every validated line as an event. The active ones are current state: at
  most `max_trendlines_per_side` (2) per side, most recently touched first, with their
  value at the state date.

**D. Channels (moved here from ADR-0022 on 2026-10-02; not yet built).**

A channel is a container of the levels layer, not a classical pattern. It is built from
the trendline machinery when the levels layer is next extended, outside Phase 5.

**Status (2026-10-08, M8 completion gate D1):** an accepted design, **deferred beyond
M8 and not built**. It is not an M8 delivery commitment. When its phase starts, it is
re-reviewed against the rules frozen since it was written: known_at visibility, 6d
placement, 6e claims, and Investigation 0001. Only then is it implemented.

- **Candidate:** four consecutive alternating primary swings. The upper line runs
  through the two highs and the lower line through the two lows.
  - The lines must be non-flat and parallel: |slope difference| ≤ `parallel_tol_atr`
    (0.01) × ATR per bar.
  - No complete close may lie outside either line inside the span.
- **Becomes known:** ACTIVE once it holds `confirm_touches` (5) touches, each within
  `fit_tol_atr` (0.5) × ATR of its line, with every close inside. `known_at` is the
  fifth touch's `known_at`. `depends_on` is the touching swings.
- **Status history:** ACTIVE, then BROKEN_UP or BROKEN_DOWN on the first complete close
  outside a line by `trendline_break_atr` × ATR at that bar (the same rule as trendlines).
  These are terminal. Each break feeds the breakout events (ADR-0022 §5) like any level
  role change.
- **De-duplication:** causal, as for trendlines.
- **Relevance:** active channels, the most recently touched first.

**F. Divergence.**

- The price side is **structure's label** of the second swing:
  - LL gives a regular bullish divergence;
  - HL gives a hidden bullish divergence;
  - HH gives a regular bearish divergence;
  - LH gives a hidden bearish divergence;
  - EQL and EQH give none.

  So the equality band and the pairing of consecutive swings are structure's own.
- **The indicator:** read at the two swing bars and compared with its minimum delta. The
  comparison is strict, and the delta must be greater than 0.
- **Strength:** |indicator change| ÷ minimum delta, which says how clearly the indicator
  disagrees. The price change in ATR, the indicator change, the delta and the bars apart
  are reported with it.
- **Confirmation:** the most extreme opposite primary swing between the two that is
  known by the divergence's `known_at`. There is no confirmation level when no such
  swing exists.
- **EXPIRED is added:** a FORMING divergence that is neither confirmed nor invalidated
  within `expiry_weeks` (26, configurable) of `known_at` is EXPIRED.
  - EXPIRED means it aged out unresolved. It is not proof the divergence was wrong; that
    is INVALIDATED.
  - CONFIRMED stays CONFIRMED: a confirmed divergence is a fact that happened. How
    recent it is, is relevance, not existence.
  - CONFIRMED, INVALIDATED and EXPIRED are terminal.

**F. Volume.**

- **Breakout volume** is judged on the bars of breaks the earlier layers recorded:
  structure BOS/CHoCH events and trendline breaks. Patterns will add their own breaks in
  later phases.
- **Climax and expansion:** the true range is compared with the ATR of the **previous**
  bar, so the bar's own range does not dampen its yardstick.
- **Volume divergence** uses structure's HH and LL labels.
- **State:** volume, volume SMA, RVOL, volume state, volume trend, OBV, and the OBV
  trend.
  - The OBV trend is RISING or FALLING when the change in OBV over `obv_trend_bars` (10),
    divided by (bars × volume SMA), exceeds `obv_trend_band` (0.10).

**F. Volatility.**

- **Percentiles** use numpy's linear interpolation over the previous `lookback` bars.
  There is no value while any of those bars is missing.
- **NR7** is strict: the narrowest bar, with no tie.
- **Contractions are episodes:** one event on the first bar of each run. Expansion
  after contraction is also one event per run, and its `depends_on` names the
  contraction episodes in its window.

**F. Candles.**

- **Context recorded on each event:**
  - the prior direction and the prior change in ATR (for a star, measured before its
    first bar);
  - structure's trend state at the bar;
  - the volume state and RVOL.
- **Bars a candle may use:** every one must have range > 0.
- **The K5 definitions are kept exactly** (Suba, Phase 4 review). The candle layer
  answers "did this candle occur?" and never "is it significant?". Significance comes
  from context (prior move, structure, S/R, volume) in later layers. Frequent candles
  stay available as evidence, and ADR-0023 only hides them on the chart by default.
- **Engulfing:** needs the current body to cover the previous body. At least one edge
  must be strictly beyond, and equal edges count.
- **Outside bar:** needs at least one strictly greater extreme. An identical bar is an
  inside bar.
