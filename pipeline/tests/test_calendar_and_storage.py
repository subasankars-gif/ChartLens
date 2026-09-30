from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from chartlens_pipeline.calendar import HolidayCalendar
from chartlens_pipeline.providers.base import Dataset, RawArtifact
from chartlens_pipeline.storage import (
    DataLakeLayout,
    ImmutableObjectError,
    LocalObjectStore,
    StorageError,
)

# --------------------------------------------------------------------------- calendar


def test_calendar_weekdays_holidays_and_special_sessions() -> None:
    holiday = date(2024, 1, 26)  # Friday
    saturday_session = date(2025, 2, 1)
    cal = HolidayCalendar("TEST", holidays=[holiday], special_sessions=[saturday_session])
    assert not cal.is_session(holiday)
    assert cal.is_session(saturday_session)
    assert not cal.is_session(date(2025, 2, 2))  # ordinary Sunday
    assert cal.sessions(date(2024, 1, 22), date(2024, 1, 28)) == [
        date(2024, 1, 22),
        date(2024, 1, 23),
        date(2024, 1, 24),
        date(2024, 1, 25),
    ]


def test_calendar_rejects_contradictory_dates() -> None:
    d = date(2024, 1, 26)
    with pytest.raises(ValueError, match="both holiday and special"):
        HolidayCalendar("TEST", holidays=[d], special_sessions=[d])


# --------------------------------------------------------------------------- storage


@pytest.fixture
def store(tmp_path: Path) -> LocalObjectStore:
    return LocalObjectStore(tmp_path / "lake")


def test_put_immutable_is_idempotent_for_identical_bytes(store: LocalObjectStore) -> None:
    assert store.put_immutable("raw/x/a.csv", b"abc") is True
    assert store.put_immutable("raw/x/a.csv", b"abc") is False
    assert store.get("raw/x/a.csv") == b"abc"


def test_put_immutable_refuses_different_bytes(store: LocalObjectStore) -> None:
    store.put_immutable("raw/x/a.csv", b"abc")
    with pytest.raises(ImmutableObjectError):
        store.put_immutable("raw/x/a.csv", b"abd")
    assert store.get("raw/x/a.csv") == b"abc"


def test_put_replaces_derived_objects(store: LocalObjectStore) -> None:
    store.put("curated/daily/nse/S1.parquet", b"v1")
    store.put("curated/daily/nse/S1.parquet", b"v2")
    assert store.get("curated/daily/nse/S1.parquet") == b"v2"


def test_list_is_sorted_and_prefix_scoped(store: LocalObjectStore) -> None:
    for key in ("raw/b/2.csv", "raw/a/1.csv", "curated/x.parquet"):
        store.put(key, b"")
    assert store.list("raw") == ["raw/a/1.csv", "raw/b/2.csv"]
    assert store.list("missing") == []


@pytest.mark.parametrize("key", ["", "/abs", "../escape", "a/../../b", "a\\b"])
def test_invalid_keys_are_rejected(store: LocalObjectStore, key: str) -> None:
    with pytest.raises(StorageError):
        store.put(key, b"")


def test_get_missing_object(store: LocalObjectStore) -> None:
    with pytest.raises(StorageError, match="no such object"):
        store.get("nope")


# --------------------------------------------------------------------------- layout


def _artifact(content: bytes, logical_date: date | None) -> RawArtifact:
    return RawArtifact(
        exchange="NSE",
        provider="nse",
        dataset=Dataset.DAILY_BARS,
        source_dataset="bhavcopy",
        logical_date=logical_date,
        filename="bhav.csv.zip",
        content=content,
        fetched_at=datetime(2026, 9, 30, 12, 0, tzinfo=UTC),
    )


def test_raw_key_for_dated_artifact() -> None:
    art = _artifact(b"day-file", date(2026, 9, 29))
    assert DataLakeLayout.raw_key(art) == (
        f"raw/nse/bhavcopy/2026/2026-09-29__{art.sha256[:12]}__bhav.csv.zip"
    )


def test_reissued_file_for_same_date_gets_its_own_key() -> None:
    first = _artifact(b"original", date(2026, 9, 29))
    reissue = _artifact(b"corrected", date(2026, 9, 29))
    assert DataLakeLayout.raw_key(first) != DataLakeLayout.raw_key(reissue)


def test_raw_key_for_snapshot() -> None:
    art = _artifact(b"list", None)
    assert DataLakeLayout.raw_key(art).startswith("raw/nse/bhavcopy/snapshots/20260930T120000Z__")


def test_curated_and_metadata_keys() -> None:
    assert DataLakeLayout.curated_weekly_key("NSE", "SEC1") == "curated/weekly/nse/SEC1.parquet"
    assert DataLakeLayout.security_master_key("NSE").startswith("metadata/security_master/nse/")
