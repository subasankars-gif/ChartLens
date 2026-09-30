"""Market-data providers. Each exchange is a subpackage implementing
:class:`chartlens_pipeline.providers.base.ExchangeProvider`; ``get_provider`` is the
only place that knows which exchanges exist."""

from __future__ import annotations

from chartlens_core.config import ChartLensSettings
from chartlens_pipeline.http import HttpFetcher
from chartlens_pipeline.providers.base import (
    DailyBarSource,
    Dataset,
    DownloadResult,
    DownloadStatus,
    ExchangeProvider,
    RawArtifact,
)


class UnknownExchangeError(LookupError):
    pass


def get_provider(exchange: str, settings: ChartLensSettings, fetcher: HttpFetcher) -> ExchangeProvider:
    code = exchange.upper()
    if code == "NSE":
        from chartlens_pipeline.providers.nse import NseProvider

        return NseProvider(settings.providers.nse, fetcher)
    raise UnknownExchangeError(f"no provider for exchange {exchange!r} (available: NSE)")


__all__ = [
    "DailyBarSource",
    "Dataset",
    "DownloadResult",
    "DownloadStatus",
    "ExchangeProvider",
    "RawArtifact",
    "UnknownExchangeError",
    "get_provider",
]
