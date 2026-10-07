# Investigation 0001: analytical price-level outputs outside the valid market-price domain

(Filed as "negative analytical price levels"; retitled at review, §7.)

**Status:** **ACCEPTED FOR METHODOLOGY REVIEW** · 2026-10-07. Split into 0001-A, 0001-B
and 0001-C (§7); no engine change approved. Read-only. Nothing in ChartLens was
changed: no analytical behaviour, explanation behaviour, stored value, published
artifact, 6e, ADR-0028, the claim checker, the API, the chart panel or the publication
schema.

**Why:** the 6e explanations restated stored values verbatim and made readable some
price levels that fall outside the valid market-price domain, though not outside
mathematics (ADR-0028 §11; e.g. RANEHOLDIN: a measured-move zone of
−358.8337 to −234.6086 with the price near 1,536). Suba's instruction: census every
negative-valued analytical level from the **authoritative analysis documents** (not the
explanation artifacts), classify, determine the construction behind each, and stop.

## 1. Method and provenance

- Two throwaway probe runs (read-only towards the lake; overlay on the runner). Each
  re-ran the ANALYSIS stage on the live inputs and read the resulting analysis manifest,
  every analysis document, both event datasets and the weekly files. Both produced
  `analysis_set_hash` **cada6bbd5275…**, identical to the 6e checkpoint's: the census
  covers exactly the 6e analysis set (`analysis-d1e09c0ddd55`, weekly `wk-8931d5539ffd`,
  data to 2026-10-07, 3,193 securities).
- **Pass 1** walked every number in every document (indicators per series) and every
  event row, and recorded every negative value with its path, owning object, nearest
  date, `document_sha256`, pattern or Fibonacci attributes, whether the object is in
  `current`, and the security's last close. Rows whose field is a price-named field are
  kept individually (38,067 rows, `negative_price_rows.csv.gz` in the probe results).
- **Pass 2** visited every object of the three constructions found in pass 1 (negative or
  not) to give denominators, the construction inputs, whether a corporate-action
  adjustment falls inside the object's span (the weekly `close / raw_close` ratio differs
  between the span's ends), and the security's data-quality status.

## 2. What is negative

Of all negative numbers in the documents, almost all are legitimately signed quantities,
not prices: swing `price_change` / `atr_change` (5.2 M), MACD, ROC and OBV values (3.0 M),
candle-context changes, label differences in ATR, slopes, change ratios (8.88 M values on
35 paths in all). They are out of scope.

**Price-valued fields with negative values: exactly three constructions** (plus copies
of one of them). No other price field is ever negative: no swing price (minimum stored
swing price 0.022577), key point, zone bound, horizontal level, structure-event level,
active-trendline value or breakout `level_at_break`; the event datasets hold no negative
number at all.

| Construction | Negative | Out of | Share | Objects | Securities | Most negative | In `current` |
|---|---|---|---|---|---|---|---|
| Fibonacci extension level (down legs) | 36,338 levels | 121,803 down-leg extension levels | 29.8% | 24,495 of 40,601 down legs (60.3%) | 2,412 | −28,426.332 | 2,395 levels |
| Pattern measured-move zone (`target_low` / `target_high`) | 932 zones (932 low, 667 high) | 15,718 zones | 5.9% (13.8% of downward; 0 of 8,966 upward) | 932 | 652 | −3,252.366 | 168 values |
| Pattern boundary-line coordinate (`start_value` / `end_value`) | 121 lines (115 start, 6 end) | 26,672 lines | 0.45% | 121 | 118 | −1,055.606 | 5 |
| Copies of a measured-move zone in a COMPLETED status entry (`measured_values.target_low`) | 9 | — | — | 9 | 7 | −22.873 | 0 |

In all, 2,412 securities carry at least one negative price level; 1,380 of them in an
object the engine lists as current (so on the default chart layers and in the 6e
explanations). Median value relative to the last close: −0.31× (Fibonacci), −0.20×
(measured moves).

## 3. Each construction, from the engine code

### 3.1 Fibonacci extensions — mathematically exact, outside the price domain (category 1)

`fibonacci/analyzer.py`: `height = counter.price − anchor.price`; extension level
`price = anchor.price + e × height` for e ∈ {1.272, 1.618, 2.618}. For a down leg this is
`anchor − e × leg`, which is negative exactly when the leg's decline `leg / anchor`
exceeds `1 / e` (78.6%, 61.8%, 38.2%).

The data confirm the algebra exactly: the smallest decline among legs with a negative
2.618 extension is 0.38199 (= 1/2.618); counts by ratio: 2.618 → 24,495 (60.3% of down
legs), 1.618 → 8,515 (21.0%), 1.272 → 3,328 (8.2%). Weekly down legs are deep on NSE:
median decline 43%, p90 75%. Up legs (40,942) never produce a negative level, and
retracements (between anchor and counter) cannot.

**Assessment:** the formula is applied correctly; the result is an arithmetic projection
that leaves the valid price domain whenever the leg is deep enough. It is an **intended
price-level output** (stored as a level, drawn, explained). Category 1.

### 3.2 Measured-move zones — mostly category 1, with a construction gap for triangles (categories 1 and 2)

