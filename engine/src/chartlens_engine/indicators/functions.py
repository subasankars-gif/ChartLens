"""Indicator formulas (ADR-0020 §A). Pure functions over numpy arrays.

Conventions shared by every function:

* Input arrays are float64, oldest first, one value per bar of **one continuity segment**.
* A value at ``t`` uses inputs ``0 … t`` only (causal). Running on a prefix gives exactly
  the same values for that prefix, bit for bit: windows are evaluated independently and
  recursions run left to right from the same seed.
* Warm-up is NaN — the caller reports it as null. Nothing is filled or extrapolated.
* Division by zero gives NaN unless the ADR states a convention (RSI, stochastic).
"""

from __future__ import annotations

from typing import Final

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view
from numpy.typing import NDArray

Array = NDArray[np.float64]
NAN: Final = float("nan")


def _empty(n: int) -> Array:
    return np.full(n, NAN, dtype=np.float64)


def _windows(x: Array, n: int) -> NDArray[np.float64]:
    """Every length-``n`` window ending at bars ``n-1 … len-1`` (a read-only view)."""
    return sliding_window_view(x, n)


def sma(x: Array, n: int) -> Array:
    """Mean of the last ``n`` values; first value at ``t = n - 1``."""
    out = _empty(len(x))
    if n <= len(x):
        out[n - 1 :] = _windows(x, n).mean(axis=1)
    return out


def ema(x: Array, n: int) -> Array:
    """Exponential average, α = 2/(n+1), seeded with SMA(n) at ``t = n - 1``.

    NaN inputs (e.g. a warm-up prefix of another indicator) are skipped: the seed is the
    SMA of the first ``n`` valid values, and the recursion continues from there."""
    out = _empty(len(x))
    valid = np.flatnonzero(~np.isnan(x))
    if len(valid) < n:
        return out
    start = int(valid[0])
    if np.isnan(x[start:]).any():
        raise ValueError("ema input may only have NaN as a leading warm-up")
    seed_at = start + n - 1
    alpha = 2.0 / (n + 1)
    value = float(x[start : seed_at + 1].mean())
    out[seed_at] = value
    for t in range(seed_at + 1, len(x)):
        value = alpha * float(x[t]) + (1.0 - alpha) * value
        out[t] = value
    return out


def _wilder(values: Array, n: int, first: int) -> Array:
    """Wilder smoothing: the first average is the mean of ``values[first-n+1 … first]``,
    then avg[t] = (avg[t-1]·(n-1) + values[t]) / n."""
    out = _empty(len(values))
    if first >= len(values) or first - n + 1 < 0:
        return out
    avg = float(values[first - n + 1 : first + 1].mean())
    out[first] = avg
    for t in range(first + 1, len(values)):
        avg = (avg * (n - 1) + float(values[t])) / n
        out[t] = avg
    return out


def rsi(close: Array, n: int) -> Array:
    """Wilder's RSI. First value at ``t = n`` (the first ``n`` changes). L = 0 and G > 0
    gives 100; G = L = 0 gives 50."""
    out = _empty(len(close))
    if len(close) <= n:
        return out
    delta = np.diff(close, prepend=NAN)  # delta[0] is NaN: no change before the first bar
    gains = np.where(delta > 0, delta, 0.0)
    losses = np.where(delta < 0, -delta, 0.0)
    avg_gain = _wilder(gains, n, n)
    avg_loss = _wilder(losses, n, n)
    for t in range(n, len(close)):
        g, lo = avg_gain[t], avg_loss[t]
        if lo == 0.0:
            out[t] = 100.0 if g > 0.0 else 50.0
        else:
            out[t] = 100.0 - 100.0 / (1.0 + g / lo)
    return out


def macd(close: Array, fast: int, slow: int, signal: int) -> tuple[Array, Array, Array]:
    """MACD line = EMA(fast) − EMA(slow) (from ``t = slow - 1``); signal = EMA(signal) of
    the line, seeded with the SMA of its first ``signal`` values; histogram = line − signal."""
    line = ema(close, fast) - ema(close, slow)
    sig = ema(line, signal)
    return line, sig, line - sig


def stochastic(
    high: Array, low: Array, close: Array, k: int, k_smoothing: int, d: int
) -> tuple[Array, Array, Array]:
    """Slow stochastic: raw %K = 100·(c − LL_k)/(HH_k − LL_k), 50 when HH_k = LL_k;
    %K = SMA(raw, k_smoothing); %D = SMA(%K, d)."""
    raw = _empty(len(close))
    if k <= len(close):
        hh = _windows(high, k).max(axis=1)
        ll = _windows(low, k).min(axis=1)
        span = hh - ll
        c = close[k - 1 :]
        with np.errstate(divide="ignore", invalid="ignore"):
            raw[k - 1 :] = np.where(span > 0, 100.0 * (c - ll) / span, 50.0)
    k_slow = _sma_after_warmup(raw, k_smoothing)
    d_line = _sma_after_warmup(k_slow, d)
    return raw, k_slow, d_line


