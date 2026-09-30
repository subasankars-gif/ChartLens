"""Object storage for the data lake (ADR-0002, ADR-0010).

The pipeline talks to an :class:`ObjectStore`; the concrete store is chosen by
``storage.backend`` in config: :class:`LocalObjectStore` for development and tests,
:class:`GcsObjectStore` in production. Both satisfy the same contract tests.

Two write modes, on purpose:

* :meth:`ObjectStore.put_immutable` — for raw source files and their metadata.
  Writing identical bytes again is a no-op; writing *different* bytes to an existing
  key is an error. Raw data is never overwritten.
* :meth:`ObjectStore.put` — for derived objects (canonical bars, quarantine,
  manifests, security master), which are rebuildable from raw and may be replaced.
"""

from __future__ import annotations

import os
import tempfile
from datetime import date
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Protocol

from chartlens_core.config import StorageConfig

if TYPE_CHECKING:
    from chartlens_pipeline.providers.base import RawArtifact


class StorageError(RuntimeError):
    pass


class ImmutableObjectError(StorageError):
    """Attempt to replace an immutable object with different content."""


class ObjectStore(Protocol):
    def put(self, key: str, data: bytes) -> None: ...

    def put_immutable(self, key: str, data: bytes) -> bool:
        """Write once. Returns True if written, False if identical content already existed."""
        ...

    def get(self, key: str) -> bytes: ...

    def exists(self, key: str) -> bool: ...

    def list(self, prefix: str) -> list[str]:
        """Keys under ``prefix``, sorted."""
        ...


def validate_key(key: str) -> str:
    path = PurePosixPath(key)
    if not key or key.startswith("/") or ".." in path.parts or "\\" in key:
        raise StorageError(f"invalid object key: {key!r}")
    return key


