"""Security master and identity resolution (ADR-0003, ADR-0009).

A ``security_id`` is ChartLens's own immutable identifier. External identifiers
(ISIN, SYMBOL, SERIES) are recorded as **observed spans**: the first and last date
on which the exchange published that identifier for that security. Spans are
extended only across gaps of at most ``identity.max_symbol_gap_days``; a longer gap
starts a new span, so a symbol that leaves and later returns is represented honestly.

Resolution rules, in order — the first that applies decides:

Rows with an ISIN
  1. ISIN already known → that security. (Symbol change? Same security, new SYMBOL span.)
  2. Override ``link_isin`` → the security holding the target ISIN.
  3. Same-issuer rule (exchange policy) → link to the *one* existing security whose ISIN
     has the same issuer AND whose symbol evidence connects (same symbol within the gap,
     or an exchange symbol-change notice between its symbol and this one). More than one
     candidate → IDENTITY_CONFLICT.
  4. Otherwise a new security — unless another security holds this symbol on this very
     day, which is a contradiction → IDENTITY_CONFLICT.

Rows without an ISIN (NSE legacy files before ~2011)
  5. The one security holding this symbol within the gap → that security.
  6. Else a symbol-change notice (old → new) links it to the one holder of the other symbol.
  7. Else a new security with ``identity_basis = SYMBOL_CONTINUITY``.
  More than one candidate at step 5 or 6 → AMBIGUOUS_IDENTITY.

  Rows without an ISIN are resolved only once the dataset is *anchored* (the next
  ISIN-bearing session after them has been ingested with no gap); until then they
  are quarantined as UNRESOLVED_IDENTITY and retried on reprocessing. This prevents
  the same company being created twice when history is backfilled out of order.

After deciding every row of a day: two rows mapped to one security, or two securities
claiming one symbol, are quarantined — never silently resolved.

Every decision is a pure function of (master state, the day's rows, notices,
overrides, config), and rows are processed in a fixed order, so reruns are identical.
"""

from __future__ import annotations

import tomllib
import uuid
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Protocol

import pyarrow as pa

from chartlens_core.config import IdentityConfig
from chartlens_pipeline.daily import QuarantineReason

_ID_NAMESPACE = uuid.UUID("6f1c2d6e-8b4a-5c3e-9d2f-4a6b8c0e1f23")


class IdentifierType(StrEnum):
    ISIN = "ISIN"
    SYMBOL = "SYMBOL"
    SERIES = "SERIES"


class Evidence(StrEnum):
    OBSERVED = "OBSERVED"
    """Published by the exchange for this security on these dates."""
    SAME_ISSUER_ISIN = "SAME_ISSUER_ISIN"
    """New ISIN linked by the exchange's issuer rule plus symbol evidence."""
    SYMBOL_CHANGE_NOTICE = "SYMBOL_CHANGE_NOTICE"
    """Linked through the exchange's published symbol-change list."""
    SYMBOL_CONTINUITY = "SYMBOL_CONTINUITY"
    """No ISIN available: linked by uninterrupted use of the symbol."""
    OVERRIDE = "OVERRIDE"
    """A reviewed manual decision in config/identity/{exchange}.toml."""


class IdentityBasis(StrEnum):
    ISIN = "ISIN"
    SYMBOL_CONTINUITY = "SYMBOL_CONTINUITY"


class ListingStatus(StrEnum):
    ACTIVE = "ACTIVE"
    """Traded within ``identity.active_within_sessions`` of the latest ingested session."""
    INACTIVE = "INACTIVE"
    """Not traded recently. Absence alone is never treated as delisting."""
    DELISTED = "DELISTED"
    """Only with evidence: a reviewed override (exchange delisting data arrives in M3)."""
    UNKNOWN = "UNKNOWN"


class IdentityPolicy(Protocol):
    """Exchange-specific identity knowledge."""

    def issuer_key(self, isin: str) -> str | None:
        """Key shared by ISINs of the same issuer and security type, or None if the
        exchange offers no such rule for this ISIN."""
        ...


@dataclass(frozen=True)
class SymbolChangeNotice:
    old_symbol: str
    new_symbol: str
    effective: date
    company: str


@dataclass(frozen=True)
class StatusOverride:
    status: ListingStatus
    effective: date
    reason: str


