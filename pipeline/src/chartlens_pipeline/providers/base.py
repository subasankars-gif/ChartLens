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
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, computed_field

from chartlens_core.domain import utc_now
from chartlens_pipeline.calendar import TradingCalendar
from chartlens_pipeline.corporate_actions_model import SubjectInterpretation
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

    @property
    def corporate_actions(self) -> CorporateActionSource: ...


# --------------------------------------------------------------- corporate actions (ADR-0011)


@dataclass(frozen=True)
class CorporateActionRecord:
    """One corporate-action announcement as the exchange published it.

    Fields are normalised (dates parsed, blanks to None) but nothing is interpreted:
    the subject text is verbatim. Interpreting it (split? bonus 1:1?) is the job of
    the exchange's subject parser, kept separate so it can be versioned and re-run.
    """

    exchange: str
    symbol: str
    series: str | None
    isin: str | None
    company: str | None
    subject: str
    ex_date: date | None
    record_date: date | None
    face_value: Decimal | None
    """As published. NSE reports the *current* face value on historical records
    (a 2011 "10 → 2" split record shows 1), so it is never used as the face value at
    the time without reconstruction (ADR-0011)."""
    broadcast_at: str | None
    record_key: str
    """SHA-256 of the record's canonical JSON — a stable identity for the exact record."""


@dataclass
class ParsedActions:
    records: list[CorporateActionRecord]
    rejected: list[tuple[str, str]]
    """(reason, raw record JSON) for records that could not be normalised. Never dropped."""


class CorporateActionSource(Protocol):
    @property
    def source_dataset(self) -> str: ...

    @property
    def parser_version(self) -> str: ...

    def windows(self, start: date, end: date) -> list[tuple[date, date]]:
        """Download windows (inclusive) covering ``[start, end]``, by ex-date."""
        ...

    def download(self, window: tuple[date, date]) -> DownloadResult: ...

    def parse(self, content: bytes) -> ParsedActions: ...

    @property
    def grammar_version(self) -> str: ...

    def interpret(self, subject: str) -> SubjectInterpretation:
        """Exchange-specific reading of the free-text subject into neutral components."""
        ...
