"""Orchestration: one security's complete analysis, and its stored form (ADR-0024).

The orchestrator composes authoritative outputs; it does not reinterpret them.
"""

from chartlens_engine.analysis.canonical import (
    CANONICAL_SERIALIZATION_VERSION,
    CanonicalError,
    canonical_json,
    content_hash,
)
from chartlens_engine.analysis.model import (
    AnalysisIdentity,
    AnalysisInputs,
    AnalysisVersions,
    CurrentView,
    EvidenceSection,
    SectionProvenance,
    TechnicalAnalysis,
)
from chartlens_engine.analysis.orchestrator import GRAPH, AnalysisInputError, analyze_security
from chartlens_engine.analysis.serialize import (
    DATASETS,
    EventDataset,
    SerializedAnalysis,
    event_content_hash,
    serialize,
)
from chartlens_engine.analysis.versions import (
    ANALYZERS,
    COMPONENTS,
    DOCUMENT_SCHEMA_VERSION,
    EVENT_SCHEMA_VERSION,
    analysis_version,
)

__all__ = [
    "ANALYZERS",
    "CANONICAL_SERIALIZATION_VERSION",
    "COMPONENTS",
    "DATASETS",
    "DOCUMENT_SCHEMA_VERSION",
    "EVENT_SCHEMA_VERSION",
    "GRAPH",
    "AnalysisIdentity",
    "AnalysisInputError",
    "AnalysisInputs",
    "AnalysisVersions",
    "CanonicalError",
    "CurrentView",
    "EventDataset",
    "EvidenceSection",
    "SectionProvenance",
    "SerializedAnalysis",
    "TechnicalAnalysis",
    "analysis_version",
    "analyze_security",
    "canonical_json",
    "content_hash",
    "event_content_hash",
    "serialize",
]
