"""Breakout events (ADR-0022 §19).

A breakout event is derived from an already-authoritative source event (a pattern's
breakout, or a level's role change) and records what followed within a declared
observation window. It never reinterprets, relabels or modifies its source, and never
feeds back into it: the event layer reads the source, and never writes to it.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from chartlens_engine.bar_evidence import BarVolumeEvidence
from chartlens_engine.causal import Frozen
from chartlens_engine.interfaces import AnalyzerResult

FollowUpKind = Literal["RETEST", "FALSE_BREAKOUT", "FAILED_RETEST", "WINDOW_ENDED"]
TERMINAL_FOLLOW_UPS: frozenset[str] = frozenset({"FALSE_BREAKOUT", "FAILED_RETEST", "WINDOW_ENDED"})
Direction = Literal["BREAKOUT", "BREAKDOWN"]


class BreakoutFollowUp(Frozen):
    """One observation after the break, dated by the complete bar that caused it."""

    kind: FollowUpKind
    effective_date: date
    known_at: date
    authority: Literal["RETEST_RULE", "PATTERN_LIFECYCLE", "LEVEL_ROLE_CHANGE", "WINDOW"]
    """Who decided the underlying fact. FALSE_BREAKOUT / FAILED_RETEST only reference
    the source's own reversal (the pattern's FAILED event, the level's next role change);
    the event layer classifies it relative to a retest and never redefines it.
    WINDOW_ENDED means exactly: the declared observation period ended without another
    qualifying event. It does not mean the breakout succeeded."""
    source_outcome_ref: str | None = None
    measured_values: dict[str, float]
    provisional: bool


class BreakoutEvent(Frozen):
    event_id: str
    """Deterministic identity: ``PBE-``/``LBE-`` + the first 32 hex digits of SHA-256
    over (dataset, security_id, continuity segment, source id, source event date, source
    event type, direction, source version). Same source + same source methodology → same
    id, independent of row or processing order; never random. The event layer's own
    version is not part of it: a change to follow-up rules changes an event's history,
    not which break it is."""
    event_key: str
    """The readable natural key, ``{source_id}:{BREAKOUT|BREAKDOWN}:{bar_date}``."""
    security_id: str
    source_version: str
    """The source's methodology version (a pattern breakout's ``methodology_version``;
    ``levels-{analyzer version}`` for a level role change)."""
    source_id: str
    source_event_ref: str
    """The authoritative source record this event is derived from."""
    continuity_segment_id: str
    direction: Direction
    bar_date: date
    known_at: date
    level_at_break: float
    reference_atr: float | None
    """The breakout's own ATR, frozen in the source record (a pattern: ATR_pre at the
    breakout bar; a level: ATR at the change bar). Never the ATR of a later bar."""
    retest_band: float | None
    """``retest_tol_atr`` × ``reference_atr``; None when the source had no ATR yet."""
    reversal_window_bars: int
    """The source's own reversal window (a pattern's ``fail_window``; a level's
    ``level_false_window``)."""
    retest_window_bars: int
    observation_bars: int
    """max(reversal window, retest window): WINDOW_ENDED falls on b + this."""
    source_measured_values: dict[str, float]
    """Copied from the source record (never recomputed)."""
    bar_volume: BarVolumeEvidence | None
    """Copied from the source record (a pattern's ``breakout_bar_volume``, a level role
    change's ``change_bar_volume``)."""
    provisional: bool
    history: list[BreakoutFollowUp]
    """Append-only. Nothing follows a terminal follow-up."""
    methodology_version: str

    @property
    def status(self) -> str:
        if self.history and self.history[-1].kind in TERMINAL_FOLLOW_UPS:
            return self.history[-1].kind
        return "RETESTED" if self.history else "OPEN"

    def as_of(self, day: date) -> BreakoutEvent | None:
        if self.known_at > day:
            return None
        return self.model_copy(update={"history": [f for f in self.history if f.known_at <= day]})


class PatternBreakoutEvent(BreakoutEvent):
    """Derived from a pattern's breakout event; the pattern lifecycle stays the authority
    for its failure (``fail_window``)."""

    source_type: Literal["PATTERN"] = "PATTERN"
    pattern_type: str
    family: str


class LevelBreakoutEvent(BreakoutEvent):
    """Derived from a level's role change; the level's next role change is the
    authority for its reversal."""

    source_type: Literal["LEVEL"] = "LEVEL"
    level_source_type: str
    """SWING or STRUCTURE (the level's own origin)."""


class BreakoutResult(AnalyzerResult):
    state_date: date | None
    pattern_events: list[PatternBreakoutEvent]
    level_events: list[LevelBreakoutEvent]
    """Kept apart: different sources, different scale (ADR-0022 §19)."""
