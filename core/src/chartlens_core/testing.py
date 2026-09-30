"""Deterministic synthetic data for tests. Never used to produce real results."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from chartlens_core.bars import BAR_DATE


def make_bars(
    start: date,
    periods: int,
    *,
    freq: str = "B",
    seed: int = 0,
    start_price: float = 100.0,
) -> pd.DataFrame:
    """Random-walk OHLCV bars satisfying the bar-frame contract and OHLC consistency."""
    rng = np.random.default_rng(seed)
    dates = pd.date_range(start=start, periods=periods, freq=freq)
    closes = start_price * np.exp(np.cumsum(rng.normal(0, 0.01, periods)))
    opens = np.concatenate(([start_price], closes[:-1]))
    spread = np.abs(rng.normal(0, 0.005, periods)) * closes
    highs = np.maximum(opens, closes) + spread
    lows = np.minimum(opens, closes) - spread
    volumes = rng.integers(10_000, 1_000_000, periods).astype("float64")
    return pd.DataFrame(
        {
            BAR_DATE: dates,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volumes,
        }
    )
