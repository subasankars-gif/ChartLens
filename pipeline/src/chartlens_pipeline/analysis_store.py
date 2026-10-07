"""Persistence mechanics for analysis artifacts (ADR-0024 §4.1, ADR-0025 §4–§5).

Everything the ANALYSIS stage (job layer) writes and PUBLISH_SERVING must validate, with
no engine or job-layer import:

- **the universe rule** (``UNIVERSE_RULE_VERSION``) and its canonical hash;
- **documents:** gzip-compressed (level 6, ``mtime=0``, no file name), named by the
  SHA-256 of the *uncompressed* canonical bytes. Compression is non-semantic: a reader
  always checks the decompressed bytes against the name;
- **event files:** Parquet with an explicit schema per dataset, named by their content
  hash (identifying metadata + canonical rows); decoding returns exactly the metadata
  and rows that were encoded, so the content hash is reproducible from the file. The
  byte hash is physical integrity only;
- **the analysis manifest:** canonical JSON, no timestamps, so the same inputs give
  the same bytes;
- **idempotent writes:** an address that already exists is accepted only if the object
  there decodes to the same logical content; anything else is a conflict.

Nothing here computes, selects or interprets analysis.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import zlib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any, Final, Literal

import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import BaseModel, ConfigDict

from chartlens_core.canonical import (
    DATASET_CONTENT_KEY,
    canonical_json,
    content_hash,
    dataset_content_hash,
)
from chartlens_core.domain import DataQualityStatus
from chartlens_pipeline.storage import (
    DataLakeLayout,
    ImmutableObjectError,
    ObjectStore,
    StorageError,
    validate_key,
)

UNIVERSE_RULE_VERSION: Final = "1"
MANIFEST_SCHEMA_VERSION: Final = 1
GZIP_LEVEL: Final = 6
Dataset = Literal["pattern_breakouts", "level_breakouts"]
EVENT_DATASETS: Final[tuple[Dataset, ...]] = ("pattern_breakouts", "level_breakouts")
ANALYSED_STATUSES: Final = frozenset(
    {DataQualityStatus.USABLE.value, DataQualityStatus.USABLE_WITH_WARNINGS.value}
)

Row = dict[str, Any]


class AnalysisStoreError(RuntimeError):
    """An analysis artifact or its inputs are not what they must be."""


class ArtifactConflict(AnalysisStoreError):
    """An object at a content address holds different logical content."""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ----------------------------------------------------------------------------- universe


def analysis_universe(status_rows: Iterable[Mapping[str, Any]], dq_version: str) -> list[str]:
    """ADR-0025 §4: ``analytical_universe`` and status USABLE or USABLE_WITH_WARNINGS, from
    the data-quality status of exactly ``dq_version``. Sorted, without duplicates."""
    ids: list[str] = []
    for row in status_rows:
        if row["dq_version"] != dq_version:
            raise AnalysisStoreError(
                f"data-quality status of {row['security_id']} is {row['dq_version']}, "
                f"not {dq_version}"
            )
        if row["analytical_universe"] and row["status"] in ANALYSED_STATUSES:
            ids.append(str(row["security_id"]))
    if len(set(ids)) != len(ids):
        raise AnalysisStoreError("a security appears twice in the data-quality status")
    return sorted(ids)


def universe_sha256(security_ids: Iterable[str]) -> str:
    """The universe is a set: its canonical form is the sorted list of ids."""
    return content_hash(sorted(security_ids))


# ----------------------------------------------------------------------------- idempotent writes


def _put_logical(
    store: ObjectStore, key: str, data: bytes, same_content: Callable[[bytes], bool]
) -> bool:
    """Create-only. True if written; False if the address already held the same logical
    content (left as it is); :class:`ArtifactConflict` otherwise."""
    try:
        return store.put_immutable(key, data)
    except ImmutableObjectError:
        if same_content(store.get(key)):
            return False
        raise ArtifactConflict(f"{key} holds different content") from None


# ----------------------------------------------------------------------------- documents


def compress_document(canonical: bytes) -> bytes:
    """Deterministic gzip: no timestamp, no file name. The bytes are physical only."""
    return gzip.compress(canonical, compresslevel=GZIP_LEVEL, mtime=0)


def decompress_document(data: bytes) -> bytes:
    return gzip.decompress(data)


def write_document(store: ObjectStore, exchange: str, canonical: bytes) -> str:
    """Store a document under the SHA-256 of its uncompressed canonical bytes; returns
    that address. Never the hash of the compressed bytes."""
    address = _sha(canonical)

    def same(existing: bytes) -> bool:
        return _sha(decompress_document(existing)) == address

    key = DataLakeLayout.serving_analysis_key(exchange, address)
    _put_logical(store, key, compress_document(canonical), same)
    return address


def read_document(store: ObjectStore, exchange: str, address: str) -> bytes:
    """The canonical bytes, verified against their address."""
    canonical = decompress_document(
        store.get(DataLakeLayout.serving_analysis_key(exchange, address))
    )
    if _sha(canonical) != address:
        raise AnalysisStoreError(f"analysis document {address} does not match its address")
    return canonical


# ----------------------------------------------------------------------------- event files

_F64 = pa.float64()
_MEASURED = pa.map_(pa.string(), _F64)


def _req(name: str, t: pa.DataType) -> pa.Field[Any]:
    return pa.field(name, t, nullable=False)


def _opt(name: str, t: pa.DataType) -> pa.Field[Any]:
    return pa.field(name, t, nullable=True)


_FOLLOW_UP = pa.struct(
    [
        _req("kind", pa.string()),
        _req("effective_date", pa.date32()),
        _req("known_at", pa.date32()),
        _req("authority", pa.string()),
        _opt("source_outcome_ref", pa.string()),
        _req("measured_values", _MEASURED),
        _req("provisional", pa.bool_()),
    ]
)
_BAR_VOLUME = pa.struct(
    [
        _req("bar_date", pa.date32()),
        _req("volume", _F64),
        _req("baseline_bars", pa.int64()),
        _opt("baseline_mean_volume", _F64),
        _opt("rvol", _F64),
        _opt("classification", pa.string()),
        _req("expansion_threshold", _F64),
        _req("contraction_threshold", _F64),
        _req("evidence_refs", pa.list_(pa.string())),
        _req("measurement_version", pa.string()),
    ]
)
_COMMON = [
    _req("event_id", pa.string()),
    _req("event_key", pa.string()),
    _req("security_id", pa.string()),
    _req("source_version", pa.string()),
    _req("source_id", pa.string()),
    _req("source_event_ref", pa.string()),
    _req("continuity_segment_id", pa.string()),
    _req("direction", pa.string()),
    _req("bar_date", pa.date32()),
    _req("known_at", pa.date32()),
    _req("level_at_break", _F64),
    _opt("reference_atr", _F64),
    _opt("retest_band", _F64),
    _req("reversal_window_bars", pa.int64()),
    _req("retest_window_bars", pa.int64()),
    _req("observation_bars", pa.int64()),
    _req("source_measured_values", _MEASURED),
    _opt("bar_volume", _BAR_VOLUME),
    _req("provisional", pa.bool_()),
    _req("history", pa.list_(_FOLLOW_UP)),
    _req("methodology_version", pa.string()),
    _req("source_type", pa.string()),
]
EVENT_SCHEMAS: Final[dict[Dataset, pa.Schema]] = {
    "pattern_breakouts": pa.schema(
        [*_COMMON, _req("pattern_type", pa.string()), _req("family", pa.string())]
    ),
    "level_breakouts": pa.schema([*_COMMON, _req("level_source_type", pa.string())]),
}
"""Explicit, one per dataset; ``test_analysis_store`` holds them to the engine's models."""


