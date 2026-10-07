# Architecture decision records

Each record captures one decision, why it was made, and what it rules out. A
decision is changed by writing a new ADR that supersedes the old one, never by
editing history. Anything that changes a computed result (methodology) must have
an ADR before the code changes (spec §57 rule 13).

| ADR | Decision | Status | Implemented in |
|---|---|---|---|
| [0001](0001-technology-stack.md) | Technology stack and repository layout | Accepted (job layer added to the dependency graph by 0024) | M1 |
| [0002](0002-data-storage.md) | GCS data lake (Parquet) + Firestore for application state | Accepted (curated/daily layout superseded by 0010) | M1 (layout), M2–M3 |
| [0003](0003-security-identity.md) | Immutable internal security IDs with identifier history | Accepted | M2 |
| [0004](0004-weekly-candles.md) | Weekly candle construction and week boundaries | Accepted (field names superseded by 0014) | M4 |
| [0005](0005-price-adjustment.md) | Corporate-action price adjustment policy | Accepted (method details superseded by 0011) | M3 |
| [0006](0006-point-in-time.md) | Point-in-time (`as_of`) enforcement | Accepted | M1 (primitives), all |
| [0007](0007-job-execution.md) | Batch jobs on GitHub-hosted runners behind a CLI | Accepted | M1 (skeleton), M2, M7 |
| [0008](0008-nse-provider-and-calendar.md) | NSE provider, bhavcopy formats, trading calendar | Accepted | M2 |
| [0009](0009-security-identity-resolution.md) | Security identity resolution | Accepted | M2 |
| [0010](0010-canonical-daily-dataset-and-ingestion.md) | Canonical daily dataset and ingestion | Accepted | M2 |
| [0011](0011-corporate-actions-and-adjustment.md) | Corporate actions, exact factors, validation, adjusted dataset | Accepted | M3 |
| [0012](0012-data-quality-and-usable-from.md) | Data quality findings, status and `usable_from` | Accepted | M3 |
| [0013](0013-identity-rebuild-and-aliases.md) | Identity rebuild, aliases, reviewed identity links | Accepted | M3 |
| [0014](0014-weekly-data-product.md) | Weekly data product, continuity segments, point-in-time weekly bars | Accepted | M4 |
| [0015](0015-non-regular-sessions-and-comparability-guard.md) | Non-regular sessions (kept as traded, flagged, provisional confirmations) and the engine comparability guard | Accepted | M4 follow-up |
| [0016](0016-api-auth-and-serving.md) | API as a read-only presentation layer; private access (Firebase Auth + Firestore allowlist); version-bound serving snapshot; us-central1 | Accepted (weekly serving path corrected by 0018; analysis serving added by 0023) | M5 |
| [0017](0017-frontend.md) | Frontend: a faithful, untrusted visualization layer (Firebase Hosting, Google sign-in, Lightweight Charts) | Accepted (visualization rule extended to analysis by 0023) | M6 |
| [0018](0018-production-runs.md) | Production runs: tracked refresh (GitHub Actions + Firestore), one active run, immutable content-hashed serving, snapshot history | Accepted (ANALYSIS stage added by 0019; owned by the job layer per 0024) | M7 |
| [0019](0019-technical-engine-architecture.md) | Technical engine: layered modules, causal `known_at` event model, point-in-time rules, valid segment only, separate analysis methodology hash, precomputed in a tracked ANALYSIS stage | Accepted (ANALYSIS ownership and result provenance amended by 0024) | M8 |
| [0020](0020-indicators-swings-structure.md) | Indicators (causal, per-segment warm-up), swing methods and sensitivities (configurable primary), market structure and trend states | Accepted | M8 |
| [0021](0021-levels-and-evidence.md) | Support/resistance zones and trendlines, Fibonacci, divergence, volume, volatility and candlestick evidence; causal composition (`depends_on`) | Accepted | M8 |
| [0022](0022-patterns.md) | Classical patterns: broad candidates, fixed geometry, deterministic identity and evolution, FORMING → CONFIRMED / INVALIDATED / EXPIRED, overlap vs relevance, per-pattern parameters, confidence as definition fit | Accepted | M8 |
| [0023](0023-analysis-serving-and-visualization.md) | Serving analysis (content-addressed, schema 3), the analysis API and chart overlays drawn only from backend geometry | Accepted (storage by 0024/0025; API and K7 to be amended by 0026) | M8 |
| [0024](0024-analysis-orchestration-and-publication.md) | Analysis orchestration and publication: one pure orchestrator ("composes, never reinterprets"), the job layer and ANALYSIS stage, content addressing, atomic schema-3 publication, separate event datasets, API contract, explain from structured facts | Accepted (amends 0001, 0018, 0019, 0023) | M8 |
| [0025](0025-job-layer-and-analysis-stage.md) | The job layer (`chartlens_jobs`) and the ANALYSIS stage: input identity from the analysed bars, reuse keys and runtime fingerprint, the universe rule and coverage proof, artifacts and the analysis manifest | Accepted (amends 0024 §6) | M8 |
| [0026](0026-schema-3-publication-and-analysis-api.md) | Schema-3 publication (a verbatim, hash-pinned analysis manifest; independent verification before the pointer moves) and the analysis API (whole-section selection, predicate retrieval of events, values served exactly) | Accepted | M8 |
| [0027](0027-chart-layers.md) | Chart layers: only stored objects or selections of them, placed by stored dates and values; one snapshot per chart; nothing before it was knowable; no structure across a continuity break | Accepted | M8 |
