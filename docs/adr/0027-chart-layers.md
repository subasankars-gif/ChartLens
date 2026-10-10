# ADR-0027: Chart layers

**Status:** Accepted · 2026-10-07. **Phase 6d CLOSED** (2026-10-07). Follow-ups (§10):
10.2 (level knowability) implemented as an accepted as-built correction; 10.1 (spans
across missing weeks) open. Review clarifications frozen below (decisions
1–5 approved; the `known_at` ≤ snapshot `as_of` invariant, per-component snapshot
identity, a declarative `/chart`, and the stricter property tests added). It extends
ADR-0017 (the faithful-visualization rule) and ADR-0023 (the layer toggles), and builds
on ADR-0026 (the analysis API). The weekly chart semantics, segment boundaries,
`known_at` and layer authority are settled here first; styling comes after.

> **6d may select and render authoritative analytical objects; it must not infer,
> recompute, rank, merge, or reinterpret them.**

```text
immutable analysis document ─► API (serves, selects whole sections) ─► frontend
                                                     (selects objects, maps stored coordinates to pixels)
```

## 1. What a chart layer is

A chart layer is one of exactly two things:

1. **An existing object of the published `TechnicalAnalysis`**, drawn from its stored
   fields; or
2. **A deterministic selection of existing objects** by a stored attribute or an
   engine-given list: swings of one method and sensitivity; the patterns the engine
   lists in `current.included_pattern_ids`; the Fibonacci structures in
   `current.fibonacci_ids`.

A layer is **never** a newly computed analytical object. Nothing in 6d fits, detects,
tests, extends, interpolates, smooths, scores, ranks, merges or decides.

**Layer authority** (each toggle, its source, and the stored fields that place it):

