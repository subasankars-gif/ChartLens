"""Pattern objects (ADR-0022). A pattern's geometry is fixed when it becomes known; later
swings only add touches, and status entries are appended. ``as_of`` is the projection
every prefix-stability test uses."""

from __future__ import annotations

from datetime import date
from typing import Literal

from chartlens_engine.causal import Frozen, StatusEntry, history_as_of
from chartlens_engine.interfaces import AnalyzerResult

Direction = Literal["BULLISH", "BEARISH", "NEUTRAL"]


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
    """Everything fixed at ``known_at``. Never refitted (ADR-0022 §2)."""

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
    status_history: list[StatusEntry]

    @property
    def status(self) -> str:
        return self.status_history[-1].status

    def as_of(self, day: date) -> Pattern | None:
        if self.known_at > day:
            return None
        return self.model_copy(
            update={
                "touches": [t for t in self.touches if t.known_at <= day],
                "status_history": history_as_of(self.status_history, day),
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
