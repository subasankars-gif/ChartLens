from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from chartlens_core.config import StorageConfig
from chartlens_pipeline.providers.base import Dataset, RawArtifact
from chartlens_pipeline.providers.nse.bhavcopy import PARSER_VERSION
from chartlens_pipeline.sources import RawSourceStore, SourceIntegrityError
from chartlens_pipeline.storage import (
    DataLakeLayout,
    GcsObjectStore,
    ImmutableObjectError,
    LocalObjectStore,
    ObjectStore,
    StorageError,
    object_store_from_config,
)

DAY = date(2024, 1, 10)


def artifact(content: bytes, day: date | None = DAY, at: datetime | None = None) -> RawArtifact:
    return RawArtifact(
        exchange="NSE",
        provider="nse",
        dataset=Dataset.DAILY_BARS,
        source_dataset="bhavcopy",
        logical_date=day,
        filename="cm10JAN2024bhav.csv.zip",
        content=content,
        url="https://example.test/cm10JAN2024bhav.csv.zip",
        content_type="application/zip",
        fetched_at=at or datetime(2024, 1, 10, 13, 0, tzinfo=UTC),
    )


# ----------------------------------------------------------------------------- GCS fake


class _PreconditionFailed(Exception):
    pass


class _NotFound(Exception):
    pass


class FakeBlob:
    def __init__(self, bucket: FakeBucket, name: str) -> None:
        self.bucket, self.name = bucket, name

    def upload_from_string(self, data: bytes, if_generation_match: int | None = None) -> None:
        from google.api_core.exceptions import PreconditionFailed

        if if_generation_match == 0 and self.name in self.bucket.objects:
            raise PreconditionFailed("exists")
        self.bucket.objects[self.name] = bytes(data)

    def download_as_bytes(self) -> bytes:
        from google.api_core.exceptions import NotFound

        if self.name not in self.bucket.objects:
            raise NotFound("missing")
        return self.bucket.objects[self.name]

    def exists(self) -> bool:
        return self.name in self.bucket.objects

    def delete(self) -> None:
        from google.api_core.exceptions import NotFound

        if self.bucket.objects.pop(self.name, None) is None:
            raise NotFound("missing")


class FakeBucket:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def blob(self, name: str) -> FakeBlob:
        return FakeBlob(self, name)


class FakeClient:
    def __init__(self) -> None:
        self.bucket_ = FakeBucket()

    def bucket(self, _name: str) -> FakeBucket:
        return self.bucket_

    def list_blobs(self, bucket: FakeBucket, prefix: str) -> list[Any]:
        return [FakeBlob(bucket, n) for n in bucket.objects if n.startswith(prefix)]


