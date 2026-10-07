"""The ANALYSIS stage (ADR-0025): coverage of exactly the universe, reuse that survives a
rewritten weekly file but not changed bars, the deterministic recompute check, and a
stage that fails whole (no manifest) on every listed hard failure."""

from __future__ import annotations

import gzip
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import chartlens_jobs.analysis_stage as stage_module
import pyarrow as pa
import pytest
from analysis_lake import EX, Spec, build, manifest, read_bars, republish, with_new_close
from chartlens_jobs.analysis_stage import (
    RECOMPUTE_SAMPLE_SIZE,
    AnalysisStage,
    AnalysisStageFailed,
    SecurityOutcome,
    StoreSpec,
    sample_of,
)

from chartlens_core.canonical import canonical_json
from chartlens_core.config import ChartLensSettings
from chartlens_pipeline.analysis_store import (
    read_document,
    read_events,
    read_manifest,
    universe_sha256,
)
from chartlens_pipeline.daily import to_parquet_bytes
from chartlens_pipeline.storage import DataLakeLayout, LocalObjectStore

SPECS = [
    Spec("SEC-A", seed=1),
    Spec("SEC-B", seed=2, status="USABLE_WITH_WARNINGS", forming=True),
    Spec("SEC-C", seed=3, break_at=250),
    Spec("SEC-D", seed=4, weeks=40),
    Spec("SEC-X", seed=5, status="NOT_USABLE"),
    Spec("SEC-Y", seed=6, analytical=False),
]
UNIVERSE = ["SEC-A", "SEC-B", "SEC-C", "SEC-D"]


@pytest.fixture
def lake(tmp_path: Path) -> LocalObjectStore:
    store = LocalObjectStore(tmp_path / "lake")
    build(store, SPECS)
    return store


def stage(store: LocalObjectStore, **kw: Any) -> AnalysisStage:
    kw.setdefault("workers", 1)
    return AnalysisStage(ChartLensSettings(), EX, StoreSpec("local", root=str(store.root)), **kw)


def manifest_bytes(store: LocalObjectStore) -> bytes | None:
    key = DataLakeLayout.analysis_manifest_key(EX)
    return store.get(key) if store.exists(key) else None


# ----------------------------------------------------------------------------- coverage


def test_a_full_run_covers_exactly_the_universe(lake: LocalObjectStore) -> None:
    summary = stage(lake).run()
    m = summary.manifest
    assert m.universe == UNIVERSE and [e.security_id for e in m.entries] == UNIVERSE
    assert m.universe_sha256 == universe_sha256(UNIVERSE)
    assert (summary.computed, summary.reused) == (4, 0)
    assert read_manifest(lake, EX) == m
    events = 0
    for e in m.entries:
        doc = json.loads(read_document(lake, EX, e.document_sha256))
        assert doc["identity"]["security_id"] == e.security_id
        assert doc["inputs"]["bars_sha256"] == e.bars_sha256
        assert e.weekly_file_sha256 == manifest(lake)["files"][e.security_id]
        for name, art in e.events.items():
            rows, physical = read_events(lake, EX, name, art.content_sha256)  # type: ignore[arg-type]
            assert len(rows) == art.row_count and physical == art.physical_sha256
            assert doc["breakout_events"]["datasets"][name]["content_sha256"] == art.content_sha256
            events += art.row_count
    assert events > 0


def test_the_current_segment_only_is_analysed(lake: LocalObjectStore) -> None:
    m = stage(lake).run().manifest
    c = next(e for e in m.entries if e.security_id == "SEC-C")
    doc = json.loads(read_document(lake, EX, c.document_sha256))
    bars = read_bars(lake, "SEC-C")
    assert c.continuity_segment_id == bars[-1].continuity_segment_id
    assert doc["identity"]["bar_count"] == sum(
        1 for b in bars if b.continuity_segment_id == c.continuity_segment_id
    )
    b = next(e for e in m.entries if e.security_id == "SEC-B")
    assert json.loads(read_document(lake, EX, b.document_sha256))["identity"][
        "forming_week_present"
    ]


def test_the_manifest_records_no_run_facts(lake: LocalObjectStore) -> None:
    stage(lake).run()
    raw = manifest_bytes(lake)
    assert raw is not None
    for word in (b"timestamp", b"generated_at", b"run_id", b"computed", b"host"):
        assert word not in raw


# ----------------------------------------------------------------------------- reuse


def test_a_rerun_reuses_everything_and_writes_the_same_manifest(lake: LocalObjectStore) -> None:
    stage(lake).run()
    first = manifest_bytes(lake)
    again = stage(lake).run()
    assert (again.computed, again.reused) == (0, 4)
    assert again.sample == UNIVERSE  # fewer than 32 reused: all are recomputed and match
    assert manifest_bytes(lake) == first


