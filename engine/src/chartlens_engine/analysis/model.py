"""The complete analysis of one security (ADR-0024 §2, amendments A and B).

``TechnicalAnalysis`` is an assembly, not a mega-object: every layer section is the
layer's own result, unchanged. The orchestrator adds only

- what went in (``identity``, ``inputs``),
- what interprets it (``versions``),
- how the sections were composed (``provenance``), and
- ``current``: references, by id or date, to objects the layers themselves report as
  current. It is a serving-oriented projection of existing analytical objects, not an
  analytical layer: it holds no value, rank, score or signal of its own.

It never records when or where it was computed (no timestamps, run ids, snapshot
versions or hosts), so identical inputs give identical bytes (§4, decision 3).
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict

from chartlens_engine.breakouts import BreakoutResult
from chartlens_engine.evidence import (
    CandleResult,
    DivergenceResult,
    VolatilityResult,
    VolumeResult,
)
from chartlens_engine.fibonacci import FibonacciResult
from chartlens_engine.indicators import IndicatorResult
from chartlens_engine.levels import LevelsResult
from chartlens_engine.patterns import PatternResult, RelevanceResult
from chartlens_engine.structure import StructureResult
from chartlens_engine.swings import SwingResult


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class AnalysisInputs(_Model):
    """Supplied by the job layer and recorded verbatim. The engine reads no file, so it
    cannot know these; it never discovers or recomputes them. Physical provenance (the
    weekly file's hash) is deliberately absent: it changes every run even when the bars
    do not, and lives in the analysis manifest (ADR-0025 §1)."""

    exchange: str
    weekly_schema_version: str
    weekly_builder_version: str
    usable_from: date | None
    """The security's ``usable_from`` (ADR-0012) in the published data quality."""


class InputRecord(AnalysisInputs):
    """The document's record of its inputs: the supplied ones, plus ``bars_sha256``, the
    logical identity of the bars, computed by the orchestrator from the frame it was
    given (never accepted from the caller)."""

    bars_sha256: str


class AnalysisIdentity(_Model):
    security_id: str
    exchange: str
    timeframe: str
    continuity_segment_id: str
    first_bar_date: date | None
    """The first bar analysed (the start of the segment's data)."""
    as_of: date
    """The analysis date: the last bar given, complete or forming."""
    state_date: date | None
    """The last complete bar: the date every layer's state refers to."""
    bar_count: int
    forming_week_present: bool


class NamedVersion(_Model):
    name: str
    version: str


class AnalysisVersions(_Model):
    """Analytical provenance: what is needed to interpret the result."""

    document_schema_version: str
    canonical_serialization_version: str
    event_schema_version: str
    analysis_version: str
    analysis_methodology_hash: str
    data_methodology_hash: str
    engine_version: str
    analyzers: list[NamedVersion]
    """In execution order."""
    components: list[NamedVersion]


class EvidenceSection(_Model):
    divergence: DivergenceResult
    volume: VolumeResult
    volatility: VolatilityResult
    candles: CandleResult


class CurrentView(_Model):
    """References only, as of ``state_date``, each taken from the owning layer's own
    notion of current. Every reference resolves to an object in the same document."""

    state_date: date | None
    trend_since: date | None
    """``structure.trend``: the ``trend_history`` entry with this ``since``."""
    zone_ids: list[str]
    """``levels.zones`` (the levels layer keeps current zones only)."""
    active_trendline_ids: list[str]
    """``levels.active_trendlines``."""
    fibonacci_ids: list[str]
    """``fibonacci.current()``: the latest structure per sensitivity."""
    included_pattern_ids: list[str]
    """``relevance.included_as_of(state_date)``, in the relevance list's order."""


class SectionProvenance(_Model):
    section: str
    analyzer: str
    analyzer_version: str
    consumes: list[str]
    """The sections passed to this analyzer, besides the bars and its settings."""


class TechnicalAnalysis(_Model):
    identity: AnalysisIdentity
    inputs: InputRecord
    versions: AnalysisVersions
    indicators: IndicatorResult
    swings: SwingResult
    structure: StructureResult
    fibonacci: FibonacciResult
    levels: LevelsResult
    evidence: EvidenceSection
    patterns: PatternResult
    relevance: RelevanceResult
    breakout_events: BreakoutResult
    current: CurrentView
    provenance: list[SectionProvenance]
    """One per section, in execution order."""
