from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from chartlens_core.asof import AsOfViolation, slice_as_of
from chartlens_core.bars import BAR_DATE, BarFrameError
from chartlens_core.domain import SecurityId, Timeframe
from chartlens_core.testing import make_bars
from chartlens_engine.interfaces import (
    AnalysisContext,
    Analyzer,
    AnalyzerResult,
    last_bar_date,
    run_analyzer,
)


class BarCount(AnalyzerResult):
    count: int
    last: date | None


class CountingAnalyzer:
    """Minimal analyzer used to exercise the contract."""

    name = "bar_count"
    version = "1.0.0"

    def __init__(self) -> None:
        self.seen: pd.DataFrame | None = None

    def analyze(self, bars: pd.DataFrame, context: AnalysisContext) -> BarCount:
        self.seen = bars
        return BarCount(
            analyzer=self.name,
            analyzer_version=self.version,
            context=context,
            count=len(bars),
            last=last_bar_date(bars),
        )


class LyingAnalyzer(CountingAnalyzer):
    def analyze(self, bars: pd.DataFrame, context: AnalysisContext) -> BarCount:
        result = super().analyze(bars, context)
        return result.model_copy(update={"analyzer_version": "9.9.9"})


def _ctx(as_of: date) -> AnalysisContext:
    return AnalysisContext(
        security_id=SecurityId("SEC-TEST"),
        timeframe=Timeframe.WEEKLY,
        as_of=as_of,
        methodology_hash="0" * 12,
    )


def test_counting_analyzer_satisfies_protocol() -> None:
    assert isinstance(CountingAnalyzer(), Analyzer)


def test_run_analyzer_passes_point_in_time_bars_through() -> None:
    bars = make_bars(date(2021, 1, 4), 30, freq="W-FRI")
    as_of = bars[BAR_DATE].iloc[19].date()
    result = run_analyzer(CountingAnalyzer(), slice_as_of(bars, as_of), _ctx(as_of))
    assert result.count == 20
    assert result.last == as_of


def test_run_analyzer_refuses_future_bars_before_the_analyzer_sees_them() -> None:
    bars = make_bars(date(2021, 1, 4), 30, freq="W-FRI")
    analyzer = CountingAnalyzer()
    with pytest.raises(AsOfViolation):
        run_analyzer(analyzer, bars, _ctx(bars[BAR_DATE].iloc[10].date()))
    assert analyzer.seen is None


def test_run_analyzer_validates_contract() -> None:
    bars = make_bars(date(2021, 1, 4), 5).drop(columns=["volume"])
    with pytest.raises(BarFrameError):
        run_analyzer(CountingAnalyzer(), bars, _ctx(date(2030, 1, 1)))


def test_run_analyzer_rejects_mismatched_provenance() -> None:
    bars = make_bars(date(2021, 1, 4), 5)
    with pytest.raises(RuntimeError, match="provenance"):
        run_analyzer(LyingAnalyzer(), bars, _ctx(date(2030, 1, 1)))
