"""Identity rebuild (ADR-0013).

Identity inputs — identity/universe config, reviewed overrides and the symbol-change
snapshot — are pinned to the security master. Changing any of them (approving the 3i
Infotech ISIN link, adopting a newer symbol-change list) never takes effect silently:
incremental ingestion refuses (``IdentityInputsChanged``) until the master is rebuilt.

A rebuild re-resolves **every stored session from its original bytes** with the current
inputs, starting from an empty master, and rewrites the canonical daily files. Raw data
is never touched, so a rebuild is deterministic and can be repeated.

Because ``security_id`` is immutable, an id the new master no longer contains is never
reused: it gets an *alias* to the surviving security when its identifiers lead to
exactly one, or is retired with its candidates listed — never guessed.

Crash safety: the identity state is first marked ``rebuild_in_progress``; ingestion
refuses to run on that state, and the mark is cleared only when the new master is saved.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, Final

import pyarrow as pa
import pyarrow.parquet as pq

from chartlens_core.config import ChartLensSettings
from chartlens_core.domain import utc_now
from chartlens_core.logs import log_event
from chartlens_pipeline.daily import to_parquet_bytes
from chartlens_pipeline.identity import IdentifierType, IdentityOverrides, SecurityMaster
from chartlens_pipeline.ingest import DateStatus, IngestionService, new_job_id
from chartlens_pipeline.providers.base import ExchangeProvider
from chartlens_pipeline.storage import DataLakeLayout, ObjectStore

log = logging.getLogger("chartlens.pipeline.identity_rebuild")

ALIASES_SCHEMA: Final = pa.schema(
    [
        pa.field("retired_security_id", pa.string(), nullable=False),
        # Surviving security; null when the retired id's identifiers lead nowhere unique.
        pa.field("security_id", pa.string()),
        pa.field("candidates", pa.string(), nullable=False),
        pa.field("reason", pa.string(), nullable=False),
        pa.field("rebuild_id", pa.string(), nullable=False),
        pa.field("decided_at", pa.string(), nullable=False),
    ],
    metadata={b"chartlens.dataset": b"security_aliases", b"chartlens.schema_version": b"1"},
)


@dataclass
class Alias:
    retired_security_id: str
    security_id: str | None
    candidates: list[str]
    reason: str


@dataclass
class RebuildReport:
    rebuild_id: str
    exchange: str
    status: str
    old_inputs: dict[str, str]
    new_inputs: dict[str, str]
    securities_before: int
    securities_after: int
    sessions: int
    aliases: list[Alias] = field(default_factory=list)
    merged_into: dict[str, list[str]] = field(default_factory=dict)
    """Surviving security → retired ids now part of it (e.g. an approved ISIN link)."""
    new_security_ids: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def compute_aliases(old: SecurityMaster, new: SecurityMaster) -> list[Alias]:
    """Map every security id the new master no longer contains, by its own identifiers."""
    aliases: list[Alias] = []
    for sid in sorted(set(old.securities) - set(new.securities)):
        candidates: set[str] = set()
        reason = "ISIN"
        for span in old.spans_for(sid, IdentifierType.ISIN):
            candidates |= new.by_isin(span.value)
        if not candidates:
            reason = "SYMBOL"
            for span in old.spans_for(sid, IdentifierType.SYMBOL):
                candidates |= new.symbol_holders(span.value, span.valid_from)
                candidates |= new.symbol_holders(span.value, span.valid_to)
        target = next(iter(candidates)) if len(candidates) == 1 else None
        if target is None:
            reason = "AMBIGUOUS" if candidates else "NO_SUCCESSOR"
        aliases.append(Alias(sid, target, sorted(candidates), reason))
    return aliases


class IdentityRebuildService:
    def __init__(
        self,
        settings: ChartLensSettings,
        provider: ExchangeProvider,
        store: ObjectStore,
        *,
        overrides: IdentityOverrides | None = None,
        today: Callable[[], date] = lambda: utc_now().date(),
    ) -> None:
        self.settings = settings
        self.provider = provider
        self.exchange = provider.exchange_code
        self.store = store
        self._overrides = overrides
        self._today = today

    def _service(self, *, rebuild: bool) -> IngestionService:
        return IngestionService(
            self.settings,
            self.provider,
            self.store,
            overrides=self._overrides,
            today=self._today,
            rebuild=rebuild,
        )

    def run(self, *, job_id: str | None = None) -> RebuildReport:
        current = self._service(rebuild=False)
        old_master = current.master
        old_state = current.identity_state() or {"inputs": {}}
        manifests = current.manifests()
        processed = [d for d, m in manifests.items() if m.get("status") != DateStatus.NOT_PUBLISHED]
        job_id = job_id or new_job_id()
        rebuild_id = f"{self._today():%Y%m%d}-{job_id}"
        fresh = self._service(rebuild=True)
        report = RebuildReport(
            rebuild_id=rebuild_id,
            exchange=self.exchange,
            status="IN_PROGRESS",
            old_inputs=old_state.get("inputs", {}),
            new_inputs={},
            securities_before=len(old_master.securities),
            securities_after=0,
            sessions=len(processed),
        )
        self._write_record(report)
        if self.store.exists(DataLakeLayout.securities_key(self.exchange)):
            marked = {**old_state, "rebuild_in_progress": rebuild_id}
            self.store.put(
                DataLakeLayout.identity_state_key(self.exchange),
                json.dumps(marked, indent=2, sort_keys=True).encode(),
            )
        log_event(log, "identity_rebuild.start", rebuild_id=rebuild_id, sessions=len(processed))

        if processed:
            backfill = fresh.backfill(min(processed), max(processed), reprocess=True, job_id=job_id)
            report.errors = list(backfill.metrics.errors)
        else:
            fresh.save_master()
        new_master = fresh.master
        report.new_inputs = fresh.identity_inputs()
        report.securities_after = len(new_master.securities)
        report.aliases = compute_aliases(old_master, new_master)
        merged: dict[str, list[str]] = {}
        for a in report.aliases:
            if a.security_id is not None:
                merged.setdefault(a.security_id, []).append(a.retired_security_id)
        report.merged_into = merged
        report.new_security_ids = sorted(set(new_master.securities) - set(old_master.securities))
        self._write_aliases(report)
        report.status = "COMPLETE" if not report.errors else "COMPLETE_WITH_ERRORS"
        self._write_record(report)
        log_event(
            log,
            "identity_rebuild.complete",
            rebuild_id=rebuild_id,
            securities_before=report.securities_before,
            securities_after=report.securities_after,
            aliases=len(report.aliases),
            errors=len(report.errors),
        )
        return report

    def _write_record(self, report: RebuildReport) -> None:
        self.store.put(
            DataLakeLayout.identity_rebuild_key(self.exchange, report.rebuild_id),
            json.dumps(asdict(report), indent=2, sort_keys=True, default=str).encode(),
        )

    def _write_aliases(self, report: RebuildReport) -> None:
        key = DataLakeLayout.security_aliases_key(self.exchange)
        rows: list[dict[str, Any]] = (
            pq.read_table(pa.BufferReader(self.store.get(key))).to_pylist()
            if self.store.exists(key)
            else []
        )
        now = utc_now().isoformat()
        retired = {a.retired_security_id: a for a in report.aliases}
        # Earlier aliases pointing at an id retired now follow it to its successor.
        for r in rows:
            nxt = retired.get(r["security_id"] or "")
            if nxt is not None and nxt.security_id is not None:
                r["security_id"] = nxt.security_id
        rows += [
            {
                "retired_security_id": a.retired_security_id,
                "security_id": a.security_id,
                "candidates": json.dumps(a.candidates),
                "reason": a.reason,
                "rebuild_id": report.rebuild_id,
                "decided_at": now,
            }
            for a in report.aliases
        ]
        self.store.put(key, to_parquet_bytes(pa.Table.from_pylist(rows, schema=ALIASES_SCHEMA)))