def test_a_rewritten_weekly_file_with_the_same_bars_is_reused(lake: LocalObjectStore) -> None:
    """ADR-0025 §3.3: the physical file changes (new provenance columns, new hash); the
    analytical dependency does not."""
    before = stage(lake).run().manifest
    republish(lake, "wk-2")
    after = stage(lake).run()
    assert (after.computed, after.reused) == (0, 4)
    for old, new in zip(before.entries, after.manifest.entries, strict=True):
        assert new.weekly_file_sha256 != old.weekly_file_sha256
        assert new.weekly_file_sha256 == manifest(lake)["files"][new.security_id]
        assert (new.document_sha256, new.reuse_key, new.bars_sha256) == (
            old.document_sha256,
            old.reuse_key,
            old.bars_sha256,
        )


def test_changed_bars_recompute_only_that_security(lake: LocalObjectStore) -> None:
    before = {e.security_id: e for e in stage(lake).run().manifest.entries}
    bars = read_bars(lake, "SEC-A")
    republish(lake, "wk-2", {"SEC-A": with_new_close(bars, bars[-1].close + Decimal("1.5"))})
    after = stage(lake).run()
    assert (after.computed, after.reused) == (1, 3)
    new = {e.security_id: e for e in after.manifest.entries}
    assert new["SEC-A"].document_sha256 != before["SEC-A"].document_sha256
    assert all(new[s].document_sha256 == before[s].document_sha256 for s in UNIVERSE[1:])


def test_no_reuse_recomputes_everything_to_the_same_result(lake: LocalObjectStore) -> None:
    first = stage(lake).run()
    forced = stage(lake, allow_reuse=False).run()
    assert (forced.computed, forced.reused) == (4, 0)
    assert forced.manifest == first.manifest


def test_a_missing_artifact_is_recomputed_not_reused(lake: LocalObjectStore) -> None:
    m = stage(lake).run().manifest
    lake.delete(DataLakeLayout.serving_analysis_key(EX, m.entries[0].document_sha256))
    again = stage(lake).run()
    assert (again.computed, again.reused) == (1, 3)
    assert read_document(lake, EX, m.entries[0].document_sha256)


def test_the_pool_gives_the_same_manifest_as_one_process(tmp_path: Path) -> None:
    one = LocalObjectStore(tmp_path / "one")
    many = LocalObjectStore(tmp_path / "many")
    build(one, SPECS)
    build(many, SPECS)
    stage(one, workers=1).run()
    pooled = stage(many, workers=2).run()
    assert pooled.computed == 4
    assert manifest_bytes(one) == manifest_bytes(many)
    rerun = stage(many, workers=2).run()
    assert (rerun.reused, rerun.sample) == (4, UNIVERSE)


# ----------------------------------------------------------------------------- the sample


def test_the_sample_is_deterministic_bounded_and_order_free() -> None:
    ids = [f"SEC-{i:04d}" for i in range(200)]
    first = sample_of(ids, "wk-1")
    assert len(first) == RECOMPUTE_SAMPLE_SIZE == 32
    assert sample_of(list(reversed(ids)), "wk-1") == first
    assert set(first) <= set(ids)
    assert sample_of(ids, "wk-2") != first  # rotates across weekly versions
    assert sample_of(ids[:5], "wk-1") == sorted(ids[:5])  # fewer than 32: all


