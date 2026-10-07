"""Schema-4 publication (ADR-0028 §6): the explanation set is pinned verbatim, bound to
the analysis set and to each document, its version is the job's (never derived), and
every claim of a new explanation object is validated with the shared checker before the
pointer moves. A wording change publishes new explanation objects only."""

from __future__ import annotations

import gzip
import json
from typing import Any

import pytest
from analysis_fixture import AS_OF, EV, explanation_of, stage_analysis, stage_explanations
from test_publication_schema3 import EX, lake, pointer, publish, refused, reissue_weekly

from chartlens_core.canonical import canonical_json
from chartlens_pipeline.analysis_store import (
    ExplanationManifest,
    explanation_set_hash,
    read_explanation_manifest,
    write_explanation,
)
from chartlens_pipeline.serving import (
    PublicationFailed,
    ServingInputsNotReady,
    ServingPublisher,
    ServingSnapshot,
)
from chartlens_pipeline.storage import DataLakeLayout

__all__ = ["lake"]  # the fixture, re-exported for pytest


def put_explanations(store: Any, manifest: ExplanationManifest) -> None:
    """Write an explanation manifest as given, bypassing the store's validation."""
    store.put(
        DataLakeLayout.explanations_manifest_key(EX),
        canonical_json(manifest.model_dump(mode="json")),
    )


def with_entries(m: ExplanationManifest, entries: list[Any]) -> ExplanationManifest:
    return m.model_copy(
        update={"entries": entries, "explanation_set_hash": explanation_set_hash(entries)}
    )


def test_a_schema_4_snapshot_pins_the_explanation_manifest_verbatim(lake: Any) -> None:
    settings, _, store = lake
    analysis = stage_analysis(store, settings)
    explained = read_explanation_manifest(store, EX)
    assert explained is not None
    out = publish(lake)
    assert out["schema_version"] == 4
    block = out["explanations"]
    assert block["explain_version"] == EV
    assert block["analysis_set_hash"] == analysis.analysis_set_hash
    assert store.get(block["manifest_key"]) == store.get(
        DataLakeLayout.explanations_manifest_key(EX)
    )
    snap = ServingSnapshot.load(store, EX)
    assert snap.explanations is not None
    assert set(snap.explanation_entries) == set(analysis.universe)
    for e in analysis.entries:
        assert snap.explanation_entries[e.security_id].document_sha256 == e.document_sha256


def test_no_explanation_manifest_is_not_ready(lake: Any) -> None:
    settings, _, store = lake
    stage_analysis(store, settings)
    store.delete(DataLakeLayout.explanations_manifest_key(EX))
    refused(lake, ServingInputsNotReady, "no explanation manifest")


def test_check_10_bound_to_another_analysis_set(lake: Any) -> None:
    settings, _, store = lake
    stage_analysis(store, settings)
    m = read_explanation_manifest(store, EX)
    assert m is not None
    put_explanations(store, m.model_copy(update={"analysis_set_hash": "0" * 64}))
    refused(lake, ServingInputsNotReady, "describe another analysis set")


def test_check_10_the_expected_explain_version_is_never_substituted(lake: Any) -> None:
    settings, _, store = lake
    m = stage_analysis(store, settings)
    stage_explanations(store, m, ev="explain-somethingelse")
    refused(lake, PublicationFailed, "the job expects")


def test_check_10_the_set_hash_and_canonical_form(lake: Any) -> None:
    settings, _, store = lake
    stage_analysis(store, settings)
    m = read_explanation_manifest(store, EX)
    assert m is not None
    put_explanations(store, m.model_copy(update={"explanation_set_hash": "0" * 64}))
    refused(lake, PublicationFailed, "does not match its set hash")
    key = DataLakeLayout.explanations_manifest_key(EX)
    store.put(key, json.dumps(m.model_dump(mode="json"), indent=1).encode())
    refused(lake, PublicationFailed, "not in canonical form")


@pytest.mark.parametrize("fault", ["missing", "swapped"])
def test_check_11_one_explanation_per_document(lake: Any, fault: str) -> None:
    settings, _, store = lake
    stage_analysis(store, settings)
    m = read_explanation_manifest(store, EX)
    assert m is not None and len(m.entries) >= 2
    entries = list(m.entries)
    if fault == "missing":
        entries = entries[1:]
    else:  # each explanation bound to the other security's document
        a, b = entries[0], entries[1]
        entries[0] = a.model_copy(update={"document_sha256": b.document_sha256})
        entries[1] = b.model_copy(update={"document_sha256": a.document_sha256})
    put_explanations(store, with_entries(m, entries))
    refused(lake, PublicationFailed, "bind exactly one explanation to each analysed document")


