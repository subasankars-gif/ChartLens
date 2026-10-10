"""Schema-3 publication (ADR-0026): the analysis set is verified independently before
the pointer moves, the commit is a compare-and-swap of the pointer, publication is
idempotent, and no incomplete or unverified analysis set can become live."""

from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from analysis_fixture import AV, EV, rewrite_manifest, stage_analysis
from test_adjust import SESSIONS, build_lake

from chartlens_core.config import AnalysisConfig, BreakoutsConfig
from chartlens_core.runs import SnapshotOutcome
from chartlens_pipeline.adjust import AdjustmentService, CorporateActionOverrides
from chartlens_pipeline.analysis_store import (
    analysis_set_hash,
    manifest_bytes,
    read_manifest,
)
from chartlens_pipeline.daily import to_parquet_bytes
from chartlens_pipeline.data_quality import DataQualityService
from chartlens_pipeline.identity import IdentityOverrides
from chartlens_pipeline.runs import MemoryRunStore
from chartlens_pipeline.serving import (
    PublicationFailed,
    ServingInputsNotReady,
    ServingPublisher,
    ServingSnapshot,
    SnapshotUnavailable,
)
from chartlens_pipeline.storage import DataLakeLayout, LocalObjectStore
from chartlens_pipeline.weekly import WeeklyService

EX = "NSE"
T0 = datetime(2026, 10, 7, 15, 0, tzinfo=UTC)


@pytest.fixture
def lake(tmp_path: Path) -> Any:
    settings, provider, store, today = build_lake(tmp_path, ex=SESSIONS[17])
    AdjustmentService(
        settings, provider, store, overrides=CorporateActionOverrides(), today=today
    ).run()
    DataQualityService(settings, provider, store, identity_overrides=IdentityOverrides()).run()
    WeeklyService(settings, provider, store).run()
    return settings, provider, store


def publish(lake: Any, *, av: str = AV, history: Any = None, store: Any = None) -> dict[str, Any]:
    settings, provider, own = lake
    return ServingPublisher(
        settings,
        provider,
        store or own,
        expected_analysis_version=av,
        expected_explain_version=EV,
        history=history,
        clock=lambda: T0,
    ).run()


def pointer(store: LocalObjectStore) -> bytes | None:
    key = DataLakeLayout.serving_manifest_key(EX)
    return store.get(key) if store.exists(key) else None


def refused(lake: Any, error: type[Exception], match: str, **kw: Any) -> None:
    """The publication fails and the live pointer is exactly what it was."""
    store = lake[2]
    before = pointer(store)
    with pytest.raises(error, match=match):
        publish(lake, **kw)
    assert pointer(store) == before


def reissue_weekly(store: LocalObjectStore, sid: str) -> None:
    key = DataLakeLayout.curated_weekly_key(EX, sid)
    table = pq.read_table(pa.BufferReader(store.get(key)))
    sink = pa.BufferOutputStream()
    pq.write_table(table, sink, compression="gzip")
    data = sink.getvalue().to_pybytes()
    store.put(key, data)
    mkey = DataLakeLayout.weekly_manifest_key(EX)
    m = json.loads(store.get(mkey))
    import hashlib

    m["files"][sid] = hashlib.sha256(data).hexdigest()
    store.put(mkey, json.dumps(m).encode())


# ----------------------------------------------------------------------------- the snapshot


def test_a_schema_3_snapshot_pins_the_analysis_manifest_verbatim(lake: Any) -> None:
    settings, _, store = lake
    staged = stage_analysis(store, settings)
    history = MemoryRunStore()
    out = publish(lake, history=history)
    assert out["outcome"] == SnapshotOutcome.PUBLISHED and out["schema_version"] == 4
    block = out["analysis"]
    copy = store.get(block["manifest_key"])
    assert copy == store.get(DataLakeLayout.analysis_manifest_key(EX)) == manifest_bytes(staged)
    assert block["manifest_sha256"] == __import__("hashlib").sha256(copy).hexdigest()
    assert block["analysis_set_hash"] == staged.analysis_set_hash
    assert block["universe_sha256"] == staged.universe_sha256
    assert block["securities"] == len(staged.universe) == out["counts"]["analysed"]
    snap = ServingSnapshot.load(store, EX)
    assert snap.schema_version == 4 and snap.analysis is not None
    assert set(snap.analysis_entries) == set(staged.universe)
    record = history.get_snapshot(out["meta_version"])
    assert record is not None and record.analysis is not None
    assert record.analysis["analysis_set_hash"] == staged.analysis_set_hash
    assert out["verification"]["objects_fully_verified"] == 4 * len(staged.universe)


