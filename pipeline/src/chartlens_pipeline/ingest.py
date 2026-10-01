"""Daily-bar ingestion and backfill (spec §20–26, ADR-0010).

For each expected session, in **descending date order** (so identity anchors from
the ISIN era exist before pre-ISIN rows are resolved)::

    locate/download original file → store immutable bytes (hash = identity)
    → parse (verified bytes) → compare other stored versions (conflicts)
    → resolve securities → write security master → write canonical Parquet
    → write quarantine → write manifest (commit marker, written last)

A date whose manifest is missing or stale is (re)processed; a date whose manifest is
current is skipped as "already ingested". Crashing mid-date is safe: the date has no
fresh manifest and is simply processed again, producing identical results.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import logging
import os
import time
import uuid
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from chartlens_core.config import ChartLensSettings, config_dir
from chartlens_core.domain import utc_now
from chartlens_core.logs import log_event
from chartlens_pipeline.calendar import CalendarEvidence, TradingCalendar
from chartlens_pipeline.daily import (
    DAILY_BAR_SCHEMA_VERSION,
    IDENTITY_REASONS,
    NormalizedRow,
    ParsedDaily,
    QuarantinedRow,
    QuarantineReason,
    SourceParseError,
    daily_bar_schema,
    quarantine_schema,
    to_parquet_bytes,
)
from chartlens_pipeline.identity import (
    IDENTIFIER_HISTORY_SCHEMA,
    SECURITIES_SCHEMA,
    IdentityOverrides,
    Observation,
    SecurityMaster,
    SymbolChangeNotice,
)
from chartlens_pipeline.providers.base import DownloadStatus, ExchangeProvider
from chartlens_pipeline.sources import RawSourceStore, SourceRecord
from chartlens_pipeline.storage import DataLakeLayout, ObjectStore

log = logging.getLogger("chartlens.pipeline.ingest")

RECHECK_UNPUBLISHED_DAYS = 7
"""A date that was 'not published' is re-checked while it is this recent (files can appear late)."""


class IdentityInputsChanged(RuntimeError):
    """The identity inputs differ from the ones the existing security master was built with.
    Continuing would mix two identity states, so incremental processing refuses."""


class DateStatus(StrEnum):
    INGESTED = "INGESTED"
    QUARANTINED = "QUARANTINED"
    """The file parsed, but an abnormal share of its rows was rejected (or none were in
    scope). Not a successful ingestion: counted as an error and reprocessed every run
    (from stored bytes) until a parser fix makes it pass."""
    NOT_PUBLISHED = "NOT_PUBLISHED"
    FAILED = "FAILED"


class DateAction(StrEnum):
    DOWNLOAD = "DOWNLOAD"
    PROCESS_STORED = "PROCESS_STORED"
    SKIP = "SKIP"


@dataclass
class IngestionMetrics:
    requested_days: int = 0
    trading_sessions: int = 0
    dates_ingested: int = 0
    dates_skipped: int = 0
    dates_failed: int = 0
    files_downloaded: int = 0
    files_already_stored: int = 0
    files_not_published: int = 0
    files_failed: int = 0
    rows_parsed: int = 0
    rows_accepted: int = 0
    rows_quarantined: int = 0
    rows_out_of_scope: int = 0
    duplicate_rows: int = 0
    unresolved_identifiers: int = 0
    securities_created: int = 0
    securities_updated: int = 0
    """Distinct existing securities whose identifiers or dates changed during the run."""
    identity_links: int = 0
    source_conflicts: int = 0
    listing_status_changes: int = 0
    quarantine_by_reason: Counter[str] = field(default_factory=Counter)
    created_ids: set[str] = field(default_factory=set, repr=False)
    updated_ids: set[str] = field(default_factory=set, repr=False)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("created_ids")
        data.pop("updated_ids")
        data["quarantine_by_reason"] = dict(sorted(self.quarantine_by_reason.items()))
        return data


@dataclass
class BackfillReport:
    job_id: str
    exchange: str
    start: date
    end: date
    dry_run: bool
    metrics: IngestionMetrics
    plan: dict[str, str] = field(default_factory=dict)
    """date → planned action (dry run only)."""
    missing_sessions: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0

    def render(self) -> str:
        m = self.metrics
        title = "Backfill plan (dry run)" if self.dry_run else "Backfill complete"
        lines = [
            title,
            "",
            f"Job: {self.job_id}",
            f"Requested dates: {self.start} → {self.end}  ({m.requested_days} days)",
            f"Trading sessions: {m.trading_sessions:,}",
        ]
        if self.dry_run:
            planned = Counter(self.plan.values())
            lines += [f"Planned {action}: {n:,}" for action, n in sorted(planned.items())]
            return "\n".join(lines)
        lines += [
            f"Dates ingested: {m.dates_ingested:,}   "
            f"already ingested (skipped): {m.dates_skipped:,}   failed: {m.dates_failed:,}",
            f"Source files downloaded: {m.files_downloaded:,}   "
            f"already stored: {m.files_already_stored:,}",
            f"Source files missing (not published): {m.files_not_published:,}   "
            f"download failures: {m.files_failed:,}",
            "",
            f"Rows parsed: {m.rows_parsed:,}",
            f"Rows accepted: {m.rows_accepted:,}",
            f"Rows quarantined: {m.rows_quarantined:,}",
            f"Rows outside universe (other series): {m.rows_out_of_scope:,}",
            "",
            f"New securities: {m.securities_created:,}",
            f"Updated securities: {m.securities_updated:,}",
            f"Identity links (new ISIN / symbol change): {m.identity_links:,}",
            f"Unresolved identifiers: {m.unresolved_identifiers:,}",
            f"Source conflicts: {m.source_conflicts:,}",
            "",
            f"Errors: {len(m.errors)}",
            f"Warnings: {len(m.warnings)}",
            f"Duration: {self.duration_seconds:.1f}s",
        ]
        if m.quarantine_by_reason:
            lines += ["", "Quarantine by reason:"]
            lines += [f"  {r}: {n:,}" for r, n in sorted(m.quarantine_by_reason.items())]
        if self.missing_sessions:
            lines += [
                "",
                "Expected sessions with no published file: "
                + ", ".join(self.missing_sessions[:20]),
            ]
        if m.errors:
            lines += ["", "Errors:"] + [f"  {e}" for e in m.errors[:20]]
        return "\n".join(lines)


def new_job_id() -> str:
    run = os.environ.get("GITHUB_RUN_ID")
    if run:
        return f"gh-{run}-{os.environ.get('GITHUB_RUN_ATTEMPT', '1')}"
    return f"local-{utc_now():%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"


class IngestionService:
    def __init__(
        self,
        settings: ChartLensSettings,
        provider: ExchangeProvider,
        store: ObjectStore,
        *,
        overrides: IdentityOverrides | None = None,
        today: Callable[[], date] = lambda: utc_now().date(),
        rebuild: bool = False,
    ) -> None:
        """``rebuild=True`` starts from an empty security master and ignores the pinned
        identity state, adopting the current inputs (used only by the identity rebuild)."""
        self.settings = settings
        self._rebuild = rebuild
        self.provider = provider
        self.exchange = provider.exchange_code
        self.store = store
        self.sources = RawSourceStore(store)
        self.calendar: TradingCalendar = provider.trading_calendar()
        self.universe = frozenset(settings.universe.series)
        self.overrides = overrides if overrides is not None else self._load_overrides()
        self._today = today
        self.master = SecurityMaster(self.exchange) if rebuild else self._load_master()
        self._manifests: dict[date, dict[str, Any]] | None = None
        self._notices: list[SymbolChangeNotice] | None = None
        self._notices_source: str | None = None
        self._notices_hash: str | None = None

    # ------------------------------------------------------------------ setup

    def _load_overrides(self) -> IdentityOverrides:
        directory = config_dir()
        path = directory / "identity" / f"{self.exchange.lower()}.toml" if directory else None
        return IdentityOverrides.load(path)

    def _load_master(self) -> SecurityMaster:
        sec_key = DataLakeLayout.securities_key(self.exchange)
        hist_key = DataLakeLayout.identifier_history_key(self.exchange)
        if not self.store.exists(sec_key):
            return SecurityMaster(self.exchange)
        securities = pq.read_table(pa.BufferReader(self.store.get(sec_key)))
        history = pq.read_table(pa.BufferReader(self.store.get(hist_key)))
        return SecurityMaster.from_tables(self.exchange, securities, history)

    def save_master(self) -> None:
        securities, history = self.master.to_tables()
        self.store.put(DataLakeLayout.securities_key(self.exchange), to_parquet_bytes(securities))
        self.store.put(
            DataLakeLayout.identifier_history_key(self.exchange), to_parquet_bytes(history)
        )
        state = {"exchange": self.exchange, "inputs": self.identity_inputs()}
        self.store.put(
            DataLakeLayout.identity_state_key(self.exchange),
            json.dumps(state, indent=2, sort_keys=True).encode(),
        )

    def identity_state(self) -> dict[str, Any] | None:
        if self._rebuild:
            return None
        key = DataLakeLayout.identity_state_key(self.exchange)
        return json.loads(self.store.get(key)) if self.store.exists(key) else None

    def identity_inputs(self) -> dict[str, str]:
        """Everything that can change which security a row is assigned to.

        Only identity-relevant settings are included (identity + universe), so changing,
        say, adjustment methodology never invalidates identity."""
        config = {
            "identity": self.settings.identity.model_dump(mode="json"),
            "universe": self.settings.universe.model_dump(mode="json"),
        }
        canonical = json.dumps(config, sort_keys=True, separators=(",", ":"))
        return {
            "config": hashlib.sha256(canonical.encode()).hexdigest()[:12],
            "overrides": self.overrides.fingerprint,
            "symbol_changes": self._notices_hash or "none",
        }

    def check_identity_inputs(self) -> None:
        """Refuse to continue an existing master with different identity inputs (ADR-0009)."""
        if self._rebuild or not self.store.exists(DataLakeLayout.securities_key(self.exchange)):
            return
        state = self.identity_state()
        if state is not None and state.get("rebuild_in_progress"):
            raise IdentityInputsChanged(
                f"identity rebuild {state['rebuild_in_progress']} did not complete; canonical "
                "rows may mix two identity states. Re-run the identity rebuild."
            )
        if state is None:
            raise IdentityInputsChanged(
                "the security master has no recorded identity inputs (built before they were "
                "tracked); rebuild it from stored sources before continuing"
            )
        current = self.identity_inputs()
        changed = {
            k: (state["inputs"].get(k), v)
            for k, v in current.items()
            if state["inputs"].get(k) != v
        }
        if changed:
            detail = ", ".join(f"{k}: {old} → {new}" for k, (old, new) in sorted(changed.items()))
            raise IdentityInputsChanged(
                f"identity inputs changed since the security master was built ({detail}). "
                "Continuing would mix two identity states; rebuild the security master "
                "from stored sources instead."
            )

    def manifests(self) -> dict[date, dict[str, Any]]:
        if self._manifests is None:
            prefix = DataLakeLayout.ingestion_manifest_prefix(self.exchange)
            self._manifests = {}
            for key in self.store.list(prefix):
                data = json.loads(self.store.get(key))
                self._manifests[date.fromisoformat(str(data["trading_date"]))] = data
        return self._manifests

    def identity_fingerprint(self) -> str:
        i = self.identity_inputs()
        return f"{i['config']}/{i['overrides']}/{i['symbol_changes'][:12]}"

    def load_notices(
        self, *, download: bool, metrics: IngestionMetrics
    ) -> list[SymbolChangeNotice]:
        """Symbol-change evidence for identity resolution.

        The snapshot is **pinned** to the one the security master was built with. A newer
        snapshot is still downloaded and stored (raw, immutable), but adopting it changes
        identity inputs and therefore requires a rebuild — never a silent switch."""
        if self._notices is not None:
            return self._notices
        downloaded: SourceRecord | None = None
        if download:
            result = self.provider.download_symbol_changes()
            if result.status is DownloadStatus.FOUND and result.artifact is not None:
                downloaded, _ = self.sources.store(
                    result.artifact, parser_version="symbol_changes_v1"
                )
            else:
                metrics.warnings.append(f"symbol-change list unavailable ({result.detail})")

        state = self.identity_state()
        record: SourceRecord | None
        if state is not None:
            pinned = state["inputs"].get("symbol_changes", "none")
            record = None if pinned == "none" else self.sources.get_by_hash(self.exchange, pinned)
            if pinned != "none" and record is None:
                raise IdentityInputsChanged(
                    f"pinned symbol-change snapshot {pinned[:12]} is missing from the raw store"
                )
            if downloaded is not None and downloaded.content_hash != pinned:
                metrics.warnings.append(
                    f"a newer symbol-change snapshot was stored ({downloaded.source_id}); the "
                    "security master stays pinned to the previous one until it is rebuilt"
                )
        else:
            record = downloaded or self.sources.latest_snapshot(self.exchange, "symbolchange")

        if record is None:
            self._notices, self._notices_source, self._notices_hash = [], None, None
        else:
            self._notices = self.provider.parse_symbol_changes(self.sources.load(record))
            self._notices_source, self._notices_hash = record.source_id, record.content_hash
        return self._notices

    # ------------------------------------------------------------------ planning

    def plan_date(self, day: date, *, refetch: bool, reprocess: bool) -> DateAction:
        manifest = self.manifests().get(day)
        stored = self.sources.list_for_date(
            self.exchange, self.provider.daily_bars.source_dataset, day
        )
        if refetch:
            return DateAction.DOWNLOAD
        if not stored:
            if manifest and manifest["status"] == DateStatus.NOT_PUBLISHED:
                recent = day >= self._today() - timedelta(days=RECHECK_UNPUBLISHED_DAYS)
                return DateAction.DOWNLOAD if recent else DateAction.SKIP
            return DateAction.DOWNLOAD
        if reprocess or manifest is None or not self._manifest_current(manifest, stored):
            return DateAction.PROCESS_STORED
        return DateAction.SKIP

    def _manifest_current(self, manifest: dict[str, Any], stored: Sequence[SourceRecord]) -> bool:
        return (
            manifest["status"] == DateStatus.INGESTED
            and manifest.get("parser_version") == self.provider.daily_bars.parser_version
            and manifest.get("daily_bar_schema_version") == DAILY_BAR_SCHEMA_VERSION
            and manifest.get("identity_fingerprint") == self.identity_fingerprint()
            and int(manifest.get("pending_identity_rows", 0)) == 0
            and sorted(manifest.get("source_versions", []))
            == sorted(r.content_hash for r in stored)
        )

    # ------------------------------------------------------------------ anchoring

    def _anchored(self, day: date, sessions_after: Sequence[date]) -> bool:
        """True when every session after ``day`` up to the first ISIN-bearing one is ingested."""
        manifests = self.manifests()
        for s in sessions_after:
            m = manifests.get(s)
            if m is None:
                return False
            if m["status"] == DateStatus.INGESTED and m.get("has_isin"):
                return True
        return False

    def _sessions_after(self, day: date, horizon: Sequence[date]) -> Sequence[date]:
        i = bisect.bisect_right(horizon, day)
        return horizon[i:]

    # ------------------------------------------------------------------ one date

    def ingest_date(
        self,
        day: date,
        *,
        job_id: str,
        metrics: IngestionMetrics,
        refetch: bool = False,
        reprocess: bool = False,
        horizon: Sequence[date] = (),
    ) -> DateAction:
        started = time.monotonic()
        action = self.plan_date(day, refetch=refetch, reprocess=reprocess)
        if action is DateAction.SKIP:
            metrics.dates_skipped += 1
            log_event(
                log,
                "ingest.date.skipped",
                job_id=job_id,
                trading_date=str(day),
                reason="already ingested",
            )
            return action

        bars = self.provider.daily_bars
        download_info: dict[str, Any] = {}
        if action is DateAction.DOWNLOAD:
            result = bars.download(day)
            download_info = {
                "status": str(result.status),
                "tried": result.tried,
                "detail": result.detail,
            }
            if result.status is DownloadStatus.FOUND and result.artifact is not None:
                record, created = self.sources.store(
                    result.artifact, parser_version=bars.parser_version
                )
                if created:
                    metrics.files_downloaded += 1
                else:
                    metrics.files_already_stored += 1
                log_event(
                    log,
                    "source.stored",
                    job_id=job_id,
                    trading_date=str(day),
                    source_id=record.source_id,
                    source_hash=record.content_hash,
                    bytes=record.byte_size,
                    created=created,
                    url=record.url,
                )
            elif result.status is DownloadStatus.FAILED:
                metrics.files_failed += 1
                stored_now = self.sources.list_for_date(self.exchange, bars.source_dataset, day)
                if not stored_now:
                    self._fail(
                        day,
                        job_id,
                        metrics,
                        f"download failed: {result.detail}",
                        download_info,
                        started,
                    )
                    return action
                metrics.warnings.append(
                    f"{day}: re-download failed, using stored source ({result.detail})"
                )

        stored = self.sources.list_for_date(self.exchange, bars.source_dataset, day)
        if not stored:
            metrics.files_not_published += 1
            self._write_manifest(
                day,
                {
                    "status": DateStatus.NOT_PUBLISHED,
                    "job_id": job_id,
                    "download": download_info,
                    "dq_condition": "EXPECTED_SESSION_NOT_PUBLISHED",
                    "calendar_evidence": str(self.calendar.evidence(day.year)),
                    "duration_seconds": round(time.monotonic() - started, 3),
                },
            )
            log_event(
                log,
                "ingest.date.not_published",
                logging.WARNING,
                job_id=job_id,
                trading_date=str(day),
            )
            return action

        selected = stored[-1]  # canonical selection rule: most recently downloaded version
        try:
            parsed = bars.parse(self.sources.load(selected), day, self.universe)
        except SourceParseError as err:
            self._fail(
                day,
                job_id,
                metrics,
                f"{selected.source_id}: {err}",
                download_info,
                started,
                selected,
            )
            return action

        conflict = (
            self._compare_versions(day, stored, selected, parsed) if len(stored) > 1 else None
        )
        if conflict is not None:
            metrics.source_conflicts += 1
            metrics.warnings.append(
                f"{day}: source versions disagree ({conflict['differing_rows']} rows)"
            )
            self.store.put(
                DataLakeLayout.conflict_key(self.exchange, day),
                json.dumps(conflict, indent=2).encode(),
            )

        notices = self._notices or []
        observations = [
            Observation(r.row_number, r.symbol, r.series, r.isin, r.name) for r in parsed.rows
        ]
        has_isin = any(r.isin for r in parsed.rows)
        needs_anchor = any(r.isin is None for r in parsed.rows)
        anchored = self._anchored(day, self._sessions_after(day, horizon)) if needs_anchor else True
        resolution = self.master.resolve(
            observations,
            day,
            source_id=selected.source_id,
            policy=self.provider.identity_policy(),
            config=self.settings.identity,
            notices=notices,
            overrides=self.overrides,
            anchored=anchored,
        )

        ingested_at = utc_now()
        by_row = {r.row_number: r for r in parsed.rows}
        accepted = [by_row[n] for n in sorted(resolution.assigned)]
        quarantined = list(parsed.quarantined) + [
            QuarantinedRow(
                n,
                reason,
                detail,
                by_row[n].symbol,
                by_row[n].series,
                by_row[n].isin,
                parsed.raw_lines.get(n, ""),
            )
            for n, (reason, detail) in sorted(resolution.quarantined.items())
        ]
        canonical = self._canonical_table(
            accepted, resolution.assigned, selected, parsed, ingested_at
        )
        quarantine = self._quarantine_table(
            day, quarantined, selected, parsed.parser_version, ingested_at
        )

        # Commit order: master → canonical → quarantine → manifest (commit marker).
        self.save_master()
        self.store.put(
            DataLakeLayout.curated_daily_key(self.exchange, day), to_parquet_bytes(canonical)
        )
        self.store.put(
            DataLakeLayout.quarantine_daily_key(self.exchange, day), to_parquet_bytes(quarantine)
        )

        reasons = Counter(str(q.reason) for q in quarantined)
        pending = sum(n for r, n in reasons.items() if QuarantineReason(r) in IDENTITY_REASONS)
        abnormal = self._abnormal_quarantine(parsed)
        manifest = {
            "status": DateStatus.QUARANTINED if abnormal else DateStatus.INGESTED,
            "dq_condition": abnormal,
            "job_id": job_id,
            "selected_source": {
                "source_id": selected.source_id,
                "content_hash": selected.content_hash,
                "original_filename": selected.original_filename,
                "storage_key": selected.storage_key,
                "downloaded_at": selected.downloaded_at.isoformat(),
            },
            "selection_rule": "most_recently_downloaded",
            "source_versions": [r.content_hash for r in stored],
            "source_format": parsed.source_format,
            "member_name": parsed.member_name,
            "parser_version": parsed.parser_version,
            "daily_bar_schema_version": DAILY_BAR_SCHEMA_VERSION,
            "calendar_version": self.calendar.version,
            "calendar_evidence": str(self.calendar.evidence(day.year)),
            "identity_fingerprint": self.identity_fingerprint(),
            "symbol_changes_source": self._notices_source,
            "anchored": anchored,
            "has_isin": has_isin,
            "counts": {
                "rows_read": parsed.rows_read,
                "rows_in_scope": len(parsed.rows) + len(parsed.quarantined),
                "rows_accepted": len(accepted),
                "rows_quarantined": len(quarantined),
                "rows_out_of_scope": sum(parsed.out_of_scope.values()),
            },
            "quarantine_by_reason": dict(sorted(reasons.items())),
            "pending_identity_rows": pending,
            "out_of_scope_by_series": parsed.out_of_scope,
            "securities_created": len(resolution.created),
            "securities_updated": len(resolution.updated),
            "identity_links": resolution.links,
            "source_conflict": conflict,
            "warnings": parsed.warnings,
            "download": download_info,
            "processed_at": ingested_at.isoformat(),
            "duration_seconds": round(time.monotonic() - started, 3),
        }
        if not self.calendar.is_trading_day(day):
            manifest["warnings"] = [
                *parsed.warnings,
                "CALENDAR_DISAGREEMENT: file exists for a non-session date",
            ]
        self._write_manifest(day, manifest)

        if abnormal:
            metrics.dates_failed += 1
            metrics.errors.append(f"{day}: {abnormal}")
            log_event(
                log,
                "ingest.date.quarantined",
                logging.ERROR,
                job_id=job_id,
                trading_date=str(day),
                condition=abnormal,
            )
        else:
            metrics.dates_ingested += 1
        metrics.rows_parsed += parsed.rows_read
        metrics.rows_accepted += len(accepted)
        metrics.rows_quarantined += len(quarantined)
        metrics.rows_out_of_scope += sum(parsed.out_of_scope.values())
        metrics.duplicate_rows += reasons.get(QuarantineReason.DUPLICATE_ROW, 0) + reasons.get(
            QuarantineReason.DUPLICATE_SECURITY_DATE, 0
        )
        metrics.unresolved_identifiers += pending
        metrics.securities_created += len(resolution.created)
        metrics.updated_ids |= resolution.updated - metrics.created_ids
        metrics.created_ids |= resolution.created
        metrics.securities_updated = len(metrics.updated_ids)
        metrics.identity_links += len(resolution.links)
        metrics.quarantine_by_reason.update(reasons)
        metrics.warnings += [f"{day}: {w}" for w in parsed.warnings]
        log_event(
            log,
            "ingest.date",
            job_id=job_id,
            trading_date=str(day),
            action=str(action),
            source_file=selected.original_filename,
            source_hash=selected.content_hash,
            source_format=parsed.source_format,
            parser_version=parsed.parser_version,
            rows_read=parsed.rows_read,
            rows_accepted=len(accepted),
            rows_rejected=len(quarantined),
            rows_out_of_scope=sum(parsed.out_of_scope.values()),
            securities_created=len(resolution.created),
            pending_identity_rows=pending,
            duration=round(time.monotonic() - started, 3),
        )
        return action

    def _abnormal_quarantine(self, parsed: ParsedDaily) -> str | None:
        """Why this session must not count as ingested, if it must not."""
        in_scope = len(parsed.rows) + len(parsed.quarantined)
        if in_scope == 0:
            return "NO_IN_SCOPE_ROWS: the file has no rows in the configured series"
        ratio = len(parsed.quarantined) / in_scope
        limit = self.settings.data_quality.max_session_quarantine_ratio
        if ratio > limit:
            return (
                f"ABNORMAL_QUARANTINE_RATIO: {len(parsed.quarantined)} of {in_scope} in-scope "
                f"rows rejected by the parser ({ratio:.1%} > {limit:.1%})"
            )
        return None

    def _fail(
        self,
        day: date,
        job_id: str,
        metrics: IngestionMetrics,
        error: str,
        download_info: dict[str, Any],
        started: float,
        source: SourceRecord | None = None,
    ) -> None:
        metrics.dates_failed += 1
        metrics.errors.append(f"{day}: {error}")
        self._write_manifest(
            day,
            {
                "status": DateStatus.FAILED,
                "job_id": job_id,
                "error": error,
                "download": download_info,
                "selected_source": {
                    "source_id": source.source_id,
                    "content_hash": source.content_hash,
                }
                if source
                else None,
                "duration_seconds": round(time.monotonic() - started, 3),
            },
        )
        log_event(
            log,
            "ingest.date.failed",
            logging.ERROR,
            job_id=job_id,
            trading_date=str(day),
            error=error,
        )

    def _write_manifest(self, day: date, fields: dict[str, Any]) -> None:
        payload = {"exchange": self.exchange, "trading_date": day.isoformat(), **fields}
        self.store.put(
            DataLakeLayout.ingestion_manifest_key(self.exchange, day),
            json.dumps(payload, indent=2, default=str).encode(),
        )
        self.manifests()[day] = json.loads(json.dumps(payload, default=str))

    # ------------------------------------------------------------------ tables

    def _canonical_table(
        self,
        rows: Sequence[NormalizedRow],
        assigned: dict[int, str],
        source: SourceRecord,
        parsed: ParsedDaily,
        ingested_at: datetime,
    ) -> pa.Table:
        records = [
            {
                "exchange": self.exchange,
                "security_id": assigned[r.row_number],
                "trading_date": r.trading_date,
                "symbol": r.symbol,
                "series": r.series,
                "isin": r.isin,
                "open": r.open,
                "high": r.high,
                "low": r.low,
                "close": r.close,
                "prev_close": r.prev_close,
                "volume": r.volume,
                "traded_value": r.traded_value,
                "trades": r.trades,
                "source_id": source.source_id,
                "source_file_hash": source.content_hash,
                "source_file_date": source.source_date or parsed.trading_date,
                "parser_version": parsed.parser_version,
                "ingested_at": ingested_at,
            }
            for r in sorted(rows, key=lambda r: assigned[r.row_number])
        ]
        return pa.Table.from_pylist(records, schema=daily_bar_schema())

    def _quarantine_table(
        self,
        day: date,
        rows: Sequence[QuarantinedRow],
        source: SourceRecord,
        parser_version: str,
        at: datetime,
    ) -> pa.Table:
        records = [
            {
                "exchange": self.exchange,
                "trading_date": day,
                "source_id": source.source_id,
                "source_file_hash": source.content_hash,
                "parser_version": parser_version,
                "row_number": q.row_number,
                "reason": str(q.reason),
                "detail": q.detail,
                "symbol": q.symbol,
                "series": q.series,
                "isin": q.isin,
                "raw": q.raw,
                "quarantined_at": at,
            }
            for q in sorted(rows, key=lambda q: q.row_number)
        ]
        return pa.Table.from_pylist(records, schema=quarantine_schema())

    # ------------------------------------------------------------------ conflicts

    def _compare_versions(
        self, day: date, stored: Sequence[SourceRecord], selected: SourceRecord, parsed: ParsedDaily
    ) -> dict[str, Any] | None:
        def keyed(p: ParsedDaily) -> dict[tuple[str, str], tuple[object, ...]]:
            return {
                (r.symbol, r.series): (
                    r.isin,
                    r.open,
                    r.high,
                    r.low,
                    r.close,
                    r.prev_close,
                    r.volume,
                    r.traded_value,
                    r.trades,
                )
                for r in p.rows
            }

        mine = keyed(parsed)
        others: list[dict[str, Any]] = []
        total = 0
        for record in stored:
            if record.content_hash == selected.content_hash:
                continue
            try:
                theirs = keyed(
                    self.provider.daily_bars.parse(self.sources.load(record), day, self.universe)
                )
            except SourceParseError as err:
                others.append({"source_id": record.source_id, "parse_error": str(err)})
                continue
            only_mine = sorted(set(mine) - set(theirs))
            only_theirs = sorted(set(theirs) - set(mine))
            changed = sorted(k for k in set(mine) & set(theirs) if mine[k] != theirs[k])
            n = len(only_mine) + len(only_theirs) + len(changed)
            if n:
                total += n
                others.append(
                    {
                        "source_id": record.source_id,
                        "content_hash": record.content_hash,
                        "rows_only_in_selected": [list(k) for k in only_mine[:50]],
                        "rows_only_in_other": [list(k) for k in only_theirs[:50]],
                        "rows_with_different_values": [
                            {
                                "key": list(k),
                                "selected": _jsonable(mine[k]),
                                "other": _jsonable(theirs[k]),
                            }
                            for k in changed[:50]
                        ],
                    }
                )
        if not total and not any("parse_error" in o for o in others):
            return None
        return {
            "trading_date": day.isoformat(),
            "selected_source": selected.source_id,
            "selection_rule": "most_recently_downloaded",
            "differing_rows": total,
            "other_versions": others,
        }

    # ------------------------------------------------------------------ backfill

    def backfill(
        self,
        start: date,
        end: date,
        *,
        dry_run: bool = False,
        refetch: bool = False,
        reprocess: bool = False,
        job_id: str | None = None,
    ) -> BackfillReport:
        started = time.monotonic()
        job_id = job_id or new_job_id()
        metrics = IngestionMetrics(requested_days=(end - start).days + 1)
        sessions = self.calendar.expected_sessions(start, end)
        metrics.trading_sessions = len(sessions)
        report = BackfillReport(job_id, self.exchange, start, end, dry_run, metrics)

        self.load_notices(download=not dry_run, metrics=metrics)
        self.check_identity_inputs()
        if dry_run:
            for day in sorted(sessions, reverse=True):
                report.plan[day.isoformat()] = str(
                    self.plan_date(day, refetch=refetch, reprocess=reprocess)
                )
            report.duration_seconds = time.monotonic() - started
            return report

        log_event(
            log,
            "backfill.start",
            job_id=job_id,
            start=str(start),
            end=str(end),
            sessions=len(sessions),
        )
        horizon = self._anchor_horizon(end, sessions)
        for day in sorted(sessions, reverse=True):
            try:
                self.ingest_date(
                    day,
                    job_id=job_id,
                    metrics=metrics,
                    refetch=refetch,
                    reprocess=reprocess,
                    horizon=horizon,
                )
            except Exception as exc:  # one bad date must not stop a 20-year backfill
                log.exception("unexpected error on %s", day)
                self._fail(
                    day,
                    job_id,
                    metrics,
                    f"unexpected {type(exc).__name__}: {exc}",
                    {},
                    time.monotonic(),
                )
        report.missing_sessions = [
            d.isoformat()
            for d in sessions
            if self.manifests().get(d, {}).get("status") == DateStatus.NOT_PUBLISHED
        ]
        self._update_listing_status(metrics)
        self.save_master()
        report.duration_seconds = time.monotonic() - started
        log_event(
            log,
            "backfill.complete",
            job_id=job_id,
            **{k: v for k, v in metrics.as_dict().items() if isinstance(v, int)},
        )
        return report

    def reprocess_pending(self, *, job_id: str | None = None) -> BackfillReport:
        """Re-run every ingested date that still has identity-pending rows (newest first)."""
        pending = sorted(
            (
                d
                for d, m in self.manifests().items()
                if int(m.get("pending_identity_rows", 0) or 0) > 0
            ),
            reverse=True,
        )
        job_id = job_id or new_job_id()
        metrics = IngestionMetrics(requested_days=len(pending), trading_sessions=len(pending))
        start, end = (min(pending), max(pending)) if pending else (self._today(), self._today())
        report = BackfillReport(job_id, self.exchange, start, end, False, metrics)
        self.load_notices(download=False, metrics=metrics)
        self.check_identity_inputs()
        horizon = self._anchor_horizon(end, pending)
        t0 = time.monotonic()
        for day in pending:
            self.ingest_date(day, job_id=job_id, metrics=metrics, reprocess=True, horizon=horizon)
        self._update_listing_status(metrics)
        self.save_master()
        report.duration_seconds = time.monotonic() - t0
        return report

    def _anchor_horizon(self, end: date, sessions: Sequence[date]) -> list[date]:
        """Sessions after each date that may serve as its identity anchor: everything in
        the calendar from the earliest requested date up to the latest ingested date."""
        known = [d for d in self.manifests() if d > end]
        last = max([end, *known])
        first = min(sessions) if sessions else end
        try:
            return self.calendar.expected_sessions(first, last)
        except LookupError:
            return sorted(set(sessions) | set(known))

    def _update_listing_status(self, metrics: IngestionMetrics) -> None:
        ingested = [
            d for d, m in self.manifests().items() if m.get("status") == DateStatus.INGESTED
        ]
        if not ingested:
            return
        latest = max(ingested)
        cutoff = self._nth_session_back(latest, self.settings.identity.active_within_sessions)
        metrics.listing_status_changes += self.master.update_listing_status(
            cutoff, latest, self.overrides
        )

    def _nth_session_back(self, latest: date, n: int) -> date:
        """The n-th session counting back from ``latest`` (inclusive), stopping at the edge
        of calendar coverage rather than assuming anything beyond it."""
        found, day, earliest = 0, latest, latest
        while found < n:
            try:
                if self.calendar.is_trading_day(day):
                    found += 1
                    earliest = day
            except LookupError:
                break
            day -= timedelta(days=1)
        return earliest


def _jsonable(values: tuple[object, ...]) -> list[object]:
    return [str(v) if isinstance(v, Decimal) else v for v in values]


def official_years(calendar: TradingCalendar, years: Sequence[int]) -> list[int]:
    return [y for y in years if calendar.evidence(y) is CalendarEvidence.OFFICIAL]


__all__ = [
    "IDENTIFIER_HISTORY_SCHEMA",
    "SECURITIES_SCHEMA",
    "BackfillReport",
    "DateAction",
    "DateStatus",
    "IngestionMetrics",
    "IngestionService",
]
