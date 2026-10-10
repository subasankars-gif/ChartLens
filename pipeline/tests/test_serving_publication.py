"""Serving publication (ADR-0018): immutable weekly copies, snapshot history, atomic
pointer, and a live snapshot that no failed or partial run can touch."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from analysis_fixture import AV, EV, stage_analysis
from test_adjust import SESSIONS, build_lake

from chartlens_core.runs import SnapshotOutcome, SnapshotRecord
from chartlens_pipeline.adjust import AdjustmentService, CorporateActionOverrides
from chartlens_pipeline.data_quality import DataQualityService
from chartlens_pipeline.identity import IdentityOverrides
from chartlens_pipeline.runs import MemoryRunStore
from chartlens_pipeline.serving import (
    PublicationFailed,
    ServingPublisher,
    ServingSnapshot,
    SnapshotUnavailable,
)
from chartlens_pipeline.storage import DataLakeLayout, LocalObjectStore
from chartlens_pipeline.weekly import WeeklyService

WED = SESSIONS[17]
T0 = datetime(2026, 10, 2, 15, 0, tzinfo=UTC)


@pytest.fixture
def lake(tmp_path: Path):  # type: ignore[no-untyped-def]
    settings, provider, store, today = build_lake(tmp_path, ex=WED)
    AdjustmentService(
        settings, provider, store, overrides=CorporateActionOverrides(), today=today
    ).run()
    DataQualityService(settings, provider, store, identity_overrides=IdentityOverrides()).run()
    WeeklyService(settings, provider, store).run()
    return settings, provider, store


def publisher(lake: Any, history: Any = None, run_id: str | None = "run-x") -> ServingPublisher:
    """ANALYSIS (a stand-in) for the current weekly version, then a publisher."""
    settings, provider, store = lake
    stage_analysis(store, settings)
    return ServingPublisher(
        settings,
        provider,
        store,
        expected_analysis_version=AV,
        expected_explain_version=EV,
        history=history,
        run_id=run_id,
        clock=lambda: T0,
    )


def reissue_weekly(store: LocalObjectStore, sid: str, compression: str = "gzip") -> str:
    """What the next run's weekly step does: same bars, new bytes, a new manifest hash."""
    key = DataLakeLayout.curated_weekly_key("NSE", sid)
    table = pq.read_table(pa.BufferReader(store.get(key)))
    sink = pa.BufferOutputStream()
    pq.write_table(table, sink, compression=compression)
    data = sink.getvalue().to_pybytes()
    store.put(key, data)
    manifest_key = DataLakeLayout.weekly_manifest_key("NSE")
    manifest = json.loads(store.get(manifest_key))
    digest = hashlib.sha256(data).hexdigest()
    manifest["files"][sid] = digest
    store.put(manifest_key, json.dumps(manifest).encode())
    return digest


def pointer(store: LocalObjectStore) -> bytes:
    return store.get(DataLakeLayout.serving_manifest_key("NSE"))


def test_publication_records_history_and_serves_immutable_copies(lake: Any) -> None:
    _, _, store = lake
    history = MemoryRunStore()
    summary = publisher(lake, history).run()
    assert summary["outcome"] == SnapshotOutcome.PUBLISHED and summary["schema_version"] == 4
    meta = summary["meta_version"]
    record = history.get_snapshot(meta)
    assert record is not None
    assert (record.status, record.published_at, record.run_id) == ("PUBLISHED", T0, "run-x")
    assert record.versions == summary["versions"] and record.data_as_of == SESSIONS[-1]
    manifest = json.loads(pointer(store))
    assert manifest["run_id"] == "run-x"
    # Every weekly file is served from a content-addressed copy, and a version copy of
    # the manifest is kept with the snapshot's files.
    for digest in manifest["weekly_files"].values():
        assert store.exists(DataLakeLayout.serving_weekly_key("NSE", digest))
    copy = store.get(DataLakeLayout.serving_version_manifest_key("NSE", meta))
    assert copy == pointer(store)


def test_an_identical_snapshot_is_not_republished(lake: Any) -> None:
    _, _, store = lake
    history = MemoryRunStore()
    first = publisher(lake, history).run()
    before = pointer(store)
    again = publisher(lake, history, run_id="run-y").run()
    assert again["outcome"] == SnapshotOutcome.UNCHANGED
    assert again["meta_version"] == first["meta_version"]
    assert pointer(store) == before  # not even rewritten
    assert len(history.snapshots) == 1


class FailingHistory(MemoryRunStore):
    def stage_snapshot(self, snapshot: SnapshotRecord) -> None:
        raise RuntimeError("firestore unavailable")


