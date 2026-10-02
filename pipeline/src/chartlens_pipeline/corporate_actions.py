"""Corporate-action acquisition (ADR-0011).

``fetch`` downloads the exchange's corporate-action feed window by window and stores
each response immutably (original bytes, content-hashed). A window whose content
changed since the last download gets a *new* stored version; the old one is kept.

``current_records`` reads, for every window, the most recently downloaded version and
returns its normalised records — a pure function of stored bytes, so every later stage
(classification, factors, adjustment) is reproducible without the network.

Windows are re-fetched while they are "live": announcements for recent and upcoming
ex-dates keep changing, so any window ending within ``live_days`` of today is always
downloaded again. Older windows are fetched once unless ``refetch`` is requested.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta

from chartlens_core.domain import utc_now
from chartlens_core.logs import log_event
from chartlens_pipeline.providers.base import (
    CorporateActionRecord,
    CorporateActionSource,
    DownloadStatus,
)
from chartlens_pipeline.sources import RawSourceStore, SourceRecord
from chartlens_pipeline.storage import ObjectStore

log = logging.getLogger("chartlens.pipeline.corporate_actions")

LIVE_DAYS = 120


@dataclass
class FetchReport:
    windows: int = 0
    downloaded: int = 0
    unchanged: int = 0
    skipped: int = 0
    failed: list[str] = field(default_factory=list)


@dataclass
class RecordSet:
    records: list[tuple[CorporateActionRecord, SourceRecord]]
    rejected: list[tuple[str, str, str]]
    """(source_id, reason, raw JSON) — kept for data quality, never dropped."""
    outside_window: int
    windows_missing: list[str]
    by_year: dict[int, int] = field(default_factory=dict)


class CorporateActionStore:
    def __init__(
        self,
        exchange: str,
        source: CorporateActionSource,
        store: ObjectStore,
        *,
        today: Callable[[], date] = lambda: utc_now().date(),
    ) -> None:
        self.exchange = exchange
        self.source = source
        self.raw = RawSourceStore(store)
        self._today = today

    def _stored(self, window: tuple[date, date]) -> list[SourceRecord]:
        return self.raw.list_for_date(self.exchange, self.source.source_dataset, window[0])

    def fetch(self, start: date, end: date, *, refetch: bool = False) -> FetchReport:
        report = FetchReport()
        live_from = self._today() - timedelta(days=LIVE_DAYS)
        for window in self.source.windows(start, end):
            report.windows += 1
            stored = self._stored(window)
            if stored and not refetch and window[1] < live_from:
                report.skipped += 1
                continue
            result = self.source.download(window)
            if result.status is DownloadStatus.FOUND and result.artifact is not None:
                record, created = self.raw.store(
                    result.artifact, parser_version=self.source.parser_version
                )
                if created:
                    report.downloaded += 1
                else:
                    report.unchanged += 1
                log_event(
                    log,
                    "ca.fetch",
                    window=str(window[0]),
                    created=created,
                    source_hash=record.content_hash,
                    bytes=record.byte_size,
                )
            else:
                report.failed.append(f"{window[0]:%Y-%m}: {result.detail}")
                log_event(
                    log,
                    "ca.fetch.failed",
                    logging.ERROR,
                    window=str(window[0]),
                    detail=result.detail,
                )
        return report

    def current_records(self, start: date, end: date) -> RecordSet:
        """Latest stored version of every window in range, normalised and de-duplicated."""
        seen: set[str] = set()
        records: list[tuple[CorporateActionRecord, SourceRecord]] = []
        rejected: list[tuple[str, str, str]] = []
        outside = 0
        missing: list[str] = []
        years: Counter[int] = Counter()
        for window in self.source.windows(start, end):
            stored = self._stored(window)
            if not stored:
                missing.append(f"{window[0]:%Y-%m}")
                continue
            selected = stored[-1]  # most recently downloaded version
            parsed = self.source.parse(self.raw.load(selected))
            rejected += [(selected.source_id, reason, raw) for reason, raw in parsed.rejected]
            for rec in parsed.records:
                if rec.record_key in seen:
                    continue
                seen.add(rec.record_key)
                if rec.ex_date is not None and not (window[0] <= rec.ex_date <= window[1]):
                    outside += 1
                if rec.ex_date is not None:
                    years[rec.ex_date.year] += 1
                records.append((rec, selected))
        records.sort(key=lambda r: (r[0].ex_date or date.min, r[0].symbol, r[0].record_key))
        return RecordSet(records, rejected, outside, missing, dict(sorted(years.items())))
