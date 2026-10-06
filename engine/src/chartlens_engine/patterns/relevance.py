"""Layer H, Phase 5b-D: pattern relevance (ADR-0022 §18).

Relevance answers one question: **why should this pattern appear in the user's attention
view?** It never answers "how likely is it to work?", and it is not a score.

Central invariant: **changing relevance rules never changes pattern identity, geometry,
lifecycle, definition fit or any underlying analytical object.** Relevance runs after the
pattern analyzer, as its own stage, over its finished result; it produces a separate
list of annotations and never writes to a pattern.

- **Rules with named reasons.** At each complete weekly bar t, for every pattern known
  by t: (1) the lifecycle stage as of t gives a reason (first match wins, precedence
  intentional); (2) containment; (3) the forming-per-type cap. Exclusions by containment
  or cap are recorded with their evidence, never silently dropped.
- **No definition fit, no outcomes.** This module never reads ``definition_fit``; the cap
  orders by recognition date only. Nothing here is tuned from historical outcomes.
- **Known at the relevance bar, appended, never rewritten.** Every input is projected as
  of t (lifecycle events known by t, structure events known by t, the close and ATR at
  t, the ``known_at`` context). An entry is appended only when the outcome or its
  explanation changes, dated by the bar that caused it. A run to T reproduces exactly
  the history up to T.
- **Tags explain, never decide.** They are facts with evidence; they never take part in
  inclusion, exclusion, the cap or any ordering.
"""

from __future__ import annotations

import math
from datetime import date
from typing import Literal

import numpy as np
import pandas as pd
from pydantic import Field

from chartlens_core.config import PatternsConfig
from chartlens_engine.causal import Frozen, complete_bars, numeric
from chartlens_engine.indicators import IndicatorResult
from chartlens_engine.interfaces import AnalysisContext, AnalyzerResult
from chartlens_engine.patterns.families import REVERSAL_FAMILIES
from chartlens_engine.patterns.model import BROKEN_OUT, TERMINAL, Pattern, PatternEvent
from chartlens_engine.patterns.model import PatternResult as Patterns
from chartlens_engine.structure import StructureResult

RELEVANCE_VERSION = "1"

Reason = Literal[
    "BREAKOUT_CONFIRMED",
    "RECOGNISED_AFTER_BREAKOUT",
    "APPROACHING_CONFIRMATION",
    "NEWLY_RECOGNISED",
    "FORMING",
    "BREAKOUT_OPEN",
    "AGED_OUT",
    "TERMINAL_COMPLETED",
    "TERMINAL_FAILED",
    "TERMINAL_INVALIDATED",
    "TERMINAL_EXPIRED",
    "CONTAINED",
    "FORMING_CAP",
]
FORMING_STAGE: frozenset[str] = frozenset(
    {"APPROACHING_CONFIRMATION", "NEWLY_RECOGNISED", "FORMING"}
)
DOWN_TRENDS = {"STRONG_DOWNTREND", "WEAKENING_DOWNTREND"}
UP_TRENDS = {"STRONG_UPTREND", "WEAKENING_UPTREND"}


class RelevanceTag(Frozen):
    """An explanatory fact, known by the entry's date. Never decides anything."""

    tag: Literal[
        "AT_SUPPORT",
        "AT_RESISTANCE",
        "STRUCTURE_SHIFT",
        "AGAINST_PRIOR_TREND",
        "DIVERGENCE",
        "BREAKOUT_VOLUME",
    ]
    evidence_refs: tuple[str, ...]


