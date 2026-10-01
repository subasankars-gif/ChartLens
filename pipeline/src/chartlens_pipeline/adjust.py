"""Corporate-action factors, validation and the adjusted analytical dataset (ADR-0011).

Pipeline (every input is a stored, versioned artefact — no network):

    stored feed versions ─► records ─► interpretation (exchange grammar)
          ─► security resolution (ISIN first, then symbol as of the ex-date)
          ─► one event per (security, ex-date) ─► factor + validation decision
          ─► adjusted per-security series (raw × exact cumulative factor)

Requirements enforced for every applied adjustment (agreed 2026-10-01):

1. identifiable source — every factor carries its feed records (or a reviewed override);
2. deterministic formula — exact fractions, one rounding (``chartlens_core.adjustment``);
3. correct ex-date — the factor must fit the gap where the ex-date discontinuity appears
   (otherwise it is rejected; a factor that fits the adjacent session is reported for
   review, never moved automatically);
4. reproducible — a pure function of stored inputs, identified by ``adjustment_version``;
5. the ex-date discontinuity is consistent with the factor, or the event is SUSPECT;
6. no adjustment creates a new discontinuity — a factor that would not reduce the
   ex-date gap is not applied (REJECTED_BY_PRICE), and the run fails unless the
   market-wide report shows 0 new and 0 worsened large gaps;
7. unquantified events are never adjusted — they are hard discontinuities.

The unadjusted canonical dataset is read, never modified. The adjusted dataset is
published manifest-last: ``_manifest.json`` names the version and the content hash of
every current per-security file, and is written only when the hard requirements hold.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import statistics
import tomllib
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum
from fractions import Fraction
from itertools import pairwise
from pathlib import Path
from typing import Any, Final, cast

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from chartlens_core.adjustment import (
    FactorEvent,
    adjust_price,
    adjust_volume,
    cumulative_factors,
    fraction_text,
)
from chartlens_core.config import ChartLensSettings, config_dir
from chartlens_core.domain import utc_now
from chartlens_core.logs import log_event
from chartlens_pipeline.corporate_actions import CorporateActionStore, RecordSet
from chartlens_pipeline.corporate_actions_model import (
    ActionClass,
    ActionComponent,
    ComponentKind,
    SubjectInterpretation,
)
from chartlens_pipeline.daily import PRICE_TYPE, to_parquet_bytes
from chartlens_pipeline.identity import (
    IdentifierType,
    IdentityOverrides,
    IdentityPolicy,
    SecurityMaster,
)
from chartlens_pipeline.providers.base import CorporateActionRecord, ExchangeProvider
from chartlens_pipeline.sources import SourceRecord
from chartlens_pipeline.storage import DataLakeLayout, ObjectStore

log = logging.getLogger("chartlens.pipeline.adjust")

ADJUSTMENT_ENGINE_VERSION: Final = "adjust_v2"
"""Bump on any change to decisions or outputs: it is part of adjustment_version, so a
behaviour change can never reuse a version label (v2: CONSISTENT, STALE_ISIN, identity
breaks, narrowed face-value uncertainty)."""
ADJUSTED_SCHEMA_VERSION: Final = 1
ADJ_PRICE_TYPE: Final = pa.decimal128(24, 6)
ADJ_VOLUME_TYPE: Final = pa.decimal128(28, 4)
MATERIAL_LOG_CHANGE: Final = 1e-4
"""A gap only counts as new/worsened if it grew by more than this (plus the bound of the
adjusted prices' own rounding) in log terms; smaller differences are rounding, not a
discontinuity."""


class Resolution(StrEnum):
    ISIN = "ISIN"
    STALE_ISIN = "STALE_ISIN"
    SYMBOL = "SYMBOL"
    UNRESOLVED = "UNRESOLVED"
    CONFLICT = "CONFLICT"
    OUT_OF_UNIVERSE = "OUT_OF_UNIVERSE"


class EventStatus(StrEnum):
    VERIFIED = "VERIFIED"
    """Applied; the ex-date gap is consistent with the factor."""
    CONSISTENT = "CONSISTENT"
    """Applied; the factor is smaller than the stock's normal overnight noise, so prices
    cannot confirm it, and the ex-date gap after applying it stays within that noise."""
    SUSPECT = "SUSPECT"
    """Applied (it reduces the ex-date discontinuity) but the residual gap is abnormal."""
    NO_ADJUSTMENT = "NO_ADJUSTMENT"
    """Factor is exactly 1 (e.g. rights priced at or above the market, or a reviewed
    override recording that the event had no price effect on this security)."""
    NOT_APPLICABLE = "NOT_APPLICABLE"
    """The security did not trade on both sides of the ex-date: nothing to adjust or check."""
    PENDING = "PENDING"
    """Ex-date after the latest ingested session: not yet in effect (point-in-time)."""
    REJECTED_BY_PRICE = "REJECTED_BY_PRICE"
    """Not applied: it would not reduce the ex-date discontinuity."""
    UNQUANTIFIED = "UNQUANTIFIED"
    """No factor exists; a hard discontinuity (``usable_from`` moves after it)."""
    CONFLICTING_RECORDS = "CONFLICTING_RECORDS"
    """The feed disagrees with itself for this security and ex-date; treated as unquantified."""


APPLIED: Final = frozenset({EventStatus.VERIFIED, EventStatus.CONSISTENT, EventStatus.SUSPECT})


# ----------------------------------------------------------------------------- overrides


@dataclass(frozen=True)
class FactorOverride:
    isin: str
    ex_date: date
    factor: Fraction
    evidence: str
    reviewed_by: str


@dataclass(frozen=True)
class CorporateActionOverrides:
    """Reviewed, version-controlled decisions in ``config/corporate_actions/{ex}.toml``.

    ``[[factor]]`` supplies a factor from a primary document for an event the feed does not
    quantify (or is missing); ``factor = "1/1"`` records a reviewed *no price effect*.
    ``[[suppress]]`` removes a feed record judged wrong. Every entry carries evidence and
    the file's hash is part of ``adjustment_version``.
    """

    factors: dict[tuple[str, date], FactorOverride] = field(default_factory=dict)
    suppress: dict[str, str] = field(default_factory=dict)
    fingerprint: str = "none"

    @classmethod
    def load(cls, path: Path | None) -> CorporateActionOverrides:
        if path is None or not path.is_file():
            return cls()
        raw = path.read_bytes()
        data = tomllib.loads(raw.decode())
        factors: dict[tuple[str, date], FactorOverride] = {}
        for e in data.get("factor", []):
            num, _, den = str(e["factor"]).partition("/")
            factor = Fraction(int(num), int(den or 1))
            if factor <= 0:
                raise ValueError(f"{path}: factor must be positive: {e}")
            for key in ("evidence", "reviewed_by"):
                if not str(e.get(key, "")).strip():
                    raise ValueError(f"{path}: factor override without {key}: {e}")
            o = FactorOverride(
                isin=str(e["isin"]).upper(),
                ex_date=date.fromisoformat(str(e["ex_date"])),
                factor=factor,
                evidence=e["evidence"],
                reviewed_by=e["reviewed_by"],
            )
            factors[(o.isin, o.ex_date)] = o
        suppress: dict[str, str] = {}
        for e in data.get("suppress", []):
            if not str(e.get("reason", "")).strip():
                raise ValueError(f"{path}: suppress entry without reason: {e}")
            suppress[e["record_key"]] = e["reason"]
        return cls(factors, suppress, hashlib.sha256(raw).hexdigest()[:12])


# ----------------------------------------------------------------------------- history


@dataclass
class SecurityHistory:
    security_id: str
    dates: list[date]
    open: list[Decimal]
    high: list[Decimal]
    low: list[Decimal]
    close: list[Decimal]
    volume: list[int]
    symbol: list[str]
    series: list[str]
    isin: list[str | None]
    source_hash: list[str]

    def index_before(self, day: date) -> int | None:
        """Index of the last row dated strictly before ``day``."""
        lo, hi = 0, len(self.dates)
        while lo < hi:
            mid = (lo + hi) // 2
            if self.dates[mid] < day:
                lo = mid + 1
            else:
                hi = mid
        return lo - 1 if lo > 0 else None

    def boundary(self, day: date) -> int | None:
        """Index of the first row on/after ``day`` when rows exist on both sides, else None."""
        prev = self.index_before(day)
        if prev is None or prev + 1 >= len(self.dates):
            return None
        return prev + 1

    def gap(self, i: int) -> float:
        """ln(open_i / close_{i-1}) — the overnight gap into row i."""
        return math.log(float(self.open[i]) / float(self.close[i - 1]))


_DAILY_COLUMNS: Final = (
    "security_id",
    "trading_date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "symbol",
    "series",
    "isin",
    "source_file_hash",
)


class DailyIndex:
    """The canonical daily dataset held columnar, materialised one security at a time."""

    def __init__(self, table: pa.Table) -> None:
        self.table = table.sort_by([("security_id", "ascending"), ("trading_date", "ascending")])
        self.spans: dict[str, tuple[int, int]] = {}
        self.last_date: date | None = None
        if self.table.num_rows == 0:
            return
        encoded = cast(
            "pa.DictionaryArray[Any, Any]",
            pc.dictionary_encode(self.table.column("security_id")).combine_chunks(),
        )
        indices = np.asarray(encoded.indices)
        bounds = np.flatnonzero(np.diff(indices)) + 1
        starts = np.concatenate(([0], bounds)).tolist()
        ends = np.concatenate((bounds, [self.table.num_rows])).tolist()
        dictionary = encoded.dictionary
        for s, e in zip(starts, ends, strict=True):
            sid = dictionary[int(indices[s])].as_py()
            self.spans[str(sid)] = (int(s), int(e))
        last = pc.max(self.table.column("trading_date")).as_py()
        self.last_date = last if isinstance(last, date) else None

    @classmethod
    def load(cls, store: ObjectStore, exchange: str, *, workers: int = 16) -> DailyIndex:
        keys = [
            k
            for k in store.list(DataLakeLayout.curated_daily_prefix(exchange))
            if k.endswith(".parquet")
        ]

        def read(key: str) -> pa.Table:
            return pq.read_table(pa.BufferReader(store.get(key)), columns=list(_DAILY_COLUMNS))

        with ThreadPoolExecutor(max_workers=workers) as pool:
            tables = list(pool.map(read, keys))
        if not tables:
            return cls(pa.table({c: pa.array([], pa.string()) for c in _DAILY_COLUMNS}))
        return cls(pa.concat_tables(tables))

    @property
    def security_ids(self) -> list[str]:
        return sorted(self.spans)

    def history(self, security_id: str) -> SecurityHistory | None:
        span = self.spans.get(security_id)
        if span is None:
            return None
        part = self.table.slice(span[0], span[1] - span[0]).to_pydict()
        dates: list[date] = part["trading_date"]
        if any(b <= a for a, b in pairwise(dates)):
            raise ValueError(f"{security_id}: canonical rows are not one per session")
        return SecurityHistory(
            security_id=security_id,
            dates=dates,
            open=part["open"],
            high=part["high"],
            low=part["low"],
            close=part["close"],
            volume=part["volume"],
            symbol=part["symbol"],
            series=part["series"],
            isin=part["isin"],
            source_hash=part["source_file_hash"],
        )


# ----------------------------------------------------------------------------- actions


@dataclass
class ResolvedAction:
    record: CorporateActionRecord
    source: SourceRecord
    interpretation: SubjectInterpretation
    security_id: str | None
    resolution: Resolution
    detail: str = ""
    suppressed: str | None = None


def _live(master: SecurityMaster, sid: str, day: date, gap_days: int) -> bool:
    return any(
        span.covers(day, gap_days)
        for kind in (IdentifierType.SYMBOL, IdentifierType.ISIN)
        for span in master.spans_for(sid, kind)
    )


def resolve_action(
    rec: CorporateActionRecord,
    master: SecurityMaster,
    universe: frozenset[str],
    gap_days: int,
    policy: IdentityPolicy | None = None,
) -> tuple[str | None, Resolution, str]:
    """Which security a feed record is about. ISIN first; symbol as of the ex-date only
    when the ISIN is unknown; any disagreement is a conflict, never a guess.

    One evidence-based exception (``STALE_ISIN``): NSE's feed sometimes carries an
    issuer's *earlier* ISIN. When the ISIN's security was not trading around the ex-date,
    exactly one security held the symbol then, and that security has an ISIN of the same
    issuer under the exchange's identity policy, the record belongs to the live security.
    """
    if rec.series is not None and rec.series not in universe:
        return None, Resolution.OUT_OF_UNIVERSE, f"series {rec.series}"
    if rec.ex_date is None:
        return None, Resolution.UNRESOLVED, "no ex-date"
    by_isin = master.by_isin(rec.isin) if rec.isin else set()
    by_symbol = master.symbol_holders(rec.symbol, rec.ex_date, gap_days)
    if len(by_isin) > 1:
        return None, Resolution.CONFLICT, f"ISIN {rec.isin} maps to {sorted(by_isin)}"
    if by_isin:
        sid = next(iter(by_isin))
        if (
            policy is not None
            and rec.isin is not None
            and len(by_symbol) == 1
            and sid not in by_symbol
            and not _live(master, sid, rec.ex_date, gap_days)
        ):
            live = next(iter(by_symbol))
            issuer = policy.issuer_key(rec.isin)
            live_issuers = {
                policy.issuer_key(span.value)
                for span in master.spans_for(live, IdentifierType.ISIN)
            }
            if issuer is not None and issuer in live_issuers:
                detail = f"ISIN {rec.isin} → {sid} (not trading then); same issuer as {live}"
                return live, Resolution.STALE_ISIN, detail
        if by_symbol and sid not in by_symbol:
            detail = f"ISIN {rec.isin} → {sid} but symbol {rec.symbol} → {sorted(by_symbol)}"
            return None, Resolution.CONFLICT, detail
        return sid, Resolution.ISIN, ""
    if len(by_symbol) == 1:
        return next(iter(by_symbol)), Resolution.SYMBOL, f"ISIN {rec.isin or '-'} not in master"
    if len(by_symbol) > 1:
        return None, Resolution.CONFLICT, f"symbol {rec.symbol} → {sorted(by_symbol)}"
    return None, Resolution.UNRESOLVED, "no matching security in the master"


# ----------------------------------------------------------------------------- events


@dataclass
class EventDecision:
    security_id: str
    ex_date: date
    action_class: ActionClass
    record_keys: list[str]
    subjects: list[str]
    components: list[ActionComponent]
    method: str
    factor: Fraction | None
    inputs: dict[str, str]
    status: EventStatus
    breaks_continuity: bool = False
    raw_gap: float | None = None
    residual: float | None = None
    tolerance: float | None = None
    boundary_date: date | None = None
    """First traded row on/after the ex-date (where the discontinuity appears)."""
    notes: list[str] = field(default_factory=list)
    override: str | None = None

    @property
    def applied(self) -> bool:
        return self.status in APPLIED and self.factor is not None


_EQUITY_KINDS: Final = frozenset(
    {ComponentKind.SPLIT, ComponentKind.CONSOLIDATION, ComponentKind.BONUS, ComponentKind.RIGHTS}
)
_FV_KINDS: Final = frozenset({ComponentKind.SPLIT, ComponentKind.CONSOLIDATION})


def _component_key(c: ActionComponent) -> tuple[object, ...]:
    return (
        c.kind,
        c.ratio_new,
        c.ratio_held,
        c.fv_from,
        c.fv_to,
        c.premium,
        c.issue_price,
        c.at_par,
    )


_CAPITAL_CHANGE: Final = re.compile(r"reduc|consolidat")


def _changes_capital(a: ResolvedAction) -> bool:
    """An unquantified action that may change the face value itself (capital reduction,
    consolidation). Demergers, mergers and schemes without such wording leave the
    company's face value unchanged, so earlier face values stay reconstructible."""
    return a.interpretation.action_class is ActionClass.UNQUANTIFIED and bool(
        _CAPITAL_CHANGE.search(a.record.subject.lower())
    )


def face_value_at(
    day: date,
    current_fv: Decimal | None,
    fv_changes: Sequence[tuple[date, ActionComponent]],
    uncertain_after: Sequence[date],
) -> tuple[Decimal | None, list[str]]:
    """Face value in force just before ``day``, reconstructed backwards from today's.

    NSE reports the *current* face value on every record, so the value at an earlier date
    is recovered by undoing every split/consolidation with a later ex-date. An
    unquantified capital change after ``day`` makes the chain unknowable.
    """
    if any(d > day for d in uncertain_after):
        return None, ["face value unknown: an unquantified capital change follows this date"]
    later = sorted(((d, c) for d, c in fv_changes if d > day), key=lambda x: x[0], reverse=True)
    notes: list[str] = []
    fv = current_fv
    for d, c in later:  # latest first: each change's fv_to should equal the value after it
        if fv is not None and c.fv_to is not None and fv != c.fv_to:
            notes.append(f"face-value chain mismatch at {d}: expected {c.fv_to}, have {fv}")
        fv = c.fv_from
    return fv, notes


def component_factor(
    c: ActionComponent, cum_close: Decimal | None, fv: Decimal | None
) -> tuple[Fraction | None, dict[str, str], list[str]]:
    """Exact price factor for one quantified component (ADR-0011 formulas)."""
    if c.kind in _FV_KINDS:
        assert c.fv_from is not None and c.fv_to is not None
        factor = Fraction(c.fv_to) / Fraction(c.fv_from)
        return factor, {"fv_from": str(c.fv_from), "fv_to": str(c.fv_to)}, []
    if c.kind is ComponentKind.BONUS:
        assert c.ratio_new is not None and c.ratio_held is not None
        a, b = Fraction(c.ratio_new), Fraction(c.ratio_held)
        return b / (a + b), {"bonus": f"{c.ratio_new}:{c.ratio_held}"}, []
    if c.kind is ComponentKind.RIGHTS:
        assert c.ratio_new is not None and c.ratio_held is not None
        if c.issue_price is not None:
            price = c.issue_price
        elif fv is None:
            return None, {}, ["rights price needs the face value at the time, which is unknown"]
        else:
            price = fv + (c.premium or Decimal(0))
        if cum_close is None:
            return None, {}, ["rights factor needs the close before the ex-date; none traded"]
        a, b = Fraction(c.ratio_new), Fraction(c.ratio_held)
        close, issue = Fraction(cum_close), Fraction(price)
        inputs = {
            "rights": f"{c.ratio_new}:{c.ratio_held}",
            "issue_price": str(price),
            "cum_rights_close": str(cum_close),
            "face_value": "" if fv is None else str(fv),
        }
        if issue >= close:
            return Fraction(1), inputs, ["rights priced at or above the market: no adjustment"]
        terp = (b * close + a * issue) / (a + b)
        return terp / close, inputs, []
    return None, {}, [f"{c.kind} is not a quantified component"]


def robust_sigma(h: SecurityHistory, upto: int, window: int, exclude: set[int]) -> float | None:
    """Median-absolute-deviation sigma of overnight gaps into rows ≤ ``upto`` (point-in-time:
    only sessions before the event). Other event sessions are excluded."""
    gaps = [h.gap(i) for i in range(max(1, upto - window + 1), upto + 1) if i not in exclude]
    if len(gaps) < 20:
        return None
    med = statistics.median(gaps)
    return 1.4826 * statistics.median(abs(g - med) for g in gaps)


@dataclass(frozen=True)
class Validation:
    status: EventStatus
    raw_gap: float | None = None
    residual: float | None = None
    tolerance: float | None = None
    notes: tuple[str, ...] = ()


def validate(
    h: SecurityHistory | None,
    ex_date: date,
    factor: Fraction,
    settings: ChartLensSettings,
    event_rows: set[int],
) -> Validation:
    """Check a factor against the overnight gap where the ex-date discontinuity appears.

    NSE's PREVCLOSE is not adjusted on ex-dates, so the evidence is the gap between the
    last close before the ex-date and the first open on/after it.
    """
    if factor == 1:
        return Validation(EventStatus.NO_ADJUSTMENT)
    first = h.boundary(ex_date) if h is not None else None
    if h is None or first is None:
        return Validation(
            EventStatus.NOT_APPLICABLE, notes=("no trades on both sides of the ex-date",)
        )
    cfg = settings.adjustment
    raw = h.gap(first)
    log_f = math.log(factor.numerator) - math.log(factor.denominator)
    residual = raw - log_f
    sigma = robust_sigma(h, first - 1, cfg.validation_window, event_rows)
    tol = max(cfg.validation_min_tolerance, cfg.validation_sigma_multiplier * (sigma or 0.0))
    if abs(log_f) <= tol:
        # The factor is smaller than this stock's normal overnight noise, so the price can
        # neither confirm nor contradict it. Apply it only if the ex-date gap afterwards
        # stays within noise (and below a large gap) or shrinks.
        if abs(residual) <= min(tol, math.log1p(cfg.gap_report_threshold)):
            return Validation(EventStatus.CONSISTENT, raw, residual, tol)
        if abs(residual) < abs(raw):
            return Validation(EventStatus.SUSPECT, raw, residual, tol)
        return Validation(
            EventStatus.REJECTED_BY_PRICE,
            raw,
            residual,
            tol,
            ("factor within normal noise, but the gap after applying it would not be",),
        )
    if abs(residual) < abs(raw):
        status = EventStatus.VERIFIED if abs(residual) <= tol else EventStatus.SUSPECT
        return Validation(status, raw, residual, tol)
    notes: list[str] = []
    for j in (first - 1, first + 1):  # requirement 3: report, never auto-correct
        if 1 <= j < len(h.dates) and abs(h.gap(j) - log_f) <= tol:
            notes.append(f"factor fits the gap into {h.dates[j]} instead: ex-date may be wrong")
    return Validation(EventStatus.REJECTED_BY_PRICE, raw, residual, tol, tuple(notes))


def decide_security(
    security_id: str,
    actions: Sequence[ResolvedAction],
    h: SecurityHistory | None,
    overrides: Mapping[date, FactorOverride],
    settings: ChartLensSettings,
    data_end: date | None,
) -> list[EventDecision]:
    """One decision per ex-date with a price-relevant action or a reviewed override."""
    live = [a for a in actions if not a.suppressed and a.record.ex_date is not None]
    groups: dict[date, list[ResolvedAction]] = defaultdict(list)
    current_fv: tuple[date, Decimal] | None = None
    fv_changes: list[tuple[date, ActionComponent]] = []
    uncertain: list[date] = []
    for a in live:
        ex = a.record.ex_date
        assert ex is not None
        groups[ex].append(a)
        fv = a.record.face_value
        if fv is not None and (current_fv is None or ex >= current_fv[0]):
            current_fv = (ex, fv)
        fv_changes += [(ex, c) for c in a.interpretation.components if c.kind in _FV_KINDS]
        if _changes_capital(a):
            uncertain.append(ex)

    decisions: list[EventDecision] = []
    for ex in sorted(set(groups) | set(overrides)):
        group = groups.get(ex, [])
        classes = {a.interpretation.action_class for a in group}
        o = overrides.get(ex)
        if o is None and not classes & {ActionClass.EQUITY_ADJUSTMENT, ActionClass.UNQUANTIFIED}:
            continue
        comps: dict[tuple[object, ...], ActionComponent] = {}
        for a in group:
            for c in a.interpretation.components:
                if c.kind in _EQUITY_KINDS:
                    comps.setdefault(_component_key(c), c)
        unquantified = ActionClass.UNQUANTIFIED in classes
        d = EventDecision(
            security_id=security_id,
            ex_date=ex,
            action_class=ActionClass.UNQUANTIFIED
            if unquantified
            else ActionClass.EQUITY_ADJUSTMENT,
            record_keys=sorted(a.record.record_key for a in group),
            subjects=sorted({a.record.subject for a in group}),
            components=sorted(comps.values(), key=lambda c: str(_component_key(c))),
            method="NONE",
            factor=None,
            inputs={},
            status=EventStatus.UNQUANTIFIED,
        )
        kinds = Counter(c.kind for c in comps.values())
        if o is not None:
            d.method, d.factor, d.override = "OVERRIDE", o.factor, f"{o.isin}@{o.ex_date}"
            d.inputs = {"evidence": o.evidence, "reviewed_by": o.reviewed_by}
        elif unquantified:
            d.status = EventStatus.UNQUANTIFIED
        elif any(n > 1 for n in kinds.values()):
            d.status = EventStatus.CONFLICTING_RECORDS
            d.notes.append("records state different terms for the same ex-date")
        else:
            prev = h.index_before(ex) if h is not None else None
            cum_close = h.close[prev] if h is not None and prev is not None else None
            fv, fv_notes = face_value_at(
                ex, current_fv[1] if current_fv else None, fv_changes, uncertain
            )
            factor: Fraction | None = Fraction(1)
            methods: list[str] = []
            for c in d.components:
                f, inputs, notes = component_factor(c, cum_close, fv)
                d.notes += notes
                if f is None:
                    factor = None
                    d.notes += fv_notes
                    break
                factor = (factor or Fraction(1)) * f
                d.inputs.update(inputs)
                methods.append(str(c.kind))
            if factor is None:
                d.status = EventStatus.UNQUANTIFIED
                d.notes.append("terms readable but not computable from the evidence held")
            else:
                d.method, d.factor = "+".join(methods), factor
        if data_end is not None and ex > data_end:
            d.status = EventStatus.PENDING
            d.notes.append("ex-date after the latest ingested session: not yet in effect")
        decisions.append(d)

    event_rows: set[int] = set()
    if h is not None:
        event_rows = {i for d in decisions if (i := h.boundary(d.ex_date)) is not None}
    # Factors whose discontinuity appears on the same session are checked *jointly*: the
    # one observed gap is the evidence for their product (e.g. two ex-dates with no
    # trading between them). Validating each alone could apply both and overshoot.
    joint: dict[int, list[EventDecision]] = defaultdict(list)
    for d in decisions:
        first = h.boundary(d.ex_date) if h is not None else None
        if h is not None and first is not None:
            d.boundary_date, d.raw_gap = h.dates[first], h.gap(first)
        if d.status is EventStatus.PENDING:
            continue
        if d.factor is None:  # UNQUANTIFIED / CONFLICTING_RECORDS: a hard break where data spans it
            d.breaks_continuity = first is not None
        elif d.factor == 1 or first is None:
            v = validate(h, d.ex_date, d.factor, settings, event_rows)
            d.status = v.status
            d.notes += v.notes
        else:
            joint[first].append(d)
    for group in joint.values():
        product = math.prod((d.factor for d in group if d.factor is not None), start=Fraction(1))
        v = validate(h, group[0].ex_date, product, settings, event_rows)
        for d in group:
            d.status, d.residual, d.tolerance = v.status, v.residual, v.tolerance
            d.notes += v.notes
            if len(group) > 1:
                others = ", ".join(str(o.ex_date) for o in group if o is not d)
                d.notes.append(f"validated jointly with {others} (same first session)")
            if v.status is EventStatus.REJECTED_BY_PRICE:
                # The factor does not explain the data; a large gap left at the ex-date is a
                # discontinuity of unknown cause.
                assert v.raw_gap is not None and v.tolerance is not None
                d.breaks_continuity = abs(v.raw_gap) > v.tolerance
    return decisions


def decide_events(
    actions: Sequence[ResolvedAction],
    histories: Mapping[str, SecurityHistory],
    overrides: Mapping[tuple[str, date], FactorOverride],
    settings: ChartLensSettings,
    data_end: date | None,
) -> list[EventDecision]:
    """All decisions, for callers holding every history in memory (tests, small runs)."""
    by_sec: dict[str, list[ResolvedAction]] = defaultdict(list)
    for a in actions:
        if a.security_id is not None:
            by_sec[a.security_id].append(a)
    out: list[EventDecision] = []
    for sid in sorted(set(by_sec) | {s for s, _ in overrides}):
        sec_overrides = {d: o for (s, d), o in overrides.items() if s == sid}
        out += decide_security(
            sid, by_sec.get(sid, []), histories.get(sid), sec_overrides, settings, data_end
        )
    return out


# ----------------------------------------------------------------------------- adjusted dataset


def adjusted_schema() -> pa.Schema:
    return pa.schema(
        [
            pa.field("exchange", pa.string(), nullable=False),
            pa.field("security_id", pa.string(), nullable=False),
            pa.field("trading_date", pa.date32(), nullable=False),
            pa.field("symbol", pa.string(), nullable=False),
            pa.field("series", pa.string(), nullable=False),
            pa.field("isin", pa.string()),
            pa.field("open", PRICE_TYPE, nullable=False),
            pa.field("high", PRICE_TYPE, nullable=False),
            pa.field("low", PRICE_TYPE, nullable=False),
            pa.field("close", PRICE_TYPE, nullable=False),
            pa.field("volume", pa.int64(), nullable=False),
            pa.field("price_factor", pa.string(), nullable=False),
            pa.field("volume_factor", pa.string(), nullable=False),
            pa.field("adj_open", ADJ_PRICE_TYPE, nullable=False),
            pa.field("adj_high", ADJ_PRICE_TYPE, nullable=False),
            pa.field("adj_low", ADJ_PRICE_TYPE, nullable=False),
            pa.field("adj_close", ADJ_PRICE_TYPE, nullable=False),
            pa.field("adj_volume", ADJ_VOLUME_TYPE, nullable=False),
            pa.field("next_factor_date", pa.date32()),
            pa.field("break_before", pa.bool_(), nullable=False),
            pa.field("source_file_hash", pa.string(), nullable=False),
        ],
        metadata={
            b"chartlens.dataset": b"daily_adjusted",
            b"chartlens.schema_version": str(ADJUSTED_SCHEMA_VERSION).encode(),
            b"chartlens.adjustment_engine": ADJUSTMENT_ENGINE_VERSION.encode(),
        },
    )


@dataclass
class AdjustedSeries:
    table: pa.Table
    adj_open: list[Decimal]
    adj_close: list[Decimal]
    factor_rows: int


def build_adjusted(
    exchange: str, h: SecurityHistory, decisions: Sequence[EventDecision]
) -> AdjustedSeries:
    """Raw × exact cumulative factor (latest view: every applied event up to the data end).

    ``break_before`` marks the first row after a continuity break; ``next_factor_date``
    lets an as-of reader rescale exactly (see ``chartlens_core.adjustment``).
    """
    applied = [
        FactorEvent(d.ex_date, d.factor, 1 / d.factor, tuple(d.record_keys))
        for d in decisions
        if d.applied and d.factor is not None
    ]
    cum = cumulative_factors(h.dates, applied)
    breaks = {d.boundary_date for d in decisions if d.breaks_continuity and d.boundary_date}
    ex_dates = sorted({e.ex_date for e in applied})
    texts: dict[Fraction, str] = {}

    def text(f: Fraction) -> str:
        if f not in texts:
            texts[f] = fraction_text(f)
        return texts[f]

    def prices(values: list[Decimal]) -> list[Decimal]:
        return [
            v if pf == 1 else adjust_price(v, pf) for v, (pf, _) in zip(values, cum, strict=True)
        ]

    nxt: list[date | None] = []
    j = 0
    for day in h.dates:
        while j < len(ex_dates) and ex_dates[j] <= day:
            j += 1
        nxt.append(ex_dates[j] if j < len(ex_dates) else None)

    adj_open, adj_close = prices(h.open), prices(h.close)
    n = len(h.dates)
    columns: list[pa.Array[Any]] = [
        pa.array([exchange] * n, pa.string()),
        pa.array([h.security_id] * n, pa.string()),
        pa.array(h.dates, pa.date32()),
        pa.array(h.symbol, pa.string()),
        pa.array(h.series, pa.string()),
        pa.array(h.isin, pa.string()),
        pa.array(h.open, PRICE_TYPE),
        pa.array(h.high, PRICE_TYPE),
        pa.array(h.low, PRICE_TYPE),
        pa.array(h.close, PRICE_TYPE),
        pa.array(h.volume, pa.int64()),
        pa.array([text(pf) for pf, _ in cum], pa.string()),
        pa.array([text(vf) for _, vf in cum], pa.string()),
        pa.array(adj_open, ADJ_PRICE_TYPE),
        pa.array(prices(h.high), ADJ_PRICE_TYPE),
        pa.array(prices(h.low), ADJ_PRICE_TYPE),
        pa.array(adj_close, ADJ_PRICE_TYPE),
        pa.array(
            [
                Decimal(v) if vf == 1 else adjust_volume(v, vf)
                for v, (_, vf) in zip(h.volume, cum, strict=True)
            ],
            ADJ_VOLUME_TYPE,
        ),
        pa.array(nxt, pa.date32()),
        pa.array([d in breaks for d in h.dates], pa.bool_()),
        pa.array(h.source_hash, pa.string()),
    ]
    table = pa.Table.from_arrays(columns, schema=adjusted_schema())
    return AdjustedSeries(table, adj_open, adj_close, sum(1 for pf, _ in cum if pf != 1))


# ----------------------------------------------------------------------------- discontinuity report


@dataclass
class DiscontinuityReport:
    """Market-wide large overnight gaps (|open / previous close − 1| > threshold).

    ``new_gaps_introduced`` and ``gaps_worsened`` must be 0 (hard requirement 6).
    """

    threshold: float
    raw_large_gaps: int = 0
    resolved_by_adjustment: int = 0
    adjusted_large_gaps: int = 0
    new_gaps_introduced: int = 0
    gaps_worsened: int = 0
    at_unquantified_events: int = 0
    at_rejected_or_suspect_events: int = 0
    at_cash_distributions: int = 0
    at_reviewed_identity_breaks: int = 0
    unexplained: int = 0
    new_gap_examples: list[str] = field(default_factory=list)

    @property
    def hard_requirements_met(self) -> bool:
        return self.new_gaps_introduced == 0 and self.gaps_worsened == 0


@dataclass(frozen=True)
class UnexplainedGap:
    security_id: str
    trading_date: date
    gap: float
    """Adjusted open / previous adjusted close − 1."""
    prev_close: Decimal
    """Raw previous close (price level, e.g. tick-size effects below ₹1)."""
    days_since_previous_session: int
    session_index: int
    """0-based position in the security's history (small = just listed)."""


def tally_gaps(
    report: DiscontinuityReport,
    h: SecurityHistory,
    series: AdjustedSeries,
    decisions: Sequence[EventDecision],
    cash_dates: Iterable[date],
    identity_breaks: Iterable[date] = (),
) -> list[UnexplainedGap]:
    """Add one security to the report; returns its remaining unexplained large gaps."""
    t = report.threshold
    applied_at = {d.boundary_date for d in decisions if d.applied}
    unq_at = {
        d.boundary_date
        for d in decisions
        if d.status in (EventStatus.UNQUANTIFIED, EventStatus.CONFLICTING_RECORDS)
    }
    bad_at = {
        d.boundary_date
        for d in decisions
        if d.status in (EventStatus.REJECTED_BY_PRICE, EventStatus.SUSPECT)
    }
    cash_at = {h.dates[i] for cd in cash_dates if (i := h.boundary(cd)) is not None}
    identity_at = {h.dates[i] for d in identity_breaks if (i := h.boundary(d)) is not None}
    unexplained: list[UnexplainedGap] = []
    for i in range(1, len(h.dates)):
        if series.adj_open[i] <= 0 or series.adj_close[i - 1] <= 0:
            report.new_gaps_introduced += 1  # an adjusted price that rounds to zero
            report.new_gap_examples.append(f"{h.security_id} {h.dates[i]} adjusted price ≤ 0")
            continue
        raw_ratio = float(h.open[i]) / float(h.close[i - 1])
        adj_ratio = float(series.adj_open[i]) / float(series.adj_close[i - 1])
        raw_big, adj_big = abs(raw_ratio - 1) > t, abs(adj_ratio - 1) > t
        # Rounding each adjusted price to 1e-6 moves the log ratio by at most
        # 0.5e-6/open + 0.5e-6/close; only growth beyond that (and MATERIAL) is real.
        rounding = 0.5e-6 / float(series.adj_open[i]) + 0.5e-6 / float(series.adj_close[i - 1])
        grew = abs(math.log(adj_ratio)) - abs(math.log(raw_ratio)) > MATERIAL_LOG_CHANGE + rounding
        day = h.dates[i]
        if raw_big:
            report.raw_large_gaps += 1
            if not adj_big and day in applied_at:
                report.resolved_by_adjustment += 1
        if not adj_big:
            continue
        report.adjusted_large_gaps += 1
        if grew:
            if raw_big:
                report.gaps_worsened += 1
            else:
                report.new_gaps_introduced += 1
            if len(report.new_gap_examples) < 50:
                report.new_gap_examples.append(
                    f"{h.security_id} {day} raw {raw_ratio - 1:+.4f} adj {adj_ratio - 1:+.4f}"
                )
        if day in unq_at:
            report.at_unquantified_events += 1
        elif day in bad_at or day in applied_at:
            report.at_rejected_or_suspect_events += 1
        elif day in cash_at:
            report.at_cash_distributions += 1
        elif day in identity_at:
            report.at_reviewed_identity_breaks += 1
        else:
            report.unexplained += 1
            unexplained.append(
                UnexplainedGap(
                    h.security_id,
                    day,
                    adj_ratio - 1,
                    h.close[i - 1],
                    (day - h.dates[i - 1]).days,
                    i,
                )
            )
    return unexplained


# ----------------------------------------------------------------------------- service


@dataclass
class AdjustmentResult:
    adjustment_version: str
    identity_version: str
    data_end: date | None
    actions: list[ResolvedAction]
    decisions: list[EventDecision]
    report: DiscontinuityReport
    counts: dict[str, Any]
    unexplained_gaps: dict[str, list[UnexplainedGap]]
    unresolved_overrides: list[str]
    published: bool


def _sha12(payload: object) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:12]