def test_publication_is_idempotent(lake: Any) -> None:
    settings, _, store = lake
    stage_analysis(store, settings)
    first = publish(lake)
    before = pointer(store)
    again = publish(lake)
    assert again["outcome"] == SnapshotOutcome.UNCHANGED
    assert again["meta_version"] == first["meta_version"]
    assert pointer(store) == before


def test_a_different_analysis_set_is_a_different_snapshot(lake: Any) -> None:
    settings, _, store = lake
    stage_analysis(store, settings)
    first = publish(lake)["meta_version"]
    stage_analysis(store, settings, variant="other")
    assert publish(lake)["meta_version"] != first


def test_no_analysis_manifest_no_publication(lake: Any) -> None:
    refused(lake, ServingInputsNotReady, "no analysis manifest")


def test_a_tampered_pinned_copy_is_not_served(lake: Any) -> None:
    settings, _, store = lake
    stage_analysis(store, settings)
    out = publish(lake)
    key = out["analysis"]["manifest_key"]
    store.put(key, store.get(key) + b" ")
    with pytest.raises(SnapshotUnavailable, match="analysis manifest"):
        ServingSnapshot.load(store, EX)


# ----------------------------------------------------------------------------- checks 1-6


def test_check_1_inputs_in_step(lake: Any) -> None:
    settings, _, store = lake
    m = stage_analysis(store, settings)
    rewrite_manifest(store, m.model_copy(update={"weekly_version": "wk-older"}))
    refused(lake, ServingInputsNotReady, "weekly_version wk-older")


def test_check_2_the_expected_analysis_version_is_never_substituted(lake: Any) -> None:
    settings, _, store = lake
    stage_analysis(store, settings)
    refused(lake, PublicationFailed, "the job expects analysis-other", av="analysis-other")


def test_check_2_other_analysis_settings(lake: Any) -> None:
    settings, provider, store = lake
    stage_analysis(store, settings)
    other = settings.model_copy(
        update={"analysis": AnalysisConfig(breakouts=BreakoutsConfig(retest_window=11))}
    )
    before = pointer(store)
    with pytest.raises(PublicationFailed, match="other \\[analysis\\] settings"):
        ServingPublisher(
            other, provider, store, expected_analysis_version=AV, expected_explain_version=EV
        ).run()
    assert pointer(store) == before


def test_check_3_the_set_hash(lake: Any) -> None:
    settings, _, store = lake
    m = stage_analysis(store, settings)
    rewrite_manifest(store, m.model_copy(update={"analysis_set_hash": "0" * 64}))
    refused(lake, PublicationFailed, "does not match its set hash")


def test_check_4_the_universe_is_re_derived(lake: Any) -> None:
    settings, _, store = lake
    stage_analysis(store, settings)
    key = DataLakeLayout.data_quality_status_key(EX)
    rows = pq.read_table(pa.BufferReader(store.get(key))).to_pylist()
    rows[0]["status"] = "NOT_USABLE"  # the published universe shrinks after ANALYSIS ran
    store.put(key, to_parquet_bytes(pa.Table.from_pylist(rows)))
    refused(lake, PublicationFailed, "analysed universe differs")


def test_check_5_entries_cover_exactly_the_universe(lake: Any) -> None:
    settings, _, store = lake
    m = stage_analysis(store, settings)
    entries = m.entries[1:]
    rewrite_manifest(
        store,
        m.model_copy(update={"entries": entries, "analysis_set_hash": analysis_set_hash(entries)}),
    )
    refused(lake, PublicationFailed, "do not cover exactly the universe")


def test_check_6_a_stale_weekly_file(lake: Any) -> None:
    settings, _, store = lake
    m = stage_analysis(store, settings)
    reissue_weekly(store, m.entries[0].security_id)  # weekly moved on after ANALYSIS
    refused(lake, PublicationFailed, "stale weekly file")