class RelevanceEntry(Frozen):
    effective_date: date
    """The complete bar whose information produced this entry."""
    relevance_known_at: date
    """The first ``as_of`` that may see it (equal to ``effective_date`` for weekly bars)."""
    included: bool
    reason: Reason
    container_pattern_id: str | None = None
    """CONTAINED: the outermost included pattern containing this one."""
    kept_pattern_ids: tuple[str, ...] = ()
    """FORMING_CAP: the patterns of the same type kept instead (most recently known)."""
    measured_values: dict[str, float] = Field(default_factory=dict[str, float])
    """The numbers a rule compared (APPROACHING: close, level, atr, distance_atr)."""
    tags: list[RelevanceTag] = Field(default_factory=list["RelevanceTag"])
    evidence_refs: tuple[str, ...]
    provisional: bool
    """A rule used a close (or a lifecycle event) from a non-regular session (ADR-0015)."""
    methodology_version: str


class PatternRelevance(Frozen):
    pattern_id: str
    pattern_type: str
    pattern_known_at: date
    history: list[RelevanceEntry]
    """Append-only, from ``pattern_known_at``: one entry per change."""

    def as_of(self, day: date) -> RelevanceEntry | None:
        """The entry in force on ``day`` (None before the pattern was known)."""
        current = None
        for e in self.history:
            if e.relevance_known_at <= day:
                current = e
        return current

    @property
    def current(self) -> RelevanceEntry:
        return self.history[-1]


class RelevanceResult(AnalyzerResult):
    state_date: date | None
    relevance: list[PatternRelevance]
    """One per pattern, in the pattern list's order."""

    def included_as_of(self, day: date) -> list[str]:
        out: list[str] = []
        for r in self.relevance:
            e = r.as_of(day)
            if e is not None and e.included:
                out.append(r.pattern_id)
        return out


