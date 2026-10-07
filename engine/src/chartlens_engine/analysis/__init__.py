"""Orchestration: one security's complete analysis, and its stored form (ADR-0024).

The orchestrator composes authoritative outputs; it does not reinterpret them.
"""

from chartlens_core.canonical import (
    CANONICAL_SERIALIZATION_VERSION,
    CanonicalError,
    canonical_json,
    content_hash,
)
from chartlens_engine.analysis.inputs import BARS_ENCODING_VERSION, bars_content_hash
from chartlens_engine.analysis.model import (
    AnalysisIdentity,
    AnalysisInputs,
    AnalysisVersions,
    CurrentView,
    EvidenceSection,
    InputRecord,
    SectionProvenance,
    TechnicalAnalysis,
)
from chartlens_engine.analysis.orchestrator import GRAPH, AnalysisInputError, analyze_security
from chartlens_engine.analysis.serialize import (
    DATASETS,
    EventDataset,
    SerializedAnalysis,
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
    "BARS_ENCODING_VERSION",
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
    "InputRecord",
    "SectionProvenance",
    "SerializedAnalysis",
    "TechnicalAnalysis",
    "analysis_version",
    "analyze_security",
    "bars_content_hash",
    "canonical_json",
    "content_hash",
    "serialize",
]