def _replace_object(store: Any, index: int, body: bytes) -> None:
    m = read_explanation_manifest(store, EX)
    assert m is not None
    entries = list(m.entries)
    entries[index] = entries[index].model_copy(
        update={"explanation_sha256": write_explanation(store, EX, body)}
    )
    put_explanations(store, with_entries(m, entries))


def test_check_12_a_tampered_claim_is_refused(lake: Any) -> None:
    settings, _, store = lake
    analysis = stage_analysis(store, settings)
    e = analysis.entries[0]
    body = json.loads(explanation_of(e.security_id, e.continuity_segment_id, e.document_sha256))
    body["claims"][0]["rendered_text"] = f"Weekly analysis as of {AS_OF}. Buy above it."
    _replace_object(store, 0, canonical_json(body))
    refused(lake, PublicationFailed, "not its template")


def test_check_12_a_claim_quoting_a_value_not_in_the_document_is_refused(lake: Any) -> None:
    settings, _, store = lake
    analysis = stage_analysis(store, settings)
    e = analysis.entries[0]
    body = json.loads(explanation_of(e.security_id, e.continuity_segment_id, e.document_sha256))
    claim = body["claims"][0]
    claim["quoted_values"][0]["value"] = "2025-01-31"
    claim["rendered_text"] = "Weekly analysis as of 2025-01-31."
    _replace_object(store, 0, canonical_json(body))
    refused(lake, PublicationFailed, "not the stored value")


def test_check_12_an_object_that_disagrees_with_its_entry(lake: Any) -> None:
    settings, _, store = lake
    analysis = stage_analysis(store, settings)
    other = analysis.entries[1]
    # A valid explanation of the second security, listed for the first.
    _replace_object(
        store,
        0,
        explanation_of(other.security_id, other.continuity_segment_id, other.document_sha256),
    )
    refused(lake, PublicationFailed, "the explanation's security_id")


def test_check_12_a_corrupt_object_is_refused(lake: Any) -> None:
    settings, _, store = lake
    stage_analysis(store, settings)
    m = read_explanation_manifest(store, EX)
    assert m is not None
    key = DataLakeLayout.serving_explanation_key(EX, m.entries[0].explanation_sha256)
    store.delete(key)
    store.put_immutable(key, gzip.compress(b"{}", mtime=0))
    refused(lake, PublicationFailed, "corrupt")


def test_a_wording_change_publishes_new_explanations_only(lake: Any) -> None:
    """The analysis objects are covered by the live snapshot (existence only); only the
    new explanation objects are fully verified; documents are untouched."""
    settings, _, store = lake
    analysis = stage_analysis(store, settings)
    first = publish(lake)
    documents = sorted(store.list(DataLakeLayout.serving_analysis_prefix(EX)))
    stage_explanations(store, analysis, ev="explain-reworded0000")
    with pytest.raises(PublicationFailed, match="the job expects"):
        publish(lake)  # the job's expected version is still the old one
    out = ServingPublisher(
        settings,
        lake[1],
        store,
        expected_analysis_version=first["analysis"]["analysis_version"],
        expected_explain_version="explain-reworded0000",
    ).run()
    assert out["meta_version"] != first["meta_version"]
    assert out["analysis"]["analysis_set_hash"] == first["analysis"]["analysis_set_hash"]
    n = len(analysis.universe)
    assert out["verification"]["objects_fully_verified"] == n
    assert out["verification"]["objects_existence_only"] == 3 * n
    assert sorted(store.list(DataLakeLayout.serving_analysis_prefix(EX))) == documents


def test_clean_up_keeps_live_and_previous_explanations(lake: Any) -> None:
    settings, _, store = lake
    m = stage_analysis(store, settings)
    publish(lake)
    old = set(store.list(DataLakeLayout.serving_explanations_prefix(EX)))
    reissue_weekly(store, m.entries[0].security_id)
    stage_analysis(store, settings, variant="next")
    publish(lake)
    now = set(store.list(DataLakeLayout.serving_explanations_prefix(EX)))
    assert old <= now  # the previous snapshot's explanations are kept
    assert pointer(store) is not None
