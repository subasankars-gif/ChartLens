from __future__ import annotations

import httpx
import pytest

from chartlens_core.config import HttpConfig
from chartlens_pipeline.http import FetchOutcome, HttpFetcher, expect_zip

URL = "https://archive.test/file.zip"
ZIP = b"PK\x03\x04rest-of-zip"


def fetcher(
    responses: list[httpx.Response | Exception], **config: float | int
) -> tuple[HttpFetcher, list[float]]:
    slept: list[float] = []
    queue = list(responses)

    def handler(_request: httpx.Request) -> httpx.Response:
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    cfg = HttpConfig(
        **{"max_attempts": 4, "backoff_seconds": 1.0, "min_request_interval_seconds": 0, **config}
    )  # type: ignore[arg-type]
    return HttpFetcher(cfg, transport=httpx.MockTransport(handler), sleep=slept.append), slept


def test_ok_returns_body() -> None:
    f, slept = fetcher([httpx.Response(200, content=ZIP)])
    result = f.fetch(URL, validate=expect_zip)
    assert result.ok and result.content == ZIP and slept == []


def test_404_is_not_found_and_not_retried() -> None:
    f, slept = fetcher([httpx.Response(404, content=b"<html>not found</html>")])
    result = f.fetch(URL, validate=expect_zip)
    assert result.outcome is FetchOutcome.NOT_FOUND and len(result.attempts) == 1 and slept == []


def test_403_is_forbidden_and_not_retried() -> None:
    f, _ = fetcher([httpx.Response(403)])
    assert f.fetch(URL).outcome is FetchOutcome.FORBIDDEN


def test_server_error_is_retried_with_exponential_backoff() -> None:
    f, slept = fetcher([httpx.Response(503), httpx.Response(502), httpx.Response(200, content=ZIP)])
    result = f.fetch(URL, validate=expect_zip)
    assert result.ok and len(result.attempts) == 3 and slept == [1.0, 2.0]


def test_retries_are_bounded() -> None:
    f, slept = fetcher([httpx.Response(500)] * 10, max_attempts=3)
    result = f.fetch(URL)
    assert (
        result.outcome is FetchOutcome.SERVER_ERROR
        and len(result.attempts) == 3
        and len(slept) == 2
    )


def test_rate_limit_honours_retry_after_but_caps_it() -> None:
    f, slept = fetcher(
        [
            httpx.Response(429, headers={"retry-after": "5"}),
            httpx.Response(429, headers={"retry-after": "999"}),
            httpx.Response(200, content=ZIP),
        ],
        max_retry_after_seconds=30,
    )
    assert f.fetch(URL, validate=expect_zip).ok
    assert slept == [5.0, 30.0]


def test_timeouts_and_network_errors_are_retried() -> None:
    f, _ = fetcher(
        [httpx.ReadTimeout("slow"), httpx.ConnectError("reset"), httpx.Response(200, content=ZIP)]
    )
    result = f.fetch(URL, validate=expect_zip)
    assert result.ok
    assert [a.outcome for a in result.attempts] == [
        FetchOutcome.TIMEOUT,
        FetchOutcome.NETWORK,
        FetchOutcome.OK,
    ]


@pytest.mark.parametrize(
    ("body", "ctype", "detail"),
    [
        (b"<!DOCTYPE html><html>maintenance</html>", "text/html", "HTML page"),
        (b"", "application/zip", "empty body"),
        (b"SYMBOL,SERIES\n", "text/csv", "not a zip"),
    ],
)
def test_wrong_kind_of_200_is_invalid_and_retried(body: bytes, ctype: str, detail: str) -> None:
    f, _ = fetcher([httpx.Response(200, content=body, headers={"content-type": ctype})] * 4)
    result = f.fetch(URL, validate=expect_zip)
    assert result.outcome is FetchOutcome.INVALID_RESPONSE and len(result.attempts) == 4
    assert detail in result.attempts[-1].detail and result.content == b""


def test_politeness_delay_between_requests() -> None:
    now = [0.0]
    slept: list[float] = []

    def sleep(s: float) -> None:
        slept.append(s)
        now[0] += s

    f = HttpFetcher(
        HttpConfig(min_request_interval_seconds=0.5),
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, content=ZIP)),
        sleep=sleep,
        clock=lambda: now[0],
    )
    f.fetch(URL)
    f.fetch(URL)
    assert slept == [0.5]