| Layer | Source (document path) | Placed by (stored fields only) |
|---|---|---|
| Candles, volume | `/weekly` bars | `last_session_date`, OHLCV decimals |
| Moving averages, Bollinger | `indicators.series` (`sma_*`, `ema_*`, `bollinger_*`) | the section's own `bar_dates[i]` with `data[i]`; null = no point |
| Oscillator pane (RSI, MACD) | `indicators.series` | as above |
| Swings | `swings.swings` with `method`, `sensitivity` = `swings.primary_method`, `primary_sensitivity` by default | `bar_date`, `price`; `known_at` in the tooltip |
| Developing extreme | `swings.pending` | **not drawn in 6d** (see §8): stored with `known_at` null, so the `known_at` ≤ `as_of` rule refuses it |
| Market structure | `structure.labels`, `structure.events` | labels: `bar_date`, `price`; BOS/CHoCH: `bar_date`, `level`, `known_at` |
| Trend state | `structure.trend_history` | each entry's `since` to the next entry's `since` (stored dates) |
| Support/resistance zones | `levels.zones` (current only, by the engine's definition) | `price_low`, `price_high`, from `first_seen` to `levels.state_date` |
| Trendlines | `levels.trendlines` | the stored touch points (`bar_date`, `line_value`) and, for an active line, `levels.active_trendlines[].value` at `levels.state_date` (§2.2) |
| Fibonacci | `fibonacci.structures` (default: `current.fibonacci_ids`) | each level's `price`, from `counter_bar_date` to the end of its stored status span |
| Divergence | `evidence.divergence.divergences` | (`date_start`, `price_1`) to (`date_end`, `price_2`) on price; `indicator_1`, `indicator_2` in the oscillator pane |
| Patterns | `patterns.patterns` (default: `current.included_pattern_ids`) | `geometry.key_points` (`bar_date`, `price`); `geometry.lines` (`start_date`, `start_value`, `end_date`, `end_value`); confirmation and invalidation levels; the measured-move zone (`target_low`, `target_high` from `target_calculated_at`) |
| Breakout events | `/breakout-events?source=…` (off by default; level events are many) | `bar_date`, `level_at_break`; follow-ups at their `effective_date` |
| Volume, volatility, candle evidence | `evidence.volume.events`, `.volatility.events`, `.candles.events` | `bar_date` (markers); candles only when cited (ADR-0023) |

The default chart stays candles and volume only (ADR-0023). The evidence panel lists
objects in the order the engine stored them; `definition_fit` is shown with its
components and the wording "definition fit", and is never used to order across families
(ADR-0022 §18.1).

## 2. Coordinates come from authoritative bars and objects

**Every drawn point is a stored (date, value) pair.** The x coordinate is a stored date
(`bar_date`, `known_at`, `start_date`, …) mapped to the bar whose `last_session_date`
equals it; the y coordinate is a stored price, level or value. The frontend never takes
a date or price from an array position (`bar_index`, `anchor_index` and similar stored
integers are ignored for drawing).

- Every object is also identified for the reader: `security_id`, segment, and its id
  (`swing_id`, `pattern_id`, `zone_id`, `event_id`, …) in the tooltip and panel.
- A stored date that matches no bar of the snapshot is **not drawn** and is counted in a
  visible "unplaced objects" note, never snapped to the nearest bar. **This is an absolute
  invariant.** The contract is "unplaced objects are refused and surfaced"; a real-data
  count of 0 is checkpoint evidence, not the contract.
- Missing weeks: x positions are the bars that exist. The frontend never inserts,
  removes or interpolates weeks; a week with no bar has no candle. (Drawing visible gaps
  would need the weekly product to state them; the frontend will not infer a calendar.)
- Lightweight Charts draws a straight segment between two stored points. That is
  rendering two authoritative points, not computing a value; a line is never drawn past
  its last stored point.

### 2.1 What this means for each geometry

- **Pattern lines** are complete: start and end are stored.
- **Fibonacci levels** are constant prices over a stored span.
- **Zones** are bands over stored dates.
- **Trendlines need a decision (decision 1).** The engine stores the first anchor's
  price, a slope per bar and the touches, but not the second anchor's price or a value
  at the end of the line's life. Drawing the line to its break or to today would mean
  the frontend computing `price + slope × bars`.

### 2.2 Trendlines (decision 1)

- **(a) Approved for 6d: draw only stored points.** The segment runs through the
  stored touches (first to last), and for an active line on to its stored value at the
  state date. A broken line ends at its last touch. Nothing is extrapolated, and the
  drawing never claims more than the engine recorded.
- **(b) Later, if wanted: an engine amendment.** The levels layer would publish each
  line's drawable segment (start and end date and value, as pattern lines already do)
  under a new levels version. That is a methodology change with its own review and
  recompute, **not a 6d scope expansion**.

## 3. One snapshot per chart: a thin envelope (decision 2)

A chart needs bars and analysis **from the same snapshot**. Two separate requests
(`/weekly`, then `/analysis`) can straddle a publication and mix snapshots A and B.

Approved: **`GET /api/v1/securities/{id}/chart?sections=…&segments=valid|all`**, a thin
envelope read from one snapshot in one request:

```text
chart
 ├── envelope    meta_version, data_as_of, versions, analysis provenance, document_sha256
 ├── bars        exactly what /weekly returns for the same segments choice
 └── document    the requested whole sections, exactly what /analysis returns
```

No new model: `bars` is the `/weekly` payload, `document` is the `/analysis` payload,
both verbatim.

- **Every component names its snapshot.** The response identifies the exact
  `meta_version` used for each returned component (envelope, bars, document), so the
  frontend never infers consistency; it checks that they are equal.
- **The request is declarative**: the security, the timeframe (weekly), the sections and
  the segments choice, plus presentation-only options. No analytical-sounding parameters
  (`sensitivity=major`, `confidence>70`, `pattern=best`, …) unless they are pure filters on
  a stored attribute. The server does *published document → select requested existing
  sections → return*, never *calculate a chart-specific analytical subset*. Further sections (as the reader turns layers on) are fetched from
`/analysis` and accepted only if their `meta_version` equals the chart's; otherwise the
chart reloads whole. The alternative, comparing `meta_version` across two requests on
the client, works too but makes every caller responsible for it.

## 4. Time: nothing before it was knowable

**The chart shows the published analysis as of the snapshot's `as_of`, and nothing
else.** Point-in-time charts (`?as_of`) stay refused (K3). Belonging to a snapshot does
not by itself make an object knowable on its drawing date, so the rule is stated at the
adapter boundary:

> **The chart may display an object only if that object exists in the selected
> published snapshot and its own `known_at` is ≤ the snapshot `as_of`. The object's
> geometry is drawn at its stored formation coordinates; `known_at` affects
> visibility/annotation, not geometry.**

Each adapter enforces it: an object without a stored `known_at` ≤ the snapshot's
`data_as_of` is refused and counted (it should never happen; publication guarantees it,
and the adapter does not rely on that).

- **The lag is visible, never hidden.** An object is drawn where it formed (`bar_date`,
  `start_date`), and it is labelled with when it became known: a swing's tooltip shows
  its confirmation date; a pattern carries a "recognised" marker at its `known_at`; a
  BOS/CHoCH is marked at its `known_at`. The chart never implies an object was known at
  its formation bar.
- **Status is the stored history, read as stored.** A pattern's, divergence's or
  Fibonacci structure's displayed status is the last entry of its stored
  `status_history`, the engine's own definition of current status. It is never derived
  from prices.
- **Provisional is shown as provisional**: the forming week is hollow (as today), and
  objects flagged `provisional` (confirmed by a week closing on a non-regular session)
  are drawn dashed with that reason.

**Replay is out of 6d (decision 3).** Scrubbing the chart back to a date T would mean
showing, at T, only objects with `known_at` ≤ T and only the history entries known by T.
That is a selection by stored fields and would be sound for objects with complete
histories (swings, structure events, patterns, divergences, breakout events). It is
**not** sound for state the engine keeps only as of `as_of`: current zones, active
trendlines, current Fibonacci, current relevance and the trend snapshot have no history
to filter. A replay that showed today's zones at T would leak the future. So: no replay in
6d. If it comes later, it either uses published historical analyses or replays only the
complete-history layers, with the others hidden, under its own review.

## 5. Segments: no structure across a continuity break

- **Analysis covers the current segment only** (K2), so every analytical object belongs
  to it. The frontend checks each drawn object's `continuity_segment_id` (or, for
  indicators, the section's context segment) against the current segment and refuses to
  draw any other, counting it as unplaced.
- **The default chart is the current segment** (`segments=valid`), so overlays and bars
  cover the same span.
- **"All segments" (history) view:** earlier segments are drawn as today: muted, each its
  own series, with a hatched break band and its cause at every boundary (ADR-0014,
  ADR-0017). **Analytical overlays are drawn only over the current segment,** and the
  break band stays visible, so no line, zone, level or indicator ever spans a break, and
  nothing implies continuous structure across one.
- `usable_from` equals the current segment's start (ADR-0025); delisted securities show
  their last segment up to their last bar, with its analysis as of that bar.

## 6. Layer loading and size

Documents average 1.7 MB (5.4 MB at most). The chart first loads `bars` plus the small
sections (`identity`, `versions`, `current`, `provenance`) and fetches heavier sections
when their layer is turned on, cached per `meta_version`. Section selection is the API's
only shaping (ADR-0026 decision 7).

## 7. Testing and checkpoint evidence

- **Adapters are pure** (`frontend/src/lib/layers/*`), one per layer: stored object →
  chart primitives. A property test for each: **every rendered coordinate ∈ stored
  (date, value) coordinates** of its input object, so an adapter cannot invent a point.
  The tests also reject, by construction: interpolation (no value between two stored
  values), extrapolation (no point past the last stored one), nearest-date matching (an
  off-bar date is refused, not moved), index-based coordinate reconstruction (stored
  indices are scrambled and the output must not change), and cross-segment coordinates
  (an object or point outside the current segment is refused).
- Unplaced objects (no matching bar, another segment, `known_at` missing or after the
  snapshot's `as_of`) are refused and counted.
- No adapter reads an array position for drawing (lint rule plus tests).
- Status and `known_at` labels come from the stored fields (tests).
- The chart route returns bars and sections from one snapshot, byte-identical to
  `/weekly` and `/analysis` (API tests); a cross-snapshot section is rejected by the
  client.
- End to end: an admin turns layers on, and a fixture pattern renders with its lines,
  status and "recognised" marker.
- Screenshots on real data: a security with a continuity break (history view), a
  delisted security, a forming week, and patterns with their measured-move zones; plus a
  real-data run of the unplaced-object count (expected 0).

## Decisions (approved 2026-10-07)

1. **Trendlines:** (a) draw only stored points in 6d (touches, plus the stored value at
   the state date for an active line); (b) an engine amendment for drawable segments
   later, if wanted.
2. **A thin `/chart` envelope** reading bars and sections from one snapshot, rather than
   two requests checked on the client.
3. **No replay in 6d**, for the reasons in §4.
4. **Default layer selections** come from the engine: primary swings
   (`primary_method`/`primary_sensitivity`), included patterns
   (`current.included_pattern_ids`), current Fibonacci (`current.fibonacci_ids`). The
   reader may switch to "all" for each, which is still selection by stored attributes.
5. **Unplaced objects are refused and counted**, never snapped to a nearby bar.

**Excluded from 6d:** trendline extrapolation, replay, historical current-state
reconstruction, and a second chart model.

**6d checkpoint gate:** real-NSE screenshots of a continuity-break security (history
view), a delisted security, a forming week and measured-move patterns, plus a real-data
unplaced-object count (expected 0, as evidence).

## 8. As built (6d)

Choices made while building, each a selection or a span between **stored** dates; none
computes a value. Listed so the review can confirm or change them.

1. **Placement is one function** (`frontend/src/lib/layers/place.ts`, `admit`). Every
   layer's candidates go through it; an object is drawn **whole or refused whole**
   (never partly), with the reason counted and listed in the panel ("Not drawn: a chart
   object is never moved to a nearby bar").
2. **Developing extremes (`swings.pending`) are not drawn.** The engine stores them with
   `known_at` null (unconfirmed); the invariant refuses objects without a stored
   `known_at`. The layer is omitted rather than counted as unplaced on every security.
   *For review:* keep it out, or have the engine state their knowability.
3. **Knowability of objects without a `known_at` field:** an indicator value is knowable
   at its own `bar_date` (the engine's causal contract: computed from bars through that
   bar); a trend state at its `since` (the engine's definition: the state as of T is the
   last entry with `since` ≤ T). Both are checked against `as_of` like any `known_at`.
4. **Spans, all between stored dates** (as amended, §10.2): zones from the later of
   `first_seen` and the zone's `known_at` → `levels.state_date`; Fibonacci levels from the
   later of `counter_bar_date` and the structure's `known_at` → `fibonacci.state_date`
   for a structure in `current.fibonacci_ids`, else → its last status entry's date;
   pattern confirmation and invalidation levels from the pattern's `known_at` → its next
   status entry's `effective_date` (the section's `as_of` while there is none); a
   measured-move zone from the later of `target_calculated_at` and its status entry's
   `known_at` → the next status entry's `effective_date` (the section's `as_of` for the
   last); a trend state `since` → the next entry's `since` (the section's `as_of` for the
   last).
