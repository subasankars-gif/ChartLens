"""The bar-frame contract shared by the pipeline and the technical engine.

A *bar frame* is a pandas DataFrame with one row per bar, for any timeframe.
The engine never sees exchange-specific columns and never branches on timeframe
to decide *how to read* bars — weekly is just the first timeframe that gets
analysed (spec §4, §50).

Required columns
----------------
``bar_date``  datetime64[ns], tz-naive. The date the bar closes: the session date
              for daily bars, the last actual session of the week for weekly bars.
``open``, ``high``, ``low``, ``close``  float64
``volume``    float64 (float so adjusted volume can be fractional before rounding)

Optional columns
----------------
``is_complete``  bool. False only for a bar still forming at build time (the
                 current week). Confirmation logic must ignore incomplete bars.

This module checks *shape*, not *quality*. OHLC consistency, gaps and suspicious
moves are the data-quality engine's job (Milestone 3).
"""

from __future__ import annotations

from typing import Final, cast

import pandas as pd
from pandas.api import types as ptypes

BAR_DATE: Final = "bar_date"
PRICE_COLUMNS: Final = ("open", "high", "low", "close")
VOLUME: Final = "volume"
IS_COMPLETE: Final = "is_complete"
REQUIRED_COLUMNS: Final = (BAR_DATE, *PRICE_COLUMNS, VOLUME)


class BarFrameError(ValueError):
    """The frame violates the bar-frame contract."""


def column(frame: pd.DataFrame, name: str) -> pd.Series:
    """A single column, typed as a Series (pandas stubs type ``df[name]`` loosely)."""
    return cast(pd.Series, frame[name])


def validate_bar_frame(bars: pd.DataFrame) -> None:
    """Raise :class:`BarFrameError` if ``bars`` does not satisfy the contract."""
    missing = [c for c in REQUIRED_COLUMNS if c not in bars.columns]
    if missing:
        raise BarFrameError(f"missing required columns: {missing}")

    dates = column(bars, BAR_DATE)
    if not ptypes.is_datetime64_dtype(dates):
        raise BarFrameError(f"{BAR_DATE} must be tz-naive datetime64, got {dates.dtype}")
    if bool(dates.isna().any()):
        raise BarFrameError(f"{BAR_DATE} contains nulls")
    if not dates.is_monotonic_increasing:
        raise BarFrameError(f"{BAR_DATE} must be sorted ascending")
    if bool(dates.duplicated().any()):
        raise BarFrameError(f"{BAR_DATE} contains duplicates")

    for name in (*PRICE_COLUMNS, VOLUME):
        dtype = column(bars, name).dtype
        if dtype != "float64":
            raise BarFrameError(f"{name} must be float64, got {dtype}")

    if IS_COMPLETE in bars.columns and not ptypes.is_bool_dtype(column(bars, IS_COMPLETE)):
        raise BarFrameError(f"{IS_COMPLETE} must be bool, got {column(bars, IS_COMPLETE).dtype}")
