# ADR-0019: The technical engine: layered, causal, point-in-time, precomputed

**Status:** Accepted · 2026-10-02 (decisions K1–K9 confirmed by Suba the same day)

M8 adds deterministic weekly technical analysis. This ADR fixes the architecture every
layer follows. The methods are defined in ADR-0020 (indicators, swings, structure),
ADR-0021 (levels and evidence) and ADR-0022 (patterns); serving and drawing are in
ADR-0023.

## What it answers, and what it never does

ChartLens describes what a weekly chart shows, and which technically defined conditions
would confirm or invalidate each structure. It never produces BUY/SELL/HOLD, never ranks
securities, and never scores a "best setup". Its only inputs are OHLCV and what can be
derived from them. It uses no fundamentals, news, ratings or sentiment, and no LLM
decides anything.

## Layers

| Layer | Module (`chartlens_engine.…`) | Consumes |
|---|---|---|
| A Indicators | `indicators` | bars |
| B Swings | `swings` | bars, ATR |
| C Market structure | `structure` | primary swings |
| E Fibonacci | `fibonacci` | primary swings (and the primary method at extra sensitivities), ATR |
| D Support / resistance and trendlines | `levels` | primary swings, structure events, Fibonacci, indicators |
| F Divergence, volume, volatility, candles | `evidence` | bars, indicators, primary swings, structure, levels |
| G Pattern candidates | `patterns.candidates` | primary swings (fine sensitivity for flags, pennants, handles) |
| H Validation and status | `patterns.validate`, `patterns.status` | candidates, structure history, levels (role histories), Fibonacci, evidence |
| — Explanations | `explain` | structured facts only |
| — Orchestration | `analysis` | runs A→H in order, typed outputs passed explicitly |

Fibonacci (E) runs before support/resistance (D), because its active levels are zone
sources (ADR-0021, Phase 4 rules). The letters keep the ADR sections stable.

Each layer is a separate module behind the existing `Analyzer` contract
(`chartlens_engine.interfaces`). There is no all-in-one function: the orchestrator only
sequences the layers. `run_analyzer` stays the single enforcement point for the
bar-frame contract, the `as_of` guard and the comparability guard (ADR-0015).

## The causal event model (first-class invariant)

Every object the engine produces that refers to a moment in the chart carries two dates.
They are never merged:

- **`bar_date`**: the market bar the object is anchored to (where the chart draws it).
- **`known_at`**: the date of the first bar at which the object became knowable. This
  is always on or after `bar_date`.

> Example: a swing low anchored on 2025-06-20 that the method can only confirm two bars
> later has `bar_date = 2025-06-20`, `known_at = 2025-07-04`. An analysis as of
> 2025-06-25 must not see it, even though its bar is before that date.

Statuses carry their own history: `status_history = [(status, date), …]`. Each entry is
dated by the complete bar that caused it. So the state of anything **as of T** is:

- the objects with `known_at ≤ T`;
- each with the last status entry dated ≤ T.

Two kinds of output follow from this:

- **Events:** swings, structure events, divergences, candles, breakouts and patterns,
  with their status histories. They carry `known_at` and are filtered as above.
- **Current state:** the trend state, active zones, active Fibonacci structures and the
  latest indicator values. These are a function of the bars ≤ `as_of`, recomputed for
  each `as_of`.

**Causality requirement:** every layer must be *prefix-stable*. Running the engine on
the bars up to T must give exactly the objects and statuses of the full run, filtered as
above. A property test enforces this for every layer (ADR-0020 §Testing). A layer that
looks ahead fails it.

## Point-in-time rules

1. **`as_of`.** The engine receives only bars dated ≤ `as_of` (`run_analyzer` refuses
   anything else). Historical runs use bars rebuilt as of T by the point-in-time weekly
   reader: daily rows ≤ T, factors with ex-date ≤ T, segments known by T (ADR-0014).
2. **The forming week.** A bar with `is_complete = false` (only ever the last) may be
   shown. Its indicator values are marked provisional. It never confirms anything: no
   trend transition, BOS/CHoCH, breakout, pattern confirmation, failure or completion.
   Confirmation logic reads `confirmable(bars)` only.
3. **Special-session closes.** A confirmation decided on a complete bar whose close came
   from a non-regular session is recorded with `provisional = true` until the next
   regular complete week holds it (ADR-0015).
4. **No wall clock.** The engine never reads the time, environment, files, network,
   GCS, Firestore or GitHub. The layer tests already enforce this; they are extended to
   the new modules.

## Continuity segments

Analysis runs on the security's **current valid segment only** (K2). Earlier segments
are closed history: they are drawn, but not analysed. Nothing crosses a break:

- indicators warm up from the segment's first bar;
- swings, structure, zones, Fibonacci, divergence and patterns use only that segment's
  bars;
- a structure that would need bars from both sides of a break does not exist.

A segment shorter than an indicator's warm-up simply has no value for it. RELIANCE (since
July 2023) has no 200-week SMA. That is the methodology, not missing data.

## Configuration and versioning

- **Two methodology hashes (K1):**
  - `methodology_hash` stays exactly as it is: universe, weekly, adjustment, data
    quality, identity. Changing an analysis threshold never changes weekly data.
  - A new `analysis_methodology_hash` covers the `[analysis]` configuration section
    only: every period, multiplier, tolerance, threshold and the primary swing method.
    Every analysis-affecting setting lives there. There are no unexplained constants in
    code; a constant that is part of a definition (e.g. 100 in RSI) is documented in
    ADR-0020.
- **Versions:**
  - each analyzer has a `version`;
  - the engine has `__version__`;
  - `analysis_version` = hash of (engine version, every analyzer's name and version,
    `analysis_methodology_hash`).
  - A result is identified by (`analysis_version`, `weekly_version` of its input, its
    security and segment). Changing a definition without changing a version is a bug;
    the golden tests catch it.
- **Every result records:** `as_of`, `analysis_version`, `analysis_methodology_hash`,
  the data `methodology_hash`, `weekly_version`, `data_version`, the serving
  `meta_version` it was published in, and the engine and analyzer versions.
  `analysis_timestamp` is the publication time recorded by the pipeline. It is never
  read inside the engine.

## Where it runs (K3, K9)

- **Precomputed in the pipeline.** A new tracked stage, `ANALYSIS`, runs between
  `WEEKLY` and `PUBLISH_SERVING` (amends ADR-0018: seven stages). For each security it:
  - reads the valid-segment weekly bars from the weekly files;
  - runs the engine;
  - writes the result.

  A result is reused when its key (weekly file hash, `analysis_version`) is unchanged.
- **Published with the snapshot.** Results are content-addressed and published with the
  serving snapshot (ADR-0023), so the API serves analysis exactly as it serves bars:
  only what the pointer names.
- **Historical `as_of`.** Through the CLI and tests only in M8. The API serves the
  published, latest analysis and refuses `?as_of` (ADR-0016).
- **Cost.** It is measured per layer (indicators, swings, structure, levels, evidence,
  candidates, validation, serialization) and optimized where the measurement points.
  The target is minutes per run. Correctness and clarity come first; the target is not
  an acceptance criterion.

## Rejected

- **Computing analysis in the API or the browser.** That would break ADR-0016 and
  ADR-0017.
- **One combined methodology hash.** Every tolerance change would rebuild weekly data.
- **Bridging segments for indicator warm-up.** That would bring the pre-break regime
  into post-break analysis.
- **Back-dating knowable events to their bar.** That would be look-ahead.