def test_check_6_another_segment(lake: Any) -> None:
    settings, _, store = lake
    m = stage_analysis(store, settings)
    e = m.entries[0].model_copy(update={"continuity_segment_id": "SEC-X@2000-01-03"})
    entries = [e, *m.entries[1:]]
    rewrite_manifest(
        store,
        m.model_copy(update={"entries": entries, "analysis_set_hash": analysis_set_hash(entries)}),
    )
    refused(lake, PublicationFailed, "analysed another segment")


# ----------------------------------------------------------------------------- checks 7-9


def test_check_7_a_missing_document(lake: Any) -> None:
    settings, _, store = lake
    m = stage_analysis(store, settings)
    store.delete(DataLakeLayout.serving_analysis_key(EX, m.entries[0].document_sha256))
    refused(lake, PublicationFailed, "missing")


def test_check_7_a_corrupt_document(lake: Any) -> None:
    settings, _, store = lake
    m = stage_analysis(store, settings)
    key = DataLakeLayout.serving_analysis_key(EX, m.entries[1].document_sha256)
    store.delete(key)
    store.put_immutable(key, gzip.compress(b"{}", mtime=0))
    refused(lake, PublicationFailed, "corrupt: content does not hash to its address")


def test_check_7_a_document_that_disagrees_with_its_entry(lake: Any) -> None:
    settings, _, store = lake
    m = stage_analysis(store, settings)
    a, b = m.entries[0], m.entries[1]
    swapped = a.model_copy(update={"document_sha256": b.document_sha256})  # b's document
    entries = [swapped, *m.entries[1:]]
    rewrite_manifest(
        store,
        m.model_copy(update={"entries": entries, "analysis_set_hash": analysis_set_hash(entries)}),
    )
    refused(lake, PublicationFailed, "the document's security_id")


def test_check_8_a_corrupt_event_file(lake: Any) -> None:
    settings, _, store = lake
    m = stage_analysis(store, settings)
    art = m.entries[2].events["pattern_breakouts"]
    key = DataLakeLayout.serving_events_key(EX, "pattern_breakouts", art.content_sha256)
    store.delete(key)
    store.put_immutable(key, b"not parquet")
    refused(lake, PublicationFailed, "corrupt: not a readable event file")


def test_check_9_a_wrong_physical_hash(lake: Any) -> None:
    settings, _, store = lake
    m = stage_analysis(store, settings)
    e = m.entries[0]
    art = e.events["level_breakouts"].model_copy(update={"physical_sha256": "f" * 64})
    e2 = e.model_copy(update={"events": {**e.events, "level_breakouts": art}})
    entries = [e2, *m.entries[1:]]
    rewrite_manifest(
        store,
        m.model_copy(update={"entries": entries, "analysis_set_hash": analysis_set_hash(entries)}),
    )
    refused(lake, PublicationFailed, "mismatch: bytes differ from the recorded physical hash")


# ----------------------------------------------------------------------------- depth (decision 2)


def test_only_objects_covered_by_the_live_snapshot_skip_full_verification(lake: Any) -> None:
    settings, _, store = lake
    m = stage_analysis(store, settings)
    publish(lake)
    reissue_weekly(store, m.entries[0].security_id)  # a new snapshot, same analysis objects
    stage_analysis(store, settings)
    out = publish(lake)
    assert out["verification"]["objects_fully_verified"] == 0
    assert out["verification"]["objects_existence_only"] == 4 * len(m.universe)


def test_an_orphan_object_gets_full_verification(lake: Any) -> None:
    """An object that merely exists in the serving store (here: written, corrupted, never
    published) is not covered by the live snapshot, so it is fully verified."""
    settings, _, store = lake
    first = stage_analysis(store, settings)
    publish(lake)
    m = stage_analysis(store, settings, variant="new")  # new objects, not yet live
    key = DataLakeLayout.serving_analysis_key(EX, m.entries[0].document_sha256)
    assert key not in {
        DataLakeLayout.serving_analysis_key(EX, e.document_sha256) for e in first.entries
    }
    store.delete(key)
    store.put_immutable(key, gzip.compress(b"{}", mtime=0))
    refused(lake, PublicationFailed, "corrupt")


