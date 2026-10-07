"""Analysis artifact persistence (ADR-0024 §4.1, ADR-0025 §4–§5): the universe rule,
gzip documents addressed by their uncompressed bytes, event files that decode to exactly
their rows, idempotent writes, and a manifest that must cover exactly its universe."""

from __future__ import annotations

import gzip
import hashlib
from pathlib import Path
from typing import Any

import pytest

from chartlens_core.canonical import canonical_json, content_hash, dataset_content_hash
from chartlens_pipeline.analysis_store import (
    EVENT_SCHEMAS,
    AnalysisEntry,
    AnalysisManifest,
    AnalysisStoreError,
    ArtifactConflict,
    EventArtifact,
    analysis_set_hash,
    analysis_universe,
    compress_document,
    decode_events,
    encode_events,
    read_document,
    read_events,
    read_manifest,
    universe_sha256,
    write_document,
    write_events,
    write_manifest,
)
from chartlens_pipeline.storage import DataLakeLayout, LocalObjectStore


@pytest.fixture
def store(tmp_path: Path) -> LocalObjectStore:
    return LocalObjectStore(tmp_path / "lake")


# ----------------------------------------------------------------------------- universe


def status(sid: str, analytical: bool, state: str, dq: str = "dq-1") -> dict[str, Any]:
    return {
        "security_id": sid,
        "analytical_universe": analytical,
        "status": state,
        "dq_version": dq,
    }


def test_the_universe_is_analytical_and_usable_including_inactive_securities() -> None:
    rows = [
        status("C", True, "USABLE_WITH_WARNINGS"),
        status("A", True, "USABLE"),
        status("B", True, "NOT_USABLE"),
        status("D", False, "USABLE"),
    ]
    assert analysis_universe(rows, "dq-1") == ["A", "C"]


def test_the_universe_refuses_another_dq_version_and_duplicates() -> None:
    with pytest.raises(AnalysisStoreError):
        analysis_universe([status("A", True, "USABLE", dq="dq-0")], "dq-1")
    with pytest.raises(AnalysisStoreError):
        analysis_universe([status("A", True, "USABLE"), status("A", True, "USABLE")], "dq-1")


def test_the_universe_hash_is_of_the_set() -> None:
    assert universe_sha256(["B", "A"]) == universe_sha256(["A", "B"]) == content_hash(["A", "B"])
    assert universe_sha256(["A"]) != universe_sha256(["A", "B"])


# ----------------------------------------------------------------------------- documents

CANONICAL = canonical_json({"identity": {"security_id": "S1"}, "values": [1.5, -0.0, None]})


def test_a_document_is_addressed_by_its_uncompressed_bytes(store: LocalObjectStore) -> None:
    """ADR-0024 §4.1: SHA-256 of the canonical bytes, never of the gzip bytes."""
    address = write_document(store, "NSE", CANONICAL)
    stored = store.get(DataLakeLayout.serving_analysis_key("NSE", address))
    assert address == hashlib.sha256(CANONICAL).hexdigest()
    assert address != hashlib.sha256(stored).hexdigest()
    assert gzip.decompress(stored) == CANONICAL
    assert read_document(store, "NSE", address) == CANONICAL


def test_compression_is_deterministic_and_carries_no_metadata() -> None:
    a, b = compress_document(CANONICAL), compress_document(CANONICAL)
    assert a == b
    assert a[4:8] == b"\x00\x00\x00\x00"  # gzip MTIME
    assert not a[3] & 0x08  # FLG.FNAME: no file name


def test_compression_is_non_semantic(store: LocalObjectStore) -> None:
    """Another compressor's bytes at the same address are the same document: the write
    is idempotent and the address never changes."""
    address = hashlib.sha256(CANONICAL).hexdigest()
    key = DataLakeLayout.serving_analysis_key("NSE", address)
    store.put_immutable(key, gzip.compress(CANONICAL, compresslevel=1, mtime=0))
    assert write_document(store, "NSE", CANONICAL) == address
    assert read_document(store, "NSE", address) == CANONICAL


def test_different_content_at_an_address_is_a_conflict(store: LocalObjectStore) -> None:
    address = hashlib.sha256(CANONICAL).hexdigest()
    key = DataLakeLayout.serving_analysis_key("NSE", address)
    store.put_immutable(key, gzip.compress(b"{}", mtime=0))
    with pytest.raises(ArtifactConflict):
        write_document(store, "NSE", CANONICAL)
    with pytest.raises(AnalysisStoreError):
        read_document(store, "NSE", address)