class RelevanceAnalyzer:
    name = "pattern_relevance"
    version = RELEVANCE_VERSION

    def __init__(
        self,
        config: PatternsConfig,
        indicators: IndicatorResult,
        patterns: Patterns,
        structure: StructureResult,
    ) -> None:
        self.config = config
        self.indicators = indicators
        self.patterns = patterns
        self.structure = structure

    def analyze(self, bars: pd.DataFrame, context: AnalysisContext) -> RelevanceResult:
        for layer in (self.patterns, self.structure):
            if layer.context != context:
                raise ValueError(f"{layer.analyzer} was computed for another context")
        cb = complete_bars(bars, context, self.indicators)
        n = cb.n
        atr = numeric(self.indicators, "atr", n)
        volume_state = self.indicators.get("volume_state").data[:n]
        index = cb.index()
        cfg = self.config.relevance
        pats = self.patterns.patterns
        histories: dict[str, list[RelevanceEntry]] = {p.pattern_id: [] for p in pats}
        last_key: dict[str, tuple[object, ...]] = {}
        done: set[str] = set()  # terminal or aged out: permanent
        by_known: dict[int, list[Pattern]] = {}
        for p in pats:
            by_known.setdefault(index[p.known_at], []).append(p)
        active: list[Pattern] = []
        chochs = self.structure.character_changes()
        version = f"relevance-{RELEVANCE_VERSION}"

        for t in range(n):
            day = cb.dates[t]
            active += by_known.get(t, [])
            stage: dict[str, _Stage] = {}
            for p in active:
                if p.pattern_id not in done:
                    stage[p.pattern_id] = self._stage(p, t, day, cb.close, atr, index)
            # (2) containment among stage-included patterns
            included = [p for p in active if p.pattern_id in stage and stage[p.pattern_id].included]
            swings = {p.pattern_id: set(p.depends_on) for p in included}
            for p in included:
                containers = [
                    q
                    for q in included
                    if q.pattern_id != p.pattern_id
                    and q.direction == p.direction
                    and len(swings[q.pattern_id]) > len(swings[p.pattern_id])
                    and swings[p.pattern_id] <= swings[q.pattern_id]
                ]
                if containers:
                    outer = min(
                        containers, key=lambda q: (-len(swings[q.pattern_id]), q.pattern_id)
                    )
                    s = stage[p.pattern_id]
                    stage[p.pattern_id] = _Stage(
                        False, "CONTAINED", s.event, container=outer.pattern_id
                    )
            # (3) the forming-per-type cap: most recently known first, ties by pattern_id
            by_type: dict[str, list[Pattern]] = {}
            for p in active:
                s = stage.get(p.pattern_id)
                if s is not None and s.included and s.reason in FORMING_STAGE:
                    by_type.setdefault(p.pattern_type, []).append(p)
            for group in by_type.values():
                group.sort(key=lambda p: (-index[p.known_at], p.pattern_id))
                kept = tuple(p.pattern_id for p in group[: cfg.max_forming_per_type])
                for p in group[cfg.max_forming_per_type :]:
                    s = stage[p.pattern_id]
                    stage[p.pattern_id] = _Stage(False, "FORMING_CAP", s.event, kept=kept)
            # append entries that changed
            for p in active:
                pid = p.pattern_id
                if pid in done:
                    continue
                s = stage[pid]
                tags = self._tags(p, s, t, day, chochs, volume_state, index)
                key = (s.included, s.reason, s.container, s.kept, tuple(tags))
                if last_key.get(pid) == key:
                    continue
                last_key[pid] = key
                histories[pid].append(
                    RelevanceEntry(
                        effective_date=day,
                        relevance_known_at=day,
                        included=s.included,
                        reason=s.reason,  # type: ignore[arg-type]
                        container_pattern_id=s.container,
                        kept_pattern_ids=s.kept,
                        measured_values=s.values,
                        tags=[RelevanceTag(tag=g, evidence_refs=r) for g, r in tags],  # type: ignore[arg-type]
                        evidence_refs=_refs(p, s),
                        provisional=bool(cb.special[t])
                        or (s.event is not None and s.event.provisional),
                        methodology_version=version,
                    )
                )
                if s.permanent:
                    done.add(pid)

        return RelevanceResult(
            analyzer=self.name,
            analyzer_version=self.version,
            context=context,
            state_date=cb.dates[n - 1] if n else None,
            relevance=[
                PatternRelevance(
                    pattern_id=p.pattern_id,
                    pattern_type=p.pattern_type,
                    pattern_known_at=p.known_at,
                    history=histories[p.pattern_id],
                )
                for p in pats
            ],
        )

    # ------------------------------------------------------------------ (1) stage

    def _stage(
        self,
        p: Pattern,
        t: int,
        day: date,
        close: np.ndarray,
        atr: np.ndarray,
        index: dict[date, int],
    ) -> _Stage:
        """The lifecycle as of t. Precedence is intentional and fixed (ADR-0022 §18): a
        pattern may satisfy several predicates; only the first reason is emitted."""
        cfg = self.config.relevance
        events = [e for e in p.status_history if e.known_at <= day]
        last = events[-1]
        if last.status in TERMINAL:
            return _Stage(False, f"TERMINAL_{last.status}", last, permanent=True)
        breakout = next((e for e in events if e.status in BROKEN_OUT), None)
        if breakout is not None:
            b = index[breakout.effective_date]
            if t < b + cfg.recent_breakout_bars:
                reason = (
                    "RECOGNISED_AFTER_BREAKOUT"
                    if breakout.status == "RECOGNISED_AFTER_BREAKOUT"
                    else "BREAKOUT_CONFIRMED"
                )
                return _Stage(True, reason, breakout)
            window = int(self.config.common(getattr(self.config, p.family), "completion_window"))
            if t - b <= window:
                return _Stage(True, "BREAKOUT_OPEN", breakout)
            return _Stage(False, "AGED_OUT", breakout, permanent=True)
        forming = events[0]
        approach = self._approach(p, t, close, atr)
        if approach is not None:
            return _Stage(True, "APPROACHING_CONFIRMATION", forming, values=approach)
        if t < index[p.known_at] + cfg.new_pattern_bars:
            return _Stage(True, "NEWLY_RECOGNISED", forming)
        return _Stage(True, "FORMING", forming)

    def _approach(
        self, p: Pattern, t: int, close: np.ndarray, atr: np.ndarray
    ) -> dict[str, float] | None:
        """|close[t] − level[t]| ≤ approaching_confirmation_atr × ATR[t]; for a neutral
        pattern, the nearer of its two boundaries."""
        a = float(atr[t])
        if math.isnan(a) or a <= 0:
            return None
        geo = p.geometry
        lines = {ln.label: ln for ln in geo.lines}
        if geo.confirmation_level is not None:
            levels = [geo.confirmation_level]
        elif geo.confirmation_line is not None:
            levels = [lines[geo.confirmation_line].value_at(t)]
        elif p.direction == "NEUTRAL":
            levels = [ln.value_at(t) for ln in geo.lines]
        else:
            return None
        c = float(close[t])
        level = min(levels, key=lambda x: abs(c - x))
        distance = abs(c - level) / a
        if distance > self.config.relevance.approaching_confirmation_atr:
            return None
        return {"close": c, "level": level, "atr": a, "distance_atr": distance}

    # ------------------------------------------------------------------ tags (inert)

    def _tags(
        self,
        p: Pattern,
        s: _Stage,
        t: int,
        day: date,
        chochs: list,  # type: ignore[type-arg]
        volume_state: list,  # type: ignore[type-arg]
        index: dict[date, int],
    ) -> list[tuple[str, tuple[str, ...]]]:
        ctx = p.context
        if ctx is None:
            return []
        out: list[tuple[str, tuple[str, ...]]] = []
        for role, tag in (("SUPPORT", "AT_SUPPORT"), ("RESISTANCE", "AT_RESISTANCE")):
            ids = tuple(lv.level_id for lv in ctx.levels_near_base if lv.role == role)
            if ids:
                out.append((tag, ids))
        if p.direction != "NEUTRAL":
            want = "UP" if p.direction == "BULLISH" else "DOWN"
            shifts = tuple(
                e.event_id
                for e in chochs
                if e.direction == want and e.known_at <= day and e.bar_date >= p.start_date
            )
            if shifts:
                out.append(("STRUCTURE_SHIFT", shifts))
            prior = ctx.prior_structure.state
            reversed_trend = DOWN_TRENDS if p.direction == "BULLISH" else UP_TRENDS
            if p.family in REVERSAL_FAMILIES and prior in reversed_trend:
                out.append(("AGAINST_PRIOR_TREND", (f"{p.pattern_id}#prior_structure",)))
        if ctx.divergence.presence == "PRESENT":
            out.append(("DIVERGENCE", tuple(d.divergence_id for d in ctx.divergence.divergences)))
        breakout = next(
            (e for e in p.status_history if e.status in BROKEN_OUT and e.known_at <= day), None
        )
        if breakout is not None:
            b = index[breakout.effective_date]
            if volume_state[b] == "EXPANSION":
                out.append(("BREAKOUT_VOLUME", (_event_ref(p, breakout),)))
        return out


class _Stage:
    def __init__(
        self,
        included: bool,
        reason: str,
        event: PatternEvent | None,
        *,
        container: str | None = None,
        kept: tuple[str, ...] = (),
        values: dict[str, float] | None = None,
        permanent: bool = False,
    ) -> None:
        self.included = included
        self.reason = reason
        self.event = event
        self.container = container
        self.kept = kept
        self.values = values or {}
        self.permanent = permanent


def _event_ref(p: Pattern, e: PatternEvent) -> str:
    return f"{p.pattern_id}#{e.status}@{e.effective_date}"


def _refs(p: Pattern, s: _Stage) -> tuple[str, ...]:
    refs = [p.pattern_id]
    if s.event is not None:
        refs.append(_event_ref(p, s.event))
    if s.container:
        refs.append(s.container)
    refs += list(s.kept)
    return tuple(dict.fromkeys(refs))
