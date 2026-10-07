# ADR-0028: Explanations

**Status:** Proposed · 2026-10-07. For review before any code (phase 6e). It details
ADR-0024 §9 (the explanation layer) and builds on ADR-0026 (publication, API) and
ADR-0027 (chart layers).

> **An explanation restates published analytical facts in words. It never adds a fact,
> a number, a judgement or an order that the analysis does not already contain.**

```text
published TechnicalAnalysis ──► explain (pure, engine) ──► claims (structured facts + fixed wording)
                                                                │
                                         job stage writes, publication pins, API serves, chart links
```

## 1. What an explanation is

A list of **claims** about one security's published analysis. Each claim is:

| Field | Meaning |
|---|---|
| `claim_id` | deterministic: claim type + the subject object's id |
| `claim_type` | closed vocabulary (§3) |
| `subject` | the id of the object the claim is about (resolves in the same document) |
| `facts` | every quoted value, each `{ref, path, value}`: the object id, the field path in the document, and the stored value **verbatim** |
| `text` | the rendered sentence, from a versioned template (`template_id`, `explain_version`) |
| `known_at` | the subject's stored `known_at` (or its section's knowability date, ADR-0027 §8.3) |
| `provisional` | copied from the subject's stored flag |

`explain(analysis) -> Explanations` is a pure function of one `TechnicalAnalysis` in
`chartlens_engine.explain`. It reads nothing else (no bars, no clock, no other security)
and never modifies the analysis (tested: the analysis is byte-identical with and
without it).

## 2. Rules every claim obeys (each one a test)

1. **Every reference resolves** to an object in the same document, and every fact's
   `value` equals the stored value at `path` exactly (canonical equality).
2. **Every number in `text` is a quoted fact**, rendered by the one display rule (bar
   prices as stored decimals, derived values to 4 decimals, ADR-0026 K7). A scan of
   `text` finds no number that is not the rendering of a fact. Same for dates.
3. **No arithmetic.** No differences, percentages, distances to price, counts of days,
   or "current value" of a sloped line. A sloped boundary is described by its stored
   endpoints only (the ADR-0027 decision 1a principle).
4. **No new judgement.** Words come from the template and from stored enum values
   (`STRONG_DOWNTREND`, `CONFIRMED`, tag names). Forbidden in templates: probability,
   likely, expect, will, should, target, buy, sell, hold, recommend, signal, strength as
   a verdict, "best", "important". "Measured-move zone", "definition fit" (with "not a
   likelihood"), never "confidence".
5. **No order of its own.** Claims follow the engine's order: the `current` lists in
   their stored order, sections in document order. Nothing is ranked or sorted by a
   score; definition fit is quoted, never used to order (ADR-0022 §18.1).
6. **Time.** Every fact is in the published document, so known by its `as_of`; each claim
   carries its `known_at`; a provisional fact is said to be provisional; nothing is
   described as confirmed by the forming week.
7. **Deterministic**: byte-identical output across processes and runs (canonical JSON).

## 3. What is explained (v1): the current state, by the engine's own lists

| Claim type | Subjects | Quotes |
|---|---|---|
| `DATA_CONTEXT` | the document | segment start (`usable_from`), `as_of`, forming week present |
| `TREND_STATE` | `structure.trend` (`current.trend_since`) | state, since, last event (kind, direction, bar, level) |
| `ZONE` | `current.zone_ids`, in order | type, price range, first seen, known, last tested, touch count, role reversed |
| `ACTIVE_TRENDLINE` | `current.active_trendline_ids` | type, touch dates and values, stored value at the state date |
| `FIBONACCI` | `current.fibonacci_ids` | direction, leg (anchor and counter, dates and prices), last status, levels |
| `PATTERN` | `current.included_pattern_ids` | type, formed (start, end), recognised (`known_at`), last status and its date, the stored confirmation and invalidation conditions (§4), stored measured-move zone, definition fit with "not a likelihood", relevance tags with their evidence |

Out of v1: history (past patterns, past events), divergences (the engine keeps no
"current divergences" list; see decision 2), breakout events beyond those a pattern's
status quotes, candles, indicators.

## 4. Conditions: what would confirm or invalidate, as stored

ADR-0019: ChartLens describes "which technically defined conditions would confirm or
invalidate each structure". Explain quotes the conditions the engine stored; it never
derives one.

- A horizontal level (`confirmation_level`, `invalidation_level`, a structure event's
  `confirmation.level`): "a weekly close above 1,500.0000", plus the stored rule name.
- A sloped boundary (`confirmation_line: UPPER`, level null): "a weekly close beyond the
  upper boundary, stored from 1,620.5000 (2025-03-07) to 1,544.2500 (2025-09-19)". Its
  value at a later week is not stored and is not computed.
- A buffer the engine evaluates at the deciding bar (the pattern ATR_pre buffer) is
  named, not quantified, until a status entry records it (`measured_values`).
- Engine `description` strings are not reused as explanation text (they predate the
  wording rules); the structured fields beside them are.

## 5. Where explanations live (decision 1)

- **(a) Recommended: a separate content-addressed artifact per security**, written by
  the ANALYSIS job stage beside the document (`…/explanations/{sha}.json.gz`, the same
  codec), named in the manifest entry with `explain_version`, bound to the document by
  its `document_sha256`. Reused when the document hash and `explain_version` are
  unchanged. Publication verifies it like the other artifacts (address, binding).
  *Why:* wording will iterate faster than methodology; a wording change must not change
  `analysis_version`, every document address and the reuse keys. "The analysis is
  identical with and without explanations" becomes structural, not just a test.
- **(b) A section inside the document.** Simpler plumbing, but every wording change
  recomputes and republishes 3,193 documents (5.5 GB canonical) under a new
  `analysis_version`, and explanation text becomes part of the analytical identity.

## 6. Serving and the chart

- API: `GET /securities/{id}/explanations` (verbatim, envelope with `meta_version`,
  `document_sha256`, `explain_version`) and an `explanations` component in `/chart`
  when asked (`explain=true`), same snapshot, its own `meta_version` (ADR-0027 §3).
- Chart: a "What the chart shows" panel lists the claims in order. Each claim's
  references focus the drawn objects (the 6d focus mechanism); the text is shown as
  stored, never reworded in the browser.

## 7. Language models

Not in 6e. The claim structure is what a later rephrasing step would be checked
against (ADR-0024 §9): any output introducing a number, name, object or judgement not
in the claims is rejected. An LLM never produces or decides an analytical fact.

## 8. Checkpoint evidence

- Tests: the §2 rules per claim type; determinism; the analysis unchanged; wording scan.
- Real NSE (read-only probe): explain all 3,193 documents; every claim resolves, every
  number and date in text is a fact, 0 wording violations; claims per type; size and
  time per security; screenshots of the panel next to the chart.

## Decisions for review

1. **Storage:** (a) a separate content-addressed artifact bound to the document
   (recommended), or (b) a document section.
2. **Divergences:** leave them out of v1 until the engine publishes a current list
   (recommended), or select them by stored status (a selection by stored attribute, but
   it means the explain layer decides which statuses count as current).
3. **Conditions** as §4: stored levels quoted; sloped boundaries by stored endpoints
   only; buffers named, not computed.
4. **Text stored server-side** (validated once, identical everywhere; recommended), or
   facts only with wording rendered by the frontend.
5. **Current state only** in v1; historical narration later, under its own review.

Not in 6e: the two 6d follow-ups (ADR-0027 §10), channels, replay.