@dataclass(frozen=True)
class IdentityOverrides:
    link_isin: dict[str, str] = field(default_factory=dict)
    """new ISIN → an ISIN already in the master; both are the same security."""
    distinct_isin: frozenset[str] = frozenset()
    """ISINs that must never be linked to an existing security."""
    status: dict[str, StatusOverride] = field(default_factory=dict)
    fingerprint: str = "none"

    @classmethod
    def load(cls, path: Path | None) -> IdentityOverrides:
        if path is None or not path.is_file():
            return cls()
        import hashlib

        raw = path.read_bytes()
        data = tomllib.loads(raw.decode())
        return cls(
            link_isin={e["isin"]: e["existing_isin"] for e in data.get("link_isin", [])},
            distinct_isin=frozenset(e["isin"] for e in data.get("distinct_isin", [])),
            status={
                e["isin"]: StatusOverride(
                    ListingStatus(e["status"]), date.fromisoformat(str(e["effective"])), e["reason"]
                )
                for e in data.get("status", [])
            },
            fingerprint=hashlib.sha256(raw).hexdigest()[:12],
        )


@dataclass
class Security:
    security_id: str
    exchange: str
    identity_basis: IdentityBasis
    first_seen: date
    last_seen: date
    created_from_source: str
    security_type: str = "EQUITY"
    name: str | None = None
    name_as_of: date | None = None
    listing_status: ListingStatus = ListingStatus.UNKNOWN
    status_as_of: date | None = None


@dataclass(frozen=True)
class IdentifierSpan:
    security_id: str
    identifier_type: IdentifierType
    value: str
    valid_from: date
    valid_to: date
    evidence: Evidence
    source_id: str

    def covers(self, day: date, slack_days: int = 0) -> bool:
        slack = timedelta(days=slack_days)
        return self.valid_from - slack <= day <= self.valid_to + slack


@dataclass(frozen=True)
class Observation:
    """One in-universe row, as the resolver sees it."""

    row_number: int
    symbol: str
    series: str
    isin: str | None
    name: str | None = None


@dataclass
class Resolution:
    assigned: dict[int, str] = field(default_factory=dict)
    quarantined: dict[int, tuple[QuarantineReason, str]] = field(default_factory=dict)
    created: set[str] = field(default_factory=set)
    updated: set[str] = field(default_factory=set)
    links: list[dict[str, str]] = field(default_factory=list)


def security_id_for(exchange: str, basis: IdentityBasis, key: str) -> str:
    """Deterministic id for a newly created security (uuid5 of exchange + basis + key)."""
    return "SEC-" + str(uuid.uuid5(_ID_NAMESPACE, f"{exchange}:{basis}:{key}"))


@dataclass
class _Decision:
    obs: Observation
    security_id: str
    evidence: Evidence
    create: IdentityBasis | None = None
    link_note: str | None = None


