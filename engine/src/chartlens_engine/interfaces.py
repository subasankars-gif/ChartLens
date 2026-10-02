"""The contract every engine component implements.

Components never call each other directly with raw data; they are run through
:func:`run_analyzer`, which is the single enforcement point for the bar-frame
contract, the ``as_of`` guard and the comparability guard (ADR-0015): every bar an
analyzer receives belongs to the context's security and continuity segment and is dated
on or before its ``as_of``. So no technical relationship — HH/HL, BOS/CHoCH, a trend
transition, pattern geometry, a Fibonacci swing, a divergence — can cross a security or
a continuity boundary, and no analyzer has to remember the rule itself.

Confirmation logic additionally uses :func:`confirmable` (complete bars only) and
treats a bar whose close came from a non-regular session as provisional until the next
regular week (:func:`provisional`).
"""

from __future__ import annotations

from datetime import date
from typing import Protocol, runtime_checkable

import pandas as pd
from pydantic import BaseModel, ConfigDict

from chartlens_core.asof import ensure_as_of
from chartlens_core.bars import (
    BAR_DATE,
    CLOSES_ON_SPECIAL_SESSION,
    IS_COMPLETE,
    SECURITY,
    SEGMENT,
    BarFrameError,
    column,
    validate_bar_frame,
)
from chartlens_core.domain import SecurityId, Timeframe


class AnalysisContext(BaseModel):
    """Identifies one point-in-time analysis run."""

    model_config = ConfigDict(frozen=True)

    security_id: SecurityId
    timeframe: Timeframe
    as_of: date
    methodology_hash: str
    continuity_segment_id: str | None = None
    """The segment the bars belong to. Required for weekly analysis (ADR-0015)."""


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
    ensure_comparable(bars, context)
    result = analyzer.analyze(bars, context)
    if result.context != context:
        raise RuntimeError(f"{analyzer.name} returned a result for a different context")
    if (result.analyzer, result.analyzer_version) != (analyzer.name, analyzer.version):
        raise RuntimeError(f"{analyzer.name} returned a result with mismatched provenance")
    return result


def ensure_comparable(bars: pd.DataFrame, context: AnalysisContext) -> None:
    """Every bar belongs to ``context``'s security and continuity segment (ADR-0015).

    Weekly frames must carry both columns: weekly is where technical structure is built,
    so a weekly frame without its identity is refused rather than trusted."""
    if context.timeframe is Timeframe.WEEKLY:
        missing = [c for c in (SECURITY, SEGMENT) if c not in bars.columns]
        if missing:
            raise BarFrameError(f"weekly bars must carry {missing} (ADR-0015)")
        if context.continuity_segment_id is None:
            raise BarFrameError("weekly analysis needs context.continuity_segment_id")
    if bars.empty:
        return
    if SECURITY in bars.columns and str(column(bars, SECURITY).iloc[0]) != context.security_id:
        raise BarFrameError(
            f"bars of {column(bars, SECURITY).iloc[0]} given to an analysis of "
            f"{context.security_id}"
        )
    if (
        SEGMENT in bars.columns
        and context.continuity_segment_id is not None
        and str(column(bars, SEGMENT).iloc[0]) != context.continuity_segment_id
    ):
        raise BarFrameError(
            f"bars of segment {column(bars, SEGMENT).iloc[0]} given to an analysis of "
            f"segment {context.continuity_segment_id}"
        )


def confirmable(bars: pd.DataFrame) -> pd.DataFrame:
    """The bars a confirmation may be decided on: complete ones only (ADR-0004/0006)."""
    if IS_COMPLETE not in bars.columns:
        return bars
    return bars.loc[column(bars, IS_COMPLETE)]


def provisional(bars: pd.DataFrame) -> pd.Series:
    """True where a confirmation decided on the bar is provisional: its close came from a
    non-regular session, so the next regular week must hold it (ADR-0015)."""
    if CLOSES_ON_SPECIAL_SESSION not in bars.columns:
        return pd.Series(False, index=bars.index)
    return column(bars, CLOSES_ON_SPECIAL_SESSION).astype(bool)


def last_bar_date(bars: pd.DataFrame) -> date | None:
    """Convenience for analyzers: the date of the most recent bar, if any."""
    if bars.empty:
        return None
    ts = pd.Timestamp(column(bars, BAR_DATE).iloc[-1])
    return ts.date() if isinstance(ts, pd.Timestamp) else None
