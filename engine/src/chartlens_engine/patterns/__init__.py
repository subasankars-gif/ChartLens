"""Layers G–H — classical patterns (ADR-0022)."""

from chartlens_engine.patterns.analyzer import FAMILIES, PatternAnalyzer, definition_fit
from chartlens_engine.patterns.model import (
    CandidateCount,
    Geometry,
    KeyPoint,
    Pattern,
    PatternLine,
    PatternResult,
    PatternTouch,
    Rejection,
)
from chartlens_engine.patterns.relevance import (
    PatternRelevance,
    RelevanceAnalyzer,
    RelevanceEntry,
    RelevanceResult,
    RelevanceTag,
)

__all__ = [
    "FAMILIES",
    "CandidateCount",
    "Geometry",
    "KeyPoint",
    "Pattern",
    "PatternAnalyzer",
    "PatternLine",
    "PatternRelevance",
    "PatternResult",
    "PatternTouch",
    "Rejection",
    "RelevanceAnalyzer",
    "RelevanceEntry",
    "RelevanceResult",
    "RelevanceTag",
    "definition_fit",
]
