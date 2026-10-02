"""Exchange-independent domain vocabulary.

Nothing in this module may encode an exchange-specific assumption. NSE series
codes, symbols formats, holiday rules etc. belong to the provider layer in
``chartlens_pipeline.providers``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import NewType

SecurityId = NewType("SecurityId", str)
"""Immutable internal identifier. Never a ticker symbol or ISIN — both change over time."""

ExchangeCode = NewType("ExchangeCode", str)
"""Exchange identifier, e.g. ``NSE``. Opaque to the technical engine."""


class Timeframe(StrEnum):
    DAILY = "1D"
    WEEKLY = "1W"
    MONTHLY = "1M"


class DataQualityStatus(StrEnum):
    """Per-security status under the current methodology (ADR-0012), always relative to
    ``usable_from`` — the earliest date from which the technical history is reliable."""

    USABLE = "USABLE"
    USABLE_WITH_WARNINGS = "USABLE_WITH_WARNINGS"
    NOT_USABLE = "NOT_USABLE"
    UNKNOWN = "UNKNOWN"


class JobType(StrEnum):
    DAILY_INCREMENTAL = "DAILY_INCREMENTAL"
    BACKFILL = "BACKFILL"
    SECURITY_REFRESH = "SECURITY_REFRESH"
    FULL_RECALCULATION = "FULL_RECALCULATION"


class JobStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    PARTIAL = "PARTIAL"


def utc_now() -> datetime:
    """Timezone-aware UTC timestamp. Use this instead of ``datetime.now()``."""
    return datetime.now(UTC)
