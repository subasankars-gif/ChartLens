"""Pattern objects (ADR-0022). A pattern's geometry is fixed when it becomes known; later
swings only add touches, and status entries are appended. ``as_of`` is the projection
every prefix-stability test uses."""

from __future__ import annotations

from datetime import date
from typing import Literal

from chartlens_engine.bar_evidence import BarVolumeEvidence
from chartlens_engine.causal import Frozen
from chartlens_engine.interfaces import AnalyzerResult
from chartlens_engine.patterns.context import PatternContext
from chartlens_engine.patterns.fit import DefinitionFit

Direction = Literal["BULLISH", "BEARISH", "NEUTRAL"]
PatternStatus = Literal[
    "FORMING",
    "CONFIRMED",
    "RECOGNISED_AFTER_BREAKOUT",
    "INVALIDATED",
    "EXPIRED",
    "FAILED",
    "COMPLETED",
]
TERMINAL: frozenset[str] = frozenset({"INVALIDATED", "EXPIRED", "FAILED", "COMPLETED"})
BROKEN_OUT: frozenset[str] = frozenset({"CONFIRMED", "RECOGNISED_AFTER_BREAKOUT"})
"""The two post-breakout states. RECOGNISED_AFTER_BREAKOUT is never relabelled CONFIRMED:
historical statistics must tell a breakout ChartLens saw coming from one it recognised
afterwards."""


class MeasuredMove(Frozen):
    """The measured-move zone, frozen at the breakout bar with everything used to compute
    it (K8: a definition of COMPLETED, never a forecast)."""

    target_method: Literal["LEVEL_PLUS_HEIGHT", "FULL_RETRACE"]
    target_inputs: dict[str, float]
    """level (the confirmation level or line value at the breakout bar), height,
    direction (+1 up, −1 down), atr_pre, mm_zone_atr."""
    target_low: float
    target_high: float
    target_calculated_at: date


class PatternEvent(Frozen):
    """One immutable lifecycle event. Events are appended, never edited."""

    status: PatternStatus
    effective_date: date
    """The complete bar whose data satisfied the condition."""
    known_at: date
    """The first ``as_of`` that may see this event. Equal to ``effective_date`` for
    weekly bars (an event exists once its bar is complete); kept apart so the event
    model never depends on that coincidence."""
    reason: str
    """E.g. PATTERN_KNOWN, BREAKOUT_UP, BREAKOUT_DOWN, ALREADY_BEYOND_LEVEL,
    CLOSE_BEYOND_INVALIDATION, OPPOSITE_LINE_BROKEN, RETRACE_EXCEEDED, WAIT_WINDOW,
    APEX_REACHED, FLAG_WINDOW, CLOSE_BACK_THROUGH_LEVEL, MEASURED_MOVE_REACHED."""
    measured_values: dict[str, float]
    """The numbers the rule compared: close, open, level, buffer, atr_pre, …"""
    evidence_refs: tuple[str, ...] = ()
    methodology_version: str
    """``patterns-{analyzer version}/geometry-{family geometry version}``."""
    provisional: bool = False
    """The bar closed on a non-regular session (ADR-0015)."""
    measured_move: MeasuredMove | None = None
    """Only on the breakout event (CONFIRMED or RECOGNISED_AFTER_BREAKOUT)."""
    breakout_bar_volume: BarVolumeEvidence | None = None
    """Only on the breakout event: the breakout bar's frozen volume evidence."""


class KeyPoint(Frozen):
    label: str
    """E.g. LOW_1, NECKLINE, HEAD, RIM_1, POLE_START."""
    swing_id: str
    bar_date: date
    bar_index: int
    known_at: date
    price: float
    """The swing bar's exact price (published as that bar's decimal text, K7)."""


class PatternLine(Frozen):
    """A straight line fixed by two defining swings, advancing per bar of the segment."""

    label: str
    anchor_index: int
    anchor_value: float
    slope_per_bar: float
    start_date: date
    start_value: float
    end_date: date
    """The last defining bar: where the line is drawn to when the pattern becomes known."""
    end_value: float

    def value_at(self, bar_index: int) -> float:
        return self.anchor_value + self.slope_per_bar * (bar_index - self.anchor_index)


