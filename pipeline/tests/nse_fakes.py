"""Offline NSE: the real provider code talking to a mock HTTP transport that serves
fixture files at their real archive URLs. No test touches the network."""

from __future__ import annotations

import io
import zipfile
from collections.abc import Callable, Iterable
from datetime import date
from pathlib import Path

import httpx

from chartlens_core.config import ChartLensSettings, HttpConfig
from chartlens_pipeline.calendar import CalendarEvidence, CalendarYear, DataCalendar
from chartlens_pipeline.http import HttpFetcher
from chartlens_pipeline.providers.nse import NseProvider, legacy_url, udiff_url

FIXTURES = Path(__file__).parent / "fixtures" / "nse"
BASE = "https://nsearchives.nseindia.com"
NOT_FOUND_HTML = b"<!DOCTYPE html><html><body>404 Not Found</body></html>"


def zipped(member: str, content: bytes) -> bytes:
    """Deterministic zip (fixed timestamp) so the same content always hashes the same."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        info = zipfile.ZipInfo(member, date_time=(2020, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        zf.writestr(info, content)
    return buf.getvalue()


def fixture_csv(name: str) -> bytes:
    return (FIXTURES / f"{name}.csv").read_bytes()


def legacy_zip(day: date, content: bytes) -> tuple[str, bytes]:
    url = legacy_url(BASE, day)
    return url, zipped(url.rsplit("/", 1)[-1].removesuffix(".zip"), content)


def udiff_zip(day: date, content: bytes) -> tuple[str, bytes]:
    url = udiff_url(BASE, day)
    return url, zipped(url.rsplit("/", 1)[-1].removesuffix(".zip"), content)


class FakeNse:
    """Mutable map of URL → response, so tests can re-issue or remove files."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.errors: dict[str, int] = {}
        self.requests: list[str] = []
        self.symbol_changes: bytes | None = None

    def serve(self, url: str, body: bytes) -> None:
        self.files[url] = body

    def fail(self, url: str, status: int) -> None:
        self.errors[url] = status

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(f"{request.method} {url}")
        if url in self.errors:
            return httpx.Response(self.errors[url], content=b"error")
        if url.endswith("/content/equities/symbolchange.csv"):
            if self.symbol_changes is None:
                return httpx.Response(
                    404, content=NOT_FOUND_HTML, headers={"content-type": "text/html"}
                )
            return httpx.Response(
                200, content=self.symbol_changes, headers={"content-type": "text/csv"}
            )
        if url in self.files:
            body = self.files[url] if request.method == "GET" else b""
            return httpx.Response(200, content=body, headers={"content-type": "application/zip"})
        return httpx.Response(404, content=NOT_FOUND_HTML, headers={"content-type": "text/html"})


def calendar_for(
    years: Iterable[int],
    holidays: Iterable[date] = (),
    special: Iterable[date] = (),
    evidence: CalendarEvidence = CalendarEvidence.OFFICIAL,
) -> DataCalendar:
    hol, spe = set(holidays), set(special)
    return DataCalendar(
        "NSE",
        [
            CalendarYear(
                y,
                evidence,
                frozenset(d for d in hol if d.year == y),
                frozenset(d for d in spe if d.year == y),
            )
            for y in years
        ],
        version="test",
    )


def fake_provider(
    fake: FakeNse, calendar: DataCalendar, settings: ChartLensSettings | None = None
) -> NseProvider:
    settings = settings or ChartLensSettings.model_construct()
    http = HttpConfig(max_attempts=2, backoff_seconds=0, min_request_interval_seconds=0)
    fetcher = HttpFetcher(http, transport=httpx.MockTransport(fake.handler), sleep=lambda _s: None)
    return NseProvider(settings.providers.nse, fetcher, calendar=calendar)


Clock = Callable[[], date]