def _to_arrow(value: Any, t: pa.DataType, path: str) -> Any:
    if value is None:
        return None
    if pa.types.is_date32(t):
        return date.fromisoformat(value)
    if pa.types.is_map(t):
        return [(k, _to_arrow(v, t.item_type, f"{path}.{k}")) for k, v in value.items()]
    if pa.types.is_struct(t):
        names = [t.field(i).name for i in range(t.num_fields)]
        if set(value) != set(names):
            raise AnalysisStoreError(f"{path}: fields {sorted(value)} != schema {sorted(names)}")
        return {n: _to_arrow(value[n], t.field(i).type, f"{path}.{n}") for i, n in enumerate(names)}
    if pa.types.is_list(t):
        return [_to_arrow(v, t.value_type, f"{path}[]") for v in value]
    return value


def _from_arrow(value: Any, t: pa.DataType) -> Any:
    if value is None:
        return None
    if pa.types.is_date32(t):
        return value.isoformat()
    if pa.types.is_map(t):
        return {k: _from_arrow(v, t.item_type) for k, v in value}
    if pa.types.is_struct(t):
        return {
            t.field(i).name: _from_arrow(value[t.field(i).name], t.field(i).type)
            for i in range(t.num_fields)
        }
    if pa.types.is_list(t):
        return [_from_arrow(v, t.value_type) for v in value]
    if pa.types.is_floating(t):
        return float(value)
    return value