class LocalObjectStore:
    """Filesystem-backed store. Writes are atomic (temp file + rename)."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.root / validate_key(key)

    def _write_atomic(self, path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def put(self, key: str, data: bytes) -> None:
        self._write_atomic(self._path(key), data)

    def put_immutable(self, key: str, data: bytes) -> bool:
        path = self._path(key)
        if path.exists():
            if path.read_bytes() == data:
                return False
            raise ImmutableObjectError(f"refusing to overwrite immutable object {key}")
        self._write_atomic(path, data)
        return True

    def get(self, key: str) -> bytes:
        path = self._path(key)
        if not path.is_file():
            raise StorageError(f"no such object: {key}")
        return path.read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def list(self, prefix: str) -> list[str]:
        base = self.root / prefix if prefix else self.root
        # A prefix may end mid-name (e.g. ".../2024-01-10__"); walk its parent then filter.
        walk_root = base if base.is_dir() else base.parent
        if not walk_root.exists():
            return []
        return sorted(
            key
            for p in walk_root.rglob("*")
            if p.is_file()
            and not p.name.startswith(".tmp-")
            and (key := p.relative_to(self.root).as_posix()).startswith(prefix)
        )


class GcsObjectStore:
    """Google Cloud Storage-backed store.

    Immutability is enforced by GCS itself: ``put_immutable`` uploads with
    ``if_generation_match=0`` (create-only), so two concurrent writers cannot both
    succeed and neither can replace an existing object.
    """

    def __init__(self, bucket: str, client: Any | None = None) -> None:
        if client is None:
            from google.cloud import storage

            client = storage.Client()
        self._client = client
        self._bucket = client.bucket(bucket)

    def put(self, key: str, data: bytes) -> None:
        self._bucket.blob(validate_key(key)).upload_from_string(data)

    def put_immutable(self, key: str, data: bytes) -> bool:
        from google.api_core.exceptions import PreconditionFailed

        blob = self._bucket.blob(validate_key(key))
        try:
            blob.upload_from_string(data, if_generation_match=0)
            return True
        except PreconditionFailed:
            if blob.download_as_bytes() == data:
                return False
            raise ImmutableObjectError(f"refusing to overwrite immutable object {key}") from None

    def get(self, key: str) -> bytes:
        from google.api_core.exceptions import NotFound

        try:
            return bytes(self._bucket.blob(validate_key(key)).download_as_bytes())
        except NotFound:
            raise StorageError(f"no such object: {key}") from None

    def exists(self, key: str) -> bool:
        return bool(self._bucket.blob(validate_key(key)).exists())

    def list(self, prefix: str) -> list[str]:
        return sorted(b.name for b in self._client.list_blobs(self._bucket, prefix=prefix))


def object_store_from_config(config: StorageConfig) -> ObjectStore:
    if config.backend == "gcs":
        if not config.gcs_bucket:
            raise StorageError("storage.backend = gcs requires storage.gcs_bucket")
        return GcsObjectStore(config.gcs_bucket)
    return LocalObjectStore(config.local_root)


class DataLakeLayout:
    """Every object key in the lake is built here and nowhere else.

    ::

        raw/{ex}/{source_dataset}/{YYYY}/{YYYY-MM-DD}__{sha12}__{filename}        original bytes
        raw/{ex}/{source_dataset}/{YYYY}/{YYYY-MM-DD}__{sha12}__{filename}.meta.json
        raw/{ex}/{source_dataset}/snapshots/{fetched}__{sha12}__{filename}         undated sources
        raw/{ex}/_by_hash/{sha256}.json                                            hash → raw key
        curated/daily/exchange={EX}/year={YYYY}/{YYYY-MM-DD}.parquet               canonical bars
        quarantine/daily/exchange={EX}/year={YYYY}/{YYYY-MM-DD}.parquet            rejected rows
        quarantine/conflicts/exchange={EX}/{YYYY-MM-DD}.json                       source conflicts
        metadata/ingestion/{ex}/{YYYY}/{YYYY-MM-DD}.json                           per-date manifest
        metadata/security_master/{ex}/securities.parquet
        metadata/security_master/{ex}/identifier_history.parquet
        metadata/calendars/{ex}/calendar.parquet
        metadata/adjustments/{ex}/adjustment_factors.parquet

    Raw keys embed the content hash, so if an exchange re-issues a file for the same
    date, both versions are kept side by side instead of one replacing the other.
    """

    @staticmethod
    def raw_key(artifact: RawArtifact) -> str:
        exchange = artifact.exchange.lower()
        sha12 = artifact.sha256[:12]
        if artifact.logical_date is not None:
            d = artifact.logical_date
            name = f"{d.isoformat()}__{sha12}__{artifact.filename}"
            return validate_key(f"raw/{exchange}/{artifact.source_dataset}/{d.year:04d}/{name}")
        stamp = artifact.fetched_at.strftime("%Y%m%dT%H%M%SZ")
        name = f"{stamp}__{sha12}__{artifact.filename}"
        return validate_key(f"raw/{exchange}/{artifact.source_dataset}/snapshots/{name}")

    @staticmethod
    def raw_meta_key(raw_key: str) -> str:
        return validate_key(f"{raw_key}.meta.json")

    @staticmethod
    def raw_date_prefix(exchange: str, source_dataset: str, day: date) -> str:
        return f"raw/{exchange.lower()}/{source_dataset}/{day.year:04d}/{day.isoformat()}__"

    @staticmethod
    def raw_snapshot_prefix(exchange: str, source_dataset: str) -> str:
        return f"raw/{exchange.lower()}/{source_dataset}/snapshots/"

    @staticmethod
    def source_by_hash_key(exchange: str, sha256: str) -> str:
        return validate_key(f"raw/{exchange.lower()}/_by_hash/{sha256}.json")

    @staticmethod
    def curated_daily_key(exchange: str, day: date) -> str:
        return validate_key(
            f"curated/daily/exchange={exchange.upper()}/year={day.year:04d}/{day.isoformat()}.parquet"
        )

    @staticmethod
    def curated_daily_prefix(exchange: str) -> str:
        return f"curated/daily/exchange={exchange.upper()}/"

    @staticmethod
    def quarantine_daily_key(exchange: str, day: date) -> str:
        return validate_key(
            f"quarantine/daily/exchange={exchange.upper()}/year={day.year:04d}/{day.isoformat()}.parquet"
        )

    @staticmethod
    def quarantine_daily_prefix(exchange: str) -> str:
        return f"quarantine/daily/exchange={exchange.upper()}/"

    @staticmethod
    def conflict_key(exchange: str, day: date) -> str:
        return validate_key(
            f"quarantine/conflicts/exchange={exchange.upper()}/{day.isoformat()}.json"
        )

    @staticmethod
    def ingestion_manifest_key(exchange: str, day: date) -> str:
        return validate_key(
            f"metadata/ingestion/{exchange.lower()}/{day.year:04d}/{day.isoformat()}.json"
        )

    @staticmethod
    def ingestion_manifest_prefix(exchange: str) -> str:
        return f"metadata/ingestion/{exchange.lower()}/"

    @staticmethod
    def securities_key(exchange: str) -> str:
        return validate_key(f"metadata/security_master/{exchange.lower()}/securities.parquet")

    @staticmethod
    def identifier_history_key(exchange: str) -> str:
        return validate_key(
            f"metadata/security_master/{exchange.lower()}/identifier_history.parquet"
        )

    @staticmethod
    def curated_weekly_key(exchange: str, security_id: str) -> str:
        return validate_key(f"curated/weekly/{exchange.lower()}/{security_id}.parquet")

    @staticmethod
    def calendar_key(exchange: str) -> str:
        return validate_key(f"metadata/calendars/{exchange.lower()}/calendar.parquet")

    @staticmethod
    def adjustment_factors_key(exchange: str) -> str:
        return validate_key(f"metadata/adjustments/{exchange.lower()}/adjustment_factors.parquet")