5. **Indicator lines break at nulls** (a line across a gap would draw values the engine
   did not compute); values at provisional (forming-week) bars are dashed. Oscillator
   reference levels (RSI 30/70 and the like) are not drawn: they are not stored.
6. **Candle evidence** is drawn only when a drawn pattern cites it (`context.evidence_refs`
   or a status entry's `evidence_refs`), per ADR-0023.
7. **Breakout events** come from `/breakout-events`, one stored dataset at a time
   (patterns or levels, never merged), every page checked against the chart's
   `meta_version`.
8. **Sections and snapshots:** the chart loads `/chart` (bars plus `identity`,
   `versions`, `current`, `provenance`, and the sections of any layer already on); later
   sections come from `/analysis` and are accepted only with the chart's `meta_version`;
   any mismatch reloads the whole chart.
9. **Rendering:** one Lightweight Charts series primitive per pane maps stored
   (date, value) pairs through the chart's own time and price scales and draws straight
   segments between consecutive stored points. It adds no point, extends nothing, and
   draws no unbounded price line. Time-only marks (`known_at`, follow-ups, evidence) sit
   along the bottom of the price pane; trend state is a strip beneath them. Long labels
   appear for the object under focus only (legibility). The oscillator pane's scale comes
   from invisible carrier series holding the same stored points.
10. **Lint:** adapters may not read stored positions or slopes (`*_index`,
    `slope_per_bar`, `anchor_value`, `anchor_1_price`) and may not use `*`, `/`, `%`,
    `**` or `Math` (ESLint `no-restricted-syntax`, `frontend/eslint.config.mjs`).

## 9. Checkpoint evidence (6d)

**Tests.** API: `/chart` components byte-identical to `/weekly` and `/analysis`, one
`meta_version` throughout, a read straddling a publication reloads and reads both from
the new snapshot, bars-only with the reason when not analysed or schema < 3, refusals
(unknown sections, `as_of`, bad `segments`), static checks that the route neither ranks
nor accepts analytical-sounding parameters. Frontend (vitest, 93): per layer, over a real
`/chart` fixture (`scripts/layer_fixture.py`: continuity break, forming week, an active
and inactive trendlines, measured moves) and seeded mutations of it: every rendered
coordinate is a stored date and stored number of its object (dots: a sibling pair);
still true with every stored number randomised (interpolation and extrapolation cannot
hide); scrambling stored positions and slopes changes nothing; an off-bar date refuses
the object whole; another segment refuses it; `known_at` after `as_of` or missing
refuses it and never moves geometry; indicator lines never bridge a null. E2E
(`E2E_LAKE=synthetic`, 5 tests, in CI): one `/chart` request, patterns with stored
status and definition-fit components, every layer on with 0 unplaced in the current and
history views, engine lists as defaults, an unanalysed security.

**Real NSE** (throwaway probe, read-only towards the lake; ANALYSIS and a schema-3
publish into a local overlay, then the real API in-process):

| | |
|---|---|
| Securities analysed / charted | 3,193 / 3,193 (`segments=all`, every section, both breakout datasets) |
| Snapshot | `meta-b96930290cd1`; components naming another snapshot: **0** |
| With a continuity break / delisted / forming last week | 371 / 616 / 2,572 |
| Objects drawn (every layer, widest stored selections) | 6,703,479 |
| **Unplaced (refused)** | **0** |
| Timings | ANALYSIS 366 s, publish 158 s, chart dump + count 1,060 s |

The first run counted 8,148 refusals, all `known_at_missing`, all indicator series:
series with no stored value at all (warm-up longer than the history, e.g. `sma_200`
over a short listing). They are not objects with a missing date, so the adapter now
proposes nothing for them (with a test); the rerun drew the same 6,703,479 objects and
refused 0. That is evidence, not the contract: the contract is that unplaced objects are
refused and surfaced. The two numbers are different things: the 8,148 were not
unplaceable analytical objects but series with nothing to draw; the result is **0
objects with a valid drawable coordinate refused or misplaced**.

The current snapshot's document is bounded by `as_of`, and placement checks every stored
status entry's `known_at` as well as the object's, so "the next status entry" that ends
a measured-move zone is always one known within the selected snapshot. (Replay would
need its own treatment; it is deferred.)

Screenshots (real NSE): KOTHARIPRO history view across an unquantified-corporate-action
break (overlays only after the break); INDSWFTLTD, delisted, analysis as of its last bar;
RANEHOLDIN, forming week with an included falling wedge, its stored lines, confirmation
level, measured-move zone and "recognised" mark; CARYSIL with every layer on.

## 10. Follow-ups recorded at closure (do not reopen 6d)

### 10.1 Spans across missing weekly observations

Proposed rule (Suba): *"A drawn span may connect stored coordinates only across
contiguous available weekly observations. A missing weekly observation terminates the
drawable span; the renderer never creates an implicit connection across the missing
observation."*

Status as built: **not enforced, and not guaranteed by the segment rule.** A continuity
break needs a trading gap of more than 65 expected sessions (ADR-0012), so a shorter
gap (a suspension of a few weeks) leaves weeks with no bar *inside* one segment. Today
a path between two stored points, and an indicator line between consecutive stored
bars, is drawn straight across such a gap. The synthetic fixture has none; the number
of real in-segment gaps has not been measured yet.

A methodology question sits underneath: the engine computes on the bar sequence, so its
own geometry (pattern lines, trendlines, indicator windows) already treats the bars on
either side of a missing week as adjacent. Cutting the drawing at the gap would show
less than the engine defines; not cutting it implies continuity the data does not have.
To settle with evidence: (a) measure in-segment missing weeks on real NSE; (b) decide
whether the rule is a rendering rule only or also an engine statement; (c) add the
regression test either way.

### 10.2 Formation geometry vs. when a level became authoritative

Suba's distinction: formation geometry keeps its formation coordinates, but *an
analytical level that becomes authoritative only at `known_at` should not be visually
represented as an established level before that `known_at`.*

Status as built: **the ambiguity is present, by construction, in three spans** (counted
on the fixture; it follows from the engine's definitions, not from the data):

| Span (§8 item 4) | Drawn from | Known at | Fixture |
|---|---|---|---|
| Pattern confirmation / invalidation level | `start_date` → `end_date` | the pattern's `known_at`, after `end_date` | 10 of 10 levels lie entirely before their `known_at` |
| Fibonacci level | `counter_bar_date` → … | the structure's `known_at`, after the counter swing confirms | 20 of 20 begin before `known_at` |
| Support/resistance zone | `first_seen` → `levels.state_date` | the zone's `known_at` | 2 of 5 begin before `known_at` |

Pattern lines, key points, Fibonacci legs and trendline touches are formation geometry
(the shape that formed), and stay where they are.

**Amendment accepted and implemented (2026-10-07, before 6e code; an as-built
correction, not a reopening of 6d).** Rule: `visible_start = later(formation start,
the object's known_at)`, subject to each object's semantics (§8 item 4): a level is
authoritative only once known. Every coordinate is still a stored date and value. Level
primitives (confirmation/invalidation levels, Fibonacci levels, zones, measured-move
zones) are marked `level`, and placement refuses an object whose level would be drawn
before its `known_at` (reason `level_before_known`, counted and listed like any refusal).
A measured-move zone starts at the later of `target_calculated_at` and its status
entry's `known_at` (a pattern recognised after its breakout has a zone calculated at the
breakout bar but known later). Regression tests: every rendered level on the real
fixture starts on or after its object's `known_at`; a level dated earlier is refused;
formation geometry before `known_at` is still drawn. Real NSE after the amendment (same
read-only probe, 3,193 charts, every layer at its widest stored selection): 6,703,479
objects drawn, **0 refused** (none `level_before_known`), 0 snapshot mismatches; the
screenshot gate passes again.