class SecurityMaster:
    """In-memory security master for one exchange, persisted as two Parquet tables."""

    def __init__(
        self,
        exchange: str,
        securities: Iterable[Security] = (),
        spans: Iterable[IdentifierSpan] = (),
    ) -> None:
        self.exchange = exchange
        self.securities: dict[str, Security] = {s.security_id: s for s in securities}
        # Two indexes over the same spans: by identifier (resolution lookups) and by
        # security (current identifiers, persistence). Mutated only via _add/_remove.
        self._spans: dict[tuple[IdentifierType, str], list[IdentifierSpan]] = defaultdict(list)
        self._by_security: dict[str, list[IdentifierSpan]] = defaultdict(list)
        for span in spans:
            self._add(span)

    def _add(self, span: IdentifierSpan) -> None:
        self._spans[(span.identifier_type, span.value)].append(span)
        self._by_security[span.security_id].append(span)

    def _remove(self, span: IdentifierSpan) -> None:
        self._spans[(span.identifier_type, span.value)].remove(span)
        self._by_security[span.security_id].remove(span)

    # ------------------------------------------------------------------ queries

    def spans(self) -> list[IdentifierSpan]:
        return sorted(
            (s for group in self._by_security.values() for s in group),
            key=lambda s: (s.security_id, s.identifier_type, s.valid_from, s.value),
        )

    def spans_for(self, security_id: str, identifier_type: IdentifierType) -> list[IdentifierSpan]:
        return sorted(
            (
                s
                for s in self._by_security.get(security_id, ())
                if s.identifier_type is identifier_type
            ),
            key=lambda s: (s.valid_from, s.value),
        )

    def current(self, security_id: str, identifier_type: IdentifierType) -> str | None:
        spans = self.spans_for(security_id, identifier_type)
        if not spans:
            return None
        return max(spans, key=lambda s: (s.valid_to, s.valid_from, s.value)).value

    def by_isin(self, isin: str) -> set[str]:
        return {s.security_id for s in self._spans.get((IdentifierType.ISIN, isin), [])}

    def symbol_holders(self, symbol: str, day: date, slack_days: int = 0) -> set[str]:
        return {
            s.security_id
            for s in self._spans.get((IdentifierType.SYMBOL, symbol), [])
            if s.covers(day, slack_days)
        }

    def symbols_near(self, security_id: str, day: date, slack_days: int) -> set[str]:
        return {
            s.value
            for s in self.spans_for(security_id, IdentifierType.SYMBOL)
            if s.covers(day, slack_days)
        }

    def same_issuer_securities(self, issuer: str, policy: IdentityPolicy) -> set[str]:
        return {
            s.security_id
            for (t, value), group in self._spans.items()
            if t is IdentifierType.ISIN and policy.issuer_key(value) == issuer
            for s in group
        }

    # ------------------------------------------------------------------ mutation

    def _observe(
        self,
        security_id: str,
        identifier_type: IdentifierType,
        value: str,
        day: date,
        evidence: Evidence,
        source_id: str,
        gap_days: int,
    ) -> bool:
        mine = [
            s
            for s in self._by_security.get(security_id, ())
            if s.identifier_type is identifier_type and s.value == value
        ]
        if any(s.covers(day) for s in mine):
            return False
        # Merge only with spans backed by the same kind of evidence, so an inferred
        # extension (e.g. SYMBOL_CONTINUITY) never masquerades as an observed one.
        near = [s for s in mine if s.evidence is evidence and s.covers(day, gap_days)]
        if near:
            merged_from = min([day, *(s.valid_from for s in near)])
            merged_to = max([day, *(s.valid_to for s in near)])
            base = min(near, key=lambda s: s.valid_from)
            for s in near:
                self._remove(s)
            self._add(replace(base, valid_from=merged_from, valid_to=merged_to))
        else:
            self._add(
                IdentifierSpan(security_id, identifier_type, value, day, day, evidence, source_id)
            )
        return True

    def _touch(self, security_id: str, day: date, name: str | None) -> bool:
        sec = self.securities[security_id]
        changed = False
        if day < sec.first_seen:
            sec.first_seen, changed = day, True
        if day > sec.last_seen:
            sec.last_seen, changed = day, True
        if name and (sec.name_as_of is None or day >= sec.name_as_of) and name != sec.name:
            sec.name, sec.name_as_of, changed = name, day, True
        return changed

    # ------------------------------------------------------------------ resolution

    def resolve(
        self,
        observations: Sequence[Observation],
        day: date,
        *,
        source_id: str,
        policy: IdentityPolicy,
        config: IdentityConfig,
        notices: Sequence[SymbolChangeNotice] = (),
        overrides: IdentityOverrides | None = None,
        anchored: bool = True,
    ) -> Resolution:
        overrides = overrides or IdentityOverrides()
        gap = config.max_symbol_gap_days
        result = Resolution()
        decisions: list[_Decision] = []

        ordered = sorted(
            observations, key=lambda o: (o.isin is None, o.isin or "", o.symbol, o.series)
        )
        for obs in ordered:
            outcome = (
                self._decide_with_isin(obs, day, policy, config, notices, overrides)
                if obs.isin
                else self._decide_without_isin(obs, day, config, notices, anchored)
            )
            if isinstance(outcome, _Decision):
                decisions.append(outcome)
            else:
                result.quarantined[obs.row_number] = outcome

        # Cross-row checks for the day: one security per row, one security per symbol.
        by_security: dict[str, list[_Decision]] = defaultdict(list)
        by_symbol: dict[str, set[str]] = defaultdict(set)
        by_isin: dict[str, list[_Decision]] = defaultdict(list)
        for d in decisions:
            by_security[d.security_id].append(d)
            by_symbol[d.obs.symbol].add(d.security_id)
            if d.obs.isin:
                by_isin[d.obs.isin].append(d)
        accepted: list[_Decision] = []
        for d in decisions:
            same_isin = by_isin.get(d.obs.isin or "", [])
            if len(by_security[d.security_id]) > 1 or len(same_isin) > 1:
                # One instrument twice in one day (same security, or same ISIN under
                # different symbols) — never pick one, and never split it across securities.
                group = (
                    by_security[d.security_id] if len(by_security[d.security_id]) > 1 else same_isin
                )
                rows = sorted(x.obs.row_number for x in group)
                result.quarantined[d.obs.row_number] = (
                    QuarantineReason.DUPLICATE_SECURITY_DATE,
                    f"rows {rows} are the same instrument on {day}",
                )
            elif len(by_symbol[d.obs.symbol]) > 1:
                result.quarantined[d.obs.row_number] = (
                    QuarantineReason.IDENTITY_CONFLICT,
                    f"symbol {d.obs.symbol} claimed by {sorted(by_symbol[d.obs.symbol])} on {day}",
                )
            else:
                accepted.append(d)

        for d in accepted:
            if d.create is not None and d.security_id not in self.securities:
                self.securities[d.security_id] = Security(
                    security_id=d.security_id,
                    exchange=self.exchange,
                    identity_basis=d.create,
                    first_seen=day,
                    last_seen=day,
                    created_from_source=source_id,
                )
                result.created.add(d.security_id)
            changed = False
            if d.obs.isin:
                isin_evidence = d.evidence if d.link_note else Evidence.OBSERVED
                changed |= self._observe(
                    d.security_id,
                    IdentifierType.ISIN,
                    d.obs.isin,
                    day,
                    isin_evidence,
                    source_id,
                    gap,
                )
            symbol_evidence = Evidence.OBSERVED if d.obs.isin else d.evidence
            changed |= self._observe(
                d.security_id,
                IdentifierType.SYMBOL,
                d.obs.symbol,
                day,
                symbol_evidence,
                source_id,
                gap,
            )
            changed |= self._observe(
                d.security_id,
                IdentifierType.SERIES,
                d.obs.series,
                day,
                Evidence.OBSERVED,
                source_id,
                gap,
            )
            changed |= self._touch(d.security_id, day, d.obs.name)
            if changed and d.security_id not in result.created:
                result.updated.add(d.security_id)
            if d.link_note:
                result.links.append(
                    {
                        "security_id": d.security_id,
                        "row": str(d.obs.row_number),
                        "evidence": d.link_note,
                    }
                )
            result.assigned[d.obs.row_number] = d.security_id
        return result

    def _conflicting_holders(self, symbol: str, day: date, security_id: str) -> set[str]:
        return self.symbol_holders(symbol, day) - {security_id}

    def _decide_with_isin(
        self,
        obs: Observation,
        day: date,
        policy: IdentityPolicy,
        config: IdentityConfig,
        notices: Sequence[SymbolChangeNotice],
        overrides: IdentityOverrides,
    ) -> _Decision | tuple[QuarantineReason, str]:
        assert obs.isin is not None
        isin, gap = obs.isin, config.max_symbol_gap_days
        known = self.by_isin(isin)
        if len(known) > 1:
            return QuarantineReason.IDENTITY_CONFLICT, f"ISIN {isin} maps to {sorted(known)}"

        decision: _Decision | None = None
        if known:
            decision = _Decision(obs, next(iter(known)), Evidence.OBSERVED)
        elif isin in overrides.link_isin:
            target = overrides.link_isin[isin]
            targets = self.by_isin(target)
            if len(targets) != 1:
                return (
                    QuarantineReason.UNRESOLVED_IDENTITY,
                    f"override links {isin} to {target}, which is not (uniquely) known yet",
                )
            decision = _Decision(
                obs, next(iter(targets)), Evidence.OVERRIDE, link_note=f"OVERRIDE {isin}→{target}"
            )
        elif config.link_same_issuer_isin and isin not in overrides.distinct_isin:
            issuer = policy.issuer_key(isin)
            if issuer is not None:
                linked: dict[str, str] = {}
                for sid in sorted(self.same_issuer_securities(issuer, policy)):
                    near_symbols = self.symbols_near(sid, day, gap)
                    if obs.symbol in near_symbols:
                        linked[sid] = f"SAME_ISSUER_ISIN {isin} with symbol {obs.symbol}"
                        continue
                    for n in notices:
                        pair = {n.old_symbol, n.new_symbol}
                        if (
                            obs.symbol in pair
                            and (pair - {obs.symbol}) & near_symbols
                            and abs((n.effective - day).days) <= gap
                        ):
                            linked[sid] = (
                                f"SAME_ISSUER_ISIN {isin} + SYMBOL_CHANGE_NOTICE "
                                f"{n.old_symbol}→{n.new_symbol} ({n.effective})"
                            )
                            break
                if len(linked) > 1:
                    return (
                        QuarantineReason.IDENTITY_CONFLICT,
                        f"new ISIN {isin} matches several same-issuer securities {sorted(linked)}",
                    )
                if linked:
                    sid, note = next(iter(linked.items()))
                    decision = _Decision(obs, sid, Evidence.SAME_ISSUER_ISIN, link_note=note)

        if decision is None:
            new_id = security_id_for(self.exchange, IdentityBasis.ISIN, isin)
            holders = self._conflicting_holders(obs.symbol, day, new_id)
            if holders:
                return (
                    QuarantineReason.IDENTITY_CONFLICT,
                    f"new ISIN {isin} uses symbol {obs.symbol}, held on {day} by {sorted(holders)}",
                )
            return _Decision(obs, new_id, Evidence.OBSERVED, create=IdentityBasis.ISIN)

        holders = self._conflicting_holders(obs.symbol, day, decision.security_id)
        if holders:
            return (
                QuarantineReason.IDENTITY_CONFLICT,
                f"symbol {obs.symbol} held on {day} by {sorted(holders)}, but ISIN {isin} "
                f"belongs to {decision.security_id}",
            )
        return decision

    def _decide_without_isin(
        self,
        obs: Observation,
        day: date,
        config: IdentityConfig,
        notices: Sequence[SymbolChangeNotice],
        anchored: bool,
    ) -> _Decision | tuple[QuarantineReason, str]:
        if not config.resolve_without_isin:
            return QuarantineReason.UNRESOLVED_IDENTITY, "no ISIN and resolve_without_isin is off"
        if not anchored:
            return (
                QuarantineReason.UNRESOLVED_IDENTITY,
                "no ISIN; waiting for the next ISIN-bearing session to be ingested (anchor)",
            )
        gap = config.max_symbol_gap_days
        candidates = self.symbol_holders(obs.symbol, day, gap)
        evidence, note = Evidence.SYMBOL_CONTINUITY, None
        if not candidates:
            for n in notices:
                if (
                    n.old_symbol == obs.symbol
                    and day < n.effective
                    and (n.effective - day).days <= gap
                ):
                    found = self.symbol_holders(n.new_symbol, n.effective, gap)
                elif (
                    n.new_symbol == obs.symbol
                    and day >= n.effective
                    and (day - n.effective).days <= gap
                ):
                    found = self.symbol_holders(n.old_symbol, n.effective, gap)
                else:
                    continue
                if found:
                    candidates |= found
                    evidence = Evidence.SYMBOL_CHANGE_NOTICE
                    note = f"SYMBOL_CHANGE_NOTICE {n.old_symbol}→{n.new_symbol} ({n.effective})"
        if len(candidates) > 1:
            return (
                QuarantineReason.AMBIGUOUS_IDENTITY,
                f"no ISIN; symbol {obs.symbol} matches {sorted(candidates)}",
            )
        if candidates:
            sid = next(iter(candidates))
            holders = self._conflicting_holders(obs.symbol, day, sid)
            if holders:
                return (
                    QuarantineReason.IDENTITY_CONFLICT,
                    f"symbol {obs.symbol} held by {sorted(holders)}",
                )
            return _Decision(obs, sid, evidence, link_note=note)
        new_id = security_id_for(
            self.exchange, IdentityBasis.SYMBOL_CONTINUITY, f"{obs.symbol}:{day}"
        )
        return _Decision(
            obs, new_id, Evidence.SYMBOL_CONTINUITY, create=IdentityBasis.SYMBOL_CONTINUITY
        )

    # ------------------------------------------------------------------ lifecycle

    def update_listing_status(
        self, active_cutoff: date, as_of: date, overrides: IdentityOverrides | None = None
    ) -> int:
        """ACTIVE if last traded on/after ``active_cutoff``, else INACTIVE; overrides win.
        Returns the number of securities whose status changed."""
        overrides = overrides or IdentityOverrides()
        by_isin = {
            s.value: s.security_id
            for group in self._spans.values()
            for s in group
            if s.identifier_type is IdentifierType.ISIN
        }
        forced = {by_isin[i]: o for i, o in overrides.status.items() if i in by_isin}
        changed = 0
        for sec in self.securities.values():
            if sec.security_id in forced and forced[sec.security_id].effective <= as_of:
                status = forced[sec.security_id].status
            else:
                status = (
                    ListingStatus.ACTIVE
                    if sec.last_seen >= active_cutoff
                    else ListingStatus.INACTIVE
                )
            if status != sec.listing_status:
                changed += 1
            sec.listing_status, sec.status_as_of = status, as_of
        return changed

    # ------------------------------------------------------------------ persistence

    def to_tables(self) -> tuple[pa.Table, pa.Table]:
        secs = sorted(self.securities.values(), key=lambda s: s.security_id)
        securities = pa.Table.from_pylist(
            [
                {
                    "security_id": s.security_id,
                    "exchange": s.exchange,
                    "isin": self.current(s.security_id, IdentifierType.ISIN),
                    "current_symbol": self.current(s.security_id, IdentifierType.SYMBOL),
                    "current_series": self.current(s.security_id, IdentifierType.SERIES),
                    "security_name": s.name,
                    "name_as_of": s.name_as_of,
                    "security_type": s.security_type,
                    "identity_basis": str(s.identity_basis),
                    "listing_status": str(s.listing_status),
                    "status_as_of": s.status_as_of,
                    "first_seen": s.first_seen,
                    "last_seen": s.last_seen,
                    "created_from_source": s.created_from_source,
                }
                for s in secs
            ],
            schema=SECURITIES_SCHEMA,
        )
        history = pa.Table.from_pylist(
            [
                {
                    "security_id": s.security_id,
                    "identifier_type": str(s.identifier_type),
                    "identifier_value": s.value,
                    "valid_from": s.valid_from,
                    "valid_to": s.valid_to,
                    "evidence": str(s.evidence),
                    "source_id": s.source_id,
                }
                for s in self.spans()
            ],
            schema=IDENTIFIER_HISTORY_SCHEMA,
        )
        return securities, history

    @classmethod
    def from_tables(cls, exchange: str, securities: pa.Table, history: pa.Table) -> SecurityMaster:
        secs = [
            Security(
                security_id=r["security_id"],
                exchange=r["exchange"],
                identity_basis=IdentityBasis(r["identity_basis"]),
                first_seen=r["first_seen"],
                last_seen=r["last_seen"],
                created_from_source=r["created_from_source"],
                security_type=r["security_type"],
                name=r["security_name"],
                name_as_of=r["name_as_of"],
                listing_status=ListingStatus(r["listing_status"]),
                status_as_of=r["status_as_of"],
            )
            for r in securities.to_pylist()
        ]
        spans = [
            IdentifierSpan(
                security_id=r["security_id"],
                identifier_type=IdentifierType(r["identifier_type"]),
                value=r["identifier_value"],
                valid_from=r["valid_from"],
                valid_to=r["valid_to"],
                evidence=Evidence(r["evidence"]),
                source_id=r["source_id"],
            )
            for r in history.to_pylist()
        ]
        return cls(exchange, secs, spans)