def test_a_live_copy_that_fails_its_hash_covers_nothing(lake: Any) -> None:
    settings, _, store = lake
    m = stage_analysis(store, settings)
    out = publish(lake)
    key = out["analysis"]["manifest_key"]
    store.put(key, store.get(key) + b" ")  # the live copy no longer matches its pointer
    reissue_weekly(store, m.entries[0].security_id)
    stage_analysis(store, settings)
    again = publish(lake)
    assert again["verification"]["objects_existence_only"] == 0
    assert again["verification"]["objects_fully_verified"] == 4 * len(m.universe)


# ----------------------------------------------------------------------------- the commit


def test_a_crash_before_the_commit_leaves_the_old_snapshot_and_a_retry_succeeds(
    lake: Any,
) -> None:
    settings, _, store = lake
    m = stage_analysis(store, settings)
    first = publish(lake)
    reissue_weekly(store, m.entries[0].security_id)
    stage_analysis(store, settings)

    class CrashOnSwap:
        def __init__(self, inner: LocalObjectStore) -> None:
            self.inner = inner

        def __getattr__(self, name: str) -> Any:
            return getattr(self.inner, name)

        def swap(self, key: str, data: bytes, expected: str | None) -> None:
            raise KeyboardInterrupt("killed just before step 7")

    before = pointer(store)
    with pytest.raises(KeyboardInterrupt):
        publish(lake, store=CrashOnSwap(store))
    assert pointer(store) == before
    assert ServingSnapshot.load(store, EX).meta_version == first["meta_version"]
    retry = publish(lake)  # rewrites the uncommitted version's files, then commits
    assert retry["outcome"] == SnapshotOutcome.PUBLISHED
    assert ServingSnapshot.load(store, EX).meta_version == retry["meta_version"]


def test_the_commit_is_a_compare_and_swap(lake: Any) -> None:
    """Another publisher moves the pointer while this one verifies: this one fails and
    leaves the other's pointer in place."""
    settings, _, store = lake
    m = stage_analysis(store, settings)
    publish(lake)
    reissue_weekly(store, m.entries[0].security_id)
    stage_analysis(store, settings)
    interloper = b'{"meta_version": "meta-interloper"}'

    class Interloper(MemoryRunStore):
        def stage_snapshot(self, snapshot: Any) -> None:
            super().stage_snapshot(snapshot)
            store.put(DataLakeLayout.serving_manifest_key(EX), interloper)

    with pytest.raises(PublicationFailed, match="the live pointer moved"):
        publish(lake, history=Interloper())
    assert pointer(store) == interloper


# ----------------------------------------------------------------------------- clean-up


def test_clean_up_keeps_the_live_the_previous_and_the_latest_analysis(lake: Any) -> None:
    settings, _, store = lake
    a = stage_analysis(store, settings, variant="a")
    publish(lake)
    b = stage_analysis(store, settings, variant="b")
    publish(lake)  # live b, previous a
    c = stage_analysis(store, settings, variant="c")  # latest analysis, not published
    orphan = DataLakeLayout.serving_analysis_key(EX, "0" * 64)
    store.put_immutable(orphan, gzip.compress(b"orphan", mtime=0))
    d = stage_analysis(store, settings, variant="d")  # newest: replaces c as the latest
    publish(lake)  # live d, previous b; a and c are neither
    prefix = DataLakeLayout.serving_analysis_prefix(EX)
    held = set(store.list(prefix))

    def docs(m: Any) -> set[str]:
        return {DataLakeLayout.serving_analysis_key(EX, e.document_sha256) for e in m.entries}

    assert docs(d) <= held and docs(b) <= held
    assert not (docs(a) & held) and not (docs(c) & held)
    assert orphan not in held
    assert read_manifest(store, EX) == d


def test_a_covered_object_that_vanished_is_caught(lake: Any) -> None:
    """Existence-only still means existence: a covered object deleted since is refused."""
    settings, _, store = lake
    m = stage_analysis(store, settings)
    publish(lake)
    reissue_weekly(store, m.entries[0].security_id)
    stage_analysis(store, settings)
    store.delete(DataLakeLayout.serving_analysis_key(EX, m.entries[1].document_sha256))
    refused(lake, PublicationFailed, "missing")
