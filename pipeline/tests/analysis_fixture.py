"""A structurally valid analysis set for publication tests, written with the pipeline's
own analysis store and no engine: one minimal document and two empty event datasets per
security of the universe, plus the analysis manifest, and one explanation per document
(a single claim built with the shared claim model) plus the explanation manifest. It
stands in for the ANALYSIS stage (job layer) so publication can be tested on its own, and
corrupted on purpose.
"""

from __future__ import annotations

import json
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from chartlens_core.canonical import canonical_json, dataset_content_hash
from chartlens_core.claims import (
    Claim,
    Explanation,
    QuotedValue,
    Reference,
    explanation_bytes,
    explanation_key,
    render,
)
from chartlens_core.config import ChartLensSettings
from chartlens_pipeline.analysis_store import (
    EVENT_DATASETS,
    AnalysisEntry,
    AnalysisManifest,
    EventArtifact,
    ExplanationEntry,
    ExplanationManifest,
    analysis_set_hash,
    analysis_universe,
    explanation_set_hash,
    universe_sha256,
    write_document,
    write_events,
    write_explanation,
    write_explanation_manifest,
    write_manifest,
)
from chartlens_pipeline.storage import DataLakeLayout, ObjectStore

AV = "analysis-000000000test"
EV = "explain-0000000000test"
AS_OF = "2024-01-26"


def current_segments(store: ObjectStore, ex: str = "NSE") -> dict[str, str]:
    rows = pq.read_table(
        pa.BufferReader(store.get(DataLakeLayout.continuity_segments_key(ex)))
    ).to_pylist()
    latest: dict[str, dict[str, Any]] = {}
    for r in rows:
        if (
            r["security_id"] not in latest
            or r["segment_start"] > latest[r["security_id"]]["segment_start"]
        ):
            latest[r["security_id"]] = r
    return {sid: str(r["continuity_segment_id"]) for sid, r in latest.items()}


def document(
    sid: str, segment: str, digests: dict[str, str], variant: str = "", av: str = AV
) -> bytes:
    return canonical_json(
        {
            "identity": {"security_id": sid, "continuity_segment_id": segment, "as_of": AS_OF},
            "inputs": {"bars_sha256": "b" * 64},
            "versions": {"analysis_version": av},
            "breakout_events": {
                "datasets": {
                    n: {"schema_version": "1", "row_count": 0, "content_sha256": digests[n]}
                    for n in EVENT_DATASETS
                }
            },
            "variant": variant,
        }
    )


def stage_analysis(
    store: ObjectStore,
    settings: ChartLensSettings,
    *,
    ex: str = "NSE",
    variant: str = "",
    av: str = AV,
) -> AnalysisManifest:
    """What the ANALYSIS stage leaves for publication, for the current weekly version."""
    weekly = json.loads(store.get(DataLakeLayout.weekly_manifest_key(ex)))
    status = pq.read_table(
        pa.BufferReader(store.get(DataLakeLayout.data_quality_status_key(ex)))
    ).to_pylist()
    universe = analysis_universe(status, weekly["dq_version"])
    segments = current_segments(store, ex)
    entries: list[AnalysisEntry] = []
    for sid in universe:
        seg = segments[sid]
        events: dict[str, EventArtifact] = {}
        for name in EVENT_DATASETS:
            meta = {
                "chartlens.dataset": name,
                "chartlens.security_id": sid,
                "chartlens.continuity_segment_id": seg,
                "chartlens.variant": variant,
            }
            digest, physical = write_events(store, ex, name, [], meta)
            assert digest == dataset_content_hash(meta, [])
            events[name] = EventArtifact(
                content_sha256=digest, physical_sha256=physical, row_count=0
            )
        address = write_document(
            store,
            ex,
            document(sid, seg, {n: events[n].content_sha256 for n in events}, variant, av),
        )
        entries.append(
            AnalysisEntry(
                security_id=sid,
                continuity_segment_id=seg,
                reuse_key="r" * 64,
                weekly_file_sha256=weekly["files"][sid],
                bars_sha256="b" * 64,
                document_sha256=address,
                events=events,
            )
        )
    manifest = AnalysisManifest(
        exchange=ex,
        weekly_version=weekly["weekly_version"],
        dq_version=weekly["dq_version"],
        as_of=str(weekly["as_of"]),
        methodology_hash=weekly["methodology_hash"],
        analysis_version=av,
        analysis_methodology_hash=settings.analysis.methodology_hash(),
        document_schema_version="1",
        canonical_serialization_version="1",
        event_schema_version="1",
        runtime={},
        reuse_key_version="1",
        universe_rule_version="1",
        reuse_validation_sample_size=32,
        reuse_validation_selection_version="1",
        universe=universe,
        universe_sha256=universe_sha256(universe),
        entries=entries,
        analysis_set_hash=analysis_set_hash(entries),
    )
    write_manifest(store, manifest)
    stage_explanations(store, manifest, ex=ex)
    return manifest


def explanation_of(sid: str, segment: str, document_sha256: str, ev: str = EV) -> bytes:
    """One true claim about a stand-in document, built with the shared claim model."""
    ref = Reference(id="/identity", pointer="/identity")
    quoted = [QuotedValue(name="as_of", ref="/identity", field="/as_of", kind="date", value=AS_OF)]
    claim = Claim(
        claim_id="DATA_CONTEXT",
        claim_type="DATA_CONTEXT",
        template_id="DATA_CONTEXT_AS_OF",
        subject="/identity",
        references=[ref],
        quoted_values=quoted,
        rendered_text=render("DATA_CONTEXT_AS_OF", quoted),
        known_at=None,
        provisional=False,
    )
    return explanation_bytes(
        Explanation(
            explain_version=ev,
            explanation_key=explanation_key(sid, document_sha256, ev),
            security_id=sid,
            continuity_segment_id=segment,
            document_sha256=document_sha256,
            claims=[claim],
        )
    )


def stage_explanations(
    store: ObjectStore, manifest: AnalysisManifest, *, ex: str = "NSE", ev: str = EV
) -> ExplanationManifest:
    entries: list[ExplanationEntry] = []
    for e in manifest.entries:
        body = explanation_of(e.security_id, e.continuity_segment_id, e.document_sha256, ev)
        entries.append(
            ExplanationEntry(
                security_id=e.security_id,
                document_sha256=e.document_sha256,
                explanation_key=explanation_key(e.security_id, e.document_sha256, ev),
                explanation_sha256=write_explanation(store, ex, body),
            )
        )
    explained = ExplanationManifest(
        exchange=ex,
        explain_version=ev,
        analysis_set_hash=manifest.analysis_set_hash,
        entries=entries,
        explanation_set_hash=explanation_set_hash(entries),
    )
    write_explanation_manifest(store, explained)
    return explained


def rewrite_manifest(store: ObjectStore, manifest: AnalysisManifest, ex: str = "NSE") -> None:
    """Write a manifest as given (even an invalid one), in canonical form, bypassing the
    store's own validation, to test that publication checks it independently."""
    store.put(
        DataLakeLayout.analysis_manifest_key(ex), canonical_json(manifest.model_dump(mode="json"))
    )
    stage_explanations(store, manifest, ex=ex)  # keep the explanations bound to it
