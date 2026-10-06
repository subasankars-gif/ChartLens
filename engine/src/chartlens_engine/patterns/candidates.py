"""Layer G: broad candidate generation and geometric validation (ADR-0022 §7).

Every swing sequence of the right shape is a candidate. A candidate is dropped only when
a geometric rule of its definition fails, and the first failed rule is counted (and kept
when diagnostics are on). Context never creates or deletes a candidate.

Bullish and bearish forms share one implementation: prices are mapped to ``q = s·price``
(s = +1 bullish, −1 bearish), so a double top is a double bottom in q-space. Lines,
levels and fit coefficients are mapped back before they leave this module.

Every check uses only the defining swings and bars up to ``known_at``. Geometry is
fixed here and never refitted (ADR-0022 §2).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Literal

import numpy as np

from chartlens_core.config import PatternsConfig, PatternSection
from chartlens_engine.causal import Array, CompleteBars
from chartlens_engine.patterns.model import Direction
from chartlens_engine.swings import SwingPoint

Kind = Literal["HIGH", "LOW"]


@dataclass(frozen=True)
class Line:
    """A straight line in price space through a defining swing."""

    label: str
    anchor: int
    value: float
    slope: float

    def at(self, b: int | float) -> float:
        return self.value + self.slope * (b - self.anchor)


@dataclass
class Spec:
    """A candidate that passed every geometric rule."""

    family: str
    pattern_type: str
    direction: Direction
    sensitivity: str
    defining: tuple[SwingPoint, ...]
    labels: tuple[str, ...]
    known_index: int
    atr_d: float
    lines: tuple[Line, ...] = ()
    confirmation_level: float | None = None
    confirmation_line: str | None = None
    invalidation_level: float | None = None
    invalidation_line: str | None = None
    height: float = 0.0
    measures: dict[str, float] = field(default_factory=dict[str, float])
    touch_tol_atr: float | None = None
    """Later swings within this many ATR of a line are touches (boundary patterns)."""
    touch_lines: dict[str, str] = field(default_factory=dict[str, str])
    """Swing type → the line it can touch."""
    touch_fine: bool = False
    """Touches come from the fine swings (flags, pennants)."""
    horizon_end: int = 0
    """Touches count only when known before this bar (exclusive): the first complete close
    outside the lines, the apex, or the end of the waiting window."""


@dataclass
class Tally:
    generated: Counter[str] = field(default_factory=Counter[str])
    rejected: dict[str, Counter[str]] = field(default_factory=dict[str, Counter[str]])
    rejections: list[tuple[str, tuple[str, ...], str]] = field(
        default_factory=list[tuple[str, tuple[str, ...], str]]
    )


def _opposite(kind: Kind) -> Kind:
    return "LOW" if kind == "HIGH" else "HIGH"


class Generator:
    """All candidate families over one segment's swings and complete bars."""

    def __init__(
        self,
        config: PatternsConfig,
        cb: CompleteBars,
        atr: Array,
        primary: list[SwingPoint],
        fine: list[SwingPoint],
        diagnostics: bool,
    ) -> None:
        self.cfg = config
        self.cb = cb
        self.atr = atr
        self.atr_pre = np.concatenate(([np.nan], atr[:-1])) if len(atr) else atr
        self.index_of = cb.index()
        self.primary = sorted(primary, key=lambda s: (s.bar_index, s.type))
        self.fine = sorted(fine, key=lambda s: (s.bar_index, s.type))
        self.diagnostics = diagnostics
        self.tally = Tally()

    # ------------------------------------------------------------------ helpers

    def known(self, s: SwingPoint) -> int:
        assert s.known_at is not None
        return self.index_of[s.known_at]

    def known_index(self, swings: tuple[SwingPoint, ...]) -> int:
        return max(self.known(s) for s in swings)

    def generated(self, family: str) -> None:
        self.tally.generated[family] += 1

    def reject(self, family: str, swings: tuple[SwingPoint, ...], rule: str) -> None:
        self.tally.rejected.setdefault(family, Counter())[rule] += 1
        if self.diagnostics:
            self.tally.rejections.append((family, tuple(s.swing_id for s in swings), rule))

    def atr_at(self, b: int) -> float:
        return float(self.atr[b])

    def buffer(self, section: PatternSection, start: int, end: int) -> Array:
        """``breakout_atr`` × ATR of the bar before, for bars start..end-1 (NaN → 0)."""
        k = self.cfg.common(section, "breakout_atr")
        return np.nan_to_num(self.atr_pre[start:end] * k, nan=0.0)

    def first_outside(
        self, section: PatternSection, upper: Line, lower: Line, start: int, end: int
    ) -> int | None:
        """The first bar in [start, end) whose complete close is above ``upper`` or below
        ``lower`` by the breakout buffer (price space)."""
        if start >= end:
            return None
        bars = np.arange(start, end)
        close = self.cb.close[start:end]
        buf = self.buffer(section, start, end)
        up = upper.value + upper.slope * (bars - upper.anchor)
        lo = lower.value + lower.slope * (bars - lower.anchor)
        hits = np.flatnonzero((close > up + buf) | (close < lo - buf))
        return start + int(hits[0]) if hits.size else None

    def horizon(
        self,
        section: PatternSection,
        lines: tuple[Line, Line] | None,
        k: int,
        last: int,
    ) -> int:
        """Exclusive end of the touch horizon: ``last`` capped by the first close outside."""
        last = min(last, self.cb.n)
        if lines is None:
            return last
        hit = self.first_outside(section, lines[0], lines[1], k + 1, last)
        return last if hit is None else hit

    @staticmethod
    def windows(swings: list[SwingPoint], kinds: tuple[Kind, ...]) -> list[tuple[SwingPoint, ...]]:
        n = len(kinds)
        return [
            tuple(swings[i : i + n])
            for i in range(len(swings) - n + 1)
            if tuple(s.type for s in swings[i : i + n]) == kinds
        ]

    @staticmethod
    def through(label: str, a: SwingPoint, b: SwingPoint) -> Line:
        slope = (b.price - a.price) / (b.bar_index - a.bar_index)
        return Line(label, a.bar_index, a.price, slope)

    def fit(self, b0: int, b1: int, s: float) -> tuple[float, float, float, float]:
        """Least-squares quadratic of q-closes over bars b0..b1 (x = bar − b0):
        (a, b, c, R²)."""
        y = s * self.cb.close[b0 : b1 + 1]
        x = np.arange(len(y), dtype=np.float64)
        a, b, c = (float(v) for v in np.polyfit(x, y, 2))
        resid = y - (a * x * x + b * x + c)
        total = float(((y - y.mean()) ** 2).sum())
        r2 = 0.0 if total == 0 else 1.0 - float((resid**2).sum()) / total
        return a, b, c, r2

    # ------------------------------------------------------------------ run

    def run(self) -> list[Spec]:
        out: list[Spec] = []
        for s in (1.0, -1.0):
            out += self.double(s)
            out += self.triple(s)
            out += self.head_shoulders(s)
            out += self.rounding(s)
            out += self.v(s)
            out += self.flags(s)
            out += self.cup_handle(s)
        out += self.rectangles()
        out += self.converging()
        return out

    # ------------------------------------------------------------------ 7.1 double

    def double(self, s: float) -> list[Spec]:
        cfg = self.cfg.double
        up: Kind = "HIGH" if s > 0 else "LOW"
        down = _opposite(up)
        out: list[Spec] = []
        for w in self.windows(self.primary, (down, up, down)):
            a, h, c = w
            self.generated("double")
            atr = self.atr_at(c.bar_index)
            if np.isnan(atr) or atr <= 0:
                self.reject("double", w, "atr_warmup")
                continue
            qa, qh, qc = s * a.price, s * h.price, s * c.price
            diff = abs(qa - qc)
            sep = c.bar_index - a.bar_index
            height = qh - max(qa, qc)
            if diff > cfg.eq_tol_atr * atr:
                self.reject("double", w, "equal_extremes")
            elif not cfg.min_sep <= sep <= cfg.max_sep:
                self.reject("double", w, "separation")
            elif height < cfg.min_height_atr * atr:
                self.reject("double", w, "height")
            else:
                k = self.known_index(w)
                bull = s > 0
                ext = "LOW" if bull else "HIGH"
                out.append(
                    Spec(
                        family="double",
                        pattern_type="DOUBLE_BOTTOM" if bull else "DOUBLE_TOP",
                        direction="BULLISH" if bull else "BEARISH",
                        sensitivity=c.sensitivity,
                        defining=w,
                        labels=(f"{ext}_1", "NECKLINE", f"{ext}_2"),
                        known_index=k,
                        atr_d=atr,
                        lines=(Line("NECKLINE", a.bar_index, h.price, 0.0),),
                        confirmation_level=h.price,
                        invalidation_level=s * min(qa, qc),
                        height=qh - min(qa, qc),
                        measures={
                            "extreme_difference_atr": diff / atr,
                            "height_atr": height / atr,
                            "separation_bars": float(sep),
                        },
                        horizon_end=min(k + cfg.max_wait_bars + 1, self.cb.n),
                    )
                )
        return out

    # ------------------------------------------------------------------ 7.2 triple

    def triple(self, s: float) -> list[Spec]:
        cfg = self.cfg.triple
        up: Kind = "HIGH" if s > 0 else "LOW"
        down = _opposite(up)
        out: list[Spec] = []
        for w in self.windows(self.primary, (down, up, down, up, down)):
            l1, h1, l2, h2, l3 = w
            self.generated("triple")
            atr = self.atr_at(l3.bar_index)
            if np.isnan(atr) or atr <= 0:
                self.reject("triple", w, "atr_warmup")
                continue
            lows = [s * x.price for x in (l1, l2, l3)]
            peaks = [s * h1.price, s * h2.price]
            spread = max(lows) - min(lows)
            sep = l3.bar_index - l1.bar_index
            height = min(peaks) - max(lows)
            if spread > cfg.eq_tol_atr * atr:
                self.reject("triple", w, "equal_extremes")
            elif not cfg.min_sep <= sep <= cfg.max_sep:
                self.reject("triple", w, "separation")
            elif height < cfg.min_height_atr * atr:
                self.reject("triple", w, "height")
            else:
                k = self.known_index(w)
                bull = s > 0
                ext = "LOW" if bull else "HIGH"
                neck = max(peaks)
                out.append(
                    Spec(
                        family="triple",
                        pattern_type="TRIPLE_BOTTOM" if bull else "TRIPLE_TOP",
                        direction="BULLISH" if bull else "BEARISH",
                        sensitivity=l3.sensitivity,
                        defining=w,
                        labels=(f"{ext}_1", "PEAK_1", f"{ext}_2", "PEAK_2", f"{ext}_3"),
                        known_index=k,
                        atr_d=atr,
                        lines=(Line("NECKLINE", l1.bar_index, s * neck, 0.0),),
                        confirmation_level=s * neck,
                        invalidation_level=s * min(lows),
                        height=neck - min(lows),
                        measures={
                            "extreme_spread_atr": spread / atr,
                            "height_atr": height / atr,
                            "separation_bars": float(sep),
                            "peak_difference_atr": abs(peaks[0] - peaks[1]) / atr,
                        },
                        horizon_end=min(k + cfg.max_wait_bars + 1, self.cb.n),
                    )
                )
        return out

    # ------------------------------------------------------------------ 7.3 H&S

    def head_shoulders(self, s: float) -> list[Spec]:
        cfg = self.cfg.head_shoulders
        up: Kind = "HIGH" if s > 0 else "LOW"
        down = _opposite(up)
        out: list[Spec] = []
        for w in self.windows(self.primary, (down, up, down, up, down)):
            ls, n1, head, n2, rs = w
            self.generated("head_shoulders")
            atr = self.atr_at(rs.bar_index)
            if np.isnan(atr) or atr <= 0:
                self.reject("head_shoulders", w, "atr_warmup")
                continue
            qls, qn1, qh, qn2, qrs = (s * x.price for x in w)
            slope_q = (qn2 - qn1) / (n2.bar_index - n1.bar_index)
            prominence = min(qls, qrs) - qh
            neck_at_head = qn1 + slope_q * (head.bar_index - n1.bar_index)
            height = neck_at_head - qh
            ratio = (head.bar_index - ls.bar_index) / (rs.bar_index - head.bar_index)
            span = rs.bar_index - ls.bar_index
            if prominence < cfg.head_prominence_atr * atr:
                self.reject("head_shoulders", w, "head_prominence")
            elif abs(qls - qrs) > cfg.shoulder_tol_atr * atr:
                self.reject("head_shoulders", w, "shoulder_symmetry")
            elif abs(slope_q) > cfg.neckline_max_slope_atr * atr:
                self.reject("head_shoulders", w, "neckline_slope")
            elif not cfg.time_balance_min <= ratio <= cfg.time_balance_max:
                self.reject("head_shoulders", w, "time_balance")
            elif span > cfg.max_span:
                self.reject("head_shoulders", w, "span")
            elif height < cfg.min_height_atr * atr:
                self.reject("head_shoulders", w, "height")
            else:
                k = self.known_index(w)
                bull = s > 0
                out.append(
                    Spec(
                        family="head_shoulders",
                        pattern_type="INVERSE_HEAD_SHOULDERS" if bull else "HEAD_SHOULDERS",
                        direction="BULLISH" if bull else "BEARISH",
                        sensitivity=rs.sensitivity,
                        defining=w,
                        labels=("LEFT_SHOULDER", "NECK_1", "HEAD", "NECK_2", "RIGHT_SHOULDER"),
                        known_index=k,
                        atr_d=atr,
                        lines=(self.through("NECKLINE", n1, n2),),
                        confirmation_line="NECKLINE",
                        invalidation_level=head.price,
                        height=height,
                        measures={
                            "head_prominence_atr": prominence / atr,
                            "shoulder_difference_atr": abs(qls - qrs) / atr,
                            "neckline_slope_atr": slope_q / atr,
                            "time_ratio": ratio,
                            "span_bars": float(span),
                            "height_atr": height / atr,
                        },
                        horizon_end=min(k + cfg.max_wait_bars + 1, self.cb.n),
                    )
                )
        return out

    # ------------------------------------------------------------------ 7.4 rounding

    def _rims(
        self, s: float, min_span: int, max_span: int
    ) -> list[tuple[SwingPoint, SwingPoint, list[SwingPoint], list[SwingPoint]]]:
        """Pairs of primary up-swings (q-space highs) within the span, with the swings
        between them."""
        up: Kind = "HIGH" if s > 0 else "LOW"
        out: list[tuple[SwingPoint, SwingPoint, list[SwingPoint], list[SwingPoint]]] = []
        sw = self.primary
        for i, a in enumerate(sw):
            if a.type != up:
                continue
            for j in range(i + 1, len(sw)):
                b = sw[j]
                span = b.bar_index - a.bar_index
                if span > max_span:
                    break
                if b.type != up or span < min_span:
                    continue
                between = sw[i + 1 : j]
                out.append(
                    (
                        a,
                        b,
                        [x for x in between if x.type == up],
                        [x for x in between if x.type != up],
                    )
                )
        return out

    def rounding(self, s: float) -> list[Spec]:
        cfg = self.cfg.rounding
        out: list[Spec] = []
        for ra, rb, ups, downs in self._rims(s, cfg.min_span, cfg.max_span):
            w = (ra, rb)
            self.generated("rounding")
            atr = self.atr_at(rb.bar_index)
            if np.isnan(atr) or atr <= 0:
                self.reject("rounding", w, "atr_warmup")
                continue
            qa, qb = s * ra.price, s * rb.price
            rim = min(qa, qb)
            if any(s * x.price >= rim for x in ups):
                self.reject("rounding", w, "interior_above_rims")
                continue
            if abs(qa - qb) > cfg.rim_tol_atr * atr:
                self.reject("rounding", w, "rim_equality")
                continue
            m = rb.bar_index - ra.bar_index
            a2, a1, a0, r2 = self.fit(ra.bar_index, rb.bar_index, s)
            if a2 <= 0:
                self.reject("rounding", w, "curvature")
                continue
            vertex = -a1 / (2 * a2)
            fitted_min = a0 - a1 * a1 / (4 * a2)
            depth = rim - fitted_min
            if r2 < cfg.min_r2:
                self.reject("rounding", w, "fit")
            elif not cfg.vertex_min <= vertex / m <= cfg.vertex_max:
                self.reject("rounding", w, "vertex_position")
            elif depth < cfg.min_depth_atr * atr:
                self.reject("rounding", w, "depth")
            elif any(s * x.price < fitted_min - cfg.low_tol_atr * atr for x in downs):
                self.reject("rounding", w, "low_below_fit")
            else:
                k = self.known_index(w)
                bull = s > 0
                confirm = max(qa, qb)
                out.append(
                    Spec(
                        family="rounding",
                        pattern_type="ROUNDING_BOTTOM" if bull else "ROUNDING_TOP",
                        direction="BULLISH" if bull else "BEARISH",
                        sensitivity=rb.sensitivity,
                        defining=w,
                        labels=("RIM_1", "RIM_2"),
                        known_index=k,
                        atr_d=atr,
                        lines=(Line("RIM", ra.bar_index, s * confirm, 0.0),),
                        confirmation_level=s * confirm,
                        invalidation_level=s * (fitted_min - cfg.low_tol_atr * atr),
                        height=depth,
                        measures={
                            "rim_difference_atr": abs(qa - qb) / atr,
                            "r2": r2,
                            "vertex_fraction": vertex / m,
                            "depth_atr": depth / atr,
                            "fit_a": s * a2,
                            "fit_b": s * a1,
                            "fit_c": s * a0,
                            "fit_origin_index": float(ra.bar_index),
                        },
                        horizon_end=min(k + cfg.max_wait_bars + 1, self.cb.n),
                    )
                )
        return out

    # ------------------------------------------------------------------ 7.5 V

    def v(self, s: float) -> list[Spec]:
        cfg = self.cfg.v
        up: Kind = "HIGH" if s > 0 else "LOW"
        out: list[Spec] = []
        for w in self.windows(self.primary, (up, _opposite(up))):
            h0, lo = w
            self.generated("v")
            atr = self.atr_at(lo.bar_index)
            if np.isnan(atr) or atr <= 0:
                self.reject("v", w, "atr_warmup")
                continue
            drop = s * h0.price - s * lo.price
            bars = lo.bar_index - h0.bar_index
            if drop < cfg.v_move_atr * atr:
                self.reject("v", w, "drop")
            elif bars > cfg.max_drop_bars:
                self.reject("v", w, "drop_speed")
            else:
                k = self.known_index(w)
                bull = s > 0
                out.append(
                    Spec(
                        family="v",
                        pattern_type="V_BOTTOM" if bull else "V_TOP",
                        direction="BULLISH" if bull else "BEARISH",
                        sensitivity=lo.sensitivity,
                        defining=w,
                        labels=("START", "LOW" if bull else "HIGH"),
                        known_index=k,
                        atr_d=atr,
                        confirmation_level=s * (s * lo.price + cfg.recovery_ratio * drop),
                        invalidation_level=lo.price,
                        height=drop,
                        measures={"drop_atr": drop / atr, "drop_bars": float(bars)},
                        horizon_end=min(k + cfg.max_wait_bars + 1, self.cb.n),
                    )
                )
        return out

    # ------------------------------------------------------------------ 7.6 rectangle

    def _alternating4(self) -> list[tuple[SwingPoint, ...]]:
        return self.windows(self.primary, ("HIGH", "LOW", "HIGH", "LOW")) + self.windows(
            self.primary, ("LOW", "HIGH", "LOW", "HIGH")
        )

    def rectangles(self) -> list[Spec]:
        cfg = self.cfg.rectangle
        out: list[Spec] = []
        for w in sorted(self._alternating4(), key=lambda w: w[0].bar_index):
            self.generated("rectangle")
            first, last = w[0].bar_index, w[-1].bar_index
            atr = self.atr_at(last)
            if np.isnan(atr) or atr <= 0:
                self.reject("rectangle", w, "atr_warmup")
                continue
            highs = [x for x in w if x.type == "HIGH"]
            lows = [x for x in w if x.type == "LOW"]
            upper = max(x.price for x in highs)
            lower = min(x.price for x in lows)
            span = last - first
            k = self.known_index(w)
            top, bottom = Line("UPPER", first, upper, 0.0), Line("LOWER", first, lower, 0.0)
            if abs(highs[0].price - highs[1].price) > cfg.band_tol_atr * atr:
                self.reject("rectangle", w, "upper_band")
            elif abs(lows[0].price - lows[1].price) > cfg.band_tol_atr * atr:
                self.reject("rectangle", w, "lower_band")
            elif not cfg.min_span <= span <= cfg.max_span:
                self.reject("rectangle", w, "span")
            elif upper - lower < cfg.min_height_atr * atr:
                self.reject("rectangle", w, "height")
            elif self.first_outside(cfg, top, bottom, first, k + 1) is not None:
                self.reject("rectangle", w, "close_outside")
            else:
                out.append(
                    Spec(
                        family="rectangle",
                        pattern_type="RECTANGLE",
                        direction="NEUTRAL",
                        sensitivity=w[-1].sensitivity,
                        defining=w,
                        labels=tuple(f"{x.type}_{i + 1}" for i, x in enumerate(w)),
                        known_index=k,
                        atr_d=atr,
                        lines=(top, bottom),
                        height=upper - lower,
                        measures={
                            "upper_difference_atr": abs(highs[0].price - highs[1].price) / atr,
                            "lower_difference_atr": abs(lows[0].price - lows[1].price) / atr,
                            "height_atr": (upper - lower) / atr,
                            "span_bars": float(span),
                        },
                        touch_tol_atr=cfg.band_tol_atr,
                        touch_lines={"HIGH": "UPPER", "LOW": "LOWER"},
                        horizon_end=self.horizon(cfg, (top, bottom), k, k + cfg.max_wait_bars + 1),
                    )
                )
        return out

    # ------------------------------------------------------- 7.7–7.8 triangles, wedges

    def converging(self) -> list[Spec]:
        out: list[Spec] = []
        for w in sorted(self._alternating4(), key=lambda w: w[0].bar_index):
            highs = [x for x in w if x.type == "HIGH"]
            lows = [x for x in w if x.type == "LOW"]
            upper = self.through("UPPER", highs[0], highs[1])
            lower = self.through("LOWER", lows[0], lows[1])
            for family in ("triangle", "wedge"):
                spec = self._converging(family, w, upper, lower)
                if spec is not None:
                    out.append(spec)
        return out

    def _converging(
        self,
        family: Literal["triangle", "wedge"],
        w: tuple[SwingPoint, ...],
        upper: Line,
        lower: Line,
    ) -> Spec | None:
        cfg = self.cfg.triangle if family == "triangle" else self.cfg.wedge
        self.generated(family)
        first, last = w[0].bar_index, w[-1].bar_index
        atr = self.atr_at(last)
        if np.isnan(atr) or atr <= 0:
            self.reject(family, w, "atr_warmup")
            return None
        flat = cfg.flat_slope_atr * atr
        su, sl = upper.slope, lower.slope
        kind: str | None = None
        if family == "triangle":
            ratio = abs(su) / abs(sl) if sl != 0 else float("inf")
            r = self.cfg.triangle.symmetry_ratio
            if abs(su) <= flat and sl > flat:
                kind = "TRIANGLE_ASCENDING"
            elif abs(sl) <= flat and su < -flat:
                kind = "TRIANGLE_DESCENDING"
            elif su < -flat and sl > flat:
                if not 1 / r <= ratio <= r:
                    self.reject(family, w, "symmetry")
                    return None
                kind = "TRIANGLE_SYMMETRICAL"
        elif su > flat and sl > flat and sl > su:
            kind = "WEDGE_RISING"
        elif su < -flat and sl < -flat and su < sl:
            kind = "WEDGE_FALLING"
        if kind is None:
            self.reject(family, w, "slopes")
            return None
        w0 = upper.at(first) - lower.at(first)
        w1 = upper.at(last) - lower.at(last)
        closing = sl - su  # > 0 when the lines converge
        apex = first + w0 / closing if closing > 0 else float("inf")
        span = last - first
        k = self.known_index(w)
        if w0 <= 0 or w1 > cfg.converge_ratio * w0:
            self.reject(family, w, "convergence")
            return None
        if not 0 < apex - last <= cfg.apex_max_bars:
            self.reject(family, w, "apex")
            return None
        if w0 < cfg.min_height_atr * atr:
            self.reject(family, w, "height")
            return None
        if not cfg.min_span <= span <= cfg.max_span:
            self.reject(family, w, "span")
            return None
        if self.first_outside(cfg, upper, lower, first, k + 1) is not None:
            self.reject(family, w, "close_outside")
            return None
        direction: Direction
        confirm, invalid = {
            "TRIANGLE_ASCENDING": ("UPPER", "LOWER"),
            "TRIANGLE_DESCENDING": ("LOWER", "UPPER"),
            "TRIANGLE_SYMMETRICAL": (None, None),
            "WEDGE_RISING": ("LOWER", "UPPER"),
            "WEDGE_FALLING": ("UPPER", "LOWER"),
        }[kind]
        direction = {
            "TRIANGLE_ASCENDING": "BULLISH",
            "TRIANGLE_DESCENDING": "BEARISH",
            "TRIANGLE_SYMMETRICAL": "NEUTRAL",
            "WEDGE_RISING": "BEARISH",
            "WEDGE_FALLING": "BULLISH",
        }[kind]  # type: ignore[assignment]
        apex_bar = int(np.ceil(apex))
        return Spec(
            family=family,
            pattern_type=kind,
            direction=direction,
            sensitivity=w[-1].sensitivity,
            defining=w,
            labels=tuple(f"{x.type}_{i + 1}" for i, x in enumerate(w)),
            known_index=k,
            atr_d=atr,
            lines=(upper, lower),
            confirmation_line=confirm,
            invalidation_line=invalid,
            height=w0,
            measures={
                "upper_slope_atr": su / atr,
                "lower_slope_atr": sl / atr,
                "width_start_atr": w0 / atr,
                "width_end_atr": w1 / atr,
                "apex_bars_after_last": apex - last,
                "span_bars": float(span),
            },
            touch_tol_atr=cfg.fit_tol_atr,
            touch_lines={"HIGH": "UPPER", "LOW": "LOWER"},
            horizon_end=self.horizon(
                cfg, (upper, lower), k, min(k + cfg.max_wait_bars + 1, apex_bar)
            ),
        )

    # ------------------------------------------------------- 7.10–7.11 flag, pennant

    def flags(self, s: float) -> list[Spec]:
        up: Kind = "HIGH" if s > 0 else "LOW"
        down = _opposite(up)
        out: list[Spec] = []
        for w in self.windows(self.fine, (down, up, down, up, down)):
            for family in ("flag", "pennant"):
                spec = self._flag(family, w, s)
                if spec is not None:
                    out.append(spec)
        return out

    def _flag(
        self, family: Literal["flag", "pennant"], w: tuple[SwingPoint, ...], s: float
    ) -> Spec | None:
        cfg = self.cfg.flag if family == "flag" else self.cfg.pennant
        self.generated(family)
        l0, h1, l2, h2, l3 = w
        atr = self.atr_at(l3.bar_index)
        if np.isnan(atr) or atr <= 0:
            self.reject(family, w, "atr_warmup")
            return None
        q0, q1, q2, q3, q4 = (s * x.price for x in w)
        pole = q1 - q0
        retrace = (q1 - min(q2, q4)) / pole if pole > 0 else float("inf")
        su = (q3 - q1) / (h2.bar_index - h1.bar_index)
        sl = (q4 - q2) / (l3.bar_index - l2.bar_index)
        flat = cfg.flat_slope_atr * atr
        duration = l3.bar_index - h1.bar_index
        if pole < cfg.pole_atr * atr:
            return self._no(family, w, "pole_height")
        if h1.bar_index - l0.bar_index > cfg.pole_max_bars:
            return self._no(family, w, "pole_speed")
        if not cfg.flag_min_bars <= duration <= cfg.flag_max_bars:
            return self._no(family, w, "flag_duration")
        if retrace > cfg.max_retrace:
            return self._no(family, w, "retrace")

        def q_upper(b: float) -> float:
            return q1 + su * (b - h1.bar_index)

        def q_lower(b: float) -> float:
            return q2 + sl * (b - l2.bar_index)

        if family == "flag":
            if abs(su - sl) > cfg.parallel_tol_atr * atr:
                return self._no(family, w, "parallel")
            if su > flat or sl > flat:
                return self._no(family, w, "slope_against_pole")
        else:
            if not (su < -flat and sl > flat):
                return self._no(family, w, "slopes")
            width_start = q_upper(h1.bar_index) - q_lower(h1.bar_index)
            width_end = q_upper(l3.bar_index) - q_lower(l3.bar_index)
            if width_start <= 0 or width_end > cfg.converge_ratio * width_start:
                return self._no(family, w, "convergence")
        # Lines back in price space: q-upper is the upper line when bullish, the lower
        # line when bearish.
        bull = s > 0
        hi_label, lo_label = ("UPPER", "LOWER") if bull else ("LOWER", "UPPER")
        upper_q = Line(hi_label, h1.bar_index, h1.price, s * su)
        lower_q = Line(lo_label, l2.bar_index, l2.price, s * sl)
        top, bottom = (upper_q, lower_q) if bull else (lower_q, upper_q)
        k = self.known_index(w)
        if self.first_outside(cfg, top, bottom, h1.bar_index, k + 1) is not None:
            return self._no(family, w, "close_outside")
        name = ("BULL_" if bull else "BEAR_") + family.upper()
        return Spec(
            family=family,
            pattern_type=name,
            direction="BULLISH" if bull else "BEARISH",
            sensitivity=l3.sensitivity,
            defining=w,
            labels=("POLE_START", "POLE_END", "FLAG_1", "FLAG_2", "FLAG_3"),
            known_index=k,
            atr_d=atr,
            lines=(top, bottom),
            confirmation_line=hi_label,
            invalidation_line=lo_label,
            invalidation_level=s * (q1 - cfg.max_retrace * pole),
            height=pole,
            measures={
                "pole_atr": pole / atr,
                "pole_bars": float(h1.bar_index - l0.bar_index),
                "flag_bars": float(duration),
                "retrace": retrace,
                "upper_slope_atr": su / atr,
                "lower_slope_atr": sl / atr,
            },
            touch_tol_atr=cfg.fit_tol_atr,
            touch_lines={"HIGH": "UPPER", "LOW": "LOWER"},
            touch_fine=True,
            horizon_end=self.horizon(cfg, (top, bottom), k, h1.bar_index + cfg.flag_max_bars + 1),
        )

    def _no(self, family: str, w: tuple[SwingPoint, ...], rule: str) -> None:
        self.reject(family, w, rule)

    # ------------------------------------------------------------------ 7.12 cup

    def cup_handle(self, s: float) -> list[Spec]:
        cfg = self.cfg.cup_handle
        down: Kind = "LOW" if s > 0 else "HIGH"
        out: list[Spec] = []
        for ra, rb, ups, _ in self._rims(s, cfg.cup_min_bars, cfg.cup_max_bars):
            handle = next(
                (x for x in self.fine if x.type == down and x.bar_index > rb.bar_index), None
            )
            w = (ra, rb) if handle is None else (ra, rb, handle)
            self.generated("cup_handle")
            qa, qb = s * ra.price, s * rb.price
            rim = min(qa, qb)
            if any(s * x.price >= rim for x in ups):
                self.reject("cup_handle", w, "interior_above_rims")
                continue
            if handle is None:
                self.reject("cup_handle", w, "handle_missing")
                continue
            hbars = handle.bar_index - rb.bar_index
            if not cfg.handle_min_bars <= hbars <= cfg.handle_max_bars:
                self.reject("cup_handle", w, "handle_window")
                continue
            atr = self.atr_at(handle.bar_index)
            if np.isnan(atr) or atr <= 0:
                self.reject("cup_handle", w, "atr_warmup")
                continue
            if abs(qa - qb) > cfg.rim_tol_atr * atr:
                self.reject("cup_handle", w, "rim_equality")
                continue
            a2, a1, a0, r2 = self.fit(ra.bar_index, rb.bar_index, s)
            if a2 <= 0:
                self.reject("cup_handle", w, "curvature")
                continue
            fitted_min = a0 - a1 * a1 / (4 * a2)
            depth = rim - fitted_min
            midpoint = (rim + fitted_min) / 2
            handle_depth = qb - s * handle.price
            if r2 < cfg.min_r2:
                self.reject("cup_handle", w, "fit")
            elif depth < cfg.min_depth_atr * atr:
                self.reject("cup_handle", w, "depth")
            elif depth > cfg.max_depth_ratio * min(abs(ra.price), abs(rb.price)):
                self.reject("cup_handle", w, "depth_ratio")
            elif handle_depth > cfg.handle_max_ratio * depth:
                self.reject("cup_handle", w, "handle_depth")
            elif s * handle.price <= midpoint:
                self.reject("cup_handle", w, "handle_below_midpoint")
            else:
                k = self.known_index(w)
                bull = s > 0
                out.append(
                    Spec(
                        family="cup_handle",
                        pattern_type="CUP_HANDLE" if bull else "INVERSE_CUP_HANDLE",
                        direction="BULLISH" if bull else "BEARISH",
                        sensitivity=rb.sensitivity,
                        defining=w,
                        labels=("RIM_1", "RIM_2", "HANDLE"),
                        known_index=k,
                        atr_d=atr,
                        lines=(Line("RIM", ra.bar_index, rb.price, 0.0),),
                        confirmation_level=rb.price,
                        invalidation_level=handle.price,
                        height=depth,
                        measures={
                            "rim_difference_atr": abs(qa - qb) / atr,
                            "r2": r2,
                            "depth_atr": depth / atr,
                            "handle_ratio": handle_depth / depth,
                            "handle_bars": float(hbars),
                            "cup_midpoint": s * midpoint,
                            "fit_a": s * a2,
                            "fit_b": s * a1,
                            "fit_c": s * a0,
                            "fit_origin_index": float(ra.bar_index),
                        },
                        horizon_end=min(k + cfg.max_wait_bars + 1, self.cb.n),
                    )
                )
        return out
