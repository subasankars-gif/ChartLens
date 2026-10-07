"""The one authoritative orchestrator (ADR-0024 §1).

> The orchestrator may assemble references to layer outputs but may not transform,
> score, reinterpret, filter, or recalculate those outputs. (Amendment A.)

``analyze_security`` runs every layer once, in the fixed order, through
:func:`run_analyzer`, passing each the typed results it consumes, and assembles them.
It is the only production code that instantiates an analyzer. It contains no
arithmetic, no ordering and no selection: static tests hold this module to that
(``tests/test_analysis_orchestrator.py``).

It is pure: no I/O, no clock, no environment. The universe, the bars, the context and
the input provenance are all given by the caller (the job layer); nothing is discovered.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from chartlens_core.config import AnalysisConfig
from chartlens_core.domain import Timeframe
from chartlens_engine import __version__
from chartlens_engine.analysis.canonical import CANONICAL_SERIALIZATION_VERSION
from chartlens_engine.analysis.model import (
    AnalysisIdentity,
    AnalysisInputs,
    AnalysisVersions,
    CurrentView,
    EvidenceSection,
    NamedVersion,
    SectionProvenance,
    TechnicalAnalysis,
)
from chartlens_engine.analysis.versions import (
    ANALYZERS,
    COMPONENTS,
    DOCUMENT_SCHEMA_VERSION,
    EVENT_SCHEMA_VERSION,
    analysis_version,
)
from chartlens_engine.breakouts import BreakoutEventAnalyzer
from chartlens_engine.evidence import (
    CandleAnalyzer,
    DivergenceAnalyzer,
    VolatilityAnalyzer,
    VolumeAnalyzer,
)
from chartlens_engine.fibonacci import FibonacciAnalyzer
from chartlens_engine.indicators import IndicatorAnalyzer
from chartlens_engine.interfaces import AnalysisContext, AnalyzerResult, run_analyzer
from chartlens_engine.levels import LevelsAnalyzer
from chartlens_engine.patterns import PatternAnalyzer, RelevanceAnalyzer
from chartlens_engine.structure import StructureAnalyzer
from chartlens_engine.swings import SwingAnalyzer

if TYPE_CHECKING:
    import pandas as pd

GRAPH: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("indicators", ()),
    ("swings", ("indicators",)),
    ("structure", ("indicators", "swings")),
    ("fibonacci", ("indicators", "swings")),
    ("levels", ("indicators", "swings", "structure", "fibonacci")),
    ("evidence.divergence", ("indicators", "swings", "structure")),
    ("evidence.volume", ("indicators", "swings", "structure", "levels")),
    ("evidence.volatility", ("indicators",)),
    ("evidence.candles", ("indicators", "structure")),
    (
        "patterns",
        (
            "indicators",
            "swings",
            "structure",
            "levels",
            "fibonacci",
            "evidence.divergence",
            "evidence.volatility",
        ),
    ),
    ("relevance", ("indicators", "patterns", "structure")),
    ("breakout_events", ("indicators", "levels", "patterns")),
)
"""Each section and the sections its analyzer is given, in execution order. A test
checks it against every analyzer's constructor."""


class AnalysisInputError(ValueError):
    """The caller's bars, context and inputs do not describe one weekly segment."""


def analyze_security(
    bars: pd.DataFrame,
    context: AnalysisContext,
    config: AnalysisConfig,
    inputs: AnalysisInputs,
) -> TechnicalAnalysis:
    """Every layer for one security's current segment, as of ``context.as_of``."""
    if context.timeframe is not Timeframe.WEEKLY:
        raise AnalysisInputError("the orchestrator analyses weekly bars only")
    segment = context.continuity_segment_id
    if segment is None:
        raise AnalysisInputError("weekly analysis needs a continuity segment")

    ind = run_analyzer(IndicatorAnalyzer(config.indicators), bars, context)
    sw = run_analyzer(SwingAnalyzer(config.swings, ind), bars, context)
    st = run_analyzer(StructureAnalyzer(config.structure, ind, sw), bars, context)
    fib = run_analyzer(FibonacciAnalyzer(config.fibonacci, ind, sw), bars, context)
    lv = run_analyzer(LevelsAnalyzer(config.levels, ind, sw, st, fib), bars, context)
    div = run_analyzer(DivergenceAnalyzer(config.divergence, ind, sw, st), bars, context)
    vol = run_analyzer(VolumeAnalyzer(config.volume, ind, sw, st, lv), bars, context)
    vty = run_analyzer(VolatilityAnalyzer(config.volatility, ind), bars, context)
    cdl = run_analyzer(CandleAnalyzer(config.candles, ind, st), bars, context)
    pat = run_analyzer(
        PatternAnalyzer(
            config.patterns,
            ind,
            sw,
            structure=st,
            levels=lv,
            fibonacci=fib,
            divergence=div,
            volatility=vty,
        ),
        bars,
        context,
    )
    rel = run_analyzer(RelevanceAnalyzer(config.patterns, ind, pat, st), bars, context)
    bo = run_analyzer(BreakoutEventAnalyzer(config, ind, lv, pat), bars, context)

    sections: tuple[AnalyzerResult, ...] = (ind, sw, st, fib, lv, div, vol, vty, cdl, pat, rel, bo)
    state_date = bo.state_date
    for layer in (fib, lv, vol, vty, rel):
        if layer.state_date != state_date:
            raise RuntimeError(f"{layer.analyzer} reports another state date")

    return TechnicalAnalysis(
        identity=AnalysisIdentity(
            security_id=str(context.security_id),
            exchange=inputs.exchange,
            timeframe=context.timeframe.value,
            continuity_segment_id=segment,
            first_bar_date=next(iter(ind.bar_dates), None),
            as_of=context.as_of,
            state_date=state_date,
            bar_count=len(ind.bar_dates),
            forming_week_present=any(ind.provisional),
        ),
        inputs=inputs,
        versions=AnalysisVersions(
            document_schema_version=DOCUMENT_SCHEMA_VERSION,
            canonical_serialization_version=CANONICAL_SERIALIZATION_VERSION,
            event_schema_version=EVENT_SCHEMA_VERSION,
            analysis_version=analysis_version(config),
            analysis_methodology_hash=config.methodology_hash(),
            data_methodology_hash=context.methodology_hash,
            engine_version=__version__,
            analyzers=[NamedVersion(name=n, version=v) for n, v in ANALYZERS],
            components=[NamedVersion(name=n, version=v) for n, v in COMPONENTS],
        ),
        indicators=ind,
        swings=sw,
        structure=st,
        fibonacci=fib,
        levels=lv,
        evidence=EvidenceSection(divergence=div, volume=vol, volatility=vty, candles=cdl),
        patterns=pat,
        relevance=rel,
        breakout_events=bo,
        current=CurrentView(
            state_date=state_date,
            trend_since=None if st.trend is None else st.trend.since,
            zone_ids=[z.zone_id for z in lv.zones],
            active_trendline_ids=[t.trendline_id for t in lv.active_trendlines],
            fibonacci_ids=[f.fib_id for f in fib.current()],
            included_pattern_ids=[] if state_date is None else rel.included_as_of(state_date),
        ),
        provenance=[
            SectionProvenance(
                section=name,
                analyzer=result.analyzer,
                analyzer_version=result.analyzer_version,
                consumes=list(consumes),
            )
            for (name, consumes), result in zip(GRAPH, sections, strict=True)
        ],
    )