def test_a_recompute_mismatch_fails_the_stage(
    lake: LocalObjectStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage(lake).run()
    first = manifest_bytes(lake)
    real = stage_module.serialize

    def drifted(analysis: Any) -> Any:
        out = real(analysis)
        return type(out)(out.document + b" ", out.document_sha256, out.events)

    monkeypatch.setattr(stage_module, "serialize", drifted)
    with pytest.raises(AnalysisStageFailed, match="recompute check failed"):
        stage(lake).run()
    assert manifest_bytes(lake) == first


# ----------------------------------------------------------------------------- hard failures


def fails(store: LocalObjectStore, match: str, **kw: Any) -> None:
    before = manifest_bytes(store)
    with pytest.raises(AnalysisStageFailed, match=match):
        stage(store, **kw).run()
    assert manifest_bytes(store) == before  # no manifest written, the previous untouched


def test_a_corrupted_weekly_file_fails(lake: LocalObjectStore) -> None:
    stage(lake).run()
    key = DataLakeLayout.curated_weekly_key(EX, "SEC-B")
    lake.put(key, lake.get(key) + b"x")
    fails(lake, "SEC-B: the weekly file does not match")


def test_a_universe_member_without_a_weekly_file_fails(lake: LocalObjectStore) -> None:
    m = manifest(lake)
    del m["files"]["SEC-C"]
    lake.put(DataLakeLayout.weekly_manifest_key(EX), json.dumps(m).encode())
    fails(lake, "SEC-C: no weekly file")


def test_an_engine_failure_fails(lake: LocalObjectStore, monkeypatch: pytest.MonkeyPatch) -> None:
    real = stage_module.analyze_security

    def boom(bars: Any, context: Any, config: Any, inputs: Any) -> Any:
        if context.security_id == "SEC-C":
            raise ValueError("engine bug")
        return real(bars, context, config, inputs)

    monkeypatch.setattr(stage_module, "analyze_security", boom)
    fails(lake, "SEC-C: analysis failed")


@pytest.mark.parametrize("fault", ["dropped", "duplicate", "unexpected"])
def test_the_manifest_comes_from_the_completed_results(
    lake: LocalObjectStore, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    """ADR-0025 §4: a job that believes it processed everything but did not emit one
    result, emitted one twice, or emitted one outside the universe, fails."""
    real = AnalysisStage._execute

    def faulty(self: AnalysisStage, pool: Any, tasks: Any) -> list[SecurityOutcome]:
        out = real(self, pool, tasks)
        if fault == "dropped":
            return out[1:]
        if fault == "duplicate":
            return [*out, out[0]]
        stray = out[0].entry.model_copy(update={"security_id": "SEC-Z"})
        return [*out, SecurityOutcome(stray, computed=True)]

    monkeypatch.setattr(AnalysisStage, "_execute", faulty)
    fails(lake, {"dropped": "no result", "duplicate": "duplicate", "unexpected": "outside"}[fault])


def test_usable_from_must_be_the_current_segment_start(lake: LocalObjectStore) -> None:
    key = DataLakeLayout.data_quality_status_key(EX)
    rows = pa.BufferReader(lake.get(key))
    import pyarrow.parquet as pq

    table = pq.read_table(rows).to_pylist()
    for r in table:
        if r["security_id"] == "SEC-A":
            r["usable_from"] = r["usable_from"].replace(year=2011)
    lake.put(key, to_parquet_bytes(pa.Table.from_pylist(table)))
    fails(lake, "SEC-A: usable_from")


def test_a_segment_mismatch_fails(lake: LocalObjectStore) -> None:
    key = DataLakeLayout.continuity_segments_key(EX)
    import pyarrow.parquet as pq

    rows = [r for r in pq.read_table(pa.BufferReader(lake.get(key))).to_pylist()]
    # The data-quality current segment of SEC-C becomes its first one.
    rows = [r for r in rows if not (r["security_id"] == "SEC-C" and r["segment_start"].year > 2010)]
    lake.put(key, to_parquet_bytes(pa.Table.from_pylist(rows)))
    status_key = DataLakeLayout.data_quality_status_key(EX)
    status = pq.read_table(pa.BufferReader(lake.get(status_key))).to_pylist()
    first = next(r["segment_start"] for r in rows if r["security_id"] == "SEC-C")
    for r in status:
        if r["security_id"] == "SEC-C":
            r["usable_from"] = first
    lake.put(status_key, to_parquet_bytes(pa.Table.from_pylist(status)))
    fails(lake, "SEC-C: the file's current segment")


def test_out_of_step_inputs_fail(lake: LocalObjectStore) -> None:
    lake.put(
        DataLakeLayout.data_quality_report_key(EX), json.dumps({"dq_version": "dq-2"}).encode()
    )
    fails(lake, "data quality is now dq-2")


def test_a_conflicting_object_at_an_address_fails(lake: LocalObjectStore) -> None:
    m = stage(lake).run().manifest
    address = m.entries[0].document_sha256
    key = DataLakeLayout.serving_analysis_key(EX, address)
    lake.delete(key)
    lake.put_immutable(key, gzip.compress(b'{"not":"it"}', mtime=0))
    fails(lake, "holds different content", allow_reuse=False)


def test_an_unreadable_previous_manifest_only_costs_reuse(lake: LocalObjectStore) -> None:
    stage(lake).run()
    lake.put(DataLakeLayout.analysis_manifest_key(EX), b"{broken")
    again = stage(lake).run()
    assert (again.computed, again.reused) == (4, 0)


# ----------------------------------------------------------------------------- artifacts


def test_documents_are_addressed_by_their_uncompressed_bytes_in_production(
    lake: LocalObjectStore,
) -> None:
    """ADR-0024 §4.1 regression, through the stage's own write path."""
    import hashlib

    m = stage(lake).run().manifest
    for e in m.entries:
        stored = lake.get(DataLakeLayout.serving_analysis_key(EX, e.document_sha256))
        canonical = gzip.decompress(stored)
        assert hashlib.sha256(canonical).hexdigest() == e.document_sha256
        assert hashlib.sha256(stored).hexdigest() != e.document_sha256
        assert canonical == canonical_json(json.loads(canonical))