def encode_events(dataset: Dataset, rows: list[Row], metadata: Mapping[str, str]) -> bytes:
    """Rows (the engine's events in JSON mode) → Parquet: explicit schema, zstd, one row
    group. Physical bytes only; identity is the rows' content hash."""
    schema = EVENT_SCHEMAS[dataset]
    struct = pa.struct(list(schema))
    arrow_rows = [_to_arrow(r, struct, f"{dataset}[{i}]") for i, r in enumerate(rows)]
    meta = {k.encode(): v.encode() for k, v in sorted(metadata.items())}
    table = pa.Table.from_pylist(arrow_rows, schema=schema.with_metadata(meta))
    sink = io.BytesIO()
    pq.write_table(table, sink, compression="zstd", row_group_size=max(1, table.num_rows))
    return sink.getvalue()


def decode_events(data: bytes) -> tuple[Dataset, list[Row], dict[str, str]]:
    """Parquet → exactly the rows that were encoded, and the file metadata."""
    table = pq.read_table(pa.BufferReader(data))
    meta = {k.decode(): v.decode() for k, v in (table.schema.metadata or {}).items()}
    dataset = meta.get("chartlens.dataset")
    if dataset not in EVENT_SCHEMAS:
        raise AnalysisStoreError(f"not an event file (dataset {dataset!r})")
    schema = EVENT_SCHEMAS[dataset]  # type: ignore[index]
    if not table.schema.equals(schema, check_metadata=False):
        raise AnalysisStoreError(f"{dataset} file does not have the {dataset} schema")
    struct = pa.struct(list(schema))
    rows = [_from_arrow(r, struct) for r in table.to_pylist()]
    return dataset, rows, meta  # type: ignore[return-value]


def write_events(
    store: ObjectStore,
    exchange: str,
    dataset: Dataset,
    rows: list[Row],
    metadata: Mapping[str, str],
) -> tuple[str, str]:
    """Store an event file under its content hash (identifying metadata + rows, ADR-0025
    §5). Returns (content hash, the file's byte SHA-256 for physical integrity)."""
    digest = dataset_content_hash(metadata, rows)
    if metadata.get(DATASET_CONTENT_KEY, digest) != digest:
        raise AnalysisStoreError(f"{dataset}: metadata and rows do not match its content hash")
    if metadata.get("chartlens.dataset") != dataset:
        raise AnalysisStoreError(f"{dataset}: metadata names another dataset")
    data = encode_events(dataset, rows, {**metadata, DATASET_CONTENT_KEY: digest})
    if _event_content(data)[1] != digest:
        raise AnalysisStoreError(f"{dataset}: the encoded file does not reproduce its content")

    def same(existing: bytes) -> bool:
        return _event_content(existing)[1] == digest

    key = DataLakeLayout.serving_events_key(exchange, dataset, digest)
    if _put_logical(store, key, data, same):
        return digest, _sha(data)
    return digest, _sha(store.get(key))  # an equal file was already there: its bytes


def _event_content(data: bytes) -> tuple[Dataset, str, list[Row]]:
    dataset, rows, meta = decode_events(data)
    return dataset, dataset_content_hash(meta, rows), rows


