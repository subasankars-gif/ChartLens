"""Exchange-independent market-data provider contract.

Every provider method comes as a **download / parse pair**. Downloads return the
exchange's original bytes, which are stored immutably before anything else
happens. Parsing is a pure function of those bytes. This means:

* a parser bug is fixed by re-parsing stored files — no re-download, and no
  dependence on the exchange still serving an old file;
* every canonical row can be traced back to the exact source file (by sha256).

Parsed outputs use the exchange-neutral schemas below. Anything exchange-specific
(series codes, file formats, URL layouts) stays inside the provider.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime
from enum import StrEnum
from typing import Final, Protocol

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, computed_field

from chartlens_core.domain import utc_now
from chartlens_pipeline.calendar import TradingCalendar


class Dataset(StrEnum):
    DAILY_BARS = "daily_bars"
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
    fetched_at: datetime = Field(default_factory=utc_now)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()


# Parsed daily bars: one row per security per session, prices exactly as traded.
RAW_DAILY_COLUMNS: Final = (
    "trade_date",  # datetime64[ns], tz-naive
    "exchange",
    "symbol",
    "series",
    "isin",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "source_sha256",  # the RawArtifact this row came from
)


class SecurityListing(BaseModel):
    model_config = ConfigDict(frozen=True)

    exchange: str
    symbol: str
    series: str
    isin: str | None
    name: str | None
    listing_date: date | None = None


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

    Ratios are expressed as ``numerator:denominator`` in the exchange's own terms
    (e.g. bonus 1:1, split face value 10→2 as 10:2). Converting them into price
    adjustment factors is the adjustment stage's job (Milestone 3), not the parser's.
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
    """Rights issues only."""
    raw_description: str
    source_sha256: str


class MarketDataProvider(Protocol):
    exchange: str
    name: str

    def calendar(self, start: date, end: date) -> TradingCalendar: ...

    def download_daily(self, trade_date: date) -> RawArtifact | None:
        """The full-market daily file for ``trade_date``, or None if no session was held."""
        ...

    def parse_daily(self, artifact: RawArtifact) -> pd.DataFrame:
        """Rows in ``RAW_DAILY_COLUMNS`` order, restricted to the configured universe."""
        ...

    def download_corporate_actions(self, start: date, end: date) -> RawArtifact: ...

    def parse_corporate_actions(self, artifact: RawArtifact) -> list[CorporateActionRecord]: ...

    def download_securities(self) -> RawArtifact: ...

    def parse_securities(self, artifact: RawArtifact) -> list[SecurityListing]: ...
