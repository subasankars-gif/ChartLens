# ADR-0028: Explanations

**Status:** Accepted · 2026-10-07, with Suba's clarifications 1–11 frozen below; §6
(publication, schema 4) approved as written, with the provenance invariant added there.
It details ADR-0024 §9 and builds
on ADR-0025 (job layer), ADR-0026 (publication, API) and ADR-0027 (chart layers).

> **An explanation restates published analytical facts in words. It never adds a fact,
> a number, a judgement or an order that the analysis does not already contain.**
>
> 6e explains what ChartLens knows; it does not decide what ChartLens knows.

```text
analysis document (document_sha256)
        │
        ▼
explain (pure) ──► explanation object  (bound to document_sha256 + explain_version;
        │                               its own content address)
        ▼
explanation manifest (pinned by the snapshot) ──► API ──► chart panel
```

6e is not an analytical layer. Nothing flows back from it into the analysis; no LLM is
used.

## 1. The claim: a first-class, auditable structure

| Field | Meaning |
|---|---|
| `claim_id` | deterministic: `claim_type` + the subject's id |
| `claim_type` | closed vocabulary (§3) |
| `template_id` | one template of the versioned template set |
| `subject` | the id of the object the claim is about |
| `references[]` | every object id the claim relies on; each resolves in the bound document |
| `quoted_values[]` | `{name, ref, path, value}`: the template slot, the object id, the field path in the document, and the stored value **verbatim** |
| `rendered_text` | `render(template_id, quoted_values)`, nothing else |
| `known_at` | the subject's stored `known_at` (or its section's knowability date, ADR-0027 §8.3) |
| `provisional` | copied from the subject's stored flag |

**The text is derivable solely from the template and the quoted values.** A validator
re-renders every claim and requires byte equality, resolves every reference and path in
the bound document, and requires every quoted value to equal the stored value exactly
(canonical equality). A quoted value is therefore always a stored field, never a derived
one.

`explain(document) -> Explanation` is a pure function in `chartlens_engine.explain`
that **selects** subjects and fields. The claim model, the template set, the renderer and
the validator live in `chartlens_core.claims` (generic, no engine import), so the job
stage, the publisher and the tests all check claims with the same code (§6).

## 2. Rules (each one a test; clarifications 2–9)

1. **Bound to one analysis.** An explanation names the `document_sha256` it describes
   and is generated from exactly those bytes, verified, never from "the current file".
   It is never served with another analysis (the API checks the binding, §7).
2. **References resolve; values are quoted** (§1).
3. **Numbers and dates.** *Every domain-specific numeric or date value in rendered text
   originates from an explicitly quoted analytical value.* Templates contain no digits
   (a static test); template words ("weekly", "first", "current") and version metadata
   are not domain values. Quoted numbers are rendered by the one display rule (bar
   prices as stored decimals, derived values to 4 decimals; ADR-0026 K7).
4. **No arithmetic.** No differences, percentages, distances to price, durations
   ("forming for four weeks"), counts derived from dates, or the value of a sloped line
   at a later week. A duration or count is said only if stored.
5. **Conditions only as stored.** *A prose statement may describe a condition only when
   the condition itself is represented by an authoritative stored field or event. The
   generator may not reconstruct a condition from lower-level fields.* A stored level is
   quoted as "the stored confirmation level 1,500.0000"; a stored boundary line by its
   stored endpoints; a buffer the engine evaluates at the deciding bar (pattern ATR_pre)
   is named only when a status entry has recorded it, and never turned into a "move
   needed".
6. **No new judgement, by construction.** Templates are closed and reviewed; values come
   from objects; there is no arithmetic and no ordering. A forbidden-vocabulary scan
   (buy, sell, hold, guaranteed, will, must, should, likely, probability, target,
   recommend, best, strongest, most important, confidence) is the **last guardrail**,
   not the semantic control. "Measured-move zone"; "definition fit" with "not a
   likelihood".
7. **Authoritative order only.** Claims follow the engine's order: `current` lists in
   their stored order, claim types in a fixed order. No "most important", "strongest",
   "largest" or "highest" unless the engine stored that ordering; definition fit is
   quoted, never used to order (ADR-0022 §18.1).
8. **Time.** Every fact is in the published document (known by its `as_of`); each claim
   carries `known_at`; provisional facts are said to be provisional; nothing is described
   as confirmed by the forming week.
9. **Deterministic**: byte-identical across processes and runs.

## 3. What v1 explains: the current state, by the engine's own lists (clarification 10)

