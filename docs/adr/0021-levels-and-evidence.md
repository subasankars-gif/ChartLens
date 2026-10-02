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
