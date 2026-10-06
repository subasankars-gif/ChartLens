"""Layer H (Phase 5b-A): confirmation and lifecycle (ADR-0022 §3, §5).

Reads a pattern's frozen geometry and the complete bars; appends immutable events and
never writes geometry. One forward pass from ``known_at``:

* **Before a breakout**, bar by bar from the known bar: the breakout rule first, then
  invalidation, then expiry. Each event is the *first* bar that objectively satisfies
  its condition; nothing later rewrites it.
* **Breakout thresholds** use ``breakout_atr`` × the ATR of the bar *before*
  (ATR_pre): the breakout week's own range never sets its own threshold.
* **On the known bar** a pattern whose close is already beyond its confirmation level is
  RECOGNISED_AFTER_BREAKOUT, never CONFIRMED. CONFIRMED means ChartLens knew the pattern
  before the breakout bar.
* **After a breakout**, the measured-move zone is frozen on that bar with its method and
  inputs. FAILED (a close back through the level within ``fail_window``) and COMPLETED
  (the zone reached within ``completion_window``) are judged from the next bar; when
  both happen on one bar, FAILED wins (the close is the bar's final state). If neither
  happens, the pattern stays in its breakout state as a historical fact.
* **The breakout event freezes the breakout bar's volume evidence** (RVOL, its inputs and
  the indicator layer's classification), from data through that bar only. It is
  evidence, never a condition: no transition reads it (ADR-0022 §18.4).
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

from chartlens_core.config import PatternsConfig, PatternSection
from chartlens_engine.causal import Array, CompleteBars
from chartlens_engine.patterns.candidates import GEOMETRY_VERSIONS, Line, Spec
from chartlens_engine.patterns.model import (
    BreakoutBarVolume,
    MeasuredMove,
    PatternEvent,
    PatternStatus,
)

Level = Callable[[int], float]


def section(config: PatternsConfig, family: str) -> PatternSection:
    section_: PatternSection = getattr(config, family)
    return section_


BREAKOUT_VOLUME_VERSION = "1"


@dataclass(frozen=True)
class VolumeSource:
    """The indicator layer's relative volume and volume state, read at the breakout bar
    only (never recomputed or reclassified here)."""

    rvol: Array
    state: list[object]
    baseline: int
    expansion: float
    contraction: float


class Lifecycle:
    def __init__(
        self,
        config: PatternsConfig,
        cb: CompleteBars,
        atr_pre: Array,
        analyzer_version: str,
        volume: VolumeSource,
    ) -> None:
        self.config = config
        self.cb = cb
        self.atr_pre = atr_pre
        self.analyzer_version = analyzer_version
        self.volume = volume

    def breakout_bar_volume(self, t: int) -> BreakoutBarVolume:
        """The breakout bar's volume evidence from data through bar t only."""
        cb, v = self.cb, self.volume
        n = v.baseline
        base = float(cb.volume[t - n : t].mean()) if t >= n else None
        r = float(v.rvol[t])
        state = v.state[t]
        day = cb.dates[t]
        return BreakoutBarVolume(
            bar_date=day,
            volume=float(cb.volume[t]),
            baseline_bars=n,
            baseline_mean_volume=base,
            rvol=None if math.isnan(r) else r,
            classification=None if state is None else str(state),  # type: ignore[arg-type]
            expansion_threshold=v.expansion,
            contraction_threshold=v.contraction,
            evidence_refs=(f"indicators:relative_volume@{day}", f"indicators:volume_state@{day}"),
            measurement_version=BREAKOUT_VOLUME_VERSION,
        )

    def events(self, spec: Spec) -> list[PatternEvent]:
        cb = self.cb
        k = spec.known_index
        cfg = section(self.config, spec.family)
        version = f"patterns-{self.analyzer_version}/geometry-{GEOMETRY_VERSIONS[spec.family]}"
        breakout_atr = self.config.common(cfg, "breakout_atr")
        lines = {line.label: line for line in spec.lines}

        def event(
            status: PatternStatus,
            t: int,
            reason: str,
            values: dict[str, float],
            measured_move: MeasuredMove | None = None,
        ) -> PatternEvent:
            return PatternEvent(
                status=status,
                effective_date=cb.dates[t],
                known_at=cb.dates[t],
                reason=reason,
                measured_values=values,
                methodology_version=version,
                provisional=bool(cb.special[t]),
                measured_move=measured_move,
                breakout_bar_volume=None if measured_move is None else self.breakout_bar_volume(t),
            )

        def buffer(t: int) -> float | None:
            a = float(self.atr_pre[t])
            return None if math.isnan(a) else breakout_atr * a

        history = [event("FORMING", k, "PATTERN_KNOWN", {})]
        sides = self._confirmation_sides(spec, lines)
        invalidations = self._invalidations(spec, lines)
        expire_at, expire_reason = self._expiry(spec)

        # ---------------------------------------------------------- before a breakout
        broke: tuple[int, int, Level] | None = None  # (bar, direction, level)
        for t in range(k, min(cb.n, expire_at + 1)):
            buf = buffer(t)
            close = float(cb.close[t])
            for direction, level in sides:
                lv = level(t)
                if buf is None or (close - lv) * direction < buf:
                    continue
                values = {"close": close, "level": lv, "buffer": buf, "atr_pre": buf / breakout_atr}
                if t == k:
                    broke = (t, direction, level)
                    history.append(
                        event(
                            "RECOGNISED_AFTER_BREAKOUT",
                            t,
                            "ALREADY_BEYOND_LEVEL",
                            values,
                            self._measured_move(spec, t, direction, lv, cfg),
                        )
                    )
                    break
                prev_close, prev_buf = float(cb.close[t - 1]), buffer(t - 1)
                prev_beyond = prev_buf is not None and (
                    (prev_close - level(t - 1)) * direction >= prev_buf
                )
                body = (close - float(cb.open[t])) * direction > 0
                if body and not prev_beyond:
                    broke = (t, direction, level)
                    values |= {"open": float(cb.open[t]), "previous_close": prev_close}
                    history.append(
                        event(
                            "CONFIRMED",
                            t,
                            "BREAKOUT_UP" if direction > 0 else "BREAKOUT_DOWN",
                            values,
                            self._measured_move(spec, t, direction, lv, cfg),
                        )
                    )
                    break
            if broke is not None:
                break
            hit = self._invalidated(invalidations, t, close, buf)
            if hit is not None:
                reason, values = hit
                history.append(event("INVALIDATED", t, reason, values))
                return history
            if t == expire_at:
                history.append(event("EXPIRED", t, expire_reason, {"bars_waited": float(t - k)}))
                return history
        if broke is None:
            return history

        # ---------------------------------------------------------- after a breakout
        c, direction, level = broke
        move = history[-1].measured_move
        assert move is not None
        fail_window = int(self.config.common(cfg, "fail_window"))
        completion_window = int(self.config.common(cfg, "completion_window"))
        for t in range(c + 1, min(cb.n, c + max(fail_window, completion_window) + 1)):
            buf = buffer(t)
            close = float(cb.close[t])
            lv = level(t)
            if t <= c + fail_window and buf is not None and (close - lv) * direction <= -buf:
                history.append(
                    event(
                        "FAILED",
                        t,
                        "CLOSE_BACK_THROUGH_LEVEL",
                        {"close": close, "level": lv, "buffer": buf},
                    )
                )
                return history
            extreme = float(cb.high[t]) if direction > 0 else float(cb.low[t])
            reached = extreme >= move.target_low if direction > 0 else extreme <= move.target_high
            if t <= c + completion_window and reached:
                history.append(
                    event(
                        "COMPLETED",
                        t,
                        "MEASURED_MOVE_REACHED",
                        {
                            "extreme": extreme,
                            "target_low": move.target_low,
                            "target_high": move.target_high,
                        },
                    )
                )
                return history
        return history

    # ------------------------------------------------------------------ rules

    @staticmethod
    def _confirmation_sides(spec: Spec, lines: dict[str, Line]) -> list[tuple[int, Level]]:
        """(direction, level at bar) pairs that can confirm the pattern."""
        if spec.direction == "NEUTRAL":
            return [(1, lines["UPPER"].at), (-1, lines["LOWER"].at)]
        direction = 1 if spec.direction == "BULLISH" else -1
        if spec.confirmation_level is not None:
            fixed = spec.confirmation_level
            return [(direction, lambda _t: fixed)]
        assert spec.confirmation_line is not None
        return [(direction, lines[spec.confirmation_line].at)]

    @staticmethod
    def _invalidations(spec: Spec, lines: dict[str, Line]) -> list[tuple[str, int, Level, bool]]:
        """(reason, direction of the move that invalidates, level, uses the buffer)."""
        if spec.direction == "NEUTRAL":
            return []
        against = -1 if spec.direction == "BULLISH" else 1
        out: list[tuple[str, int, Level, bool]] = []
        if spec.invalidation_line is not None:
            out.append(("OPPOSITE_LINE_BROKEN", against, lines[spec.invalidation_line].at, True))
        if spec.invalidation_level is not None:
            fixed = spec.invalidation_level
            reason = (
                "RETRACE_EXCEEDED"
                if spec.family in ("flag", "pennant")
                else ("CLOSE_BEYOND_INVALIDATION")
            )
            out.append((reason, against, lambda _t: fixed, False))
        return out

    @staticmethod
    def _invalidated(
        rules: list[tuple[str, int, Level, bool]], t: int, close: float, buf: float | None
    ) -> tuple[str, dict[str, float]] | None:
        for reason, against, level, buffered in rules:
            lv = level(t)
            if buffered:
                if buf is not None and (close - lv) * against >= buf:
                    return reason, {"close": close, "level": lv, "buffer": buf}
            elif (close - lv) * against > 0:
                return reason, {"close": close, "level": lv}
        return None

    def _expiry(self, spec: Spec) -> tuple[int, str]:
        window = spec.known_index + spec.max_wait if spec.max_wait is not None else math.inf
        geometry = spec.deadline_index if spec.deadline_index is not None else math.inf
        if geometry <= window:
            assert spec.deadline_reason is not None
            return int(max(geometry, spec.known_index)), spec.deadline_reason
        return int(window), "WAIT_WINDOW"

    def _measured_move(
        self, spec: Spec, t: int, direction: int, level: float, cfg: PatternSection
    ) -> MeasuredMove:
        atr_pre = float(self.atr_pre[t])
        zone = self.config.common(cfg, "mm_zone_atr") * atr_pre
        if spec.family == "v":
            method = "FULL_RETRACE"
            centre = spec.defining[0].price  # the V's starting extreme
        else:
            method = "LEVEL_PLUS_HEIGHT"
            centre = level + direction * spec.height
        return MeasuredMove(
            target_method=method,
            target_inputs={
                "level": level,
                "height": spec.height,
                "direction": float(direction),
                "atr_pre": atr_pre,
                "mm_zone_atr": self.config.common(cfg, "mm_zone_atr"),
            },
            target_low=centre - zone,
            target_high=centre + zone,
            target_calculated_at=self.cb.dates[t],
        )
