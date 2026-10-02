"""NSE (National Stock Exchange of India) provider — ADR-0008.

Everything NSE-specific lives in this package: archive URLs, the two bhavcopy
formats, the symbol-change list, the ISIN issuer rule and the trading calendar
data. Nothing outside ``providers/nse`` may depend on it.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable
from datetime import date, datetime
from functools import cache
from importlib import resources
from pathlib import Path
from typing import Final

from chartlens_core.config import NseProviderConfig
from chartlens_pipeline.calendar import DataCalendar
from chartlens_pipeline.daily import ParsedDaily
from chartlens_pipeline.http import FetchOutcome, HttpFetcher, expect_text, expect_zip
from chartlens_pipeline.identity import SymbolChangeNotice
from chartlens_pipeline.providers.base import (
    Dataset,
    DownloadResult,
    DownloadStatus,
    RawArtifact,
)
from chartlens_pipeline.providers.nse.bhavcopy import PARSER_VERSION, parse_bhavcopy
from chartlens_pipeline.providers.nse.corporate_actions import NseCorporateActions

EXCHANGE: Final = "NSE"
PROVIDER: Final = "nse"
_MONTHS: Final = (
    "JAN",
    "FEB",
    "MAR",
    "APR",
    "MAY",
    "JUN",
    "JUL",
    "AUG",
    "SEP",
    "OCT",
    "NOV",
    "DEC",
)


def legacy_url(base: str, day: date) -> str:
    mon = _MONTHS[day.month - 1]
    name = f"cm{day.day:02d}{mon}{day.year}bhav.csv.zip"
    return f"{base}/content/historical/EQUITIES/{day.year}/{mon}/{name}"


def udiff_url(base: str, day: date) -> str:
    return f"{base}/content/cm/BhavCopy_NSE_CM_0_0_0_{day:%Y%m%d}_F_0000.csv.zip"


def candidate_urls(config: NseProviderConfig, day: date) -> list[str]:
    """Where a bhavcopy for ``day`` may be published, in order of preference.

    UDiFF first where it may exist (it carries names and is NSE's current format);
    legacy wherever NSE published it. Both are tried in their overlap (Jan–Jul 2024).
    """
    base = config.archive_base_url.rstrip("/")
    urls: list[str] = []
    if day >= config.udiff_first_date:
        urls.append(udiff_url(base, day))
    if day <= config.legacy_last_date:
        urls.append(legacy_url(base, day))
    return urls


class NseIdentityPolicy:
    """Indian ISINs (NSDL/CDSL): ``IN`` + issuer type (1) + issuer code (4) + security
    type (2) + serial (2) + check digit. When a company's ISIN is re-issued (e.g. after
    a face-value split) the issuer code and security type stay the same and the serial
    changes, so ``isin[:9]`` identifies "same issuer, same kind of security"."""

    def issuer_key(self, isin: str) -> str | None:
        if len(isin) == 12 and isin.startswith("INE"):
            return isin[:9]
        return None

    def instrument_type(self, isins: Iterable[str]) -> str:
        """From the latest ISIN: ``INF`` = mutual-fund / ETF units; otherwise the 2-digit
        security type at positions 8-9: ``01`` equity shares (``IN9`` DVRs included),
        ``20`` rights entitlements, anything else OTHER_<type>. No ISIN (NSE rows before
        2011-06-22 only) = UNKNOWN."""
        latest = [i for i in isins if len(i) == 12][-1:]
        if not latest:
            return "UNKNOWN"
        isin = latest[0]
        if isin.startswith("INF"):
            return "FUND_UNIT"
        kind = isin[7:9]
        return {"01": "EQUITY_SHARE", "20": "RIGHTS_ENTITLEMENT"}.get(kind, f"OTHER_{kind}")


class NseDailyBars:
    def __init__(self, config: NseProviderConfig, fetcher: HttpFetcher) -> None:
        self._config = config
        self._fetcher = fetcher
        self.source_dataset = "bhavcopy"
        self.parser_version = PARSER_VERSION

    def _run(self, day: date, *, keep_body: bool) -> DownloadResult:
        # Always GET and validate the body. HEAD is not trustworthy on NSE's archive: the
        # legacy path answers HEAD with 200 for files that do not exist (found by the
        # calendar derivation, 2026-09-30), and HEAD cannot tell a zip from an HTML page.
        tried: list[tuple[str, str, int | None]] = []
        failures: list[str] = []
        for url in candidate_urls(self._config, day):
            result = self._fetcher.fetch(url, validate=expect_zip)
            last = result.attempts[-1] if result.attempts else None
            tried.append((url, str(result.outcome), last.status if last else None))
            if result.ok:
                artifact = (
                    RawArtifact(
                        exchange=EXCHANGE,
                        provider=PROVIDER,
                        dataset=Dataset.DAILY_BARS,
                        source_dataset=self.source_dataset,
                        logical_date=day,
                        filename=url.rsplit("/", 1)[-1],
                        content=result.content,
                        url=url,
                        content_type=result.content_type,
                    )
                    if keep_body
                    else None
                )
                return DownloadResult(DownloadStatus.FOUND, artifact, tuple(tried))
            if result.outcome is not FetchOutcome.NOT_FOUND:
                detail = f" ({last.detail})" if last and last.detail else ""
                failures.append(f"{url}: {result.outcome}{detail}")
        if not tried:
            return DownloadResult(
                DownloadStatus.FAILED, None, (), f"no NSE source configured for {day}"
            )
        if failures:
            return DownloadResult(DownloadStatus.FAILED, None, tuple(tried), "; ".join(failures))
        return DownloadResult(
            DownloadStatus.NOT_PUBLISHED, None, tuple(tried), "404 at every location"
        )

    def download(self, day: date) -> DownloadResult:
        return self._run(day, keep_body=True)

    def check_published(self, day: date) -> DownloadResult:
        return self._run(day, keep_body=False)

    def parse(self, content: bytes, day: date, universe_series: frozenset[str]) -> ParsedDaily:
        return parse_bhavcopy(content, day, universe_series)


def parse_symbol_changes(content: bytes) -> list[SymbolChangeNotice]:
    """NSE ``symbolchange.csv``: company, old symbol, new symbol, date (``DD-MON-YYYY``).
    The file has no header. Lines that do not parse are skipped (they are identity
    *evidence*, not data; a missing notice only means fewer automatic links)."""
    text = content.decode("utf-8-sig", errors="replace")
    notices: list[SymbolChangeNotice] = []
    for cells in csv.reader(io.StringIO(text)):
        if len(cells) < 4:
            continue
        company, old, new, when = (c.strip() for c in cells[:4])
        try:
            effective = datetime.strptime(when, "%d-%b-%Y").date()  # noqa: DTZ007
        except ValueError:
            continue
        if old and new and old.upper() != new.upper():
            notices.append(SymbolChangeNotice(old.upper(), new.upper(), effective, company))
    return sorted(notices, key=lambda n: (n.effective, n.old_symbol, n.new_symbol))


@cache
def load_calendar(path: Path | None = None) -> DataCalendar:
    if path is not None:
        return DataCalendar.from_toml(path)
    with resources.as_file(resources.files(__package__) / "data" / "calendar.toml") as p:
        return DataCalendar.from_toml(p)


class NseProvider:
    exchange_code = EXCHANGE

    def __init__(
        self,
        config: NseProviderConfig,
        fetcher: HttpFetcher,
        *,
        calendar: DataCalendar | None = None,
    ) -> None:
        self._config = config
        self._fetcher = fetcher
        self._calendar = calendar
        self.daily_bars = NseDailyBars(config, fetcher)
        self.corporate_actions = NseCorporateActions(config, fetcher)

    def trading_calendar(self) -> DataCalendar:
        return self._calendar if self._calendar is not None else load_calendar()

    def identity_policy(self) -> NseIdentityPolicy:
        return NseIdentityPolicy()

    def download_symbol_changes(self) -> DownloadResult:
        url = f"{self._config.archive_base_url.rstrip('/')}/content/equities/symbolchange.csv"
        result = self._fetcher.fetch(url, validate=expect_text)
        last = result.attempts[-1] if result.attempts else None
        tried = ((url, str(result.outcome), last.status if last else None),)
        if not result.ok:
            status = (
                DownloadStatus.NOT_PUBLISHED
                if result.outcome is FetchOutcome.NOT_FOUND
                else DownloadStatus.FAILED
            )
            return DownloadResult(status, None, tried, str(result.outcome))
        artifact = RawArtifact(
            exchange=EXCHANGE,
            provider=PROVIDER,
            dataset=Dataset.SYMBOL_CHANGES,
            source_dataset="symbolchange",
            logical_date=None,
            filename="symbolchange.csv",
            content=result.content,
            url=url,
            content_type=result.content_type,
        )
        return DownloadResult(DownloadStatus.FOUND, artifact, tried)

    def parse_symbol_changes(self, content: bytes) -> list[SymbolChangeNotice]:
        return parse_symbol_changes(content)
