"""Exchange-independent provider contract (ADR-0008).

An exchange is plugged in by implementing :class:`ExchangeProvider`. Downstream code
(ingestion, security master, backfill) depends only on this module, so adding BSE,
NYSE or NASDAQ means adding a provider — not changing consumers.

Downloading and parsing are separate on purpose. ``download`` returns the source's
original bytes, which are stored immutably before anything else happens; ``parse``
is a pure function of those bytes. A parser bug is therefore fixed by re-parsing
stored files, and every canonical row traces back to one exact file by its hash.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, computed_field

from chartlens_core.domain import utc_now
from chartlens_pipeline.calendar import TradingCalendar
from chartlens_pipeline.daily import ParsedDaily
from chartlens_pipeline.identity import IdentityPolicy, SymbolChangeNotice


class Dataset(StrEnum):
    DAILY_BARS = "daily_bars"
    SYMBOL_CHANGES = "symbol_changes"
    CORPORATE_ACTIONS = "corporate_actions"
    SECURITIES = "securities"
    CALENDAR = "calendar"


class RawArtifact(BaseModel):
    """One file exactly as the source served it."""

    model_config = ConfigDict(frozen=True)

    exchange: str
    provider: str
    dataset: Dataset
    source_dataset: str
    """The provider's own name for this feed (e.g. ``bhavcopy``); used as the raw folder name."""
    logical_date: date | None
    """The market date the file describes (None for undated snapshots)."""
    filename: str
    content: bytes = Field(repr=False)
    url: str = ""
    content_type: str = ""
    fetched_at: datetime = Field(default_factory=utc_now)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()


class DownloadStatus(StrEnum):
    FOUND = "FOUND"
    NOT_PUBLISHED = "NOT_PUBLISHED"
    """Every candidate location answered 'not found'. A fact about the source."""
    FAILED = "FAILED"
    """At least one candidate failed for another reason; nothing can be concluded."""


@dataclass(frozen=True)
class DownloadResult:
    status: DownloadStatus
    artifact: RawArtifact | None
    tried: tuple[tuple[str, str, int | None], ...]
    """(url, outcome, http status) for every location tried, in order."""
    detail: str = ""


class DailyBarSource(Protocol):
    @property
    def source_dataset(self) -> str: ...

    @property
    def parser_version(self) -> str: ...

    def download(self, day: date) -> DownloadResult: ...

    def check_published(self, day: date) -> DownloadResult:
        """Like ``download`` but only establishes whether a file exists (no artifact)."""
        ...

    def parse(self, content: bytes, day: date, universe_series: frozenset[str]) -> ParsedDaily: ...


class ExchangeProvider(Protocol):
    @property
    def exchange_code(self) -> str: ...

    @property
    def daily_bars(self) -> DailyBarSource: ...

    def trading_calendar(self) -> TradingCalendar: ...

    def identity_policy(self) -> IdentityPolicy: ...

    def download_symbol_changes(self) -> DownloadResult:
        """The exchange's list of symbol changes (identity evidence), if it publishes one."""
        ...

    def parse_symbol_changes(self, content: bytes) -> list[SymbolChangeNotice]: ...


# --------------------------------------------------------------- corporate actions (Milestone 3)


class CorporateActionType(StrEnum):
    SPLIT = "SPLIT"
    BONUS = "BONUS"
    RIGHTS = "RIGHTS"
    DIVIDEND = "DIVIDEND"
    SYMBOL_CHANGE = "SYMBOL_CHANGE"
    MERGER = "MERGER"
    DELISTING = "DELISTING"
    OTHER = "OTHER"


class CorporateActionRecord(BaseModel):
    """A parsed corporate action. The original text is always kept for audit.

    Ratios are expressed as ``numerator:denominator`` in the exchange's own terms.
    Converting them into price adjustment factors is Milestone 3's job, not the parser's.
    """

    model_config = ConfigDict(frozen=True)

    exchange: str
    symbol: str
    isin: str | None
    ex_date: date
    action_type: CorporateActionType
    numerator: float | None = None
    denominator: float | None = None
    issue_price: float | None = None
    raw_description: str
    source_sha256: str