`patterns/lifecycle.py` `_measured_move`: `centre = level + direction × height`; zone =
`centre ± mm_zone_atr × atr_pre` (V patterns: `FULL_RETRACE`, centre = the starting
extreme). A downward zone is below zero when `height > level` (or, for `target_low`
only, nearly so, by the half-zone).

- Every negative zone is downward (direction −1); none upward.
- Height ÷ level for downward zones: negative ones p10 0.97, median 1.21, p90 1.97,
  maximum 7.39; non-negative ones median 0.54.
- By family (negative / all zones): **triangle 537 / 1,586 (33.9%)**, head-and-shoulders
  139 / 943 (14.7%), rectangle 119 / 1,952 (6.1%), wedge 24 / 557 (4.3%), flag 10 / 285,
  triple 10 / 287, pennant 8 / 269, double 82 / 3,833 (2.1%), rounding 2 / 262,
  V 1 / 4,253, cup-and-handle 0 / 1,491.
- Latest status of the 932 patterns with a negative zone: CONFIRMED 556, FAILED 352,
  RECOGNISED_AFTER_BREAKOUT 15, COMPLETED 9 (the last: a low reached a zone straddling
  zero, so the zone's negative `target_low` is also recorded in `measured_values`).

**Assessment.** The arithmetic is applied as defined (category 1: a classical
measured move projected linearly past zero). For triangles there is also a construction
question (category 2): a triangle's `height` is its width at its first defining bar
(`w0 = upper.at(first) − lower.at(first)`, `patterns/candidates.py`), and the candidate
rules bound height only from below and only in ATR (`min_height_atr` = 2.0); nothing
bounds a pattern's amplitude relative to its price. Examples: a symmetrical triangle
from LOW_1 355.50 to HIGH_2 1,649.95 (a 4.6× swing inside one "triangle"), level
1,136.81, height 1,631.36, zone −561.15 to −427.95; a descending triangle from 8.74 to
21.50, level 10.92, height 16.73, zone −6.52 to −5.12. Whether such shapes should be
triangles at all is the methodology question, separate from where the projection lands.

### 3.3 Pattern boundary-line coordinates — a valid intermediate coordinate, drawn (category 3)

`patterns/analyzer.py`: each stored line is evaluated at the pattern's first and last
defining bars: `start_value = line.at(first.bar_index)`. A line is anchored on its own
touches; the first defining point of a wedge or triangle usually belongs to the *other*
line, so the lower line's `start_value` is that line extended back beyond its own first
touch. With a steep line on a large relative range it crosses zero, e.g. a symmetrical
triangle whose lower line (anchored at LOW_2 61.90, slope 2.418 per bar) is −46.92 at
HIGH_1's date; a rising wedge's lower line (anchored at 14.60) is −10.57 at its HIGH_1.
Concentrated in WEDGE_RISING (60 of 616 lines, 9.7%), symmetrical and ascending
triangles (2.0%, 1.7%); 0.45% of all lines.

**Assessment:** a geometrically correct coordinate of the fitted line; the engine does not
use it to decide anything (confirmation evaluates the line at the deciding bar). But it
is stored as the line's drawable start, so the 6d chart draws it and a 6e confirmation-
boundary claim can quote it. Category 3, with a presentation consequence.

### 3.4 Data quality and adjustment (category 4) — not the primary cause

- No stored bar-derived price is ≤ 0 (swing prices, key points, zone bounds from swings).
- Negative rates are somewhat **higher** where an adjustment falls inside the object's
  span or the security is USABLE_WITH_WARNINGS:

  | | adjustment in span | no adjustment | USABLE_WITH_WARNINGS | USABLE |
  |---|---|---|---|---|
  | measured moves negative | 72 / 663 (10.9%) | 860 / 15,055 (5.7%) | 359 / 4,860 (7.4%) | 573 / 10,858 (5.3%) |
  | Fibonacci extensions negative | 1,531 / 3,189 (48.0%) | 34,807 / 118,614 (29.3%) | 12,201 / 34,968 (34.9%) | 24,137 / 86,835 (27.8%) |

- But 92% of negative measured moves (860 / 932) and 96% of negative Fibonacci levels
  (34,807 / 36,338) have **no** adjustment in their span. The constructions explain the
  negatives without any data event; the elevated rates are a correlation (deep moves and
  corporate actions both occur around large repricings), not evidence that adjustment
  produced them. The 72 + 1,531 adjusted-span cases could be sampled in a data-quality
  follow-up if wanted.

## 4. Classification summary

| Construction | Intended output or intermediate | Category |
|---|---|---|
| Fibonacci down-leg extensions | intended price level | **1** — mathematically correct, outside the price domain |
| Measured-move zones | intended price level | **1** for all families; **2** in addition for triangles (amplitude unbounded relative to price; height = width at the first bar) |
| `measured_values.target_low` in COMPLETED entries | a recorded copy of the zone | follows the zone (1) |
| Pattern line `start_value` / `end_value` | intermediate geometric coordinate, but stored as the drawable line end | **3** — valid coordinate; presentation consequence |
| (all) | — | **4** not supported as the primary cause; mild correlation noted |

A common thread, stated as an observation only: all three constructions are **linear in
price** on weekly histories that routinely span moves of 40–80% or more. That is the
methodology question for review; this report does not propose an answer.

## 5. For the methodology review (not decided here)

Suba's constraints apply: no clamping, no "negative looks bad → zero", no change to 6e.
The census suggests these questions:

1. Fibonacci and measured moves (category 1): keep the exact arithmetic value and add an
   explicit engine state for a level outside the price domain (e.g.
   `OUTSIDE_PRICE_DOMAIN`), versus changing the construction (e.g. a stated projection
   convention), versus leaving as is.
2. Triangles (category 2): whether pattern candidates need an amplitude constraint
   relative to price (they are bounded only below, in ATR), and whether `height` = width
   at the first bar is the intended definition for wide triangles.
3. Pattern line ends (category 3): whether the stored drawable span of a line should
   start at the line's own first touch rather than the pattern's first defining bar
   (this would also touch the ADR-0027 decision 1b "drawable segments" item).
4. Data quality (category 4): whether to sample the adjusted-span cases.

Any change would be an engine methodology change with a new `analysis_version`, then new
explanations (`explain_version` unchanged).

## 6. Reproduction

Probe scripts (throwaway branch `probe/pattern-stats`): `scripts/census_negative_levels.py`
(pass 1, commit 3dfe9fe) and `scripts/census_pass2.py` (pass 2, commit d1a05c3); results
were pushed to `probe-results/pattern-stats`. Both branches are on the repository
cleanup list.

## 7. Review outcome (Suba, 2026-10-07)

**Verdict: ACCEPTED FOR METHODOLOGY REVIEW.** None of the §5 options is approved. No
change to 6e, ADR-0028, explanation artifacts, analysis documents, publication, the API,
the chart panel, M3 or current engine behaviour.

**Recorded finding:**

> The engine contains mathematically valid linear projections and geometric coordinates
> that can fall outside the valid market-price domain. This is not, by itself, evidence
> of an arithmetic defect. Triangle geometry and drawable line extent require separate
> methodology review.

**Terminology.** The issue is *analytical price-level outputs outside the valid
market-price domain*, not "negative prices". A negative traded price is impossible in
the market-price domain; a negative projection is not impossible mathematically. That
is why the explanation layer was right to report the value. The census also shows the
explanation layer did not create the behaviour; it surfaced it.

**Decisions by issue:**

| Issue | Decision | Why |
|---|---|---|
| Fibonacci extensions outside the domain | no change yet | formula correct; a domain/semantic question |
| Measured-move zones outside the domain | no change yet | formula correct; triangles reviewed separately |
| Triangle amplitude | separate methodology question (0001-B) | possibly a genuine pattern-definition issue |
| Pattern-line coordinates | chart-geometry review (0001-C) | coordinate valid; drawable span may be wrong |
| Corporate-action correlation | M3 not reopened | not the primary cause; keep the adjusted-span rate as a diagnostic |
| 6e / explanations | no change | they correctly expose authoritative values |

**Guardrails for the follow-ups.**
- Exact values are kept. Clamping to zero destroys what the projection was.
- A share of out-of-domain values (29.8%, 33.9%) is a description, not evidence that a
  method is wrong.
- No constraint is added in order to remove awkward outputs. A rule changes only if it
  fails to implement the definition ChartLens intends.

### 0001-A: Price-domain semantics

Fibonacci extensions, measured-move zones, and any future construct able to produce a
coordinate outside [0, ∞). Question: *does ChartLens preserve mathematically valid
projections outside the feasible market-price domain, and how should their domain status
be represented?* A reusable concept (e.g. `price_domain: IN_DOMAIN | OUTSIDE_DOMAIN`)
may follow, but nothing is added until the semantics are defined across all level types
and approved.

### 0001-B: Triangle geometry

Question: *what is the formal definition of a triangle ChartLens intends to implement,
and does the existing candidate geometry faithfully represent it?* If it does, keep it,
even where projections cross zero; if not, correct the definition independently of the
out-of-domain observation. Scope: the width definition (height = width at the first
defining bar), amplitude relative to price, to ATR and to the base price, duration,
slope and convergence quality, and `definition_fit`. Method: compare the 537 triangles
whose zones are outside the domain with the 1,049 that are not, as populations, to decide
whether the former are the extreme tail of otherwise valid triangles or a qualitatively
different population. Do not rely on the worst examples alone.

### 0001-C: Drawable line extent

Question: *should a pattern boundary line be drawable before its own first anchor
touch?* Scope: the pattern's first defining bar against the line's first touch;
backward extrapolation; 6d rendering; confirmation evaluation (which evaluates the line
at the deciding bar); and whether changing the drawable extent affects analytical
identity or only presentation. Decide explicitly whether *geometry* and *drawable span*
must be separate concepts. This belongs with ADR-0027 (chart geometry) and can be
evaluated without touching pattern detection.

Each of 0001-A, B and C is a read-only investigation with its own report and review;
any resulting engine change would need its own approval, a new `analysis_version`, and
new explanations.