| Claim type | Subjects | Quotes (stored fields only) |
|---|---|---|
| `DATA_CONTEXT` | the document | `inputs.usable_from`, `identity.as_of`, `identity.forming_week_present` |
| `TREND_STATE` | `structure.trend` | state, since; its last event's kind, direction, bar date, level |
| `ZONE` | `current.zone_ids`, in order | type, `price_low`, `price_high`, `first_seen`, `known_at`, `last_tested`, `role_reversed` |
| `ACTIVE_TRENDLINE` | `current.active_trendline_ids` | type, `known_at`, first and last touch (date, line value), stored value at `levels.state_date` |
| `FIBONACCI` | `current.fibonacci_ids` | direction, anchor and counter (date, price), last status and its date, each level's ratio and price |
| `PATTERN` | `current.included_pattern_ids` | type, `start_date`, `end_date`, `known_at`, last status and its date; stored confirmation/invalidation level or boundary-line endpoints; stored measured-move zone; `definition_fit.value` ("not a likelihood"); relevance tags of the latest relevance entry with their evidence ids |

**Not in v1:** divergences (clarification 3: no divergence claim until the engine
publishes an authoritative current-divergence list; never approximated from status,
dates, overlap, relevance or recency); history and replay; breakout events beyond what a
pattern's status quotes; candles; indicators; touch counts (a count of a stored list is
arithmetic, so only stored counts are quoted).

## 4. Versions and identity (clarifications 1–2)

- `explain_version` = `explain-` + 12 hex of the canonical hash of the claim schema
  version, the selection rules version and the full template set (ids and texts). A
  template change cannot ship without a new `explain_version`.
- `explanation_key` = canonical hash of `{key_version, security_id, document_sha256,
  explain_version}`: the complete dependency fingerprint. Reuse needs an existing,
  verified explanation with the same key.
- The explanation object is canonical JSON `{explanation_schema_version,
  explain_version, explanation_key, security_id, continuity_segment_id, document_sha256,
  claims[]}`, stored gzip-compressed (the ADR-0024 codec) at
  `curated/serving/exchange={EX}/explanations/{sha}.json.gz`, addressed by the SHA-256 of
  its uncompressed canonical bytes. Immutable; a wording change makes a new object, never
  a rewrite.
- A wording change changes `explain_version`, every explanation object and the
  explanation manifest, and nothing analytical: `analysis_version`, documents, event
  files, the analysis manifest and their hashes stay identical (a test).

## 5. Generation: in the ANALYSIS job stage

After a security's document is computed or reused (and verified), the stage reuses the
explanation whose key matches (verified like any artifact, ADR-0026 clarification 3:
corrupt → quarantine, regenerate) or generates it from the verified document bytes and
validates every claim before writing. The stage then writes the **explanation manifest**
`curated/analysis/exchange={EX}/_explanations_manifest.json` (canonical, no run facts):
`{manifest_schema_version, explain_version, analysis_set_hash, explanation_set_hash,
entries: [{security_id, document_sha256, explanation_key, explanation_sha256}]}`,
generated from the completed result set. Its `analysis_set_hash` binds it to the
analysis manifest it explains. No new run stage.

## 6. Publication: schema 4 pins the explanation manifest (clarification 11)

```text
snapshot v={meta}
 ├── analysis_manifest.json        (verbatim, pinned; ADR-0026)
 ├── explanations_manifest.json    (verbatim, pinned by sha256 in the `explanations` block)
 ├── documents, event files        (content-addressed)
 └── explanation objects           (content-addressed)
```

- **Schema 4** = schema 3 + the `explanations` block (`manifest_sha256`,
  `explain_version`, `explanation_set_hash`, `securities`) and the verbatim manifest
  copy. `meta_version` covers it. Mandatory once 6e ships (as schema 3 was in 6c); the
  API still reads schema 3 and answers "no explanations in this snapshot".
- **Checks before anything is written**, added to the nine of ADR-0026:
  10. the explanation manifest's `analysis_set_hash` equals the analysis manifest's, and
      its `explain_version` equals the job-supplied expected one (publication never
      derives or substitutes it);
  11. coverage: exactly one explanation per analysed security, each bound to that
      security's `document_sha256` in the analysis manifest;
  12. each explanation object not covered by the live verified snapshot: decompresses,
      hashes to its address, its internal identity matches the entry, and **every claim
      validates** against the bound document with the `chartlens_core.claims` validator.
      Objects covered by the live verified snapshot: existence (ADR-0026 clarification 2).