# ----------------------------------------------------------------------------- events


def follow_up(kind: str, day: str, values: dict[str, float]) -> dict[str, Any]:
    return {
        "kind": kind,
        "effective_date": day,
        "known_at": day,
        "authority": "RETEST_RULE",
        "source_outcome_ref": None,
        "measured_values": values,
        "provisional": False,
    }


def level_row(i: int, volume: bool) -> dict[str, Any]:
    return {
        "event_id": f"LBE-{i:032x}",
        "event_key": f"L{i}:BREAKOUT:2024-01-0{i + 1}",
        "security_id": "S1",
        "source_version": "levels-2",
        "source_id": f"L{i}",
        "source_event_ref": f"L{i}#ROLE_CHANGE@2024-01-0{i + 1}",
        "continuity_segment_id": "S1@2006-01-06",
        "direction": "BREAKOUT",
        "bar_date": f"2024-01-0{i + 1}",
        "known_at": f"2024-01-0{i + 1}",
        "level_at_break": 101.25 + i,
        "reference_atr": None if i == 0 else 2.0000000000000004,
        "retest_band": None if i == 0 else 1.0,
        "reversal_window_bars": 3,
        "retest_window_bars": 10,
        "observation_bars": 10,
        "source_measured_values": {"close": 103.5, "level": 101.25, "open": -0.0},
        "bar_volume": {
            "bar_date": f"2024-01-0{i + 1}",
            "volume": 1e6,
            "baseline_bars": 20,
            "baseline_mean_volume": None,
            "rvol": None,
            "classification": None,
            "expansion_threshold": 1.5,
            "contraction_threshold": 0.7,
            "evidence_refs": ["volume_sma_20"],
            "measurement_version": "bar-volume-1",
        }
        if volume
        else None,
        "provisional": i == 1,
        "history": [follow_up("RETEST", "2024-01-12", {"extreme": 101.0, "band": 1.0})]
        if i
        else [],
        "methodology_version": "breakouts-1",
        "source_type": "LEVEL",
        "level_source_type": "SWING",
    }


ROWS = [level_row(0, False), level_row(1, True), level_row(2, True)]
META = {"chartlens.dataset": "level_breakouts", "chartlens.security_id": "S1"}


def test_event_files_decode_to_exactly_their_rows() -> None:
    data = encode_events("level_breakouts", ROWS, META)
    dataset, rows, meta = decode_events(data)
    assert dataset == "level_breakouts" and meta == META
    assert rows == ROWS
    assert canonical_json(rows) == canonical_json(ROWS)
    assert content_hash(rows) == content_hash(ROWS)


def test_an_empty_dataset_round_trips() -> None:
    meta = {"chartlens.dataset": "pattern_breakouts"}
    assert decode_events(encode_events("pattern_breakouts", [], meta))[1] == []


def test_a_row_that_does_not_match_the_schema_is_refused() -> None:
    extra = {**ROWS[0], "surprise": 1}
    with pytest.raises(AnalysisStoreError):
        encode_events("level_breakouts", [extra], META)
    bad_struct = {**ROWS[1], "bar_volume": {**ROWS[1]["bar_volume"], "extra": 1}}
    with pytest.raises(AnalysisStoreError):
        encode_events("level_breakouts", [bad_struct], META)


def test_event_files_are_named_by_content_and_record_their_bytes(store: LocalObjectStore) -> None:
    digest, physical = write_events(store, "NSE", "level_breakouts", ROWS, META)
    assert digest == dataset_content_hash(META, ROWS)
    key = DataLakeLayout.serving_events_key("NSE", "level_breakouts", digest)
    assert physical == hashlib.sha256(store.get(key)).hexdigest()
    rows, again = read_events(store, "NSE", "level_breakouts", digest)
    assert rows == ROWS and again == physical
    assert write_events(store, "NSE", "level_breakouts", ROWS, META) == (digest, physical)
    stamped = {**META, "chartlens.content_sha256": "0" * 64}
    with pytest.raises(AnalysisStoreError):
        write_events(store, "NSE", "level_breakouts", ROWS, stamped)