SECURITY_MASTER_SCHEMA_VERSION = 1

SECURITIES_SCHEMA = pa.schema(
    [
        pa.field("security_id", pa.string(), nullable=False),
        pa.field("exchange", pa.string(), nullable=False),
        pa.field("isin", pa.string()),
        pa.field("current_symbol", pa.string()),
        pa.field("current_series", pa.string()),
        pa.field("security_name", pa.string()),
        pa.field("name_as_of", pa.date32()),
        pa.field("security_type", pa.string(), nullable=False),
        pa.field("identity_basis", pa.string(), nullable=False),
        pa.field("listing_status", pa.string(), nullable=False),
        pa.field("status_as_of", pa.date32()),
        pa.field("first_seen", pa.date32(), nullable=False),
        pa.field("last_seen", pa.date32(), nullable=False),
        pa.field("created_from_source", pa.string(), nullable=False),
    ],
    metadata={
        b"chartlens.dataset": b"securities",
        b"chartlens.schema_version": str(SECURITY_MASTER_SCHEMA_VERSION).encode(),
    },
)

IDENTIFIER_HISTORY_SCHEMA = pa.schema(
    [
        pa.field("security_id", pa.string(), nullable=False),
        pa.field("identifier_type", pa.string(), nullable=False),
        pa.field("identifier_value", pa.string(), nullable=False),
        pa.field("valid_from", pa.date32(), nullable=False),
        pa.field("valid_to", pa.date32(), nullable=False),
        pa.field("evidence", pa.string(), nullable=False),
        pa.field("source_id", pa.string(), nullable=False),
    ],
    metadata={
        b"chartlens.dataset": b"identifier_history",
        b"chartlens.schema_version": str(SECURITY_MASTER_SCHEMA_VERSION).encode(),
    },
)