def read_events(
    store: ObjectStore, exchange: str, dataset: Dataset, content_sha256: str
) -> tuple[list[Row], str]:
    """The rows, verified against their content hash, and the file's byte SHA-256."""
    data = store.get(DataLakeLayout.serving_events_key(exchange, dataset, content_sha256))
    found, digest, rows = _event_content(data)
    if found != dataset or digest != content_sha256:
        raise AnalysisStoreError(f"{dataset} file {content_sha256} does not match its address")
    return rows, _sha(data)


# ----------------------------------------------------------------------------- inspection


@dataclass(frozen=True)
class ArtifactProblem:
    """Why an artifact cannot be used. ``corrupt``: the object does not match its own
    content address (quarantine it); ``mismatch``: a valid object that differs from what
    an entry records; ``missing``: no object."""

    key: str
    kind: Literal["missing", "corrupt", "mismatch"]
    detail: str


def inspect_document(
    store: ObjectStore, exchange: str, address: str
) -> tuple[bytes | None, ArtifactProblem | None]:
    """The document's canonical bytes if it is present and matches its address."""
    key = DataLakeLayout.serving_analysis_key(exchange, address)
    try:
        data = store.get(key)
    except StorageError:
        return None, ArtifactProblem(key, "missing", "no object")
    try:
        canonical = decompress_document(data)
    except (OSError, EOFError, zlib.error) as exc:
        return None, ArtifactProblem(key, "corrupt", f"not gzip ({exc})")
    if _sha(canonical) != address:
        return None, ArtifactProblem(key, "corrupt", "content does not hash to its address")
    return canonical, None


def inspect_events(
    store: ObjectStore, exchange: str, dataset: Dataset, artifact: EventArtifact
) -> tuple[tuple[list[Row], dict[str, str]] | None, ArtifactProblem | None]:
    """Rows and metadata if the file is present, matches its content address
    (logically) and the recorded bytes and row count (physically)."""
    key = DataLakeLayout.serving_events_key(exchange, dataset, artifact.content_sha256)
    try:
        data = store.get(key)
    except StorageError:
        return None, ArtifactProblem(key, "missing", "no object")
    try:
        found, rows, meta = decode_events(data)
    except Exception as exc:
        return None, ArtifactProblem(key, "corrupt", f"not a readable event file ({exc})")
    if found != dataset or dataset_content_hash(meta, rows) != artifact.content_sha256:
        return None, ArtifactProblem(key, "corrupt", "content does not hash to its address")
    if _sha(data) != artifact.physical_sha256:
        return None, ArtifactProblem(
            key, "mismatch", "bytes differ from the recorded physical hash"
        )
    if len(rows) != artifact.row_count:
        return None, ArtifactProblem(key, "mismatch", "row count differs from the entry")
    return (rows, meta), None


def entry_problems(
    store: ObjectStore, exchange: str, entry: AnalysisEntry
) -> list[ArtifactProblem]:
    """Every problem with an entry's artifacts, logical and physical (ADR-0026 §1.4)."""
    problems: list[ArtifactProblem] = []
    _, problem = inspect_document(store, exchange, entry.document_sha256)
    problems += [problem] if problem else []
    for name in EVENT_DATASETS:
        artifact = entry.events.get(name)
        if artifact is None:
            problems.append(ArtifactProblem(name, "mismatch", "the entry has no such dataset"))
            continue
        _, problem = inspect_events(store, exchange, name, artifact)
        problems += [problem] if problem else []
    return problems


def quarantine(store: ObjectStore, key: str) -> str:
    """Move a corrupt object out of its content address: its bytes are first kept,
    unchanged, under ``quarantine/`` (named by their own hash), then removed from the
    address, so the expected object can be written there (ADR-0026 §1.4). Returns the
    quarantine key."""
    data = store.get(key)
    held = validate_key(f"quarantine/{key}.{_sha(data)}")
    store.put_immutable(held, data)
    store.delete(key)
    return held


# ----------------------------------------------------------------------------- manifest


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EventArtifact(_Model):
    content_sha256: str
    """Logical identity: the canonical hash of the rows (the file's name)."""
    physical_sha256: str
    """Physical integrity: the SHA-256 of the Parquet bytes."""
    row_count: int


