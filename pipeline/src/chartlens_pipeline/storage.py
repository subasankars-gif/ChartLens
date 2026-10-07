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

import contextlib
import hashlib
import os
import tempfile
import threading
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


class SwapConflict(StorageError):
    """A conditional replacement found the object changed since it was read."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


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

    def delete(self, key: str) -> None:
        """Remove a derived object. Raw (immutable source) objects can never be deleted."""
        ...

    def swap(self, key: str, data: bytes, expected_sha256: str | None) -> None:
        """Replace ``key`` atomically, and only if its current content hashes to
        ``expected_sha256`` (``None``: only if it does not exist). Otherwise
        :class:`SwapConflict`, with nothing changed. The commit primitive of a publication
        (ADR-0026 §1.5): a reader sees the old object or the new one, never a mixture."""
        ...


def check_deletable(key: str) -> str:
    validate_key(key)
    if key.startswith("raw/"):
        raise ImmutableObjectError(f"refusing to delete raw source object {key}")
    return key


def validate_key(key: str) -> str:
    path = PurePosixPath(key)
    if not key or key.startswith("/") or ".." in path.parts or "\\" in key:
        raise StorageError(f"invalid object key: {key!r}")
    return key


_SWAP_LOCK = threading.Lock()


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

    def delete(self, key: str) -> None:
        self._path(check_deletable(key)).unlink(missing_ok=True)

    def swap(self, key: str, data: bytes, expected_sha256: str | None) -> None:
        path = self._path(key)
        with _SWAP_LOCK:  # one process: a lock, a comparison, then an atomic rename
            current = _sha256(path.read_bytes()) if path.is_file() else None
            if current != expected_sha256:
                raise SwapConflict(f"{key} changed since it was read")
            self._write_atomic(path, data)


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

    def delete(self, key: str) -> None:
        from google.api_core.exceptions import NotFound

        with contextlib.suppress(NotFound):
            self._bucket.blob(check_deletable(key)).delete()

    def swap(self, key: str, data: bytes, expected_sha256: str | None) -> None:
        """Compare-and-swap on the object's generation: GCS replaces an object atomically,
        and the precondition makes the write fail if anyone replaced it meanwhile."""
        from google.api_core.exceptions import PreconditionFailed

        blob = self._bucket.get_blob(validate_key(key))
        if blob is None:
            if expected_sha256 is not None:
                raise SwapConflict(f"{key} no longer exists")
            generation = 0
        else:
            generation = int(blob.generation)
            current = bytes(blob.download_as_bytes(if_generation_match=generation))
            if _sha256(current) != expected_sha256:
                raise SwapConflict(f"{key} changed since it was read")
        try:
            self._bucket.blob(key).upload_from_string(data, if_generation_match=generation)
        except PreconditionFailed:
            raise SwapConflict(f"{key} was replaced during the swap") from None


def object_store_from_config(config: StorageConfig) -> ObjectStore:
    if config.backend == "gcs":
        if not config.gcs_bucket:
            raise StorageError("storage.backend = gcs requires storage.gcs_bucket")
        return GcsObjectStore(config.gcs_bucket)
    return LocalObjectStore(config.local_root)


def _require_sha(sha256: str) -> None:
    if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
        raise StorageError(f"not a SHA-256: {sha256!r}")


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
        metadata/data_quality/{ex}/continuity_segments.parquet                     segments
        curated/adjusted/exchange={EX}/{security_id}.parquet (+ _manifest.json)    adjusted daily
        curated/weekly/exchange={EX}/{security_id}.parquet (+ _manifest.json)      weekly bars
        curated/weekly_scan/exchange={EX}/v={version}/part-NNN.parquet (+ _manifest.json)
        curated/serving/exchange={EX}/v={meta_version}/{name}.parquet (+ _manifest.json)
        curated/serving/exchange={EX}/weekly/{sha256}.parquet   immutable weekly copies (ADR-0018)
        curated/serving/exchange={EX}/analysis/{sha256}.json.gz         analysis (ADR-0025)
        curated/serving/exchange={EX}/events/{dataset}/{sha256}.parquet breakout events
        curated/analysis/exchange={EX}/_manifest.json                  latest analysis manifest

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
    def identity_state_key(exchange: str) -> str:
        return validate_key(f"metadata/security_master/{exchange.lower()}/identity_state.json")

    @staticmethod
    def identifier_history_key(exchange: str) -> str:
        return validate_key(
            f"metadata/security_master/{exchange.lower()}/identifier_history.parquet"
        )

    @staticmethod
    def security_aliases_key(exchange: str) -> str:
        """Retired security_id → surviving security_id, accumulated over identity rebuilds."""
        return validate_key(f"metadata/security_master/{exchange.lower()}/aliases.parquet")

    @staticmethod
    def identity_rebuild_key(exchange: str, rebuild_id: str) -> str:
        return validate_key(
            f"metadata/security_master/{exchange.lower()}/rebuilds/{rebuild_id}.json"
        )

    @staticmethod
    def adjusted_daily_key(exchange: str, security_id: str) -> str:
        return validate_key(f"curated/adjusted/exchange={exchange.upper()}/{security_id}.parquet")

    @staticmethod
    def adjusted_daily_prefix(exchange: str) -> str:
        return f"curated/adjusted/exchange={exchange.upper()}/"

    @staticmethod
    def adjusted_manifest_key(exchange: str) -> str:
        """Written last: the adjustment version and the content hash of every current file."""
        return validate_key(f"curated/adjusted/exchange={exchange.upper()}/_manifest.json")

    @staticmethod
    def corporate_actions_table_key(exchange: str) -> str:
        return validate_key(f"metadata/corporate_actions/{exchange.lower()}/actions.parquet")

    @staticmethod
    def adjustment_events_key(exchange: str) -> str:
        return validate_key(f"metadata/adjustments/{exchange.lower()}/events.parquet")

    @staticmethod
    def unexplained_gaps_key(exchange: str) -> str:
        return validate_key(f"metadata/adjustments/{exchange.lower()}/unexplained_gaps.parquet")

    @staticmethod
    def adjustment_report_key(exchange: str) -> str:
        return validate_key(f"metadata/adjustments/{exchange.lower()}/report.json")

    @staticmethod
    def data_quality_findings_key(exchange: str) -> str:
        return validate_key(f"metadata/data_quality/{exchange.lower()}/findings.parquet")

    @staticmethod
    def data_quality_status_key(exchange: str) -> str:
        return validate_key(f"metadata/data_quality/{exchange.lower()}/status.parquet")

    @staticmethod
    def data_quality_report_key(exchange: str) -> str:
        return validate_key(f"metadata/data_quality/{exchange.lower()}/report.json")

    @staticmethod
    def continuity_segments_key(exchange: str) -> str:
        """One row per continuity segment of every security (ADR-0014)."""
        return validate_key(f"metadata/data_quality/{exchange.lower()}/continuity_segments.parquet")

    @staticmethod
    def curated_weekly_key(exchange: str, security_id: str) -> str:
        return validate_key(f"curated/weekly/exchange={exchange.upper()}/{security_id}.parquet")

    @staticmethod
    def curated_weekly_prefix(exchange: str) -> str:
        return f"curated/weekly/exchange={exchange.upper()}/"

    @staticmethod
    def weekly_manifest_key(exchange: str) -> str:
        """Written last: the weekly version and the content hash of every per-security file."""
        return validate_key(f"curated/weekly/exchange={exchange.upper()}/_manifest.json")

    @staticmethod
    def weekly_scan_prefix(exchange: str) -> str:
        return f"curated/weekly_scan/exchange={exchange.upper()}/"

    @staticmethod
    def weekly_scan_part_key(exchange: str, weekly_version: str, part: int) -> str:
        """Parts live under their version, so a new version never overwrites what a reader
        of the previous manifest is reading."""
        return validate_key(
            f"curated/weekly_scan/exchange={exchange.upper()}/v={weekly_version}/"
            f"part-{part:03d}.parquet"
        )

    @staticmethod
    def weekly_scan_manifest_key(exchange: str) -> str:
        return validate_key(f"curated/weekly_scan/exchange={exchange.upper()}/_manifest.json")

    @staticmethod
    def serving_prefix(exchange: str) -> str:
        return f"curated/serving/exchange={exchange.upper()}/"

    @staticmethod
    def serving_file_key(exchange: str, meta_version: str, name: str) -> str:
        """Snapshot files live under their version: a published snapshot never changes."""
        return validate_key(
            f"curated/serving/exchange={exchange.upper()}/v={meta_version}/{name}.parquet"
        )

    @staticmethod
    def serving_analysis_manifest_key(exchange: str, meta_version: str) -> str:
        """The verbatim copy of the analysis manifest a schema-3 snapshot pins (ADR-0026)."""
        return validate_key(
            f"curated/serving/exchange={exchange.upper()}/v={meta_version}/analysis_manifest.json"
        )

    @staticmethod
    def serving_version_manifest_key(exchange: str, meta_version: str) -> str:
        """A copy of the snapshot's manifest, kept with its files (ADR-0018)."""
        return validate_key(
            f"curated/serving/exchange={exchange.upper()}/v={meta_version}/manifest.json"
        )

    @staticmethod
    def serving_weekly_prefix(exchange: str) -> str:
        return f"curated/serving/exchange={exchange.upper()}/weekly/"

    @staticmethod
    def serving_weekly_key(exchange: str, sha256: str) -> str:
        """An immutable copy of a weekly file, named by its content (ADR-0018). The API
        reads weekly bars only from here, so no later run can change what is served."""
        _require_sha(sha256)
        return validate_key(f"curated/serving/exchange={exchange.upper()}/weekly/{sha256}.parquet")

    @staticmethod
    def serving_analysis_prefix(exchange: str) -> str:
        return f"curated/serving/exchange={exchange.upper()}/analysis/"

    @staticmethod
    def serving_analysis_key(exchange: str, sha256: str) -> str:
        """A gzip-compressed analysis document, named by the SHA-256 of its uncompressed
        canonical bytes (ADR-0024 §4.1, ADR-0025 §5)."""
        _require_sha(sha256)
        return validate_key(f"{DataLakeLayout.serving_analysis_prefix(exchange)}{sha256}.json.gz")

    @staticmethod
    def serving_events_prefix(exchange: str, dataset: str) -> str:
        if dataset not in ("pattern_breakouts", "level_breakouts"):
            raise StorageError(f"unknown event dataset {dataset!r}")
        return f"curated/serving/exchange={exchange.upper()}/events/{dataset}/"

    @staticmethod
    def serving_events_key(exchange: str, dataset: str, sha256: str) -> str:
        """An event file, named by the content hash of its rows (ADR-0025 §5)."""
        _require_sha(sha256)
        prefix = DataLakeLayout.serving_events_prefix(exchange, dataset)
        return validate_key(f"{prefix}{sha256}.parquet")

    @staticmethod
    def analysis_manifest_key(exchange: str) -> str:
        """The latest complete analysis manifest, written last by the ANALYSIS stage."""
        return validate_key(f"curated/analysis/exchange={exchange.upper()}/_manifest.json")

    @staticmethod
    def serving_manifest_key(exchange: str) -> str:
        """Points at the current serving snapshot; written last (ADR-0016)."""
        return validate_key(f"curated/serving/exchange={exchange.upper()}/_manifest.json")

    @staticmethod
    def calendar_key(exchange: str) -> str:
        return validate_key(f"metadata/calendars/{exchange.lower()}/calendar.parquet")

    @staticmethod
    def adjustment_factors_key(exchange: str) -> str:
        return validate_key(f"metadata/adjustments/{exchange.lower()}/adjustment_factors.parquet")
