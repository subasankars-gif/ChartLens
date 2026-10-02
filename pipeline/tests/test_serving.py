"""Serving snapshot: immutable, versioned, hash-verified; never mixes lake versions (ADR-0016)."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest
from test_adjust import SESSIONS, build_lake

from chartlens_pipeline.adjust import AdjustmentService, CorporateActionOverrides
from chartlens_pipeline.data_quality import DataQualityService
from chartlens_pipeline.identity import IdentityOverrides
from chartlens_pipeline.serving import (
    ServingInputsNotReady,
    ServingPublisher,
    ServingSnapshot,
    SnapshotUnavailable,
    StaleSnapshot,
)
from chartlens_pipeline.storage import DataLakeLayout
from chartlens_pipeline.weekly import WeeklyService

WED = SESSIONS[17]


@pytest.fixture
def lake(tmp_path: Path):  # type: ignore[no-untyped-def]
    settings, provider, store, today = build_lake(tmp_path, ex=WED)
    AdjustmentService(
        settings, provider, store, overrides=CorporateActionOverrides(), today=today
    ).run()
    DataQualityService(settings, provider, store, identity_overrides=IdentityOverrides()).run()
    WeeklyService(settings, provider, store).run()
    return settings, provider, store


def test_snapshot_is_published_whole_and_read_back_verified(lake) -> None:  # type: ignore[no-untyped-def]
    settings, provider, store = lake
    summary = ServingPublisher(settings, provider, store).run()
    snap = ServingSnapshot.load(store, "NSE")
    assert snap.meta_version == summary["meta_version"] and snap.as_of == SESSIONS[-1]
    weekly = json.loads(store.get(DataLakeLayout.weekly_manifest_key("NSE")))
    assert snap.versions["weekly_version"] == weekly["weekly_version"]
    assert set(snap.versions) == {
        "weekly_version",
        "data_version",
        "adjustment_version",
        "identity_version",
        "dq_version",
        "calendar_version",
        "methodology_hash",
    }
    by_symbol = {s["symbol"]: s for s in snap.securities.values()}
    demer = by_symbol["DEMERCO"]
    assert demer["usable_from"] == WED and demer["segments"] == 2
    assert demer["current_segment_id"] == f"{demer['security_id']}@{WED.isoformat()}"
    assert snap.weekly_bars(store, demer["security_id"])[-1].last_session_date == SESSIONS[-1]
    assert [s["symbol"] for s in snap.search("splitco", analytical_only=True, limit=5)] == [
        "SPLITCO"
    ]
    assert snap.search("INE467B01029", analytical_only=True, limit=5)[0]["symbol"] == "DEMERCO"

    # Same inputs → same version; nothing new to serve.
    assert ServingPublisher(settings, provider, store).run()["meta_version"] == snap.meta_version


def test_a_republished_weekly_file_never_changes_a_published_snapshot(lake) -> None:  # type: ignore[no-untyped-def]
    """Schema 2 (ADR-0018) serves immutable copies; schema 1 refused a rewritten file."""
    settings, provider, store = lake
    ServingPublisher(settings, provider, store).run()
    snap = ServingSnapshot.load(store, "NSE")
    sid = next(iter(snap.securities))
    bars = snap.weekly_bars(store, sid)
    key = DataLakeLayout.curated_weekly_key("NSE", sid)
    store.put(key, store.get(key) + b"x")  # a later run rewrote it in place
    assert snap.weekly_bars(store, sid) == bars
    legacy = dataclasses.replace(snap, schema_version=1)
    with pytest.raises(StaleSnapshot):
        legacy.weekly_bars(store, sid)


def test_a_tampered_or_missing_snapshot_is_not_served(lake) -> None:  # type: ignore[no-untyped-def]
    settings, provider, store = lake
    with pytest.raises(SnapshotUnavailable, match="no serving snapshot"):
        ServingSnapshot.load(store, "NSE")
    summary = ServingPublisher(settings, provider, store).run()
    key = summary["files"]["securities"]["key"]
    store.put(key, store.get(key) + b"x")
    with pytest.raises(SnapshotUnavailable, match="does not match"):
        ServingSnapshot.load(store, "NSE")


def test_publishing_refuses_inputs_out_of_step(lake) -> None:  # type: ignore[no-untyped-def]
    settings, provider, store = lake
    report_key = DataLakeLayout.data_quality_report_key("NSE")
    report = json.loads(store.get(report_key))
    store.put(report_key, json.dumps({**report, "dq_version": "dq-newer"}).encode())
    with pytest.raises(ServingInputsNotReady, match="run `weekly`"):
        ServingPublisher(settings, provider, store).run()