def test_the_same_rows_of_two_owners_are_two_datasets(store: LocalObjectStore) -> None:
    """Regression (found by the pool test): two securities with no events must not share
    one file, or the file's metadata would name whichever wrote first."""
    a = {"chartlens.dataset": "pattern_breakouts", "chartlens.security_id": "S1"}
    b = {"chartlens.dataset": "pattern_breakouts", "chartlens.security_id": "S2"}
    da, _ = write_events(store, "NSE", "pattern_breakouts", [], a)
    db, _ = write_events(store, "NSE", "pattern_breakouts", [], b)
    assert da != db
    meta = decode_events(
        store.get(DataLakeLayout.serving_events_key("NSE", "pattern_breakouts", db))
    )[2]
    assert meta["chartlens.security_id"] == "S2"


def test_an_event_file_with_other_rows_at_the_address_is_a_conflict(
    store: LocalObjectStore,
) -> None:
    digest = dataset_content_hash(META, ROWS)
    key = DataLakeLayout.serving_events_key("NSE", "level_breakouts", digest)
    store.put_immutable(key, encode_events("level_breakouts", ROWS[:1], META))
    with pytest.raises(ArtifactConflict):
        write_events(store, "NSE", "level_breakouts", ROWS, META)


def test_each_dataset_has_its_own_schema() -> None:
    assert set(EVENT_SCHEMAS) == {"pattern_breakouts", "level_breakouts"}
    assert "pattern_type" in EVENT_SCHEMAS["pattern_breakouts"].names
    assert "level_source_type" in EVENT_SCHEMAS["level_breakouts"].names


# ----------------------------------------------------------------------------- manifest


def entry(sid: str) -> AnalysisEntry:
    ev = EventArtifact(content_sha256="a" * 64, physical_sha256="b" * 64, row_count=0)
    return AnalysisEntry(
        security_id=sid,
        continuity_segment_id=f"{sid}@2006-01-06",
        reuse_key="c" * 64,
        weekly_file_sha256="d" * 64,
        bars_sha256="e" * 64,
        document_sha256="f" * 64,
        events={"pattern_breakouts": ev, "level_breakouts": ev},
    )


def manifest(entries: list[AnalysisEntry], universe: list[str] | None = None) -> AnalysisManifest:
    ids = universe if universe is not None else [e.security_id for e in entries]
    return AnalysisManifest(
        exchange="NSE",
        weekly_version="wk-1",
        dq_version="dq-1",
        as_of="2026-10-06",
        methodology_hash="m",
        analysis_version="analysis-1",
        analysis_methodology_hash="a",
        document_schema_version="1",
        canonical_serialization_version="1",
        event_schema_version="1",
        runtime={"python": "3.12"},
        reuse_key_version="1",
        universe_rule_version="1",
        recompute_sample_size=32,
        sample_selection_version="1",
        universe=ids,
        universe_sha256=universe_sha256(ids),
        entries=entries,
        analysis_set_hash=analysis_set_hash(entries),
    )


def test_a_manifest_round_trips_and_is_canonical(store: LocalObjectStore) -> None:
    m = manifest([entry("A"), entry("B")])
    write_manifest(store, m)
    assert read_manifest(store, "NSE") == m
    raw = store.get(DataLakeLayout.analysis_manifest_key("NSE"))
    assert raw == canonical_json(m.model_dump(mode="json"))


@pytest.mark.parametrize(
    "bad",
    [
        lambda: manifest([entry("B"), entry("A")]),  # out of order
        lambda: manifest([entry("A"), entry("A")]),  # duplicate
        lambda: manifest([entry("A")], universe=["A", "B"]),  # missing
        lambda: manifest([entry("A"), entry("B")], universe=["A"]),  # unexpected
    ],
)
def test_a_manifest_must_cover_exactly_its_universe(store: LocalObjectStore, bad: Any) -> None:
    with pytest.raises(AnalysisStoreError):
        write_manifest(store, bad())
    assert read_manifest(store, "NSE") is None


def test_a_tampered_manifest_is_refused(store: LocalObjectStore) -> None:
    m = manifest([entry("A")])
    tampered = m.model_copy(update={"analysis_set_hash": "0" * 64})
    store.put(DataLakeLayout.analysis_manifest_key("NSE"), canonical_json(tampered.model_dump()))
    with pytest.raises(AnalysisStoreError):
        read_manifest(store, "NSE")
