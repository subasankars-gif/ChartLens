# ADR-0023: Serving and drawing technical analysis

**Status:** Accepted · 2026-10-02 (K3, K7, K8 confirmed by Suba)

This ADR covers how layer I (visualization) and layer J (API) of ADR-0019 work. It
extends ADR-0016 (serving), ADR-0017 (the frontend) and ADR-0018 (publication) without
weakening any of them.

## Storage and publication

- **Run in the pipeline.** The ANALYSIS stage writes one JSON document per security:
  a `TechnicalAnalysis` (ADR-0019) for the current valid segment, as of the data's
  `as_of`.
- **Content-addressed, like the weekly copies.** Publication copies each document to
  `curated/serving/exchange={EX}/analysis/{sha256}.json` and records the hashes in the
  manifest (`analysis_files`). This is serving schema 3, which adds
  `analysis_version` and `analysis_methodology_hash` to the manifest's versions.
- **Reuse.** A document is reused when its key (weekly file hash, `analysis_version`)
  is unchanged, and it gets the same hash.
- **What the API reads.** Only the pointer and the blobs the pointer names. It never
  lists GCS, never computes analysis and never reads unpublished documents. A
  schema-2 snapshot simply has no analysis, and `/analysis` answers 404 "no analysis in
  this snapshot".

## API

- **`GET /api/v1/securities/{id}/analysis?layers=…`:**
  - Approved users only, like every data route.
  - Returns the published `TechnicalAnalysis`, from the same snapshot as `/weekly` (the
    `meta_version` is in the response).
  - `layers` filters the top-level sections:
    - `indicators`, `swings`, `structure`, `trend`, `zones`, `fibonacci`;
    - `divergences`, `volume`, `volatility`, `candles`, `patterns`.
    - The default is everything except `swings` at non-primary sensitivities and
      `candles`.
  - `swing_method` and `swing_sensitivity` select other swing sets.
  - `?as_of` is refused with 400 (K3). Historical analysis is CLI and tests only.
- **Numbers (K7):**
  - A value that is a bar's price (a swing, a pattern key point, a zone edge taken from
    swings) is that bar's **exact decimal text**.
  - A computed value (a moving average, a Fibonacci level, a fitted line, an indicator)
    is a JSON number **rounded to 4 decimals** and marked `derived`.
  - Nulls stay null (warm-up).
- **Provenance:** every response carries `as_of`, `meta_version`, `analysis_version`,
  `analysis_methodology_hash`, the data `methodology_hash`, `weekly_version` and the
  engine and analyzer versions.
- **Other routes:** `/system/status` and the snapshot history record add
  `analysis_version` and `analysis_methodology_hash`. `/weekly` is unchanged.

## Frontend (the faithful-visualization rule, extended)

ADR-0017's invariant becomes:

> **If the API returns it, the chart may draw it; if the API does not return it, the
> frontend does not infer or calculate it.**

That now includes analysis. The frontend maps backend coordinates (dates and values) to
pixels. It never fits a line, finds a swing, tests a rule or decides a status.

**Layers control.** Toggles above the chart. The **default** is candles + volume only,
as today. The toggles:

| Toggle | Drawn as |
|---|---|
| Moving averages | Line series for the chosen SMAs/EMAs (default choice: 10W, 40W) |
| Swing points | Markers at `bar_date`, labelled with type; the pending extreme shown hollow |
| Market structure | HH/HL/LH/LL labels on swings; BOS/CHoCH markers plus a short horizontal at the level |
| Support / resistance | Shaded bands (the same custom-primitive technique as the break bands) |
| Fibonacci | Levels drawn only across the structure's time span, labelled with ratios |
| Divergence | Lines joining the two swings on price and, in the oscillator pane, on the indicator |
| Patterns | Polylines through the key points; neckline/boundary lines; confirmation (solid) and invalidation (dashed) levels; the measured-move zone as a light band; a label with type and status |

- **An optional oscillator pane:** RSI or MACD, off by default, showing the API's series.
- **A "Technical evidence" panel beside the chart:**
  - trend state and the events behind it;
  - patterns with status, confidence and its components, and the explanation text;
  - divergences;
  - the nearest zones.
  - Selecting a pattern frames the chart on its span and turns its layer on.
- **Candles** are not drawn by default. They appear as evidence in the panel when cited.
- **Wording:**
  - "Measured-move zone" (K8).
  - "Confidence (definition fit)" with its components.
  - Never "probability", "target", "buy", "sell".

## Testing

- **API:** an approved user gets the analysis; pending and signed-out users are refused;
  `?as_of` gives 400; provenance matches the snapshot; exact-decimal and derived values
  are formatted as above; a schema-2 snapshot gives 404.
- **Frontend:**
  - unit tests of the mapping from API geometry to chart primitives (no computation);
  - an end-to-end test where an admin turns layers on and a fixture pattern renders with
    its neckline and status;
  - screenshots on real data.
