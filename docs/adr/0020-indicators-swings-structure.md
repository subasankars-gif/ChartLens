# ADR-0020: Indicators, swing points and market structure

**Status:** Accepted · 2026-10-02 (K4, K6 and the causal warm-up rule confirmed by Suba)

Layers A–C of ADR-0019. Every parameter below lives in the `[analysis]` configuration
and is covered by `analysis_methodology_hash`. The defaults are the confirmed values.

## Conventions

- **Input:** one security's bars in its current valid segment, oldest first
  (`chartlens_core.bars`). Index `t` is a bar; `c, o, h, l, v` are its close, open,
  high, low and volume. Prices are floats for computation only.
- **Causal:** a value at `t` uses bars `0…t` only. No indicator, swing or state at `t`
  depends on a bar after `t`.
- **Warm-up, causal and per segment.** A value exists only once the segment holds enough
  observations. Before that it is **null**: never filled, back-filled, extrapolated, or
  warmed up across a break. An SMA(200) is null until bar 199 of the segment.
- **The forming week.** The last bar may have `is_complete = false`. Its values are
  computed and flagged `provisional`; nothing is confirmed on it (ADR-0019).
- **Division by zero** gives null, except where a convention is stated below.

## A. Indicators

| Indicator | Definition | First value at `t` |
|---|---|---|
| SMA(n), n ∈ {10, 20, 40, 50, 100, 200} | mean(c[t−n+1 … t]) | n − 1 |
| EMA(n), n ∈ {10, 20, 50, 100, 200} | seed EMA[n−1] = SMA(n)[n−1]; then EMA[t] = α·c[t] + (1−α)·EMA[t−1], α = 2/(n+1) | n − 1 |
| RSI(14), Wilder | Δ[t] = c[t] − c[t−1]; gains and losses = max(±Δ, 0); first averages = means over Δ[1…n]; then avg[t] = (avg[t−1]·(n−1) + x[t]) / n; RSI = 100 − 100/(1 + G/L). L = 0 and G > 0 → 100; G = L = 0 → 50 | n |
| MACD(12, 26, 9) | line = EMA12 − EMA26 (from t = 25); signal = EMA9 of the line, seeded with the SMA of its first 9 values; histogram = line − signal | line 25, signal 33 |
| Stochastic(14, 3, 3), slow | raw %K = 100·(c − LL14)/(HH14 − LL14), 50 when HH14 = LL14; %K = SMA3(raw); %D = SMA3(%K) | raw 13, %K 15, %D 17 |
| ROC(12) | 100·(c[t]/c[t−12] − 1) | 12 |
| ATR(14), Wilder | TR[t] = max(h−l, \|h−c[t−1]\|, \|l−c[t−1]\|) for t ≥ 1; ATR[n] = mean(TR[1…n]); then Wilder smoothing | n |
| ATR% | 100·ATR/c | as ATR |
| Bollinger(20, 2) | mid = SMA20; σ = population standard deviation (ddof 0) of the last 20 closes; upper/lower = mid ± 2σ | 19 |
| Bollinger bandwidth | (upper − lower)/mid | 19 |
| Volume SMA(20) | mean(v[t−19 … t]) | 19 |
| **Relative volume** | **RVOL[t] = v[t] / mean(v[t−20 … t−1])**: the current week is never part of its own baseline | 20 |
| OBV | OBV[0] = 0; OBV[t] = OBV[t−1] + sign(c[t] − c[t−1])·v[t]. Its level is relative to the segment start; only changes in it are meaningful | 0 |
| Volume trend | RISING if SMA10(v) > (1 + 0.10)·SMA20(v); FALLING if < (1 − 0.10)·SMA20(v); else FLAT (`volume_trend_band` = 0.10) | 19 |
| Volume state | EXPANSION if RVOL ≥ 1.5, CONTRACTION if RVOL ≤ 0.67, else NORMAL (`rvol_expansion`, `rvol_contraction`) | 20 |

- **Aliases.** "10W", "20W", "40W", "50W", "100W" and "200W" are names for SMA(n) on
  weekly bars. They are aliases of one series, never a second calculation.
- **Every series records** its name, family, parameters, `warmup_bars`, analyzer
  version and one value per bar (null in warm-up), aligned to the bar dates.