class AdjustmentService:
    def __init__(
        self,
        settings: ChartLensSettings,
        provider: ExchangeProvider,
        store: ObjectStore,
        *,
        overrides: CorporateActionOverrides | None = None,
        identity_overrides: IdentityOverrides | None = None,
        today: Callable[[], date] = lambda: utc_now().date(),
    ) -> None:
        self.settings = settings
        self.provider = provider
        self.exchange = provider.exchange_code
        self.store = store
        self.ca = CorporateActionStore(
            self.exchange, provider.corporate_actions, store, today=today
        )
        self._today = today
        if overrides is None:
            directory = config_dir()
            path = (
                directory / "corporate_actions" / f"{self.exchange.lower()}.toml"
                if directory
                else None
            )
            overrides = CorporateActionOverrides.load(path)
        self.overrides = overrides
        if identity_overrides is None:
            directory = config_dir()
            name = f"{self.exchange.lower()}.toml"
            identity_overrides = IdentityOverrides.load(
                directory / "identity" / name if directory else None
            )
        self.identity_overrides = identity_overrides

    @property
    def first_month(self) -> date:
        return getattr(self.settings.providers, self.exchange.lower()).corporate_actions_first_month

    def load_master(self) -> tuple[SecurityMaster, str]:
        def table(key: str) -> pa.Table:
            return pq.read_table(pa.BufferReader(self.store.get(key)))

        sec = table(DataLakeLayout.securities_key(self.exchange))
        hist = table(DataLakeLayout.identifier_history_key(self.exchange))
        state_key = DataLakeLayout.identity_state_key(self.exchange)
        state = json.loads(self.store.get(state_key)) if self.store.exists(state_key) else {}
        identity_version = "id-" + _sha12(state.get("inputs", {}))
        return SecurityMaster.from_tables(self.exchange, sec, hist), identity_version

    def records(self) -> RecordSet:
        return self.ca.current_records(self.first_month, self._today() + timedelta(days=366))

    def resolve_actions(self, records: RecordSet, master: SecurityMaster) -> list[ResolvedAction]:
        universe = frozenset(self.settings.universe.series)
        gap = self.settings.identity.max_symbol_gap_days
        source = self.provider.corporate_actions
        policy = self.provider.identity_policy()
        out: list[ResolvedAction] = []
        for rec, src in records.records:
            sid, how, detail = resolve_action(rec, master, universe, gap, policy)
            out.append(
                ResolvedAction(
                    rec,
                    src,
                    source.interpret(rec.subject),
                    sid,
                    how,
                    detail,
                    self.overrides.suppress.get(rec.record_key),
                )
            )
        return out

    def resolve_overrides(
        self, master: SecurityMaster
    ) -> tuple[dict[tuple[str, date], FactorOverride], list[str]]:
        resolved: dict[tuple[str, date], FactorOverride] = {}
        unresolved: list[str] = []
        for (isin, ex), o in sorted(self.overrides.factors.items()):
            sids = master.by_isin(isin)
            if len(sids) == 1:
                resolved[(next(iter(sids)), ex)] = o
            else:
                unresolved.append(f"{isin}@{ex}: ISIN maps to {sorted(sids) or 'nothing'}")
        return resolved, unresolved

    def adjustment_version(self, records: RecordSet, identity_version: str) -> str:
        source = self.provider.corporate_actions
        return "adj-" + _sha12(
            {
                "engine": ADJUSTMENT_ENGINE_VERSION,
                "feed_parser": source.parser_version,
                "grammar": source.grammar_version,
                "methodology": self.settings.adjustment.model_dump(mode="json"),
                "universe": self.settings.universe.model_dump(mode="json"),
                "overrides": self.overrides.fingerprint,
                "feed_sources": sorted({src.content_hash for _, src in records.records}),
                "identity": identity_version,
                "schema": ADJUSTED_SCHEMA_VERSION,
            }
        )

    def _previous_manifest(self) -> dict[str, Any]:
        key = DataLakeLayout.adjusted_manifest_key(self.exchange)
        return json.loads(self.store.get(key)) if self.store.exists(key) else {}

    def run(self) -> AdjustmentResult:
        master, identity_version = self.load_master()
        records = self.records()
        actions = self.resolve_actions(records, master)
        override_events, unresolved_overrides = self.resolve_overrides(master)
        daily = DailyIndex.load(self.store, self.exchange)
        version = self.adjustment_version(records, identity_version)
        log_event(
            log,
            "adjust.start",
            adjustment_version=version,
            records=len(records.records),
            securities=len(daily.spans),
            data_end=str(daily.last_date),
        )

        acts_by_sec: dict[str, list[ResolvedAction]] = defaultdict(list)
        cash_dates: dict[str, set[date]] = defaultdict(set)
        for a in actions:
            if a.security_id is None:
                continue
            acts_by_sec[a.security_id].append(a)
            cls = a.interpretation.action_class
            if cls is ActionClass.CASH_DISTRIBUTION and a.record.ex_date and not a.suppressed:
                cash_dates[a.security_id].add(a.record.ex_date)
        overrides_by_sec: dict[str, dict[date, FactorOverride]] = defaultdict(dict)
        for (sid, ex), o in override_events.items():
            overrides_by_sec[sid][ex] = o

        # Reviewed identity links that join identity but not prices (ADR-0013): only used to
        # classify the gap at the link in the report; data quality makes it a break.
        identity_breaks: dict[str, set[date]] = defaultdict(set)
        for isin in self.identity_overrides.link_breaks_continuity:
            for sid in master.by_isin(isin):
                for span in master.spans_for(sid, IdentifierType.ISIN):
                    if span.value == isin:
                        identity_breaks[sid].add(span.valid_from)

        previous = self._previous_manifest().get("files", {})
        files: dict[str, str] = {}
        decisions: list[EventDecision] = []
        report = DiscontinuityReport(threshold=self.settings.adjustment.gap_report_threshold)
        unexplained: dict[str, list[UnexplainedGap]] = {}
        factor_rows = written = 0
        sids = sorted(set(daily.spans) | set(acts_by_sec) | set(overrides_by_sec))
        for n, sid in enumerate(sids, 1):
            h = daily.history(sid)
            sec_decisions = decide_security(
                sid,
                acts_by_sec.get(sid, []),
                h,
                overrides_by_sec.get(sid, {}),
                self.settings,
                daily.last_date,
            )
            decisions += sec_decisions
            if h is None:
                continue
            series = build_adjusted(self.exchange, h, sec_decisions)
            data = to_parquet_bytes(series.table)
            digest = hashlib.sha256(data).hexdigest()
            key = DataLakeLayout.adjusted_daily_key(self.exchange, sid)
            if previous.get(sid) != digest or not self.store.exists(key):
                self.store.put(key, data)
                written += 1
            files[sid] = digest
            factor_rows += series.factor_rows
            gaps = tally_gaps(
                report,
                h,
                series,
                sec_decisions,
                cash_dates.get(sid, ()),
                identity_breaks.get(sid, ()),
            )
            if gaps:
                unexplained[sid] = gaps
            if n % 500 == 0:
                log_event(log, "adjust.progress", securities=n, of=len(sids))

        counts: dict[str, Any] = {
            "records": len(records.records),
            "records_rejected_by_feed_parser": len(records.rejected),
            "feed_windows_missing": [
                w
                for w in records.windows_missing
                if daily.last_date and w <= f"{daily.last_date:%Y-%m}"
            ],
            "records_by_class": dict(Counter(str(a.interpretation.action_class) for a in actions)),
            "records_by_resolution": dict(Counter(str(a.resolution) for a in actions)),
            "records_suppressed": sum(1 for a in actions if a.suppressed),
            "events": len(decisions),
            "events_by_status": dict(Counter(str(d.status) for d in decisions)),
            "events_by_method": dict(Counter(d.method for d in decisions)),
            "events_breaking_continuity": sum(1 for d in decisions if d.breaks_continuity),
            "securities": len(daily.spans),
            "securities_with_applied_factors": len({d.security_id for d in decisions if d.applied}),
            "rows": daily.table.num_rows,
            "rows_with_factor": factor_rows,
            "files_written": written,
        }
        self._write_tables(actions, decisions, version)
        self.store.put(
            DataLakeLayout.unexplained_gaps_key(self.exchange),
            to_parquet_bytes(
                pa.Table.from_pylist(
                    [asdict(g) for gaps in unexplained.values() for g in gaps],
                    schema=UNEXPLAINED_GAPS_SCHEMA,
                )
            ),
        )
        published = report.hard_requirements_met and not unresolved_overrides
        report_payload = {
            "exchange": self.exchange,
            "adjustment_version": version,
            "identity_version": identity_version,
            "data_end": str(daily.last_date),
            "published": published,
            "hard_requirements": {
                "new_gaps_introduced": report.new_gaps_introduced,
                "gaps_worsened": report.gaps_worsened,
                "unresolved_overrides": unresolved_overrides,
            },
            "counts": counts,
            "discontinuity": asdict(report),
        }
        self.store.put(
            DataLakeLayout.adjustment_report_key(self.exchange),
            json.dumps(report_payload, indent=2, sort_keys=True, default=str).encode(),
        )
        if published:
            manifest = {
                "exchange": self.exchange,
                "adjustment_version": version,
                "identity_version": identity_version,
                "data_end": str(daily.last_date),
                "schema_version": ADJUSTED_SCHEMA_VERSION,
                "files": files,
            }
            self.store.put(
                DataLakeLayout.adjusted_manifest_key(self.exchange),
                json.dumps(manifest, indent=1, sort_keys=True).encode(),
            )
        log_event(
            log,
            "adjust.complete" if published else "adjust.not_published",
            logging.INFO if published else logging.ERROR,
            adjustment_version=version,
            new_gaps_introduced=report.new_gaps_introduced,
            gaps_worsened=report.gaps_worsened,
            unresolved_overrides=len(unresolved_overrides),
            **{k: v for k, v in counts.items() if isinstance(v, int)},
        )
        return AdjustmentResult(
            version,
            identity_version,
            daily.last_date,
            actions,
            decisions,
            report,
            counts,
            unexplained,
            unresolved_overrides,
            published,
        )

    def _write_tables(
        self, actions: Sequence[ResolvedAction], decisions: Sequence[EventDecision], version: str
    ) -> None:
        self.store.put(
            DataLakeLayout.corporate_actions_table_key(self.exchange),
            to_parquet_bytes(actions_table(actions)),
        )
        self.store.put(
            DataLakeLayout.adjustment_events_key(self.exchange),
            to_parquet_bytes(events_table(decisions, version)),
        )