class Geometry(Frozen):
    """Everything fixed at ``known_at``. Never refitted (ADR-0022 §2).

    Geometry, confirmation and status are separate parts of a pattern. Confirmation and
    status (Phase 5b) are recorded in status entries and never write here: a breakout
    can never reshape the pattern it confirms."""

    geometry_version: str
    """The family's geometry rules version (``candidates.GEOMETRY_VERSIONS``)."""

    key_points: list[KeyPoint]
    lines: list[PatternLine]
    confirmation_level: float | None
    """A horizontal confirmation level, or None when confirmation is a line."""
    confirmation_line: str | None
    """The line whose value confirms (None for horizontal levels or either side)."""
    invalidation_level: float | None
    invalidation_line: str | None
    height: float
    """The measured-move height (a fact; the zone is fixed at confirmation)."""
    atr_d: float
    """ATR(14) at the last defining swing's bar: the unit of every tolerance."""
    measures: dict[str, float]
    """Each rule's measured value, e.g. ``low_difference_atr``, ``height_atr``,
    ``separation_bars``, ``r2``."""


class PatternTouch(Frozen):
    swing_id: str
    bar_date: date
    known_at: date
    price: float
    target: str
    """The line or level touched."""
    distance_atr: float


class Pattern(Frozen):
    pattern_id: str
    """``{segment}:PAT:{TYPE}:{defining swing bar dates}`` — never the detection date."""
    pattern_type: str
    family: str
    """The ``[analysis.patterns.<family>]`` section that defines it."""
    direction: Direction
    continuity_segment_id: str
    swing_method: str
    swing_sensitivity: str
    start_date: date
    end_date: date
    """The last defining swing's bar."""
    known_at: date
    """The latest ``known_at`` of the defining swings."""
    geometry: Geometry
    touches: list[PatternTouch]
    """Later swings within tolerance of its lines or levels, each with its own
    ``known_at``."""
    depends_on: tuple[str, ...]
    """The defining swings."""
    context: PatternContext | None = None
    """A snapshot of the facts available at ``known_at`` (5b-B); None only when the
    analyzer runs without the other layers (unit tests of geometry alone)."""
    definition_fit: DefinitionFit | None = None
    """How closely the formation satisfies the definition (5b-C, ADR-0022 §15), from the
    frozen geometry and the ``known_at`` context only. Not a probability of breakout or
    success. None exactly when ``context`` is None."""
    status_history: list[PatternEvent]
    """Append-only; the first is FORMING at ``known_at``. At most one breakout event and
    one terminal event, each the first condition objectively satisfied."""

    @property
    def status(self) -> str:
        return self.status_history[-1].status

    @property
    def breakout(self) -> PatternEvent | None:
        return next((e for e in self.status_history if e.status in BROKEN_OUT), None)

    @property
    def measured_move(self) -> MeasuredMove | None:
        event = self.breakout
        return event.measured_move if event else None

    def as_of(self, day: date) -> Pattern | None:
        if self.known_at > day:
            return None
        return self.model_copy(
            update={
                "touches": [t for t in self.touches if t.known_at <= day],
                "status_history": [e for e in self.status_history if e.known_at <= day],
            }
        )


class Rejection(Frozen):
    family: str
    swing_ids: tuple[str, ...]
    rule: str
    """The first geometric rule that failed."""


class CandidateCount(Frozen):
    generated: int
    """Swing sequences of the right shape."""
    valid: int
    """Passed every geometric rule (before same-formation handling)."""
    same_formation: int
    """Valid, but the same formation as an earlier pattern: recorded as its touches."""
    rejected: dict[str, int]
    """Failed, by the first rule that failed."""


class PatternResult(AnalyzerResult):
    patterns: list[Pattern]
    """Every pattern that exists, in the order it became known."""
    candidates: dict[str, CandidateCount]
    """Per family."""
    rejections: list[Rejection]
    """Every rejected candidate, only when the analyzer runs with ``diagnostics``."""