- **Known-value tests:**
  - hand-calculated short series for each formula;
  - published reference examples where the convention matches (Wilder RSI and ATR,
    MACD);
  - the seeding and the null warm-up are asserted explicitly.

## B. Swing points

**Methods** (every one is computed; ADR-0019's layers read the primary):

| Method | Rule | Knowable at |
|---|---|---|
| `FRACTAL(k)` | high[i] > every high in i−k … i−1 **and** ≥ every high in i+1 … i+k (lows mirrored). Ties go to the earliest bar | bar i + k |
| `ATR(m)` | ZigZag on highs and lows. In an up-leg, track the highest high H (bar j). A swing high at j is confirmed at the first later bar whose low ≤ H − m·ATR[j]. Down-legs are mirrored. The bar is checked for reversal before it can extend the leg. No pivot before ATR exists | the confirming bar |
| `PERCENT(p)` | as `ATR`, with the threshold p % of H (or of L) | the confirming bar |
| `ZIGZAG(p)` | the classic ZigZag on **closes** with a p % reversal | the confirming bar |

**Sensitivities:**

| Sensitivity | FRACTAL k | ATR m | PERCENT p | ZIGZAG p |
|---|---|---|---|---|
| MICRO | 1 | 1.0 | 5 | 5 |
| MINOR | 2 | 2.0 | 10 | 10 |
| INTERMEDIATE | 3 | 3.0 | 15 | 15 |
| MAJOR | 5 | 5.0 | 25 | 25 |

**Primary swing method (K4):** `primary_swing_method = "ATR"`,
`primary_swing_sensitivity = "INTERMEDIATE"`, both in configuration. Structure,
Fibonacci, divergence and patterns read "the primary swings" through that setting, never
a named method. Changing it is a configuration change that changes the analysis version.
It needs no code change.

**Swing record:**

- swing_id: deterministic, from method, sensitivity, segment and `bar_date`;
- security_id, timeframe, continuity_segment_id, method, sensitivity;
- type (HIGH or LOW);
- **`bar_date`** (the pivot bar) and **`known_at`** (the confirming bar, never earlier);
- price: the bar's high or low, kept as the bar's exact decimal text for output;
- bars_from_previous, price_change (from the previous opposite swing), atr_change =
  price_change / ATR[`bar_date`];
- strength = |atr_change|, null in ATR warm-up.

The leg still in progress, for each ZigZag-type method and sensitivity, is reported
separately with `confirmed = false` and `known_at = null`. It is never a swing:
- it is never in the swing list;
- only a later confirming bar can produce a swing at that bar, and that swing is then
  `known_at` that later bar, never retroactively;
- pattern candidates may cite it as unconfirmed (ADR-0022), and nothing treats it as
  confirmed.

**Implementation rules (Phase 2):**

- **Single pass.** Each method is one causal left-to-right pass over the segment's
  complete bars. It decides everything at bar `t` from bars `0…t`. The engine contains
  no backward shift, centred window, back-fill or reversed scan; a layer test enforces
  this.
- **The forming week is never scanned.** It cannot confirm, extend or create a swing,
  so the last pending leg is taken from complete bars too.
- **Special sessions.** A swing confirmed by a complete bar that closed on a non-regular
  session is `provisional` (ADR-0015). Special sessions never bypass completeness.
- **Changes are measured from the previous swing** of the same method and sensitivity,
  in bar order (`bars_from_previous`, `price_change`, `atr_change`).
  - ZigZag-type swings alternate HIGH and LOW, so this is the leg.
  - FRACTAL may give two highs (or lows) in a row, and an outside bar may be both.
  - FRACTAL sensitivities nest: every MAJOR pivot is also an INTERMEDIATE, MINOR and
    MICRO pivot.
- **Ties.** Equal FRACTAL highs go to the earliest bar. A ZigZag bar that would reverse
  both tracked extremes before the first pivot confirms the earlier one (a high on a
  tie).
- **Causality is proved three ways by tests:**
  - **prefix stability:** a run on the bars up to T equals the full run's swings with
    `known_at ≤ T`, field for field;
  - **sufficiency:** the bars up to a swing's `known_at` produce it;
  - **future independence:** replacing every bar after a cut-off leaves every swing
    known by the cut-off unchanged.

  Every swing has `bar_date ≤ known_at`.

## C. Market structure

Built from the **primary, confirmed** swings, in `known_at` order:

- **Labels.** Each swing high is compared with the previous swing high:
  - HH if higher by more than `equal_tolerance_atr` (0.25) × ATR;
  - LH if lower by more than that;
  - EQH otherwise.

  Swing lows are labelled HL, LL or EQL the same way. Each label is known at the later
  swing's `known_at`.
- **Breaks.** Breaks are judged only on **complete** bars, by **close**:
  - A bullish break: close > the latest confirmed swing high (the level) + `break_atr`
    (0.10) × ATR.
  - A bearish break: close < the latest confirmed swing low − `break_atr` × ATR.
  - A level can be broken once.
- **Regime and events:**
  - A break in the direction of the current regime, or the first break when there is no
    regime, is a **BOS**. It sets the regime.
  - A break against the regime is a **CHoCH**. It starts TRANSITION.
  - A BOS in the new direction then sets the new regime.
- **Every event records:**
  - kind and direction;
  - `bar_date` (the breaking bar) and `known_at` (the same);
  - the level and the swing that defines it;
  - the swing that defines the invalidation;
  - the confirmation condition (the close rule above);
  - the invalidation condition: a close back beyond the opposite swing of the leg;
  - provisional (closed on a special session), segment, analyzer version.
- **Trend state** at `as_of`, in priority order:
  1. **RANGE:** no regime yet, or the last `range_swings` (4) confirmed swings lie
     within `range_width_atr` (3.0) × ATR.
  2. **TRANSITION:** a CHoCH with no BOS in its direction yet.
  3. **STRONG_UPTREND:** regime up and the latest high and low labels are HH and HL.
  4. **WEAKENING_UPTREND:** regime up, otherwise.
  5. **STRONG_DOWNTREND** and **WEAKENING_DOWNTREND:** mirrored.

  The state carries `since`, the swings and events that determine it, and `provisional`.

**Implementation rules (Phase 3):**

- **Inputs.** Structure reads `SwingResult.primary()` and the ATR series, nothing else.
  It never finds a pivot itself and never names a method (a layer test enforces both).
  Removing every non-primary swing set changes nothing.
- **Order inside one bar.** Breaks are judged first, against levels known *before* the
  bar. Swings that the bar confirms become known after that.
  - A level can never be broken by the bar that makes it known.
  - An event's invalidation swing is the opposite swing known before the break.
- **Thresholds.** A break uses ATR at the breaking bar. A label's equality band uses
  ATR at the labelled swing's bar. Both are known at those bars.
- **Each event records:**
  - kind and direction;
  - `bar_date = known_at`, the triggering complete bar;
  - the close, the level and the level's originating swing;
  - the prior regime and prior trend state;
  - the confirmation and invalidation conditions, as data (rule, level, buffer, ATR,
    threshold, swing) with a description;
  - the segment, and `provisional` (the bar closed on a non-regular session).
- **Trend history is append-only.** A new entry is added whenever anything but `since`
  changes, including a provisional state being held by a regular week. So the state as
  of T is the last entry with `since ≤ T`.
- **Causality** is proved as for swings: prefix stability for events, labels and history,
  and future independence. The forming week cannot break structure: a break seen inside
  a week exists only once that week's bar is complete, and is `known_at` that bar.

## Testing (layers A–C)

- Known values, seeding and null warm-up for every indicator.
- Determinism: the same input gives the same output, byte for byte.
- **Prefix stability (causality):** for random series (Hypothesis) and for every fixture:
  - indicator values for bars `0…t` from a run on bars `0…t` are identical to the
    full run's;
  - swings and structure events with `known_at ≤ t` are identical too.
- **Segments:** a frame never spans a break (enforced by `run_analyzer`). Two segments of
  one security, analysed separately, share nothing.
- **Swings:** every method and sensitivity on hand-built series, with the expected pivots
  and `known_at` dates, including ties and same-bar reversals.
- **Structure:** HH/HL/LH/LL labels; BOS, then CHoCH, then BOS sequences; every state;
  the forming week cannot break; a special-session close gives a provisional event.
