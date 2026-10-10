"""Breakout events from pattern breakouts and level role changes (ADR-0022 §19).

Each source keeps its own approved rule and its own reversal authority; the event layer
never re-judges a break, never harmonises thresholds and never deduplicates coincident
events. Its one rule of its own is RETEST:

- **RETEST**: the first complete bar t in (b, b + ``retest_window``], before any source
  reversal, whose low (BREAKOUT) / high (BREAKDOWN) comes within the frozen retest band
  of the level, or pierces it, and whose close stays on the breakout side. The band is
  ``retest_tol_atr`` × the breakout's own reference ATR, frozen in the source record, so
  later volatility never resizes it.
- **FALSE_BREAKOUT**: the source reverses before any retest, within the source's own
  window (a pattern's FAILED event, within its ``fail_window``; a level's next role
  change within ``level_false_window``).
- **FAILED_RETEST**: the source reverses after a retest, within ``retest_window``.
- **WINDOW_ENDED**: none of the terminal outcomes by b + max(reversal, retest window).

A pattern's level is its confirmation level, or the frozen confirmation line's value at
t (the line is fixed geometry). The event layer reads complete bars for the retest rule
and the sources' records for everything else; it never reads indicators for ATR or
volume, and it never writes to a source.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import date

import pandas as pd

from chartlens_core.config import AnalysisConfig
from chartlens_engine.breakouts.model import (
    TERMINAL_FOLLOW_UPS,
    BreakoutFollowUp,
    BreakoutResult,
    Direction,
    LevelBreakoutEvent,
    PatternBreakoutEvent,
)
from chartlens_engine.causal import CompleteBars, complete_bars
from chartlens_engine.indicators import IndicatorResult
from chartlens_engine.interfaces import AnalysisContext
from chartlens_engine.levels import LevelsResult
from chartlens_engine.patterns.model import BROKEN_OUT, Pattern, PatternResult

BREAKOUTS_VERSION = "1"
LevelAt = Callable[[int], float]


def event_id(
    dataset: str,
    security_id: str,
    segment: str,
    source_id: str,
    source_date: date,
    source_event: str,
    direction: str,
    source_version: str,
) -> str:
    """Deterministic event identity (ADR-0022 §19.4): a hash of the source identity and
    the source methodology version. Never random, never order-dependent."""
    prefix = "PBE" if dataset == "pattern_breakouts" else "LBE"
    canonical = "|".join(
        (
            dataset,
            security_id,
            segment,
            source_id,
            source_date.isoformat(),
            source_event,
            direction,
            source_version,
        )
    )
    return f"{prefix}-{hashlib.sha256(canonical.encode()).hexdigest()[:32]}"


class BreakoutEventAnalyzer:
    name = "breakout_events"
    version = BREAKOUTS_VERSION

    def __init__(
        self,
        config: AnalysisConfig,
        indicators: IndicatorResult,
        levels: LevelsResult,
        patterns: PatternResult,
    ) -> None:
        self.config = config
        self.indicators = indicators
        self.levels = levels
        self.patterns = patterns

    def analyze(self, bars: pd.DataFrame, context: AnalysisContext) -> BreakoutResult:
        for layer in (self.levels, self.patterns):
            if layer.context != context:
                raise ValueError(f"{layer.analyzer} was computed for another context")
        cb = complete_bars(bars, context, self.indicators)
        index = cb.index()
        security = str(context.security_id)
        level_version = f"levels-{self.levels.analyzer_version}"
        pattern_events = [
            e for p in self.patterns.patterns if (e := self._pattern_event(p, cb, index, security))
        ]
        level_events: list[LevelBreakoutEvent] = []
        for lv in self.levels.levels:
            changes = lv.role_history
            for i in range(1, len(changes)):
                rc = changes[i]
                bar = rc.change_bar
                assert bar is not None, "a role change must carry its change-bar evidence"
                b = index[rc.date]
                reversal = changes[i + 1] if i + 1 < len(changes) else None
                cfg = self.config.breakouts
                source_ref = f"{lv.level_id}#ROLE_CHANGE@{rc.date}"
                r = None if reversal is None else index[reversal.date]
                ref_r = None if reversal is None else f"{lv.level_id}#ROLE_CHANGE@{reversal.date}"
                history = self._follow_ups(
                    cb,
                    b,
                    bar.direction,
                    lambda _t, p=bar.level: p,
                    bar.atr,
                    r,
                    ref_r,
                    "LEVEL_ROLE_CHANGE",
                    cfg.level_false_window,
                )
                level_events.append(
                    LevelBreakoutEvent(
                        event_id=event_id(
                            "level_breakouts",
                            security,
                            cb.segment,
                            lv.level_id,
                            rc.date,
                            "ROLE_CHANGE",
                            bar.direction,
                            level_version,
                        ),
                        event_key=f"{lv.level_id}:{bar.direction}:{rc.date}",
                        security_id=security,
                        source_version=level_version,
                        source_id=lv.level_id,
                        source_event_ref=source_ref,
                        continuity_segment_id=cb.segment,
                        direction=bar.direction,
                        bar_date=rc.date,
                        known_at=rc.date,
                        level_at_break=bar.level,
                        reference_atr=bar.atr,
                        retest_band=None if bar.atr is None else cfg.retest_tol_atr * bar.atr,
                        reversal_window_bars=cfg.level_false_window,
                        retest_window_bars=cfg.retest_window,
                        observation_bars=max(cfg.level_false_window, cfg.retest_window),
                        source_measured_values={
                            "open": bar.open,
                            "close": bar.close,
                            "level": bar.level,
                            "buffer": bar.buffer,
                            "threshold": bar.threshold,
                            **({"atr": bar.atr} if bar.atr is not None else {}),
                        },
                        bar_volume=rc.change_bar_volume,
                        provisional=rc.provisional,
                        history=history,
                        methodology_version=f"breakouts-{BREAKOUTS_VERSION}",
                        level_source_type=lv.source_type,
                    )
                )
        # The layer's own order for both datasets (ADR-0024 §4): by break date, then key.
        pattern_events.sort(key=lambda e: (e.bar_date, e.event_key))
        level_events.sort(key=lambda e: (e.bar_date, e.event_key))
        return BreakoutResult(
            analyzer=self.name,
            analyzer_version=self.version,
            context=context,
            state_date=cb.state_date,
            pattern_events=pattern_events,
            level_events=level_events,
        )

    # ------------------------------------------------------------------ patterns

    def _pattern_event(
        self, p: Pattern, cb: CompleteBars, index: dict[date, int], security: str
    ) -> PatternBreakoutEvent | None:
        breakout = next((e for e in p.status_history if e.status in BROKEN_OUT), None)
        if breakout is None:
            return None
        mv = breakout.measured_values
        b = index[breakout.effective_date]
        level_b, close_b = mv["level"], mv["close"]
        direction: Direction = "BREAKOUT" if close_b > level_b else "BREAKDOWN"
        geo = p.geometry
        if geo.confirmation_level is not None:
            constant = geo.confirmation_level
            level_at: LevelAt = lambda _t: constant  # noqa: E731
        else:
            # the frozen line the lifecycle broke: the one whose value at b is the level
            line = min(geo.lines, key=lambda ln: abs(ln.value_at(b) - level_b))
            level_at = line.value_at
        failed = next((e for e in p.status_history if e.status == "FAILED"), None)
        r = None if failed is None else index[failed.effective_date]
        cfg = self.config.breakouts
        fail_window = int(
            self.config.patterns.common(getattr(self.config.patterns, p.family), "fail_window")
        )
        ref = f"{p.pattern_id}#{breakout.status}@{breakout.effective_date}"
        failed_ref = None if failed is None else f"{p.pattern_id}#FAILED@{failed.effective_date}"
        atr = mv.get("atr_pre")
        history = self._follow_ups(
            cb, b, direction, level_at, atr, r, failed_ref, "PATTERN_LIFECYCLE", fail_window
        )
        return PatternBreakoutEvent(
            event_id=event_id(
                "pattern_breakouts",
                security,
                cb.segment,
                p.pattern_id,
                breakout.effective_date,
                breakout.status,
                direction,
                breakout.methodology_version,
            ),
            event_key=f"{p.pattern_id}:{direction}:{breakout.effective_date}",
            security_id=security,
            source_version=breakout.methodology_version,
            source_id=p.pattern_id,
            source_event_ref=ref,
            continuity_segment_id=cb.segment,
            direction=direction,
            bar_date=breakout.effective_date,
            known_at=breakout.known_at,
            level_at_break=level_b,
            reference_atr=atr,
            retest_band=None if atr is None else cfg.retest_tol_atr * atr,
            reversal_window_bars=fail_window,
            retest_window_bars=cfg.retest_window,
            observation_bars=max(fail_window, cfg.retest_window),
            source_measured_values=dict(mv),
            bar_volume=breakout.breakout_bar_volume,
            provisional=breakout.provisional,
            history=history,
            methodology_version=f"breakouts-{BREAKOUTS_VERSION}",
            pattern_type=p.pattern_type,
            family=p.family,
        )

    # ------------------------------------------------------------------ follow-ups

    def _follow_ups(
        self,
        cb: CompleteBars,
        b: int,
        direction: Direction,
        level_at: LevelAt,
        reference_atr: float | None,
        reversal: int | None,
        reversal_ref: str | None,
        authority: str,
        reversal_window: int,
    ) -> list[BreakoutFollowUp]:
        cfg = self.config.breakouts
        up = direction == "BREAKOUT"
        band = None if reference_atr is None else cfg.retest_tol_atr * reference_atr
        end = b + max(reversal_window, cfg.retest_window)
        out: list[BreakoutFollowUp] = []

        def add(kind: str, t: int, auth: str, values: dict[str, float], ref: str | None) -> None:
            assert not out or out[-1].kind not in TERMINAL_FOLLOW_UPS, "terminal is final"
            out.append(
                BreakoutFollowUp(
                    kind=kind,  # type: ignore[arg-type]
                    effective_date=cb.dates[t],
                    known_at=cb.dates[t],
                    authority=auth,  # type: ignore[arg-type]
                    source_outcome_ref=ref,
                    measured_values=values,
                    provisional=bool(cb.special[t]),
                )
            )

        retest: int | None = None
        if band is not None and reference_atr is not None:
            stop = min(b + cfg.retest_window, cb.n - 1)
            if reversal is not None:
                stop = min(stop, reversal - 1)
            for t in range(b + 1, stop + 1):
                lv = level_at(t)
                close = float(cb.close[t])
                if up:
                    hit = float(cb.low[t]) <= lv + band and close > lv
                    extreme = float(cb.low[t])
                else:
                    hit = float(cb.high[t]) >= lv - band and close < lv
                    extreme = float(cb.high[t])
                if hit:
                    retest = t
                    add(
                        "RETEST",
                        t,
                        "RETEST_RULE",
                        {
                            "extreme": extreme,
                            "close": close,
                            "level": lv,
                            "band": band,
                            "reference_atr": reference_atr,
                        },
                        None,
                    )
                    break
        if reversal is not None and reversal_ref is not None:
            if retest is None and reversal - b <= reversal_window:
                add(
                    "FALSE_BREAKOUT",
                    reversal,
                    authority,
                    {"bars_after_break": float(reversal - b)},
                    reversal_ref,
                )
            elif retest is not None and reversal <= b + cfg.retest_window:
                add(
                    "FAILED_RETEST",
                    reversal,
                    authority,
                    {
                        "bars_after_break": float(reversal - b),
                        "bars_after_retest": float(reversal - retest),
                    },
                    reversal_ref,
                )
        terminal = out and out[-1].kind in TERMINAL_FOLLOW_UPS
        if not terminal and end < cb.n:
            add("WINDOW_ENDED", end, "WINDOW", {"observation_bars": float(end - b)}, None)
        return out
