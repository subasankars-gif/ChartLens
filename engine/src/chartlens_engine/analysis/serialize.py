"""The stored form of a ``TechnicalAnalysis`` (ADR-0024 §4 and §6).

A security's analysis is stored as one **document** and two **event datasets**
(``pattern_breakouts``, ``level_breakouts``):

- Every section is the layer's result as Pydantic writes it in JSON mode, unchanged.
- The ``breakout_events`` section of the document keeps the layer's provenance and, per
  dataset, its schema version, row count and **content hash**: the SHA-256 of the
  canonical bytes of its identifying metadata and its rows, in the layer's order
  (``chartlens_core.canonical.dataset_content_hash``). So the document's address pins
  its events exactly, and the address does not depend on how a Parquet writer lays out
  bytes (the job layer writes the files; their byte hashes live in the manifest).
- Event rows are the events as the layer produced them, in the layer's order.

Nothing here selects, sorts, rounds or derives an analytical value.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal

from chartlens_core.canonical import DATASET_CONTENT_KEY, canonical_json, dataset_content_hash
from chartlens_engine.analysis.model import TechnicalAnalysis
from chartlens_engine.analysis.versions import EVENT_SCHEMA_VERSION
from chartlens_engine.breakouts.analyzer import BREAKOUTS_VERSION

Dataset = Literal["pattern_breakouts", "level_breakouts"]
DATASETS: tuple[Dataset, ...] = ("pattern_breakouts", "level_breakouts")

JsonObject = dict[str, object]


@dataclass(frozen=True)
class EventDataset:
    dataset: Dataset
    rows: list[JsonObject]
    """In the layer's order: by break date, then event key."""
    content_sha256: str
    metadata: dict[str, str]
    """For the file the job layer writes (amendment D)."""

    @property
    def row_count(self) -> int:
        return len(self.rows)


@dataclass(frozen=True)
class SerializedAnalysis:
    document: bytes
    document_sha256: str
    events: dict[Dataset, EventDataset]


def serialize(analysis: TechnicalAnalysis) -> SerializedAnalysis:
    bo = analysis.breakout_events
    identity = analysis.identity
    sources: dict[Dataset, str] = {
        "pattern_breakouts": f"{analysis.patterns.analyzer}-{analysis.patterns.analyzer_version}",
        "level_breakouts": f"{analysis.levels.analyzer}-{analysis.levels.analyzer_version}",
    }
    rows: dict[Dataset, list[JsonObject]] = {
        "pattern_breakouts": [e.model_dump(mode="json") for e in bo.pattern_events],
        "level_breakouts": [e.model_dump(mode="json") for e in bo.level_events],
    }
    events: dict[Dataset, EventDataset] = {}
    for name in DATASETS:
        identity_meta = {
            "chartlens.dataset": name,
            "chartlens.schema_version": EVENT_SCHEMA_VERSION,
            "chartlens.event_methodology_version": f"breakouts-{BREAKOUTS_VERSION}",
            "chartlens.source_methodology_version": sources[name],
            "chartlens.analysis_version": analysis.versions.analysis_version,
            "chartlens.security_id": identity.security_id,
            "chartlens.continuity_segment_id": identity.continuity_segment_id,
            "chartlens.bars_sha256": analysis.inputs.bars_sha256,
        }
        digest = dataset_content_hash(identity_meta, rows[name])
        events[name] = EventDataset(
            dataset=name,
            rows=rows[name],
            content_sha256=digest,
            metadata={**identity_meta, DATASET_CONTENT_KEY: digest},
        )
    document = analysis.model_dump(mode="json", exclude={"breakout_events"})
    document["breakout_events"] = {
        "analyzer": bo.analyzer,
        "analyzer_version": bo.analyzer_version,
        "context": bo.context.model_dump(mode="json"),
        "state_date": None if bo.state_date is None else bo.state_date.isoformat(),
        "datasets": {
            name: {
                "schema_version": EVENT_SCHEMA_VERSION,
                "row_count": events[name].row_count,
                "content_sha256": events[name].content_sha256,
            }
            for name in DATASETS
        },
    }
    data = canonical_json(document)
    return SerializedAnalysis(
        document=data, document_sha256=hashlib.sha256(data).hexdigest(), events=events
    )
