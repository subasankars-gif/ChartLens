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

__all__ = [
    "FAMILIES",
    "CandidateCount",
    "Geometry",
    "KeyPoint",
    "Pattern",
    "PatternAnalyzer",
    "PatternLine",
    "PatternResult",
    "PatternTouch",
    "Rejection",
    "definition_fit",
]
