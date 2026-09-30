"""ChartLens data pipeline.

Flow (ADR-0002)::

    provider download → immutable raw artifact → parse → corporate actions
    → adjustment factors → data quality → canonical daily dataset → bar builders

Only this package (and the API) may perform I/O against market-data sources
and storage. The technical engine consumes its output through bar frames.
"""

__version__ = "0.1.0"