def _components_json(cs: Iterable[ActionComponent]) -> str:
    return json.dumps(
        [{k: (str(v) if isinstance(v, Decimal) else v) for k, v in asdict(c).items()} for c in cs],
        sort_keys=True,
    )


def actions_table(actions: Sequence[ResolvedAction]) -> pa.Table:
    ordered = sorted(
        actions, key=lambda a: (a.record.ex_date or date.min, a.record.symbol, a.record.record_key)
    )
    return pa.Table.from_pylist(
        [
            {
                "record_key": a.record.record_key,
                "source_id": a.source.source_id,
                "source_file_hash": a.source.content_hash,
                "symbol": a.record.symbol,
                "series": a.record.series,
                "isin": a.record.isin,
                "company": a.record.company,
                "subject": a.record.subject,
                "ex_date": a.record.ex_date,
                "record_date": a.record.record_date,
                "face_value": None if a.record.face_value is None else str(a.record.face_value),
                "action_class": str(a.interpretation.action_class),
                "components": _components_json(a.interpretation.components),
                "grammar_version": a.interpretation.grammar_version,
                "notes": json.dumps(list(a.interpretation.notes)),
                "security_id": a.security_id,
                "resolution": str(a.resolution),
                "resolution_detail": a.detail,
                "suppressed_reason": a.suppressed,
            }
            for a in ordered
        ],
        schema=ACTIONS_SCHEMA,
    )


