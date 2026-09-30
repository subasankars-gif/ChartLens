"""Object storage for the data lake (ADR-0002).

The pipeline talks to an :class:`ObjectStore`; the concrete store is chosen by
``storage.backend`` in config. ``LocalObjectStore`` is used for development and
tests; the GCS implementation arrives with ingestion in Milestone 2 and must pass
the same contract tests.

Two write modes, on purpose:

* :meth:`ObjectStore.put_immutable` — for raw source files. Writing identical
  bytes again is a no-op; writing *different* bytes to an existing key is an
  error. Raw data is never overwritten.
* :meth:`ObjectStore.put` — for derived (curated/metadata) objects, which are
  rebuildable and may be replaced.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path, PurePosixPath
from typing import Protocol

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
        if not base.exists():
            return []
        return sorted(
            p.relative_to(self.root).as_posix()
            for p in base.rglob("*")
            if p.is_file() and not p.name.startswith(".tmp-")
        )


class DataLakeLayout:
    """Every object key in the lake is built here and nowhere else.

    ::

        raw/{exchange}/{source_dataset}/{YYYY}/{YYYY-MM-DD}__{sha12}__{filename}
        raw/{exchange}/{source_dataset}/snapshots/{fetched}__{sha12}__{filename}
        curated/daily/{exchange}/{security_id}.parquet
        curated/weekly/{exchange}/{security_id}.parquet
        metadata/security_master/{exchange}/security_master.parquet
        metadata/calendars/{exchange}/calendar.parquet
        metadata/adjustments/{exchange}/adjustment_factors.parquet

    Raw keys embed the content hash, so if an exchange re-issues a file for the
    same date, both versions are kept side by side instead of one replacing the other.
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
    def curated_daily_key(exchange: str, security_id: str) -> str:
        return validate_key(f"curated/daily/{exchange.lower()}/{security_id}.parquet")

    @staticmethod
    def curated_weekly_key(exchange: str, security_id: str) -> str:
        return validate_key(f"curated/weekly/{exchange.lower()}/{security_id}.parquet")

    @staticmethod
    def security_master_key(exchange: str) -> str:
        return validate_key(f"metadata/security_master/{exchange.lower()}/security_master.parquet")

    @staticmethod
    def calendar_key(exchange: str) -> str:
        return validate_key(f"metadata/calendars/{exchange.lower()}/calendar.parquet")

    @staticmethod
    def adjustment_factors_key(exchange: str) -> str:
        return validate_key(f"metadata/adjustments/{exchange.lower()}/adjustment_factors.parquet")
