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
    confirmable,
    last_bar_date,
    provisional,
    run_analyzer,
)

SID, SEG = "SEC-TEST", "SEC-TEST@2021-01-04"


def weekly(periods: int = 30) -> pd.DataFrame:
    """Weekly bars carrying their identity, as WeeklySeries.frame() produces them."""
    return make_bars(date(2021, 1, 4), periods, freq="W-FRI").assign(
        security_id=SID, continuity_segment_id=SEG
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


def _ctx(as_of: date, segment: str | None = SEG) -> AnalysisContext:
    return AnalysisContext(
        security_id=SecurityId(SID),
        timeframe=Timeframe.WEEKLY,
        as_of=as_of,
        methodology_hash="0" * 12,
        continuity_segment_id=segment,
    )


def test_counting_analyzer_satisfies_protocol() -> None:
    assert isinstance(CountingAnalyzer(), Analyzer)


def test_run_analyzer_passes_point_in_time_bars_through() -> None:
    bars = weekly()
    as_of = bars[BAR_DATE].iloc[19].date()
    result = run_analyzer(CountingAnalyzer(), slice_as_of(bars, as_of), _ctx(as_of))
    assert result.count == 20
    assert result.last == as_of


def test_run_analyzer_refuses_future_bars_before_the_analyzer_sees_them() -> None:
    bars = weekly()
    analyzer = CountingAnalyzer()
    with pytest.raises(AsOfViolation):
        run_analyzer(analyzer, bars, _ctx(bars[BAR_DATE].iloc[10].date()))
    assert analyzer.seen is None


def test_run_analyzer_validates_contract() -> None:
    bars = weekly(5).drop(columns=["volume"])
    with pytest.raises(BarFrameError):
        run_analyzer(CountingAnalyzer(), bars, _ctx(date(2030, 1, 1)))


def test_run_analyzer_rejects_mismatched_provenance() -> None:
    bars = weekly(5)
    with pytest.raises(RuntimeError, match="provenance"):
        run_analyzer(LyingAnalyzer(), bars, _ctx(date(2030, 1, 1)))


# ----------------------------------------------------------------------------- ADR-0015 guard


@pytest.mark.parametrize(
    ("bars", "ctx", "message"),
    [
        (weekly().drop(columns=["continuity_segment_id"]), _ctx(date(2030, 1, 1)), "must carry"),
        (weekly().drop(columns=["security_id"]), _ctx(date(2030, 1, 1)), "must carry"),
        (weekly(), _ctx(date(2030, 1, 1), segment=None), "continuity_segment_id"),
        (weekly().assign(security_id="SEC-OTHER"), _ctx(date(2030, 1, 1)), "SEC-OTHER"),
        (weekly(), _ctx(date(2030, 1, 1), segment="SEC-TEST@2025-01-06"), "segment"),
        (
            weekly().assign(continuity_segment_id=[SEG] * 15 + ["SEC-TEST@2021-04-19"] * 15),
            _ctx(date(2030, 1, 1)),
            "continuity segments",
        ),
    ],
)
def test_no_relationship_can_cross_a_security_or_continuity_boundary(
    bars: pd.DataFrame, ctx: AnalysisContext, message: str
) -> None:
    analyzer = CountingAnalyzer()
    with pytest.raises(BarFrameError, match=message):
        run_analyzer(analyzer, bars, ctx)
    assert analyzer.seen is None


def test_daily_frames_without_identity_columns_are_still_accepted() -> None:
    ctx = _ctx(date(2030, 1, 1)).model_copy(update={"timeframe": Timeframe.DAILY})
    assert run_analyzer(CountingAnalyzer(), make_bars(date(2021, 1, 4), 5), ctx).count == 5


def test_confirmations_use_complete_bars_and_special_closes_are_provisional() -> None:
    bars = weekly(4).assign(
        is_complete=[True, True, True, False],
        closes_on_special_session=[False, True, False, False],
    )
    assert list(confirmable(bars).index) == [0, 1, 2]
    assert list(provisional(bars)) == [False, True, False, False]
    assert not provisional(make_bars(date(2021, 1, 4), 3)).any()