def events_table(decisions: Sequence[EventDecision], version: str) -> pa.Table:
    return pa.Table.from_pylist(
        [
            {
                "security_id": d.security_id,
                "ex_date": d.ex_date,
                "action_class": str(d.action_class),
                "status": str(d.status),
                "applied": d.applied,
                "breaks_continuity": d.breaks_continuity,
                "method": d.method,
                "factor": None if d.factor is None else fraction_text(d.factor),
                "volume_factor": None if d.factor is None else fraction_text(1 / d.factor),
                "inputs": json.dumps(d.inputs, sort_keys=True),
                "components": _components_json(d.components),
                "record_keys": json.dumps(d.record_keys),
                "subjects": json.dumps(d.subjects),
                "boundary_date": d.boundary_date,
                "raw_gap_log": d.raw_gap,
                "residual_log": d.residual,
                "tolerance_log": d.tolerance,
                "notes": json.dumps(d.notes),
                "override": d.override,
                "adjustment_version": version,
            }
            for d in sorted(decisions, key=lambda d: (d.security_id, d.ex_date))
        ],
        schema=EVENTS_SCHEMA,
    )


UNEXPLAINED_GAPS_SCHEMA: Final = pa.schema(
    [
        pa.field("security_id", pa.string(), nullable=False),
        pa.field("trading_date", pa.date32(), nullable=False),
        pa.field("gap", pa.float64(), nullable=False),
        pa.field("prev_close", PRICE_TYPE, nullable=False),
        pa.field("days_since_previous_session", pa.int64(), nullable=False),
        pa.field("session_index", pa.int64(), nullable=False),
    ],
    metadata={b"chartlens.dataset": b"unexplained_gaps", b"chartlens.schema_version": b"1"},
)

