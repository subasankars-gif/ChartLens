"""The versions that identify an analysis (ADR-0019, ADR-0024 §4).

``analysis_version`` is computable before any analysis runs, so the job layer can use it
in the reuse key. It is a hash of:

- the engine version;
- every analyzer's name and version, in execution order;
- every component methodology version recorded on the objects (context, definition fit,
  relevance, change-bar and bar-volume evidence, breakout events), so a component change
  can never hide behind an unchanged analyzer version;
- ``analysis_methodology_hash`` (the ``[analysis]`` settings).

The document format has its own versions (``DOCUMENT_SCHEMA_VERSION``,
``CANONICAL_SERIALIZATION_VERSION``, ``EVENT_SCHEMA_VERSION``). They change the bytes
without changing the analysis, so they belong to the reuse key, not to
``analysis_version``.
"""

from __future__ import annotations

import hashlib

from chartlens_core.canonical import canonical_json
from chartlens_core.config import AnalysisConfig
from chartlens_engine import __version__
from chartlens_engine.bar_evidence import BAR_VOLUME_VERSION
from chartlens_engine.breakouts import BreakoutEventAnalyzer
from chartlens_engine.breakouts.analyzer import BREAKOUTS_VERSION
from chartlens_engine.evidence import (
    CandleAnalyzer,
    DivergenceAnalyzer,
    VolatilityAnalyzer,
    VolumeAnalyzer,
)
from chartlens_engine.fibonacci import FibonacciAnalyzer
from chartlens_engine.indicators import IndicatorAnalyzer
from chartlens_engine.levels import LevelsAnalyzer
from chartlens_engine.levels.analyzer import CHANGE_BAR_VERSION
from chartlens_engine.patterns import PatternAnalyzer, RelevanceAnalyzer
from chartlens_engine.patterns.context import CONTEXT_VERSION
from chartlens_engine.patterns.fit import FIT_VERSION
from chartlens_engine.patterns.relevance import RELEVANCE_VERSION
from chartlens_engine.structure import StructureAnalyzer
from chartlens_engine.swings import SwingAnalyzer

DOCUMENT_SCHEMA_VERSION = "1"
EVENT_SCHEMA_VERSION = "1"

ANALYZER_CLASSES = (
    IndicatorAnalyzer,
    SwingAnalyzer,
    StructureAnalyzer,
    FibonacciAnalyzer,
    LevelsAnalyzer,
    DivergenceAnalyzer,
    VolumeAnalyzer,
    VolatilityAnalyzer,
    CandleAnalyzer,
    PatternAnalyzer,
    RelevanceAnalyzer,
    BreakoutEventAnalyzer,
)
"""Every analyzer, in the orchestrator's execution order."""

ANALYZERS: tuple[tuple[str, str], ...] = tuple((a.name, a.version) for a in ANALYZER_CLASSES)

COMPONENTS: tuple[tuple[str, str], ...] = (
    ("bar_volume", BAR_VOLUME_VERSION),
    ("breakouts", BREAKOUTS_VERSION),
    ("levels.change_bar", CHANGE_BAR_VERSION),
    ("patterns.context", CONTEXT_VERSION),
    ("patterns.fit", FIT_VERSION),
    ("patterns.relevance", RELEVANCE_VERSION),
)


def analysis_version(config: AnalysisConfig) -> str:
    """``analysis-`` + 12 hex characters, from the code and the settings only."""
    payload = {
        "engine": __version__,
        "analyzers": [[name, version] for name, version in ANALYZERS],
        "components": [[name, version] for name, version in COMPONENTS],
        "analysis_methodology_hash": config.methodology_hash(),
    }
    return "analysis-" + hashlib.sha256(canonical_json(payload)).hexdigest()[:12]