def test_a_failure_before_the_pointer_moves_leaves_the_live_snapshot(lake: Any) -> None:
    _, _, store = lake
    first = publisher(lake).run()
    live = ServingSnapshot.load(store, "NSE")
    sid = next(iter(live.securities))
    bars = live.weekly_bars(store, sid)
    reissue_weekly(store, sid)  # the next run's weekly step
    before = pointer(store)
    with pytest.raises(RuntimeError, match="firestore unavailable"):
        publisher(lake, FailingHistory(), run_id="run-z").run()
    assert pointer(store) == before
    still = ServingSnapshot.load(store, "NSE")
    assert still.meta_version == first["meta_version"]
    assert still.weekly_bars(store, sid) == bars


def test_a_weekly_file_out_of_step_with_its_manifest_stops_publication(lake: Any) -> None:
    _, _, store = lake
    publisher(lake).run()
    before = pointer(store)
    sid = next(iter(ServingSnapshot.load(store, "NSE").securities))
    reissue_weekly(store, sid)
    key = DataLakeLayout.curated_weekly_key("NSE", sid)
    store.put(key, store.get(key) + b"torn")  # rewritten after the manifest
    with pytest.raises(PublicationFailed, match="no longer matches"):
        publisher(lake).run()
    assert pointer(store) == before


def test_a_new_snapshot_keeps_the_previous_one_and_removes_older(lake: Any) -> None:
    _, _, store = lake
    history = MemoryRunStore()
    first = publisher(lake, history).run()["meta_version"]
    sid = sorted(ServingSnapshot.load(store, "NSE").securities)[0]
    original = json.loads(pointer(store))["weekly_files"][sid]
    second_digest = reissue_weekly(store, sid)
    second = publisher(lake, history).run()["meta_version"]
    third_digest = reissue_weekly(store, sid, "none")
    third = publisher(lake, history).run()
    assert len({first, second, third["meta_version"]}) == 3
    prefix = DataLakeLayout.serving_prefix("NSE")
    versions = {k[len(prefix) :].split("/", 1)[0] for k in store.list(prefix + "v=")}
    assert versions == {f"v={second}", f"v={third['meta_version']}"}
    # The first snapshot's copy of that file is gone; the previous one's is kept.
    assert not store.exists(DataLakeLayout.serving_weekly_key("NSE", original))
    assert store.exists(DataLakeLayout.serving_weekly_key("NSE", second_digest))
    assert store.exists(DataLakeLayout.serving_weekly_key("NSE", third_digest))
    assert [s.snapshot_id for s in history.list_snapshots(10)] != []
    assert {s.status for s in history.snapshots.values()} == {"PUBLISHED"}


def test_a_corrupt_weekly_copy_is_refused(lake: Any) -> None:
    _, _, store = lake
    publisher(lake).run()
    snap = ServingSnapshot.load(store, "NSE")
    sid = next(iter(snap.securities))
    key = DataLakeLayout.serving_weekly_key("NSE", snap.weekly_files[sid])
    store.put(key, store.get(key) + b"x")
    with pytest.raises(SnapshotUnavailable, match="corrupt"):
        snap.weekly_bars(store, sid)


def test_after_the_pointer_moves_nothing_fails_the_publication(lake: Any) -> None:
    _, _, store = lake
    history = MemoryRunStore()
    publisher(lake, history).run()
    sid = next(iter(ServingSnapshot.load(store, "NSE").securities))
    reissue_weekly(store, sid)

    class BrokenDeletes:  # clean-up fails (a GCS 503 on one of many deletes)
        def __init__(self, inner: LocalObjectStore) -> None:
            self.inner = inner

        def __getattr__(self, name: str) -> Any:
            return getattr(self.inner, name)

        def delete(self, key: str) -> None:
            raise RuntimeError("503 from storage")

    settings, provider, _ = lake
    stage_analysis(store, settings)
    second = ServingPublisher(
        settings,
        provider,
        BrokenDeletes(store),  # type: ignore[arg-type]
        expected_analysis_version=AV,
        expected_explain_version=EV,
        history=history,
        clock=lambda: T0,
    ).run()
    assert second["outcome"] == SnapshotOutcome.PUBLISHED
    assert json.loads(pointer(store))["meta_version"] == second["meta_version"]


def test_an_unchanged_run_completes_a_record_left_staged(lake: Any) -> None:
    history = MemoryRunStore()

    class DiesBeforeMarking(MemoryRunStore):
        def publish_snapshot(self, snapshot_id: str, now: datetime) -> None:
            history.snapshots.update(self.snapshots)
            raise RuntimeError("process killed")

    first = publisher(lake, DiesBeforeMarking()).run()
    record = history.get_snapshot(first["meta_version"])
    assert record is not None and record.status == "STAGED"
    again = publisher(lake, history).run()
    assert again["outcome"] == SnapshotOutcome.UNCHANGED
    record = history.get_snapshot(first["meta_version"])
    assert record is not None and record.status == "PUBLISHED"