ACTIONS_SCHEMA: Final = pa.schema(
    [
        pa.field("record_key", pa.string(), nullable=False),
        pa.field("source_id", pa.string(), nullable=False),
        pa.field("source_file_hash", pa.string(), nullable=False),
        pa.field("symbol", pa.string(), nullable=False),
        pa.field("series", pa.string()),
        pa.field("isin", pa.string()),
        pa.field("company", pa.string()),
        pa.field("subject", pa.string(), nullable=False),
        pa.field("ex_date", pa.date32()),
        pa.field("record_date", pa.date32()),
        pa.field("face_value", pa.string()),
        pa.field("action_class", pa.string(), nullable=False),
        pa.field("components", pa.string(), nullable=False),
        pa.field("grammar_version", pa.string(), nullable=False),
        pa.field("notes", pa.string(), nullable=False),
        pa.field("security_id", pa.string()),
        pa.field("resolution", pa.string(), nullable=False),
        pa.field("resolution_detail", pa.string(), nullable=False),
        pa.field("suppressed_reason", pa.string()),
    ],
    metadata={b"chartlens.dataset": b"corporate_actions", b"chartlens.schema_version": b"1"},
)

EVENTS_SCHEMA: Final = pa.schema(
    [
        pa.field("security_id", pa.string(), nullable=False),
        pa.field("ex_date", pa.date32(), nullable=False),
        pa.field("action_class", pa.string(), nullable=False),
        pa.field("status", pa.string(), nullable=False),
        pa.field("applied", pa.bool_(), nullable=False),
        pa.field("breaks_continuity", pa.bool_(), nullable=False),
        pa.field("method", pa.string(), nullable=False),
        pa.field("factor", pa.string()),
        pa.field("volume_factor", pa.string()),
        pa.field("inputs", pa.string(), nullable=False),
        pa.field("components", pa.string(), nullable=False),
        pa.field("record_keys", pa.string(), nullable=False),
        pa.field("subjects", pa.string(), nullable=False),
        pa.field("boundary_date", pa.date32()),
        pa.field("raw_gap_log", pa.float64()),
        pa.field("residual_log", pa.float64()),
        pa.field("tolerance_log", pa.float64()),
        pa.field("notes", pa.string(), nullable=False),
        pa.field("override", pa.string()),
        pa.field("adjustment_version", pa.string(), nullable=False),
    ],
    metadata={b"chartlens.dataset": b"adjustment_events", b"chartlens.schema_version": b"1"},
)
