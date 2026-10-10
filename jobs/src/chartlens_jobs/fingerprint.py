"""The dependency fingerprint and reuse key (ADR-0025 §3, frozen).

> Anything whose change could make an existing analytical result invalid participates in
> the reuse key. Operational metadata that does not affect the analysis does not.

The fingerprint is built from the authoritative **inputs**, before analysis, and never
from a result: inputs → fingerprint → reuse lookup → analysis → ``TechnicalAnalysis``.
It holds the whole ``AnalysisContext`` and ``AnalysisInputs`` dumps, so a field added to
either is part of the key automatically.

Excluded by construction: run and job ids, timestamps, snapshot ids, the weekly file's
physical hash, storage paths, worker ids, processing order, gzip bytes, Parquet bytes.

``runtime`` is execution-environment provenance: in the key, never in the document. Any
numerical or runtime dependency later found able to affect output is added to
``RUNTIME_PACKAGES`` with a ``REUSE_KEY_VERSION`` bump.
"""

from __future__ import annotations

import platform
from importlib.metadata import version
from typing import Final

from pydantic import BaseModel, ConfigDict

from chartlens_core.canonical import CANONICAL_SERIALIZATION_VERSION, content_hash
from chartlens_engine.analysis import (
    DOCUMENT_SCHEMA_VERSION,
    EVENT_SCHEMA_VERSION,
    AnalysisInputs,
)
from chartlens_engine.interfaces import AnalysisContext

REUSE_KEY_VERSION: Final = "1"
RUNTIME_PACKAGES: Final = ("numpy", "pandas", "pydantic")


def runtime_versions() -> dict[str, str]:
    """The numeric runtime: the Python minor version and the installed packages that can
    move a float's last bit."""
    major, minor, _ = platform.python_version_tuple()
    return {"python": f"{major}.{minor}", **{p: version(p) for p in RUNTIME_PACKAGES}}


class Formats(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    document_schema_version: str = DOCUMENT_SCHEMA_VERSION
    canonical_serialization_version: str = CANONICAL_SERIALIZATION_VERSION
    event_schema_version: str = EVENT_SCHEMA_VERSION


class DependencyFingerprint(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    reuse_key_version: str = REUSE_KEY_VERSION
    context: AnalysisContext
    bars_sha256: str
    inputs: AnalysisInputs
    analysis_version: str
    """``chartlens_engine.analysis.analysis_version`` of the settings in use."""
    formats: Formats = Formats()
    runtime: dict[str, str]

    def reuse_key(self) -> str:
        return content_hash(self.model_dump(mode="json"))
