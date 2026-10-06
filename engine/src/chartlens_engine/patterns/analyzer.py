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
from chartlens_engine.causal import CompleteBars, complete_bars, numeric
from chartlens_engine.evidence import DivergenceResult, VolatilityResult
from chartlens_engine.fibonacci import FibonacciResult
from chartlens_engine.indicators import IndicatorResult
from chartlens_engine.interfaces import AnalysisContext
from chartlens_engine.levels import LevelsResult
from chartlens_engine.patterns import fit
from chartlens_engine.patterns.candidates import GEOMETRY_VERSIONS, Generator, Line, Spec
from chartlens_engine.patterns.context import ContextBuilder
from chartlens_engine.patterns.fit import DefinitionFit
from chartlens_engine.patterns.lifecycle import Lifecycle, VolumeSource
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
from chartlens_engine.structure import StructureResult
from chartlens_engine.swings import SwingPoint, SwingResult


def definition_fit(
    pattern: Pattern, config: PatternsConfig, shape_share: float | None = None
) -> DefinitionFit:
    """A pattern's definition fit from its frozen geometry and its known_at context only
    (ADR-0022 §15); never its touches, events or status. ``shape_share`` is for the
    diagnostic sensitivity report only."""
    ctx = pattern.context
    if ctx is None:
        raise ValueError("definition fit needs the known_at context")
    geo = pattern.geometry
    widths: tuple[float, float] | None = None
    if pattern.family == "pennant":
        a, b = geo.key_points[1].bar_index, geo.key_points[-1].bar_index
        upper, lower = geo.lines[0], geo.lines[1]
        widths = (
            abs(upper.value_at(a) - lower.value_at(a)),
            abs(upper.value_at(b) - lower.value_at(b)),
        )
    return fit.score(
        pattern.family,
        pattern.pattern_type,
        pattern.direction,
        dict(geo.measures),
        widths,
        ctx,
        config,
        shape_share,
    )


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
    version = "5"

    def __init__(
        self,
        config: PatternsConfig,
        indicators: IndicatorResult,
        swings: SwingResult,
        *,
        structure: StructureResult | None = None,
        levels: LevelsResult | None = None,
        fibonacci: FibonacciResult | None = None,
        divergence: DivergenceResult | None = None,
        volatility: VolatilityResult | None = None,
        diagnostics: bool = False,
    ) -> None:
        """``diagnostics`` keeps every rejected candidate with its failed rule (tests and
        reviews). It never changes which patterns exist. Context (5b-B) is built when the
        other layers are given, as the orchestrator always does."""
        self.config = config
        self.indicators = indicators
        self.swings = swings
        self.structure = structure
        self.levels = levels
        self.fibonacci = fibonacci
        self.divergence = divergence
        self.volatility = volatility
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
        atr_pre = np.concatenate(([np.nan], atr[:-1])) if cb.n else atr
        vs = self.indicators.get("volume_state")
        volume = VolumeSource(
            rvol=numeric(self.indicators, "relative_volume", cb.n),
            state=list(vs.data[: cb.n]),
            baseline=int(vs.params["baseline"]),
            expansion=float(vs.params["expansion"]),
            contraction=float(vs.params["contraction"]),
        )
        lifecycle = Lifecycle(self.config, cb, atr_pre, self.version, volume)
        builder = self._context_builder(cb, context)
        patterns: list[Pattern] = []
        accepted: list[tuple[Spec, Pattern, int]] = []
        same: dict[str, int] = {}
        for spec in specs:
            events = lifecycle.events(spec)
            # FORMING lasts until the bar of the first later event (exclusive).
            forming_end = index_of[events[1].effective_date] if len(events) > 1 else cb.n
            if self._same_formation(spec, accepted, index_of):
                same[spec.family] = same.get(spec.family, 0) + 1
                continue
            touches = self._touches(spec, atr, primary, fine, index_of, forming_end)
            pattern = self._pattern(spec, cb, touches)
            if builder is not None:
                # Touches known by known_at, cut off there, never by the lifecycle.
                at_known = self._touches(spec, atr, primary, fine, index_of, spec.known_index + 1)
                ctx = builder.build(spec, len(at_known))
                pattern = pattern.model_copy(update={"context": ctx})
                pattern = pattern.model_copy(update={"definition_fit": self._fit(pattern)})
            pattern = pattern.model_copy(update={"status_history": events})
            accepted.append((spec, pattern, forming_end))
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

    def _context_builder(self, cb: CompleteBars, ctx: AnalysisContext) -> ContextBuilder | None:
        structure, levels, fibonacci = self.structure, self.levels, self.fibonacci
        divergence, volatility = self.divergence, self.volatility
        if (
            structure is None
            or levels is None
            or fibonacci is None
            or divergence is None
            or volatility is None
        ):
            return None
        for layer in (structure, levels, fibonacci, divergence, volatility):
            if layer.context != ctx:
                raise ValueError(f"{layer.analyzer} was computed for another context")
        return ContextBuilder(
            self.config,
            cb,
            self.indicators,
            structure,
            levels,
            fibonacci,
            divergence,
            volatility,
            (self.swings.primary_method, self.swings.primary_sensitivity),
        )

    # ------------------------------------------------------------------ fit

    def _fit(self, pattern: Pattern) -> DefinitionFit:
        return definition_fit(pattern, self.config)

    # ------------------------------------------------------------------ touches

    def _touches(
        self,
        spec: Spec,
        atr: np.ndarray,
        primary: list[SwingPoint],
        fine: list[SwingPoint],
        index_of: dict[date, int],
        forming_end: int,
    ) -> list[tuple[SwingPoint, str, float]]:
        """Later swings within the touch tolerance of a line, known while the pattern is
        still FORMING (before the bar of its first lifecycle event)."""
        if spec.touch_tol_atr is None:
            return []
        lines = {line.label: line for line in spec.lines}
        last = spec.defining[-1].bar_index
        out: list[tuple[SwingPoint, str, float]] = []
        for s in fine if spec.touch_fine else primary:
            if s.bar_index <= last:
                continue
            assert s.known_at is not None
            if index_of[s.known_at] >= forming_end:
                if s.bar_index >= forming_end:
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
        spec: Spec, accepted: list[tuple[Spec, Pattern, int]], index_of: dict[date, int]
    ) -> bool:
        """ADR-0022 §2: the same type and direction, the earlier pattern still FORMING
        when this one becomes known (or known on the same bar), every defining swing of
        the new candidate a defining swing or touch of it known by then, and at least two
        shared."""
        mine = {s.swing_id for s in spec.defining}
        k = spec.known_index
        for other_spec, other, forming_end in accepted:
            if other.pattern_type != spec.pattern_type or other.direction != spec.direction:
                continue
            if not other_spec.known_index <= k < max(forming_end, other_spec.known_index + 1):
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
            geometry_version=GEOMETRY_VERSIONS[spec.family],
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
            status_history=[],
        )
