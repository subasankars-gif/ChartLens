"""HTTP fetching for market-data sources, with bounded retries (spec §30).

Every request ends in exactly one :class:`FetchOutcome`:

========================  =================================================  =========
Outcome                   Meaning                                            Retried?
========================  =================================================  =========
``OK``                    200 with a non-empty body of the expected kind     —
``NOT_FOUND``             404: the source does not exist (e.g. a holiday)    no
``FORBIDDEN``             401/403: blocked (e.g. IP range refused)           no
``RATE_LIMITED``          429                                                yes (Retry-After)
``SERVER_ERROR``          5xx                                                yes
``TIMEOUT`` / ``NETWORK`` no response                                        yes
``INVALID_RESPONSE``      200 but empty, or HTML instead of a file           yes
========================  =================================================  =========

"Not found" is a *fact about the source*; every other failure is a *fact about this
attempt*. Ingestion treats them differently: only ``NOT_FOUND`` may be recorded as
"not published".
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

import httpx

from chartlens_core.config import HttpConfig
from chartlens_core.logs import log_event

log = logging.getLogger("chartlens.pipeline.http")


class FetchOutcome(StrEnum):
    OK = "OK"
    NOT_FOUND = "NOT_FOUND"
    FORBIDDEN = "FORBIDDEN"
    RATE_LIMITED = "RATE_LIMITED"
    SERVER_ERROR = "SERVER_ERROR"
    TIMEOUT = "TIMEOUT"
    NETWORK = "NETWORK"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    UNEXPECTED_STATUS = "UNEXPECTED_STATUS"


RETRYABLE = frozenset(
    {
        FetchOutcome.RATE_LIMITED,
        FetchOutcome.SERVER_ERROR,
        FetchOutcome.TIMEOUT,
        FetchOutcome.NETWORK,
        FetchOutcome.INVALID_RESPONSE,
    }
)


@dataclass(frozen=True)
class Attempt:
    outcome: FetchOutcome
    status: int | None
    detail: str
    seconds: float


@dataclass(frozen=True)
class FetchResult:
    url: str
    outcome: FetchOutcome
    content: bytes = b""
    content_type: str = ""
    attempts: tuple[Attempt, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return self.outcome is FetchOutcome.OK


Validator = Callable[[bytes, str], str | None]
"""Checks a 200 body; returns a reason string if the body is not what was expected."""


def expect_zip(body: bytes, content_type: str) -> str | None:
    if not body:
        return "empty body"
    if body[:2] != b"PK":
        head = body[:64].lstrip().lower()
        if head.startswith((b"<!doctype", b"<html")) or "html" in content_type.lower():
            return "HTML page returned where a zip file was expected"
        return "body is not a zip archive"
    return None


def expect_text(body: bytes, content_type: str) -> str | None:
    if not body.strip():
        return "empty body"
    if body[:64].lstrip().lower().startswith((b"<!doctype", b"<html")):
        return "HTML page returned where a data file was expected"
    return None


class HttpFetcher:
    """Synchronous fetcher with retries, backoff and per-instance politeness delay.

    ``sleep`` and ``clock`` are injectable so tests run instantly and deterministically.
    """

    def __init__(
        self,
        config: HttpConfig,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._sleep = sleep
        self._clock = clock
        self._last_request: float | None = None
        self._client = httpx.Client(
            transport=transport,
            timeout=config.timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": config.user_agent, "Accept": "*/*"},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> HttpFetcher:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _pace(self) -> None:
        if self._last_request is not None:
            wait = self._config.min_request_interval_seconds - (self._clock() - self._last_request)
            if wait > 0:
                self._sleep(wait)
        self._last_request = self._clock()

    def _one(
        self, method: str, url: str, validate: Validator | None
    ) -> tuple[Attempt, httpx.Response | None]:
        self._pace()
        started = self._clock()
        try:
            response = self._client.request(method, url)
        except httpx.TimeoutException as exc:
            return Attempt(FetchOutcome.TIMEOUT, None, str(exc), self._clock() - started), None
        except httpx.TransportError as exc:
            return Attempt(FetchOutcome.NETWORK, None, str(exc), self._clock() - started), None
        elapsed = self._clock() - started
        status = response.status_code
        if status == 200:
            reason = (
                validate(response.content, response.headers.get("content-type", ""))
                if (validate and method == "GET")
                else None
            )
            if reason:
                return Attempt(FetchOutcome.INVALID_RESPONSE, status, reason, elapsed), response
            return Attempt(FetchOutcome.OK, status, "", elapsed), response
        if status == 404:
            return Attempt(FetchOutcome.NOT_FOUND, status, "", elapsed), response
        if status in (401, 403):
            return Attempt(FetchOutcome.FORBIDDEN, status, "", elapsed), response
        if status == 429:
            return Attempt(FetchOutcome.RATE_LIMITED, status, "", elapsed), response
        if status >= 500:
            return Attempt(FetchOutcome.SERVER_ERROR, status, "", elapsed), response
        return Attempt(FetchOutcome.UNEXPECTED_STATUS, status, "", elapsed), response

    def _retry_wait(self, attempt_no: int, response: httpx.Response | None) -> float:
        wait = self._config.backoff_seconds * (2 ** (attempt_no - 1))
        if response is not None and response.status_code == 429:
            header = response.headers.get("retry-after", "")
            if header.isdigit():
                wait = max(wait, float(header))
        return min(wait, self._config.max_retry_after_seconds)

    def fetch(
        self, url: str, *, validate: Validator | None = None, method: str = "GET"
    ) -> FetchResult:
        attempts: list[Attempt] = []
        response: httpx.Response | None = None
        for attempt_no in range(1, self._config.max_attempts + 1):
            attempt, response = self._one(method, url, validate)
            attempts.append(attempt)
            if attempt.outcome not in RETRYABLE or attempt_no == self._config.max_attempts:
                break
            wait = self._retry_wait(attempt_no, response)
            log_event(
                log,
                "http.retry",
                logging.WARNING,
                url=url,
                outcome=attempt.outcome,
                status=attempt.status,
                attempt=attempt_no,
                wait_seconds=wait,
            )
            self._sleep(wait)
        final = attempts[-1]
        ok = final.outcome is FetchOutcome.OK and response is not None
        return FetchResult(
            url=url,
            outcome=final.outcome,
            content=response.content if ok and method == "GET" and response is not None else b"",
            content_type=response.headers.get("content-type", "") if response is not None else "",
            attempts=tuple(attempts),
        )
