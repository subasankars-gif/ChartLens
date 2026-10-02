"""Weekly data product end to end: adjusted daily → weekly files, scan dataset, and the
point-in-time reader (ADR-0014)."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal as D
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from test_adjust import SESSIONS, build_lake

from chartlens_core.bars import validate_bar_frame
from chartlens_core.weekly import PartialReason
from chartlens_pipeline.adjust import AdjustmentService, CorporateActionOverrides
from chartlens_pipeline.data_quality import DataQualityService
from chartlens_pipeline.identity import IdentityOverrides
from chartlens_pipeline.storage import DataLakeLayout, LocalObjectStore
from chartlens_pipeline.weekly import (
    WeeklyInputsNotReady,
    WeeklyReader,
    WeeklyService,
    data_version,
)

WED = SESSIONS[17]  # 2024-01-24, a Wednesday: the corporate actions fall mid-week
assert WED.weekday() == 2


@pytest.fixture
def lake(tmp_path: Path):  # type: ignore[no-untyped-def]
    settings, provider, store, today = build_lake(tmp_path, ex=WED)
    AdjustmentService(
        settings, provider, store, overrides=CorporateActionOverrides(), today=today
    ).run()
    DataQualityService(settings, provider, store, identity_overrides=IdentityOverrides()).run()
    return settings, provider, store


def sid_of(store: LocalObjectStore, symbol: str) -> str:
    status = pq.read_table(
        pa.BufferReader(store.get(DataLakeLayout.data_quality_status_key("NSE")))
    ).to_pylist()
    return next(s["security_id"] for s in status if s["symbol"] == symbol)


def test_weekly_files_manifest_and_scan_dataset(lake) -> None:  # type: ignore[no-untyped-def]
    settings, provider, store = lake
    result = WeeklyService(settings, provider, store).run()
    assert result.securities == 3 and result.as_of == SESSIONS[-1]
    manifest = json.loads(store.get(DataLakeLayout.weekly_manifest_key("NSE")))
    adjusted = store.get(DataLakeLayout.adjusted_manifest_key("NSE"))
    assert manifest["data_version"] == data_version(adjusted) == result.data_version
    assert set(manifest["files"]) == {sid_of(store, s) for s in ("SPLITCO", "DEMERCO", "PLAIN")}
    for key in ("adjustment_version", "identity_version", "dq_version", "methodology_hash"):
        assert manifest[key]
    assert (manifest["min_session"], manifest["max_session"]) == (
        str(SESSIONS[0]),
        str(SESSIONS[-1]),
    )

    scan = json.loads(store.get(DataLakeLayout.weekly_scan_manifest_key("NSE")))
    assert scan["weekly_version"] == result.weekly_version and len(scan["parts"]) == 1
    part = pq.read_table(pa.BufferReader(store.get(scan["parts"][0]["key"])))
    assert part.num_rows == scan["row_count"] == manifest["row_count"] == result.bars
    per_security = pa.concat_tables(
        pq.read_table(pa.BufferReader(store.get(DataLakeLayout.curated_weekly_key("NSE", s))))
        for s in sorted(manifest["files"])
    )
    assert part.equals(per_security)  # derived from the same bars, nothing else

    # Same inputs → same version, nothing rewritten.
    again = WeeklyService(settings, provider, store).run()
    assert again.weekly_version == result.weekly_version and again.files_written == 0


def test_a_mid_week_split_is_one_bar_a_mid_week_break_is_two(lake) -> None:  # type: ignore[no-untyped-def]
    settings, provider, store = lake
    WeeklyService(settings, provider, store).run()
    reader = WeeklyReader(provider, store)

    split = reader.load(sid_of(store, "SPLITCO"), all_segments=True)
    assert len(split.segment_ids) == 1
    week = next(b for b in split.bars if b.first_session_date <= WED <= b.last_session_date)
    assert week.partial_reason is None and week.trading_days == 5
    assert {b.close for b in split.bars} == {D(40)}  # 200 before the 1:5 split, adjusted
    assert week.raw_close == D(40) and split.bars[0].raw_close == D(200)  # as traded

    demer = reader.load(sid_of(store, "DEMERCO"), all_segments=True)
    pre, post = (b for b in demer.bars if b.week_start_date == week.week_start_date)
    assert pre.partial_reason == post.partial_reason == PartialReason.CONTINUITY_BREAK
    assert (pre.last_session_date, pre.close, post.first_session_date, post.open) == (
        SESSIONS[16],
        D(100),
        WED,
        D(70),
    )
    assert pre.continuity_segment_id != post.continuity_segment_id
    assert demer.segment_ids == [pre.continuity_segment_id, post.continuity_segment_id]
    assert pre.is_complete and post.is_complete

    current = reader.load(sid_of(store, "DEMERCO"))  # what analysis gets: the valid segment
    assert current.bars[0] == post and current.source == "stored"
    validate_bar_frame(current.frame())
    with pytest.raises(ValueError, match="continuity segments"):
        validate_bar_frame(demer.frame())


def test_point_in_time_uses_only_actions_and_breaks_known_by_then(lake) -> None:  # type: ignore[no-untyped-def]
    settings, provider, store = lake
    WeeklyService(settings, provider, store).run()
    reader = WeeklyReader(provider, store)
    tue = SESSIONS[16]

    split = reader.load(sid_of(store, "SPLITCO"), as_of=tue)
    assert split.source == "point_in_time" and split.as_of == tue
    assert {b.close for b in split.bars} == {D(200)}  # the split had not happened yet
    assert split.bars[-1].last_session_date == tue and not split.bars[-1].is_complete

    demer = reader.load(sid_of(store, "DEMERCO"), as_of=tue, all_segments=True)
    assert len(demer.segment_ids) == 1  # the break is in the future from here
    assert all(b.partial_reason is None for b in demer.bars)


def test_point_in_time_at_the_data_end_equals_the_stored_bars(lake) -> None:  # type: ignore[no-untyped-def]
    settings, provider, store = lake
    WeeklyService(settings, provider, store).run()
    reader = WeeklyReader(provider, store)
    manifest = json.loads(store.get(DataLakeLayout.weekly_manifest_key("NSE")))
    for sid in manifest["files"]:
        stored = reader.load(sid, all_segments=True)
        rebuilt = reader.load(sid, all_segments=True, force_point_in_time=True)
        assert rebuilt.bars == stored.bars and rebuilt.segment_ids == stored.segment_ids
        assert [repr(b) for b in rebuilt.bars] == [repr(b) for b in stored.bars]  # scale too


def test_as_of_after_the_data_is_the_data_end_and_before_listing_is_empty(lake) -> None:  # type: ignore[no-untyped-def]
    settings, provider, store = lake
    WeeklyService(settings, provider, store).run()
    reader = WeeklyReader(provider, store)
    sid = sid_of(store, "PLAIN")
    assert reader.load(sid, as_of=date(2030, 1, 1)).as_of == SESSIONS[-1]
    early = reader.load(sid, as_of=date(2023, 12, 1))
    assert early.bars == [] and early.segment_ids == []


def test_weekly_needs_current_data_quality(tmp_path: Path) -> None:
    settings, provider, store, today = build_lake(tmp_path, ex=WED)
    with pytest.raises(WeeklyInputsNotReady, match="adjust"):
        WeeklyService(settings, provider, store).run()
    AdjustmentService(
        settings, provider, store, overrides=CorporateActionOverrides(), today=today
    ).run()
    with pytest.raises(WeeklyInputsNotReady, match="data-quality"):
        WeeklyService(settings, provider, store).run()
    DataQualityService(settings, provider, store, identity_overrides=IdentityOverrides()).run()
    report_key = DataLakeLayout.data_quality_report_key("NSE")
    report = json.loads(store.get(report_key))
    store.put(report_key, json.dumps({**report, "adjustment_version": "adj-old"}).encode())
    with pytest.raises(WeeklyInputsNotReady, match="assessed on adj-old"):
        WeeklyService(settings, provider, store).run()


def test_old_scan_versions_are_pruned_keeping_the_previous_one(lake) -> None:  # type: ignore[no-untyped-def]
    settings, provider, store = lake
    prefix = DataLakeLayout.weekly_scan_prefix("NSE")
    store.put(prefix + "v=wk-older/part-000.parquet", b"x")
    store.put(prefix + "v=wk-previous/part-000.parquet", b"x")
    store.put(
        DataLakeLayout.weekly_scan_manifest_key("NSE"),
        json.dumps({"weekly_version": "wk-previous"}).encode(),
    )
    result = WeeklyService(settings, provider, store).run()
    versions = {k[len(prefix) :].split("/")[0] for k in store.list(prefix + "v=")}
    assert versions == {f"v={result.weekly_version}", "v=wk-previous"}
