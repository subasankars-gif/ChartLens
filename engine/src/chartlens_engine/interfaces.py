"""The contract every engine component implements.

Components never call each other directly with raw data; they are run through
:func:`run_analyzer`, which is the single enforcement point for the bar-frame
contract and the ``as_of`` guard. An analyzer cannot forget to check for future
data because it never gets the chance to see it.
"""

from __future__ import annotations

from datetime import date
from typing import Protocol, runtime_checkable

import pandas as pd
from pydantic import BaseModel, ConfigDict

from chartlens_core.asof import ensure_as_of
from chartlens_core.bars import BAR_DATE, column, validate_bar_frame
from chartlens_core.domain import SecurityId, Timeframe


class AnalysisContext(BaseModel):
    """Identifies one point-in-time analysis run."""

    model_config = ConfigDict(frozen=True)

    security_id: SecurityId
    timeframe: Timeframe
    as_of: date
    methodology_hash: str


class AnalyzerResult(BaseModel):
    """Base class for every analyzer output. Carries provenance, not just values."""

    model_config = ConfigDict(frozen=True)

    analyzer: str
    analyzer_version: str
    context: AnalysisContext


@runtime_checkable
class Analyzer[ResultT: AnalyzerResult](Protocol):
    """A deterministic computation over bars known as of ``context.as_of``."""

    name: str
    version: str

    def analyze(self, bars: pd.DataFrame, context: AnalysisContext) -> ResultT: ...


def run_analyzer[ResultT: AnalyzerResult](
    analyzer: Analyzer[ResultT], bars: pd.DataFrame, context: AnalysisContext
) -> ResultT:
    """Validate inputs, enforce ``as_of``, run the analyzer, and check its provenance."""
    validate_bar_frame(bars)
    ensure_as_of(bars, context.as_of)
    result = analyzer.analyze(bars, context)
    if result.context != context:
        raise RuntimeError(f"{analyzer.name} returned a result for a different context")
    if (result.analyzer, result.analyzer_version) != (analyzer.name, analyzer.version):
        raise RuntimeError(f"{analyzer.name} returned a result with mismatched provenance")
    return result


def last_bar_date(bars: pd.DataFrame) -> date | None:
    """Convenience for analyzers: the date of the most recent bar, if any."""
    if bars.empty:
        return None
    ts = pd.Timestamp(column(bars, BAR_DATE).iloc[-1])
    return ts.date() if isinstance(ts, pd.Timestamp) else None
