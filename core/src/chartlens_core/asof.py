"""Point-in-time primitives (spec §45–46, ADR-0006).

Every analysis runs *as of* a date. Two operations exist, deliberately separate:

* :func:`slice_as_of` — used once, at the boundary where historical data is
  loaded for a point-in-time run. It is the only sanctioned way to cut history.
* :func:`ensure_as_of` — used everywhere downstream. It never trims; it raises.
  A component handed future data is a bug in the caller, and silently dropping
  rows would hide that bug.

Scope of the guard: it prevents *future bars* from reaching a computation. It
cannot detect *future information baked into past bars*. Two known cases are
handled elsewhere:

* Weekly bars must be rebuilt from daily bars ``<= as_of`` for historical runs
  (a stored weekly bar may have been completed after ``as_of``).
* Back-adjusted prices embed later corporate actions. Ratios are unaffected, but
  absolute-price filters must use raw prices (ADR-0005).
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from chartlens_core.bars import BAR_DATE, column


class AsOfViolation(RuntimeError):
    """A computation received data dated after its ``as_of``."""


def to_timestamp(day: date) -> pd.Timestamp:
    """Midnight ``pd.Timestamp`` for a calendar date (the form bar dates are stored in)."""
    ts = pd.Timestamp(day)
    if not isinstance(ts, pd.Timestamp):  # NaT
        raise ValueError(f"not a valid date: {day!r}")
    return ts.normalize()


def slice_as_of(bars: pd.DataFrame, as_of: date, *, date_column: str = BAR_DATE) -> pd.DataFrame:
    """Return a copy of ``bars`` restricted to rows dated on or before ``as_of``."""
    mask = column(bars, date_column) <= to_timestamp(as_of)
    return bars.loc[mask].copy()


def ensure_as_of(bars: pd.DataFrame, as_of: date, *, date_column: str = BAR_DATE) -> None:
    """Raise :class:`AsOfViolation` if any row is dated after ``as_of``."""
    limit = to_timestamp(as_of)
    dates = column(bars, date_column)
    future = int((dates > limit).sum())
    if future:
        raise AsOfViolation(
            f"{future} row(s) after as_of={limit:%Y-%m-%d} (latest {dates.max():%Y-%m-%d})"
        )