class AnalysisEntry(_Model):
    security_id: str
    continuity_segment_id: str
    reuse_key: str
    weekly_file_sha256: str
    """Physical provenance: the weekly file the bars were read from (stale-input checks)."""
    bars_sha256: str
    document_sha256: str
    events: dict[str, EventArtifact]


class AnalysisManifest(_Model):
    manifest_schema_version: int = MANIFEST_SCHEMA_VERSION
    exchange: str
    weekly_version: str
    dq_version: str
    as_of: str
    methodology_hash: str
    analysis_version: str
    analysis_methodology_hash: str
    document_schema_version: str
    canonical_serialization_version: str
    event_schema_version: str
    runtime: dict[str, str]
    reuse_key_version: str
    universe_rule_version: str
    reuse_validation_sample_size: int
    reuse_validation_selection_version: str
    universe: list[str]
    universe_sha256: str
    entries: list[AnalysisEntry]
    """One per security of the universe, by ``security_id``."""
    analysis_set_hash: str


def analysis_set_hash(entries: list[AnalysisEntry]) -> str:
    return content_hash([e.model_dump(mode="json") for e in entries])


def manifest_bytes(manifest: AnalysisManifest) -> bytes:
    return canonical_json(manifest.model_dump(mode="json"))


def read_manifest(store: ObjectStore, exchange: str) -> AnalysisManifest | None:
    key = DataLakeLayout.analysis_manifest_key(exchange)
    if not store.exists(key):
        return None
    manifest = AnalysisManifest.model_validate(json.loads(store.get(key)))
    if analysis_set_hash(manifest.entries) != manifest.analysis_set_hash:
        raise AnalysisStoreError("the analysis manifest does not match its set hash")
    return manifest


def write_manifest(store: ObjectStore, manifest: AnalysisManifest) -> None:
    """Validated, then written last (ADR-0025 §4–§5)."""
    ids = [e.security_id for e in manifest.entries]
    if ids != sorted(set(ids)):
        raise AnalysisStoreError("manifest entries must be unique and ordered by security")
    if ids != manifest.universe or universe_sha256(ids) != manifest.universe_sha256:
        raise AnalysisStoreError("manifest entries do not cover exactly its universe")
    if analysis_set_hash(manifest.entries) != manifest.analysis_set_hash:
        raise AnalysisStoreError("analysis_set_hash does not match the entries")
    store.put(DataLakeLayout.analysis_manifest_key(manifest.exchange), manifest_bytes(manifest))


# ----------------------------------------------------------------------------- inputs


class AnalysisInputSet:
    """The published inputs of one weekly version, read together and checked for being in
    step (the weekly manifest, its data-quality status and continuity segments)."""

    def __init__(self, store: ObjectStore, exchange: str) -> None:
        ex = exchange
        weekly_key = DataLakeLayout.weekly_manifest_key(ex)
        if not store.exists(weekly_key):
            raise AnalysisStoreError("no published weekly dataset")
        self.weekly: dict[str, Any] = json.loads(store.get(weekly_key))
        report = json.loads(store.get(DataLakeLayout.data_quality_report_key(ex)))
        if report["dq_version"] != self.weekly["dq_version"]:
            raise AnalysisStoreError(
                f"weekly was built on {self.weekly['dq_version']}, data quality is now "
                f"{report['dq_version']}"
            )
        self.status = {
            r["security_id"]: r
            for r in pq.read_table(
                pa.BufferReader(store.get(DataLakeLayout.data_quality_status_key(ex)))
            ).to_pylist()
        }
        segments: dict[str, list[dict[str, Any]]] = {}
        for r in pq.read_table(
            pa.BufferReader(store.get(DataLakeLayout.continuity_segments_key(ex)))
        ).to_pylist():
            segments.setdefault(r["security_id"], []).append(r)
        self.current_segment = {
            sid: max(rows, key=lambda r: r["segment_start"]) for sid, rows in segments.items()
        }
        self.exchange = ex

    @property
    def dq_version(self) -> str:
        return str(self.weekly["dq_version"])

    @property
    def files(self) -> dict[str, str]:
        return dict(self.weekly["files"])

    def universe(self) -> list[str]:
        return analysis_universe(self.status.values(), self.dq_version)