@pytest.fixture(params=["local", "gcs"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> ObjectStore:
    if request.param == "local":
        return LocalObjectStore(tmp_path / "lake")
    return GcsObjectStore("chartlens-test", client=FakeClient())


# ---------------------------------------------------------------------------- object store contract


def test_put_immutable_is_idempotent_for_identical_bytes(store: ObjectStore) -> None:
    assert store.put_immutable("raw/x/a.csv", b"abc") is True
    assert store.put_immutable("raw/x/a.csv", b"abc") is False
    assert store.get("raw/x/a.csv") == b"abc"


def test_put_immutable_refuses_different_bytes(store: ObjectStore) -> None:
    store.put_immutable("raw/x/a.csv", b"abc")
    with pytest.raises(ImmutableObjectError):
        store.put_immutable("raw/x/a.csv", b"abd")
    assert store.get("raw/x/a.csv") == b"abc"


def test_put_replaces_derived_objects(store: ObjectStore) -> None:
    store.put("curated/d.parquet", b"v1")
    store.put("curated/d.parquet", b"v2")
    assert store.get("curated/d.parquet") == b"v2"


def test_list_is_sorted_and_prefix_scoped(store: ObjectStore) -> None:
    for key in ("raw/b/2.csv", "raw/a/1.csv", "raw/a/10.csv", "curated/x.parquet"):
        store.put(key, b"")
    assert store.list("raw/") == ["raw/a/1.csv", "raw/a/10.csv", "raw/b/2.csv"]
    assert store.list("raw/a/1") == ["raw/a/1.csv", "raw/a/10.csv"]  # partial-name prefix
    assert store.list("missing/") == []


@pytest.mark.parametrize("key", ["", "/abs", "../escape", "a/../../b", "a\\b"])
def test_invalid_keys_are_rejected(store: ObjectStore, key: str) -> None:
    with pytest.raises(StorageError):
        store.put(key, b"")


def test_derived_objects_can_be_deleted_raw_never(store: ObjectStore) -> None:
    store.put("curated/d.parquet", b"v1")
    store.delete("curated/d.parquet")
    store.delete("curated/d.parquet")  # deleting what is gone is not an error
    assert not store.exists("curated/d.parquet")
    store.put_immutable("raw/x/a.csv", b"abc")
    with pytest.raises(ImmutableObjectError, match="raw source"):
        store.delete("raw/x/a.csv")
    assert store.get("raw/x/a.csv") == b"abc"


def test_get_missing_object(store: ObjectStore) -> None:
    with pytest.raises(StorageError, match="no such object"):
        store.get("nope")
    assert not store.exists("nope")


def test_store_factory(tmp_path: Path) -> None:
    assert isinstance(
        object_store_from_config(StorageConfig(local_root=tmp_path)), LocalObjectStore
    )
    with pytest.raises(StorageError, match="gcs_bucket"):
        object_store_from_config(StorageConfig(backend="gcs"))


# ----------------------------------------------------------------------------- raw sources


def test_content_hash_is_sha256_of_the_original_bytes() -> None:
    content = b"PK\x03\x04 original zip bytes"
    assert artifact(content).sha256 == hashlib.sha256(content).hexdigest()


def test_storage_key_embeds_date_and_content_hash() -> None:
    a = artifact(b"day-file")
    assert (
        DataLakeLayout.raw_key(a)
        == f"raw/nse/bhavcopy/2024/2024-01-10__{a.sha256[:12]}__cm10JAN2024bhav.csv.zip"
    )


def test_store_writes_bytes_metadata_and_hash_index(store: ObjectStore) -> None:
    raw = RawSourceStore(store)
    record, created = raw.store(artifact(b"original"), parser_version=PARSER_VERSION)
    assert created
    assert store.get(record.storage_key) == b"original"
    meta = json.loads(store.get(record.storage_key + ".meta.json"))
    assert meta["content_hash"] == hashlib.sha256(b"original").hexdigest()
    assert meta["byte_size"] == 8 and meta["parser_version"] == PARSER_VERSION
    assert meta["mime_type"] == "application/zip" and meta["source_date"] == "2024-01-10"
    assert raw.get_by_hash("NSE", record.content_hash) == record


def test_storing_identical_content_twice_is_a_no_op(store: ObjectStore) -> None:
    raw = RawSourceStore(store)
    first, created1 = raw.store(artifact(b"same"), parser_version="v1")
    later = datetime(2024, 1, 11, tzinfo=UTC)
    second, created2 = raw.store(artifact(b"same", at=later), parser_version="v1")
    assert (created1, created2) == (True, False)
    assert second == first  # original download metadata is preserved
    assert len(raw.list_for_date("NSE", "bhavcopy", DAY)) == 1


def test_different_content_for_the_same_date_is_kept_side_by_side(store: ObjectStore) -> None:
    raw = RawSourceStore(store)
    a, _ = raw.store(artifact(b"original"), parser_version="v1")
    b, _ = raw.store(
        artifact(b"re-issued", at=datetime(2024, 1, 11, tzinfo=UTC)), parser_version="v1"
    )
    assert a.storage_key != b.storage_key
    versions = raw.list_for_date("NSE", "bhavcopy", DAY)
    assert [v.content_hash for v in versions] == [a.content_hash, b.content_hash]  # oldest first
    assert raw.load(a) == b"original" and raw.load(b) == b"re-issued"


def test_retrieval_by_hash(store: ObjectStore) -> None:
    raw = RawSourceStore(store)
    raw.store(artifact(b"find me"), parser_version="v1")
    found = raw.get_by_hash("NSE", hashlib.sha256(b"find me").hexdigest())
    assert found is not None and raw.load(found) == b"find me"
    assert raw.get_by_hash("NSE", "0" * 64) is None


def test_load_detects_tampered_bytes(tmp_path: Path) -> None:
    local = LocalObjectStore(tmp_path)
    raw = RawSourceStore(local)
    record, _ = raw.store(artifact(b"original"), parser_version="v1")
    (tmp_path / record.storage_key).write_bytes(b"tampered")
    with pytest.raises(SourceIntegrityError):
        raw.load(record)


def test_store_repairs_a_missing_hash_index(tmp_path: Path) -> None:
    """Crash after writing bytes + metadata but before the index: re-storing repairs it."""
    local = LocalObjectStore(tmp_path)
    raw = RawSourceStore(local)
    a = artifact(b"crashy")
    first, _ = raw.store(a, parser_version="v1")
    index = tmp_path / DataLakeLayout.source_by_hash_key("NSE", a.sha256)
    index.unlink()  # the crash
    assert raw.get_by_hash("NSE", a.sha256) is None
    again, created = raw.store(
        artifact(b"crashy", at=datetime(2024, 1, 12, tzinfo=UTC)), parser_version="v1"
    )
    assert not created and again == first  # original metadata kept, nothing overwritten
    assert raw.get_by_hash("NSE", a.sha256) == first


def test_snapshots_are_deduplicated_by_content(store: ObjectStore) -> None:
    raw = RawSourceStore(store)
    s1, _ = raw.store(artifact(b"list-v1", day=None), parser_version="v1")
    s2, created = raw.store(
        artifact(b"list-v1", day=None, at=datetime(2024, 2, 1, tzinfo=UTC)), parser_version="v1"
    )
    assert not created and s2 == s1
    s3, _ = raw.store(
        artifact(b"list-v2", day=None, at=datetime(2024, 3, 1, tzinfo=UTC)), parser_version="v1"
    )
    assert raw.latest_snapshot("NSE", "bhavcopy") == s3