- **Provenance invariant:** *an explanation object may reference only the exact
  analysis document identified by its `document_sha256`; it may not reference another
  security's document, another snapshot's document, or a separately reconstructed
  analytical value.* Every reference and quoted value is resolved in that one document.
- **Two versions, never substituted:** `analysis_version` is the analytical
  methodology; `explain_version` is the explanation/template methodology. Publication
  checks each against the job's expected value.
- Commit, idempotence and clean-up as ADR-0026 (clean-up also keeps the live and
  previous snapshots' explanation objects and the latest explanation manifest).

## 7. Serving and the chart

- `GET /securities/{id}/explanations`: the stored object verbatim (claims with
  references, quoted values and rendered text), envelope `meta_version`,
  `document_sha256`, `explanation_sha256`, `explain_version`. The API serves it only
  when its `document_sha256` equals the snapshot's entry for that security.
- `/chart` gains an `explanations` component when asked, from the same snapshot, naming
  its `meta_version` (ADR-0027 §3).
- Chart panel "What the chart shows": claims in stored order, text as stored (never
  reworded in the browser), each claim focusing its referenced drawn objects.

## 8. Language models

Not in 6e. The claim structure is what a later rephrasing step would be checked
against (ADR-0024 §9); an LLM never produces or decides an analytical fact.

## 9. Checkpoint evidence

- Tests: the §2 rules per claim type; re-render equality; no-digit templates;
  determinism; analysis artifacts byte-identical with and without explanations; a
  wording change leaves the analysis manifest unchanged; publication checks 10–12
  (including a tampered claim, a wrong binding, a missing explanation); API binding.
- Real NSE (read-only probe): explanations for all 3,193 analysed securities; every
  claim validates; 0 vocabulary hits; claims per type; size and time; a schema-4 publish
  into the overlay; screenshots of the panel beside the chart.

## Locked clarifications (2026-10-07)

1. Explanation artifacts are immutable and content-addressed, not mutable per-security
   files.
2. Explanation identity depends on the exact analysis content hash and the explanation
   version.
3. No divergence explanation until the engine exposes an authoritative
   current-divergence list.
4. Conditions may only be described when the authoritative condition is already
   represented in the stored analysis.
5. No arithmetic and no reconstruction of thresholds.
6. Claim-level references and quoted values stay auditable.
7. Numeric and date values in text originate from quoted analytical values.
8. Forbidden-word validation is a guardrail, not the primary semantic control.
9. Claim order follows authoritative engine order.
10. Version 1 is current-state only.
11. The explanation manifest is pinned in the published snapshot (decided in §6).

Prerequisite done before 6e code: the 6d level-visibility amendment (ADR-0027 §10.2).
Not in 6e: ADR-0027 §10.1 (spans across missing weeks), channels, replay.

## 10. As built (6e)

Choices made while building, each within the clarifications; listed for the review.

1. **Where the code lives.** `chartlens_core.claims` holds the claim model, the template
   set (19 templates), the renderer, the resolver (RFC 6901 pointers) and the validator;
   `chartlens_engine.explain` only selects subjects and fields; the ANALYSIS stage
   (`chartlens_jobs`) generates, validates and writes; the publisher validates new
   objects with the same `validate`; the API serves verbatim. No layer re-implements
   another's check.
2. **One claim per stored aspect.** A pattern gives `PATTERN` (type, formation span,
   recognition date, last status), then one claim each for its stored confirmation and
   invalidation condition, its latest stored measured-move zone, its definition fit and
   each relevance tag; a Fibonacci structure gives one claim plus one per stored level; a
   reversed zone adds `ZONE_ROLE_REVERSED`; a forming last week adds `FORMING_WEEK`.
   Short sentences keep every number traceable to one quoted value.
3. **Conditions:** the stored level when there is one, otherwise the stored boundary line
   the pattern names (by its endpoints), otherwise nothing. When the engine stores both a
   level and a line, only the level is quoted.
4. **Display kinds:** stored swing prices (Fibonacci anchors, a structure event's level)
   are rendered as their exact stored text; zone bounds, line values, Fibonacci level
   prices and measured-move bounds (derived values) to 4 decimals; dates as ISO text;
   stored codes as lower-case words (`STRONG_DOWNTREND` → "strong downtrend"), event kinds
   verbatim (BOS, CHoCH). The renderer upper-cases the first character of a sentence.
5. **`known_at` and `provisional` are themselves quoted values** (named so), so they are
   resolved and checked like any other fact; a trend state's knowability is its `since`
   (ADR-0027 §8.3).
6. **Static guards:** templates hold no digit and no forbidden word (test); the explain
   module imports no numeric library, sorts or aggregates nothing, makes no ordering
   comparison, and its only arithmetic is the position of a stored list's last entry
   (`_last`, test).
7. **Review finding fixed during 6e:** the first trend template read "… since {since},
   set by a {kind} …". On data where the state changed after its last structure event
   (the engine records the last event, not a causal link), that wording implied a cause
   the analysis does not state. It now reads "… since {since}; the last structure event
   is a {kind} {direction} on {date} at the level {level}." — exactly the stored
   `last_event_id`.
8. **Serving:** the chart requests the explanation with every `/chart` call
   (`explanations=true`; one small object) and refuses one that is not bound to the
   chart's own document or snapshot. The panel shows the claims verbatim, in order; a
   claim's "Facts" lists each quoted value with its document pointer; pointing at a claim
   focuses the drawn object it is about and turns that layer on.

## 11. Checkpoint evidence (6e)

**Tests.** Core: templates (no digit, no forbidden word), one display rule per kind,
RFC 6901 resolution, and every tampering refused by `validate` (text, quoted value,
reference id, unresolvable reference, `known_at`, `provisional`, subject, a computed
value quoted from a non-existent field, another document, another security, a forged
key, a forbidden word arriving through a stored enum). Engine (real documents): every
claim validates; the document is unchanged and the output deterministic; claims follow
the `current` lists in order; no divergence claim; conditions only as stored; every
digit in text belongs to a quoted value; a template change changes `explain_version` and
nothing analytical; static no-arithmetic/no-ordering check. Job stage: one validated,
bound explanation per analysed security; reuse by key; a wording change regenerates
explanations only (analysis manifest and documents byte-identical, no analysis
recomputed); a corrupt explanation is quarantined and regenerated; a claim that does not
hold fails the stage. Publication (schema 4): the manifest pinned verbatim; checks 10–12
(another analysis set, another `explain_version`, set hash, canonical form, a missing or
swapped binding, a tampered claim, a quoted value not in the document, an object that
disagrees with its entry, a corrupt object); a wording change publishes new explanation
objects only (analysis objects existence-only). API: verbatim and bound; `/chart`
carries it only when asked; schema-3 snapshot and a re-pinned lying binding refused.
Frontend: binding check (98 vitest); e2e: the panel shows the stored claims verbatim in
stored order, a claim focuses its pattern with 0 unplaced, its facts are the quoted
values. `poe check` 1,049 tests; CI green.

**Real NSE** (throwaway probe, read-only towards the lake; data to 2026-10-07):

| | |
|---|---|
| Explanations | 3,193 generated in the ANALYSIS stage (`explain-9abff420f1fa`), one per analysed security |
| Claims | 84,800; independently re-validated: **0 problems** (so 0 vocabulary hits) |
| By type | DATA_CONTEXT 3,193 · FORMING_WEEK 2,573 · TREND_STATE 3,186 · ZONE 17,024 · ZONE_ROLE_REVERSED 8,586 · FIBONACCI 4,888 · FIBONACCI_LEVEL 39,104 · ACTIVE_TRENDLINE 813 · PATTERN 1,027 · PATTERN_CONFIRMATION 780 · PATTERN_INVALIDATION 780 · PATTERN_MEASURED_MOVE 528 · PATTERN_FIT 1,027 · PATTERN_TAG 1,291 |
| Size | mean 26.7 KB, p90 40.2 KB, max 63.9 KB per explanation (uncompressed) |
| First schema-4 publish | `meta-85f7703882b6`, 12,772 objects fully verified (every claim of every explanation validated) in 242 s; again: UNCHANGED in 2.1 s |
| ANALYSIS (computed + explained, 4 CPUs) | 459 s |
| Chart layers (6d gate, rerun) | 6,703,479 drawn, 0 refused, 0 snapshot mismatches |

**Findings the explanations surfaced (engine methodology, not 6e defects).** Restating
stored facts verbatim makes some engine values plain to read, for example on RANEHOLDIN:
a confirmed descending triangle's stored measured-move zone of −358.8337 to −234.6086
(price near 1,536), and a Fibonacci down leg's stored 2.618 extension at −1,409.9186.
The explanation layer must not suppress, clamp or reinterpret them (that would be a
judgement); whether the engine should publish negative price levels is a methodology
question for its own review, with a market-wide count first.
