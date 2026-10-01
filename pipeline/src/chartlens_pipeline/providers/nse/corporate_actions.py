"""NSE corporate-actions feed (ADR-0011).

Source: NSE's JSON API ``/api/corporates-corporateActions?index=equities&from_date=&to_date=``.
Established by the corporate-action probes (2026-10-01):

* answers GitHub-hosted runners (the www.nseindia.com home page does not; the API does);
* covers ex-dates back to at least 2006, though early years are sparse;
* filters by ex-date window; one record per announcement with ``symbol``, ``series``,
  ``isin``, ``comp``, ``subject`` (free text), ``exDate``, ``recDate``, ``faceVal``
  (**current** face value, not historical) and ``caBroadcastDate`` (null for older records).

Windows are calendar months. Each month's response is stored immutably as the original
bytes; re-fetching a month whose content changed keeps both versions.
"""

from __future__ import annotations

import hashlib
import json
from calendar import monthrange
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from chartlens_core.config import NseProviderConfig
from chartlens_pipeline.corporate_actions_model import SubjectInterpretation
from chartlens_pipeline.http import FetchOutcome, HttpFetcher
from chartlens_pipeline.providers.base import (
    CorporateActionRecord,
    Dataset,
    DownloadResult,
    DownloadStatus,
    ParsedActions,
    RawArtifact,
)
from chartlens_pipeline.providers.nse.ca_subjects import SUBJECT_GRAMMAR_VERSION, interpret

FEED_PARSER_VERSION: Final = "nse_ca_feed_v1"
_BLANK = {"", "-", "null", "None"}


def expect_json_array(body: bytes, content_type: str) -> str | None:
    if not body.strip():
        return "empty body"
    head = body.lstrip()[:1]
    if head != b"[":
        if body.lstrip()[:15].lower().startswith((b"<!doctype", b"<html")):
            return "HTML page returned where a JSON array was expected"
        return "body is not a JSON array"
    try:
        json.loads(body)
    except ValueError as exc:
        return f"invalid JSON: {exc}"
    return None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    return None if text in _BLANK else text


def _date(value: Any) -> date | None:
    text = _text(value)
    if text is None:
        return None
    for fmt in ("%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()  # noqa: DTZ007 — a calendar date
        except ValueError:
            continue
    raise ValueError(f"unrecognised date {text!r}")


def _decimal(value: Any) -> Decimal | None:
    text = _text(value)
    if text is None:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        raise ValueError(f"unrecognised number {text!r}") from None


def parse_feed(content: bytes, exchange: str = "NSE") -> ParsedActions:
    """Normalise one API response. Pure function of the bytes."""
    data = json.loads(content)
    if not isinstance(data, list):
        raise ValueError("corporate-actions response is not a JSON array")
    records: list[CorporateActionRecord] = []
    rejected: list[tuple[str, str]] = []
    for item in data:
        raw = json.dumps(item, sort_keys=True, ensure_ascii=False)
        if not isinstance(item, dict):
            rejected.append(("NOT_AN_OBJECT", raw))
            continue
        symbol, subject = _text(item.get("symbol")), _text(item.get("subject"))
        if symbol is None or subject is None:
            rejected.append(("MISSING_SYMBOL_OR_SUBJECT", raw))
            continue
        try:
            record = CorporateActionRecord(
                exchange=exchange,
                symbol=symbol.upper(),
                series=(_text(item.get("series")) or "").upper() or None,
                isin=(_text(item.get("isin")) or "").upper() or None,
                company=_text(item.get("comp")),
                subject=subject,
                ex_date=_date(item.get("exDate")),
                record_date=_date(item.get("recDate")),
                face_value=_decimal(item.get("faceVal")),
                broadcast_at=_text(item.get("caBroadcastDate")),
                record_key=hashlib.sha256(raw.encode()).hexdigest(),
            )
        except ValueError as exc:
            rejected.append((f"INVALID_FIELD: {exc}", raw))
            continue
        records.append(record)
    return ParsedActions(records=records, rejected=rejected)


def month_windows(start: date, end: date) -> list[tuple[date, date]]:
    windows: list[tuple[date, date]] = []
    current = date(start.year, start.month, 1)
    while current <= end:
        last = date(current.year, current.month, monthrange(current.year, current.month)[1])
        windows.append((current, last))
        current = date(current.year + (current.month == 12), current.month % 12 + 1, 1)
    return windows


class NseCorporateActions:
    def __init__(self, config: NseProviderConfig, fetcher: HttpFetcher) -> None:
        self._config = config
        self._fetcher = fetcher
        self.source_dataset = "corporate_actions"
        self.parser_version = FEED_PARSER_VERSION
        self.grammar_version = SUBJECT_GRAMMAR_VERSION

    def windows(self, start: date, end: date) -> list[tuple[date, date]]:
        return month_windows(start, end)

    def url(self, window: tuple[date, date]) -> str:
        first, last = window
        return (
            f"{self._config.api_base_url.rstrip('/')}/api/corporates-corporateActions"
            f"?index=equities&from_date={first:%d-%m-%Y}&to_date={last:%d-%m-%Y}"
        )

    def download(self, window: tuple[date, date]) -> DownloadResult:
        url = self.url(window)
        result = self._fetcher.fetch(url, validate=expect_json_array)
        last = result.attempts[-1] if result.attempts else None
        tried = ((url, str(result.outcome), last.status if last else None),)
        if not result.ok:
            status = (
                DownloadStatus.NOT_PUBLISHED
                if result.outcome is FetchOutcome.NOT_FOUND
                else DownloadStatus.FAILED
            )
            detail = f"{result.outcome}" + (f" ({last.detail})" if last and last.detail else "")
            return DownloadResult(status, None, tried, detail)
        artifact = RawArtifact(
            exchange="NSE",
            provider="nse",
            dataset=Dataset.CORPORATE_ACTIONS,
            source_dataset=self.source_dataset,
            logical_date=window[0],
            filename=f"ca_{window[0]:%Y%m}.json",
            content=result.content,
            url=url,
            content_type=result.content_type or "application/json",
        )
        return DownloadResult(DownloadStatus.FOUND, artifact, tried)

    def parse(self, content: bytes) -> ParsedActions:
        return parse_feed(content)

    def interpret(self, subject: str) -> SubjectInterpretation:
        return interpret(subject)
