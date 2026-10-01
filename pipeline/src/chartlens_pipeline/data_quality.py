"""Data-quality assessment of the adjusted analytical dataset (ADR-0012).

Reads only stored artefacts — the published adjusted manifest and files, the adjustment
events and corporate-action tables, the ingestion manifests, the trading calendar and the
identifier history — and writes:

* ``findings.parquet`` — every finding (market-wide ones have no security);
* ``status.parquet`` — one row per security: ``usable_from``, status and counts;
* ``report.json`` — the market-wide summary.

Continuity breaks (``usable_from`` moves after them):

* an UNQUANTIFIED or CONFLICTING_RECORDS corporate-action event;
* a factor the prices reject while a large gap remains at its ex-date;
* more than ``max_trading_gap_sessions`` expected sessions without a trade.

Warnings (reviewed, never inferred away): SUSPECT factors, unrecognised feed records,
unexplained large moves, a high share of missing sessions, unresolved price-relevant
feed records, sessions not ingested. Information: applied factors, identifier changes,
derived calendar years.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import logging
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date
from itertools import pairwise
from typing import Any, Final

import pyarrow as pa
import pyarrow.parquet as pq

from chartlens_core.config import ChartLensSettings, config_dir
from chartlens_core.logs import log_event
from chartlens_core.quality import Dimension, Finding, Severity, status
from chartlens_pipeline.adjust import EventStatus
from chartlens_pipeline.calendar import CalendarCoverageError, CalendarEvidence, TradingCalendar
from chartlens_pipeline.corporate_actions_model import ActionClass
from chartlens_pipeline.daily import to_parquet_bytes
from chartlens_pipeline.identity import IdentityOverrides
from chartlens_pipeline.providers.base import ExchangeProvider
from chartlens_pipeline.storage import DataLakeLayout, ObjectStore

log = logging.getLogger("chartlens.pipeline.data_quality")

DQ_ENGINE_VERSION: Final = "dq_v2"
"""Bump on any change to findings or status rules (part of dq_version)."""

_EVENT_FINDINGS: Final[dict[str, tuple[Severity, str]]] = {
    EventStatus.VERIFIED: (Severity.INFO, "FACTOR_APPLIED"),
    EventStatus.CONSISTENT: (Severity.INFO, "FACTOR_APPLIED_WITHIN_NOISE"),
    EventStatus.SUSPECT: (Severity.WARN, "FACTOR_SUSPECT"),
    EventStatus.NO_ADJUSTMENT: (Severity.INFO, "NO_ADJUSTMENT_NEEDED"),
    EventStatus.PENDING: (Severity.INFO, "ACTION_PENDING"),
    EventStatus.REJECTED_BY_PRICE: (Severity.WARN, "FACTOR_REJECTED_BY_PRICE"),
    EventStatus.UNQUANTIFIED: (Severity.WARN, "UNQUANTIFIED_ACTION"),
    EventStatus.CONFLICTING_RECORDS: (Severity.WARN, "CONFLICTING_ACTION_RECORDS"),
}


class AdjustedDataNotPublished(RuntimeError):
    """No adjusted manifest: run the adjustment first (or it failed its hard requirements)."""


@dataclass
class SecuritySeries:
    security_id: str
    dates: list[date]
    adj_close: list[float]
    symbol: str
    isin: str | None
    file_hash: str


@dataclass
class DataQualityResult:
    dq_version: str
    adjustment_version: str
    as_of: date | None
    findings: list[Finding]
    statuses: list[dict[str, Any]]
    summary: dict[str, Any]


def market_findings(
    manifests: dict[date, dict[str, Any]], calendar: TradingCalendar
) -> tuple[list[Finding], list[date]]:
    """Findings about whole sessions, and the expected sessions covered by ingestion."""
    ingested = [d for d, m in manifests.items() if m.get("status") == "INGESTED"]
    if not ingested:
        return [], []
    first, last = min(manifests), max(ingested)
    findings: list[Finding] = []
    try:
        expected = calendar.expected_sessions(first, last)
    except CalendarCoverageError as exc:
        return [
            Finding(
                None,
                first,
                last,
                Dimension.CALENDAR,
                Severity.FAIL,
                "CALENDAR_GAP",
                detail=str(exc),
            )
        ], []
    for year in sorted({d.year for d in expected}):
        if calendar.evidence(year) is CalendarEvidence.DERIVED:
            findings.append(
                Finding(
                    None,
                    date(year, 1, 1),
                    date(year, 12, 31),
                    Dimension.CALENDAR,
                    Severity.INFO,
                    "DERIVED_CALENDAR",
                    detail="sessions derived from published bhavcopies, not an official list",
                )
            )
    codes = {"QUARANTINED": "SESSION_QUARANTINED", "FAILED": "SESSION_FAILED"}
    for d in expected:
        state = manifests.get(d, {}).get("status")
        if state == "INGESTED":
            continue
        code = codes.get(str(state), "SESSION_MISSING")
        findings.append(
            Finding(None, d, d, Dimension.SOURCE, Severity.WARN, code, detail=f"status {state}")
        )
    return findings, expected


def security_findings(
    s: SecuritySeries,
    events: Sequence[dict[str, Any]],
    actions: Sequence[dict[str, Any]],
    identifiers: Sequence[dict[str, Any]],
    expected: Sequence[date],
    settings: ChartLensSettings,
    link_breaks: Mapping[str, str] | None = None,
) -> list[Finding]:
    cfg = settings.data_quality
    sid = s.security_id
    out: list[Finding] = []
    boundaries: set[date] = set()
    for e in events:
        severity, code = _EVENT_FINDINGS.get(e["status"], (Severity.INFO, ""))
        if e["boundary_date"] is not None:
            boundaries.add(e["boundary_date"])
        if not code:
            continue
        start = e["boundary_date"] or e["ex_date"]
        out.append(
            Finding(
                sid,
                start,
                start,
                Dimension.CORPORATE_ACTION,
                severity,
                code,
                breaks_continuity=bool(e["breaks_continuity"]) and e["boundary_date"] is not None,
                detail=f"ex {e['ex_date']}: {'; '.join(json.loads(e['subjects']))}"
                + (f" (factor {e['factor']})" if e["factor"] else ""),
                evidence=f"event {sid}@{e['ex_date']}",
            )
        )

    def boundary(day: date) -> date | None:
        i = bisect.bisect_left(s.dates, day)
        return s.dates[i] if 0 < i < len(s.dates) else None

    for a in actions:
        if a["ex_date"] is None or a["suppressed_reason"]:
            continue
        if a["action_class"] == ActionClass.CASH_DISTRIBUTION:
            if (b := boundary(a["ex_date"])) is not None:
                boundaries.add(b)
        elif a["action_class"] == ActionClass.UNRECOGNISED:
            out.append(
                Finding(
                    sid,
                    a["ex_date"],
                    a["ex_date"],
                    Dimension.CORPORATE_ACTION,
                    Severity.WARN,
                    "UNRECOGNISED_ACTION",
                    detail=a["subject"],
                    evidence=f"record {a['record_key']}",
                )
            )

    # Sessions: trading gaps break continuity; a high missing share is a warning.
    if expected and s.dates:
        lo = bisect.bisect_left(expected, s.dates[0])
        hi = bisect.bisect_right(expected, s.dates[-1])
        span = hi - lo
        traded = len(set(s.dates).intersection(expected[lo:hi]))
        if span and (span - traded) / span > cfg.max_missing_session_ratio:
            out.append(
                Finding(
                    sid,
                    s.dates[0],
                    s.dates[-1],
                    Dimension.CALENDAR,
                    Severity.WARN,
                    "MISSING_SESSIONS",
                    detail=f"{span - traded} of {span} expected sessions without a trade",
                )
            )
        for prev, cur in pairwise(s.dates):
            between = bisect.bisect_left(expected, cur) - bisect.bisect_right(expected, prev)
            if between > cfg.max_trading_gap_sessions:
                out.append(
                    Finding(
                        sid,
                        cur,
                        cur,
                        Dimension.CALENDAR,
                        Severity.WARN,
                        "TRADING_GAP",
                        breaks_continuity=True,
                        detail=f"no trade in {between} expected sessions after {prev}",
                    )
                )
                boundaries.add(cur)

    for r in identifiers:  # reviewed identity links that join identity but not prices
        reason = (link_breaks or {}).get(r["identifier_value"])
        if r["identifier_type"] != "ISIN" or reason is None:
            continue
        i = bisect.bisect_left(s.dates, r["valid_from"])
        if 0 < i < len(s.dates):
            boundaries.add(s.dates[i])
            out.append(
                Finding(
                    sid,
                    s.dates[i],
                    s.dates[i],
                    Dimension.IDENTITY,
                    Severity.WARN,
                    "REVIEWED_LINK_PRICE_BREAK",
                    breaks_continuity=True,
                    detail=f"ISIN {r['identifier_value']} linked by review; prices not continuous",
                    evidence=reason,
                )
            )

    for i in range(1, len(s.dates)):
        move = s.adj_close[i] / s.adj_close[i - 1] - 1
        if abs(move) > cfg.max_unexplained_move and s.dates[i] not in boundaries:
            out.append(
                Finding(
                    sid,
                    s.dates[i],
                    s.dates[i],
                    Dimension.PRICE,
                    Severity.WARN,
                    "UNEXPLAINED_MOVE",
                    detail=f"adjusted close {move:+.1%} with no corporate action",
                )
            )

    by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in identifiers:
        by_type[r["identifier_type"]].append(r)
    for kind, code in (("SYMBOL", "SYMBOL_CHANGE"), ("ISIN", "ISIN_CHANGE")):
        spans = sorted(
            by_type.get(kind, []), key=lambda r: (r["valid_from"], r["identifier_value"])
        )
        for before, after in pairwise(spans):
            if before["identifier_value"] == after["identifier_value"]:
                continue  # same value, new evidence (e.g. ISIN first published): no change
            out.append(
                Finding(
                    sid,
                    after["valid_from"],
                    after["valid_from"],
                    Dimension.IDENTITY,
                    Severity.INFO,
                    code,
                    detail=f"{before['identifier_value']} → {after['identifier_value']}",
                    evidence=f"evidence {after['evidence']}",
                )
            )
    return out


FINDINGS_SCHEMA: Final = pa.schema(
    [
        pa.field("security_id", pa.string()),
        pa.field("start_date", pa.date32()),
        pa.field("end_date", pa.date32()),
        pa.field("dimension", pa.string(), nullable=False),
        pa.field("severity", pa.string(), nullable=False),
        pa.field("code", pa.string(), nullable=False),
        pa.field("breaks_continuity", pa.bool_(), nullable=False),
        pa.field("detail", pa.string(), nullable=False),
        pa.field("evidence", pa.string(), nullable=False),
        pa.field("dq_version", pa.string(), nullable=False),
    ],
    metadata={b"chartlens.dataset": b"data_quality_findings", b"chartlens.schema_version": b"1"},
)

STATUS_SCHEMA: Final = pa.schema(
    [
        pa.field("security_id", pa.string(), nullable=False),
        pa.field("symbol", pa.string(), nullable=False),
        pa.field("isin", pa.string()),
        pa.field("first_date", pa.date32(), nullable=False),
        pa.field("last_date", pa.date32(), nullable=False),
        pa.field("sessions", pa.int64(), nullable=False),
        pa.field("usable_from", pa.date32()),
        pa.field("usable_sessions", pa.int64(), nullable=False),
        pa.field("status", pa.string(), nullable=False),
        pa.field("continuity_breaks", pa.int64(), nullable=False),
        pa.field("warnings", pa.int64(), nullable=False),
        pa.field("failures", pa.int64(), nullable=False),
        pa.field("as_of", pa.date32()),
        pa.field("adjustment_version", pa.string(), nullable=False),
        pa.field("dq_version", pa.string(), nullable=False),
    ],
    metadata={b"chartlens.dataset": b"data_quality_status", b"chartlens.schema_version": b"1"},
)


class DataQualityService:
    def __init__(
        self,
        settings: ChartLensSettings,
        provider: ExchangeProvider,
        store: ObjectStore,
        *,
        identity_overrides: IdentityOverrides | None = None,
    ) -> None:
        if identity_overrides is None:
            directory = config_dir()
            name = f"{provider.exchange_code.lower()}.toml"
            identity_overrides = IdentityOverrides.load(
                directory / "identity" / name if directory else None
            )
        self.identity_overrides = identity_overrides
        self.settings = settings
        self.provider = provider
        self.exchange = provider.exchange_code
        self.store = store

    def _table(self, key: str) -> pa.Table:
        return pq.read_table(pa.BufferReader(self.store.get(key)))

    def _manifests(self) -> dict[date, dict[str, Any]]:
        out: dict[date, dict[str, Any]] = {}
        for key in self.store.list(DataLakeLayout.ingestion_manifest_prefix(self.exchange)):
            data = json.loads(self.store.get(key))
            out[date.fromisoformat(str(data["trading_date"]))] = data
        return out

    def _series(self, files: dict[str, str]) -> list[SecuritySeries]:
        def read(item: tuple[str, str]) -> SecuritySeries:
            sid, digest = item
            data = self.store.get(DataLakeLayout.adjusted_daily_key(self.exchange, sid))
            actual = hashlib.sha256(data).hexdigest()
            t = pq.read_table(
                pa.BufferReader(data), columns=["trading_date", "adj_close", "symbol", "isin"]
            ).to_pydict()
            return SecuritySeries(
                sid,
                t["trading_date"],
                [float(x) for x in t["adj_close"]],
                t["symbol"][-1] if t["symbol"] else "",
                t["isin"][-1] if t["isin"] else None,
                actual if actual == digest else f"MISMATCH:{actual}",
            )

        with ThreadPoolExecutor(max_workers=16) as pool:
            return list(pool.map(read, sorted(files.items())))

    def run(self) -> DataQualityResult:
        manifest_key = DataLakeLayout.adjusted_manifest_key(self.exchange)
        if not self.store.exists(manifest_key):
            raise AdjustedDataNotPublished(manifest_key)
        manifest = json.loads(self.store.get(manifest_key))
        adjustment_version: str = manifest["adjustment_version"]
        as_of = date.fromisoformat(manifest["data_end"]) if manifest.get("data_end") else None
        calendar = self.provider.trading_calendar()
        dq_version = (
            "dq-"
            + hashlib.sha256(
                json.dumps(
                    {
                        "engine": DQ_ENGINE_VERSION,
                        "methodology": self.settings.data_quality.model_dump(mode="json"),
                        "adjustment": adjustment_version,
                        "calendar": calendar.version,
                        "identity_overrides": self.identity_overrides.document_hash,
                    },
                    sort_keys=True,
                ).encode()
            ).hexdigest()[:12]
        )

        events: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for e in self._table(DataLakeLayout.adjustment_events_key(self.exchange)).to_pylist():
            events[e["security_id"]].append(e)
        actions: dict[str | None, list[dict[str, Any]]] = defaultdict(list)
        for a in self._table(DataLakeLayout.corporate_actions_table_key(self.exchange)).to_pylist():
            actions[a["security_id"]].append(a)
        identifiers: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in self._table(DataLakeLayout.identifier_history_key(self.exchange)).to_pylist():
            identifiers[r["security_id"]].append(r)

        market, expected = market_findings(self._manifests(), calendar)
        universe = set(self.settings.universe.series)
        for a in actions.get(None, []):
            relevant = a["action_class"] in (
                ActionClass.EQUITY_ADJUSTMENT,
                ActionClass.UNQUANTIFIED,
            )
            in_range = a["ex_date"] is not None and (as_of is None or a["ex_date"] <= as_of)
            if relevant and in_range and (a["series"] is None or a["series"] in universe):
                market.append(
                    Finding(
                        None,
                        a["ex_date"],
                        a["ex_date"],
                        Dimension.CORPORATE_ACTION,
                        Severity.WARN,
                        f"ACTION_{a['resolution']}",
                        detail=(
                            f"{a['symbol']} {a['isin'] or '-'}: {a['subject']} "
                            f"({a['resolution_detail']})"
                        ),
                        evidence=f"record {a['record_key']}",
                    )
                )
        # Session-level problems concern every security trading through them; an unresolved
        # feed record concerns no known security, so it stays a market-wide review item.
        market_warnings = [
            f
            for f in market
            if f.severity is not Severity.INFO
            and f.dimension in (Dimension.SOURCE, Dimension.CALENDAR)
        ]

        findings: list[Finding] = list(market)
        statuses: list[dict[str, Any]] = []
        for s in self._series(manifest["files"]):
            own = security_findings(
                s,
                events.get(s.security_id, []),
                actions.get(s.security_id, []),
                identifiers.get(s.security_id, []),
                expected,
                self.settings,
                self.identity_overrides.link_breaks_continuity,
            )
            if s.file_hash.startswith("MISMATCH"):
                own.append(
                    Finding(
                        s.security_id,
                        None,
                        None,
                        Dimension.SOURCE,
                        Severity.FAIL,
                        "ADJUSTED_FILE_HASH_MISMATCH",
                        detail=s.file_hash,
                    )
                )
            findings += own
            window = [
                f
                for f in market_warnings
                if f.start is not None and s.dates[0] <= f.start <= s.dates[-1]
            ]
            state, since = status(s.dates[0], s.dates[-1], [*own, *window], as_of)
            statuses.append(
                {
                    "security_id": s.security_id,
                    "symbol": s.symbol,
                    "isin": s.isin,
                    "first_date": s.dates[0],
                    "last_date": s.dates[-1],
                    "sessions": len(s.dates),
                    "usable_from": since,
                    "usable_sessions": len(s.dates) - bisect.bisect_left(s.dates, since)
                    if since
                    else 0,
                    "status": str(state),
                    "continuity_breaks": sum(1 for f in own if f.breaks_continuity),
                    "warnings": sum(1 for f in own if f.severity is Severity.WARN),
                    "failures": sum(1 for f in own if f.severity is Severity.FAIL),
                    "as_of": as_of,
                    "adjustment_version": adjustment_version,
                    "dq_version": dq_version,
                }
            )

        summary = self._summary(findings, statuses, expected, as_of)
        self._write(findings, statuses, summary, dq_version, adjustment_version)
        log_event(
            log,
            "data_quality.complete",
            dq_version=dq_version,
            securities=len(statuses),
            findings=len(findings),
            **{k.lower(): v for k, v in summary["status_counts"].items()},
        )
        return DataQualityResult(dq_version, adjustment_version, as_of, findings, statuses, summary)

    def _summary(
        self,
        findings: Sequence[Finding],
        statuses: Sequence[dict[str, Any]],
        expected: Sequence[date],
        as_of: date | None,
    ) -> dict[str, Any]:
        recent = set(expected[-self.settings.identity.active_within_sessions :])
        active = [s for s in statuses if s["last_date"] in recent]
        moved = {
            s["security_id"]
            for s in statuses
            if s["usable_from"] and s["usable_from"] > s["first_date"]
        }
        return {
            "as_of": str(as_of),
            "securities": len(statuses),
            "status_counts": dict(Counter(s["status"] for s in statuses)),
            "active_securities": len(active),
            "active_status_counts": dict(Counter(s["status"] for s in active)),
            "securities_with_usable_from_after_first_date": len(moved),
            "active_with_usable_from_after_first_date": sum(
                1 for s in active if s["security_id"] in moved
            ),
            "findings_by_code": dict(
                sorted(Counter(f"{f.severity}:{f.code}" for f in findings).items())
            ),
            "breaks_by_code": dict(
                sorted(Counter(f.code for f in findings if f.breaks_continuity).items())
            ),
        }

    def _write(
        self,
        findings: Sequence[Finding],
        statuses: Sequence[dict[str, Any]],
        summary: dict[str, Any],
        dq_version: str,
        adjustment_version: str,
    ) -> None:
        rows = [
            {
                "security_id": f.security_id,
                "start_date": f.start,
                "end_date": f.end,
                "dimension": str(f.dimension),
                "severity": str(f.severity),
                "code": f.code,
                "breaks_continuity": f.breaks_continuity,
                "detail": f.detail,
                "evidence": f.evidence,
                "dq_version": dq_version,
            }
            for f in sorted(
                findings, key=lambda f: (f.security_id or "", f.start or date.min, f.code, f.detail)
            )
        ]
        self.store.put(
            DataLakeLayout.data_quality_findings_key(self.exchange),
            to_parquet_bytes(pa.Table.from_pylist(rows, schema=FINDINGS_SCHEMA)),
        )
        self.store.put(
            DataLakeLayout.data_quality_status_key(self.exchange),
            to_parquet_bytes(pa.Table.from_pylist(list(statuses), schema=STATUS_SCHEMA)),
        )
        self.store.put(
            DataLakeLayout.data_quality_report_key(self.exchange),
            json.dumps(
                {"dq_version": dq_version, "adjustment_version": adjustment_version, **summary},
                indent=2,
                sort_keys=True,
            ).encode(),
        )