def _sma_after_warmup(x: Array, n: int) -> Array:
    """SMA of a series that itself has a NaN warm-up prefix."""
    out = _empty(len(x))
    valid = np.flatnonzero(~np.isnan(x))
    if len(valid) == 0:
        return out
    start = int(valid[0])
    out[start:] = sma(x[start:], n)
    return out


def roc(close: Array, n: int) -> Array:
    """100·(c[t]/c[t−n] − 1); first value at ``t = n``; NaN when c[t−n] = 0."""
    out = _empty(len(close))
    if len(close) > n:
        base = close[:-n]
        with np.errstate(divide="ignore", invalid="ignore"):
            out[n:] = np.where(base != 0, 100.0 * (close[n:] / base - 1.0), NAN)
    return out


def true_range(high: Array, low: Array, close: Array) -> Array:
    """TR[t] = max(h−l, |h−c[t−1]|, |l−c[t−1]|) for ``t ≥ 1``; TR[0] is NaN (no previous
    close), so it never enters ATR."""
    out = _empty(len(close))
    if len(close) > 1:
        prev = close[:-1]
        h, lo = high[1:], low[1:]
        out[1:] = np.maximum.reduce([h - lo, np.abs(h - prev), np.abs(lo - prev)])
    return out


def atr(high: Array, low: Array, close: Array, n: int) -> Array:
    """Wilder's ATR: ATR[n] = mean(TR[1 … n]), then Wilder smoothing."""
    return _wilder(true_range(high, low, close), n, n)


def atr_percent(atr_values: Array, close: Array) -> Array:
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(close != 0, 100.0 * atr_values / close, NAN)


def bollinger(close: Array, n: int, k: float) -> tuple[Array, Array, Array, Array]:
    """Middle = SMA(n); σ = population standard deviation (ddof 0) of the last ``n``
    closes; upper/lower = middle ± k·σ; bandwidth = (upper − lower)/middle."""
    mid = sma(close, n)
    sd = _empty(len(close))
    if n <= len(close):
        sd[n - 1 :] = _windows(close, n).std(axis=1, ddof=0)
    upper, lower = mid + k * sd, mid - k * sd
    with np.errstate(divide="ignore", invalid="ignore"):
        width = np.where(mid != 0, (upper - lower) / mid, NAN)
    return mid, upper, lower, width


def relative_volume(volume: Array, baseline: int) -> Array:
    """RVOL[t] = v[t] / mean(v[t−n … t−1]): the current week is not in its own baseline.
    First value at ``t = n``; NaN when the baseline is 0."""
    out = _empty(len(volume))
    if len(volume) > baseline:
        base = _windows(volume[:-1], baseline).mean(axis=1)
        with np.errstate(divide="ignore", invalid="ignore"):
            out[baseline:] = np.where(base > 0, volume[baseline:] / base, NAN)
    return out


def obv(close: Array, volume: Array) -> Array:
    """On-balance volume from 0 at the segment's first bar."""
    out = np.zeros(len(close), dtype=np.float64)
    if len(close) > 1:
        out[1:] = np.cumsum(np.sign(np.diff(close)) * volume[1:])
    return out


def volume_trend(volume: Array, short: int, long: int, band: float) -> list[str | None]:
    """RISING / FALLING / FLAT: SMA(short) of volume against SMA(long) ± ``band``."""
    s, lg = sma(volume, short), sma(volume, long)
    out: list[str | None] = []
    for a, b in zip(s.tolist(), lg.tolist(), strict=True):
        if np.isnan(a) or np.isnan(b):
            out.append(None)
        elif a > b * (1.0 + band):
            out.append("RISING")
        elif a < b * (1.0 - band):
            out.append("FALLING")
        else:
            out.append("FLAT")
    return out


def volume_state(rvol: Array, expansion: float, contraction: float) -> list[str | None]:
    """EXPANSION / CONTRACTION / NORMAL from relative volume."""
    out: list[str | None] = []
    for r in rvol.tolist():
        if np.isnan(r):
            out.append(None)
        elif r >= expansion:
            out.append("EXPANSION")
        elif r <= contraction:
            out.append("CONTRACTION")
        else:
            out.append("NORMAL")
    return out
