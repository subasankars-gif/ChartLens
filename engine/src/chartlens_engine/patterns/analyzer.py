"""Layers G–H (Phase 5a): pattern candidates, geometry, identity and FORMING patterns
(ADR-0022).

The analyzer generates broad candidates from the swing layer (the primary swings, and
the configured fine sensitivity for flags and pennants), keeps those that pass every
geometric rule, fixes their geometry, gives each a deterministic identity, and applies
the causal same-formation rule. Context, confirmation and the later status transitions
come in the next step; every pattern here has one status entry, FORMING at
``known_at``.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from chartlens_core.config import PatternsConfig
from chartlens_engine.causal import CompleteBars, StatusEntry, complete_bars, numeric
from chartlens_engine.indicators import IndicatorResult
from chartlens_engine.interfaces import AnalysisContext
from chartlens_engine.patterns.candidates import Generator, Line, Spec
from chartlens_engine.patterns.model import (
    CandidateCount,
    Geometry,
    KeyPoint,
    Pattern,
    PatternLine,
    PatternResult,
    PatternTouch,
    Rejection,
)
from chartlens_engine.swings import SwingPoint, SwingResult

FAMILIES = (
    "double",
    "triple",
    "head_shoulders",
    "rounding",
    "v",
    "rectangle",
    "triangle",
    "wedge",
    "flag",
    "pennant",
    "cup_handle",
)


class PatternAnalyzer:
    name = "patterns"
    version = "1"

    def __init__(
        self,
        config: PatternsConfig,
        indicators: IndicatorResult,
        swings: SwingResult,
        *,
        diagnostics: bool = False,
    ) -> None:
        """``diagnostics`` keeps every rejected candidate with its failed rule (tests and
        reviews). It never changes which patterns exist."""
        self.config = config
        self.indicators = indicators
        self.swings = swings
        self.diagnostics = diagnostics

    def analyze(self, bars: pd.DataFrame, context: AnalysisContext) -> PatternResult:
        if self.swings.context != context:
            raise ValueError("swings were computed for another context")
        cb = complete_bars(bars, context, self.indicators)
        atr = numeric(self.indicators, "atr", cb.n)
        index_of = cb.index()
        method = self.swings.primary_method
        primary = [s for s in self.swings.primary() if s.known_at in index_of]
        fine = [
            s
            for s in self.swings.of(method, self.config.fine_sensitivity)
            if s.known_at in index_of
        ]
        generator = Generator(self.config, cb, atr, primary, fine, self.diagnostics)
        specs = generator.run()
        specs.sort(
            key=lambda sp: (
                sp.known_index,
                sp.defining[0].bar_index,
                sp.defining[-1].bar_index,
                sp.pattern_type,
            )
        )
        patterns: list[Pattern] = []
        accepted: list[tuple[Spec, Pattern]] = []
        same: dict[str, int] = {}
        for spec in specs:
            touches = self._touches(spec, cb, atr, primary, fine, index_of)
            pattern = self._pattern(spec, cb, touches)
            if self._same_formation(spec, accepted, index_of):
                same[spec.family] = same.get(spec.family, 0) + 1
                continue
            accepted.append((spec, pattern))
            patterns.append(pattern)
        tally = generator.tally
        valid: dict[str, int] = {}
        for spec in specs:
            valid[spec.family] = valid.get(spec.family, 0) + 1
        counts = {
            f: CandidateCount(
                generated=tally.generated.get(f, 0),
                valid=valid.get(f, 0),
                same_formation=same.get(f, 0),
                rejected=dict(sorted(tally.rejected.get(f, {}).items())),
            )
            for f in FAMILIES
        }
        return PatternResult(
            analyzer=self.name,
            analyzer_version=self.version,
            context=context,
            patterns=patterns,
            candidates=counts,
            rejections=[
                Rejection(family=f, swing_ids=ids, rule=rule) for f, ids, rule in tally.rejections
            ],
        )

    # ------------------------------------------------------------------ touches

    def _touches(
        self,
        spec: Spec,
        cb: CompleteBars,
        atr: np.ndarray,
        primary: list[SwingPoint],
        fine: list[SwingPoint],
        index_of: dict[date, int],
    ) -> list[tuple[SwingPoint, str, float]]:
        """Later swings within the touch tolerance of a line, known before the pattern's
        horizon ends (its first close outside, its apex or its waiting window)."""
        if spec.touch_tol_atr is None:
            return []
        lines = {line.label: line for line in spec.lines}
        last = spec.defining[-1].bar_index
        out: list[tuple[SwingPoint, str, float]] = []
        for s in fine if spec.touch_fine else primary:
            if s.bar_index <= last:
                continue
            assert s.known_at is not None
            if index_of[s.known_at] >= spec.horizon_end:
                if s.bar_index >= spec.horizon_end:
                    break
                continue
            label = spec.touch_lines.get(s.type)
            a = float(atr[s.bar_index])
            if label is None or np.isnan(a) or a <= 0:
                continue
            distance = (s.price - lines[label].at(s.bar_index)) / a
            if abs(distance) <= spec.touch_tol_atr:
                out.append((s, label, distance))
        return out

    @staticmethod
    def _same_formation(
        spec: Spec, accepted: list[tuple[Spec, Pattern]], index_of: dict[date, int]
    ) -> bool:
        """ADR-0022 §2: the same type and direction, the earlier pattern still FORMING
        (inside its horizon) when this one becomes known, every defining swing of the new
        candidate a defining swing or touch of it known by then, and at least two shared."""
        mine = {s.swing_id for s in spec.defining}
        k = spec.known_index
        for other_spec, other in accepted:
            if other.pattern_type != spec.pattern_type or other.direction != spec.direction:
                continue
            if (
                not other_spec.known_index
                <= k
                < max(other_spec.horizon_end, other_spec.known_index + 1)
            ):
                continue
            defining = {s.swing_id for s in other_spec.defining}
            touched = {t.swing_id for t in other.touches if index_of[t.known_at] <= k}
            if len(mine & defining) >= 2 and mine <= defining | touched:
                return True
        return False

    # ------------------------------------------------------------------ assembly

    def _pattern(
        self, spec: Spec, cb: CompleteBars, touches: list[tuple[SwingPoint, str, float]]
    ) -> Pattern:
        first, last = spec.defining[0], spec.defining[-1]
        segment = cb.segment
        known_at = cb.dates[spec.known_index]
        dates = ":".join(str(s.bar_date) for s in spec.defining)

        def line(ln: Line) -> PatternLine:
            return PatternLine(
                label=ln.label,
                anchor_index=ln.anchor,
                anchor_value=ln.value,
                slope_per_bar=ln.slope,
                start_date=first.bar_date,
                start_value=ln.at(first.bar_index),
                end_date=last.bar_date,
                end_value=ln.at(last.bar_index),
            )

        geometry = Geometry(
            key_points=[
                KeyPoint(
                    label=label,
                    swing_id=s.swing_id,
                    bar_date=s.bar_date,
                    bar_index=s.bar_index,
                    known_at=s.known_at,  # type: ignore[arg-type]
                    price=s.price,
                )
                for label, s in zip(spec.labels, spec.defining, strict=True)
            ],
            lines=[line(ln) for ln in spec.lines],
            confirmation_level=spec.confirmation_level,
            confirmation_line=spec.confirmation_line,
            invalidation_level=spec.invalidation_level,
            invalidation_line=spec.invalidation_line,
            height=spec.height,
            atr_d=spec.atr_d,
            measures=spec.measures,
        )
        return Pattern(
            pattern_id=f"{segment}:PAT:{spec.pattern_type}:{dates}",
            pattern_type=spec.pattern_type,
            family=spec.family,
            direction=spec.direction,
            continuity_segment_id=segment,
            swing_method=first.method,
            swing_sensitivity=spec.sensitivity,
            start_date=first.bar_date,
            end_date=last.bar_date,
            known_at=known_at,
            geometry=geometry,
            touches=[
                PatternTouch(
                    swing_id=s.swing_id,
                    bar_date=s.bar_date,
                    known_at=s.known_at,  # type: ignore[arg-type]
                    price=s.price,
                    target=label,
                    distance_atr=d,
                )
                for s, label, d in touches
            ],
            depends_on=tuple(s.swing_id for s in spec.defining),
            status_history=[
                StatusEntry(
                    status="FORMING",
                    date=known_at,
                    provisional=bool(cb.special[spec.known_index]),
                )
            ],
        )
