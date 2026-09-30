"""Immutable raw source storage and source metadata (spec §7–9).

Every downloaded file is stored as its **original bytes** under a key that embeds
its SHA-256 content hash, next to an immutable ``.meta.json`` sidecar describing it.
A hash index (``raw/{ex}/_by_hash/{sha256}.json``) answers "which stored file has
this hash?" without listing the lake.

Reading always re-verifies the hash, so a canonical row can be traced to exact,
unaltered bytes: row.source_file_hash → index → raw object → sha256 check.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date, datetime

from chartlens_pipeline.providers.base import RawArtifact
from chartlens_pipeline.storage import DataLakeLayout, ObjectStore, StorageError


class SourceIntegrityError(StorageError):
    """Stored bytes no longer match their recorded hash."""


@dataclass(frozen=True)
class SourceRecord:
    source_id: str
    exchange: str
    source_type: str
    """Exchange-neutral dataset, e.g. ``daily_bars``."""
    source_dataset: str
    """Provider's feed name, e.g. ``bhavcopy``."""
    source_date: date | None
    original_filename: str
    url: str
    content_hash: str
    """SHA-256 of the exact original bytes."""
    byte_size: int
    downloaded_at: datetime
    mime_type: str
    provider: str
    parser_version: str
    """Parser registered for this source when it was stored. The version actually used
    to produce canonical rows is recorded on the rows and in the ingestion manifest."""
    storage_key: str

    def to_json(self) -> bytes:
        payload = asdict(self)
        payload["source_date"] = self.source_date.isoformat() if self.source_date else None
        payload["downloaded_at"] = self.downloaded_at.isoformat()
        return json.dumps(payload, indent=2, sort_keys=True).encode()

    @classmethod
    def from_json(cls, data: bytes) -> SourceRecord:
        payload = json.loads(data)
        payload["source_date"] = (
            date.fromisoformat(payload["source_date"]) if payload["source_date"] else None
        )
        payload["downloaded_at"] = datetime.fromisoformat(payload["downloaded_at"])
        return cls(**payload)


def make_source_id(artifact: RawArtifact) -> str:
    when = artifact.logical_date.isoformat() if artifact.logical_date else "snapshot"
    return f"{artifact.exchange.lower()}:{artifact.source_dataset}:{when}:{artifact.sha256[:16]}"


class RawSourceStore:
    def __init__(self, store: ObjectStore) -> None:
        self._store = store

    def store(self, artifact: RawArtifact, *, parser_version: str) -> tuple[SourceRecord, bool]:
        """Persist ``artifact`` immutably. Returns (record, created). If identical bytes are
        already stored, the original record is returned and nothing is written."""
        existing = self.get_by_hash(artifact.exchange, artifact.sha256)
        if existing is not None:
            return existing, False

        key = DataLakeLayout.raw_key(artifact)
        index_key = DataLakeLayout.source_by_hash_key(artifact.exchange, artifact.sha256)
        meta_key = DataLakeLayout.raw_meta_key(key)
        if self._store.exists(meta_key):
            # A previous run stored the file but crashed before indexing it: repair the index.
            record = SourceRecord.from_json(self._store.get(meta_key))
            self._store.put_immutable(index_key, json.dumps({"storage_key": key}).encode())
            return record, False
        record = SourceRecord(
            source_id=make_source_id(artifact),
            exchange=artifact.exchange,
            source_type=str(artifact.dataset),
            source_dataset=artifact.source_dataset,
            source_date=artifact.logical_date,
            original_filename=artifact.filename,
            url=artifact.url,
            content_hash=artifact.sha256,
            byte_size=len(artifact.content),
            downloaded_at=artifact.fetched_at,
            mime_type=artifact.content_type or "application/octet-stream",
            provider=artifact.provider,
            parser_version=parser_version,
            storage_key=key,
        )
        # Order matters: bytes first, then metadata, then the index. A crash part-way
        # leaves at worst an unindexed file, which the next run re-indexes idempotently.
        self._store.put_immutable(key, artifact.content)
        self._store.put_immutable(meta_key, record.to_json())
        self._store.put_immutable(index_key, json.dumps({"storage_key": key}).encode())
        return record, True

    def get_by_hash(self, exchange: str, sha256: str) -> SourceRecord | None:
        index_key = DataLakeLayout.source_by_hash_key(exchange, sha256)
        if not self._store.exists(index_key):
            return None
        key = json.loads(self._store.get(index_key))["storage_key"]
        return SourceRecord.from_json(self._store.get(DataLakeLayout.raw_meta_key(key)))

    def load(self, record: SourceRecord) -> bytes:
        """The stored bytes, verified against the recorded hash."""
        data = self._store.get(record.storage_key)
        actual = hashlib.sha256(data).hexdigest()
        if actual != record.content_hash:
            raise SourceIntegrityError(
                f"{record.storage_key}: stored sha256 {actual} != recorded {record.content_hash}"
            )
        return data

    def list_for_date(self, exchange: str, source_dataset: str, day: date) -> list[SourceRecord]:
        """All stored versions for a date, oldest download first (ties broken by hash)."""
        prefix = DataLakeLayout.raw_date_prefix(exchange, source_dataset, day)
        records = [
            SourceRecord.from_json(self._store.get(k))
            for k in self._store.list(prefix)
            if k.endswith(".meta.json")
        ]
        return sorted(records, key=lambda r: (r.downloaded_at, r.content_hash))

    def latest_snapshot(self, exchange: str, source_dataset: str) -> SourceRecord | None:
        prefix = DataLakeLayout.raw_snapshot_prefix(exchange, source_dataset)
        metas = [k for k in self._store.list(prefix) if k.endswith(".meta.json")]
        if not metas:
            return None
        records = [SourceRecord.from_json(self._store.get(k)) for k in metas]
        return max(records, key=lambda r: (r.downloaded_at, r.content_hash))
