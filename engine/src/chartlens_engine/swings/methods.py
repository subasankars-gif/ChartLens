"""The four swing-point methods (ADR-0020 §B), as causal single-pass scans.

Each method walks the **complete** bars of one segment left to right, once, and decides
everything at bar ``t`` from bars ``0 … t`` only. It never looks at a later bar, never
uses a centred window, a backward shift or a whole-series extreme, and never builds the
final sequence first and filters it afterwards. A pivot is emitted at the bar that
confirms it: ``known`` (that bar's index) is where the decision was made and ``bar`` the
pivot it confirms, so ``bar <= known`` by construction.

The leg still developing when the bars run out is returned separately as a pending
extreme. It is never a pivot.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

import numpy as np
from numpy.typing import NDArray

Kind = Literal["HIGH", "LOW"]
Array = NDArray[np.float64]


@dataclass(frozen=True)
class Pivot:
    kind: Kind
    bar: int
    """Index of the pivot bar (its high or low)."""
    known: int
    """Index of the bar whose completion confirmed it. Always >= ``bar``."""
    price: float


@dataclass(frozen=True)
class Pending:
    """The extreme of the leg still in progress: not a swing, and never becomes one
    retroactively — only a later confirming bar can make a pivot of it."""

    kind: Kind
    bar: int
    price: float


@dataclass(frozen=True)
class ScanResult:
    pivots: tuple[Pivot, ...]
    pending: Pending | None


def fractal(high: Array, low: Array, k: int) -> ScanResult:
    """FRACTAL(k): bar ``i`` is a swing high if its high is above every high of the ``k``
    bars before it and at least every high of the ``k`` bars after it (ties go to the
    earliest bar); lows mirrored. The decision for ``i`` is taken at bar ``i + k``, the
    first bar at which those ``k`` later bars have all closed."""
    pivots: list[Pivot] = []
    n = len(high)
    for t in range(2 * k, n):  # at bar t, decide the candidate i = t - k
        i = t - k
        left_h, right_h = high[i - k : i], high[i + 1 : t + 1]
        if high[i] > left_h.max() and high[i] >= right_h.max():
            pivots.append(Pivot("HIGH", i, t, float(high[i])))
        left_l, right_l = low[i - k : i], low[i + 1 : t + 1]
        if low[i] < left_l.min() and low[i] <= right_l.min():
            pivots.append(Pivot("LOW", i, t, float(low[i])))
    return ScanResult(tuple(pivots), None)


Threshold = Callable[[float, int], float | None]
"""(extreme price, extreme bar index) → the reversal that confirms it, or None when it
cannot be measured yet (e.g. ATR still in warm-up at that bar)."""


def zigzag(high: Array, low: Array, threshold: Threshold) -> ScanResult:
    """Reversal ZigZag on highs and lows (pass closes as both for a close-based ZigZag).

    In an up-leg the highest high H (bar j) is tracked. At each bar ``t`` the bar is
    first checked for a reversal against the extreme as it stood before ``t``: if
    ``low[t] <= H - threshold(H, j)``, a swing high at ``j`` is confirmed at ``t`` and a
    down-leg starts from ``low[t]``. Only otherwise may ``t`` extend the leg. Down-legs
    are mirrored. Before the first pivot both extremes are tracked; if one bar reverses
    both, the earlier extreme is confirmed (a high on a tie)."""
    pivots: list[Pivot] = []
    n = len(high)
    if n == 0:
        return ScanResult((), None)
    direction: Kind | None = None  # the leg being tracked: "HIGH" = up-leg
    hi_p, hi_i = float(high[0]), 0
    lo_p, lo_i = float(low[0]), 0

    def reversed_from_high(t: int) -> bool:
        thr = threshold(hi_p, hi_i)
        return thr is not None and float(low[t]) <= hi_p - thr

    def reversed_from_low(t: int) -> bool:
        thr = threshold(lo_p, lo_i)
        return thr is not None and float(high[t]) >= lo_p + thr

    for t in range(1, n):
        if direction is None:
            down, up = reversed_from_high(t), reversed_from_low(t)
            if down and (not up or hi_i <= lo_i):
                pivots.append(Pivot("HIGH", hi_i, t, hi_p))
                direction, lo_p, lo_i = "LOW", float(low[t]), t
                continue
            if up:
                pivots.append(Pivot("LOW", lo_i, t, lo_p))
                direction, hi_p, hi_i = "HIGH", float(high[t]), t
                continue
            if float(high[t]) > hi_p:
                hi_p, hi_i = float(high[t]), t
            if float(low[t]) < lo_p:
                lo_p, lo_i = float(low[t]), t
        elif direction == "HIGH":
            if reversed_from_high(t):
                pivots.append(Pivot("HIGH", hi_i, t, hi_p))
                direction, lo_p, lo_i = "LOW", float(low[t]), t
            elif float(high[t]) > hi_p:
                hi_p, hi_i = float(high[t]), t
        elif reversed_from_low(t):
            pivots.append(Pivot("LOW", lo_i, t, lo_p))
            direction, hi_p, hi_i = "HIGH", float(high[t]), t
        elif float(low[t]) < lo_p:
            lo_p, lo_i = float(low[t]), t

    pending: Pending | None = None
    if direction == "HIGH":
        pending = Pending("HIGH", hi_i, hi_p)
    elif direction == "LOW":
        pending = Pending("LOW", lo_i, lo_p)
    return ScanResult(tuple(pivots), pending)


def atr_threshold(atr: Array, multiple: float) -> Threshold:
    """ATR(m): ``m × ATR`` at the extreme's own bar — known at that bar, never later."""

    def threshold(_price: float, index: int) -> float | None:
        value = float(atr[index])
        return None if math.isnan(value) else multiple * value

    return threshold


def percent_threshold(percent: float) -> Threshold:
    """PERCENT(p) / ZIGZAG(p): ``p %`` of the extreme's price."""

    def threshold(price: float, _index: int) -> float | None:
        return abs(price) * percent / 100.0

    return threshold
