# Architecture decision records

Each record captures one decision, why it was made, and what it rules out. A
decision is changed by writing a new ADR that supersedes the old one, never by
editing history. Anything that changes a computed result (methodology) must have
an ADR before the code changes (spec §57 rule 13).

| ADR | Decision | Status | Implemented in |
|---|---|---|---|
| [0001](0001-technology-stack.md) | Technology stack and repository layout | Accepted | M1 |
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
