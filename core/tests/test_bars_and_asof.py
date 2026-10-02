from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest
from hypothesis import given
from hypothesis import strategies as st

from chartlens_core.asof import AsOfViolation, ensure_as_of, slice_as_of
from chartlens_core.bars import BAR_DATE, BarFrameError, validate_bar_frame
from chartlens_core.testing import make_bars

START = date(2020, 1, 1)


def test_synthetic_bars_satisfy_contract() -> None:
    validate_bar_frame(make_bars(START, 50))


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda df: df.drop(columns=["volume"]), "missing required columns"),
        (lambda df: df.iloc[::-1].reset_index(drop=True), "sorted ascending"),
        (lambda df: pd.concat([df, df.tail(1)], ignore_index=True), "duplicates"),
        (lambda df: df.assign(close=df["close"].astype("int64")), "close must be float64"),
        (lambda df: df.assign(**{BAR_DATE: df[BAR_DATE].dt.date}), "datetime64"),
        (lambda df: df.assign(is_complete=1), "is_complete must be bool"),
        (
            lambda df: df.assign(continuity_segment_id=["S@a"] * 10 + ["S@b"] * 10),
            "continuity segments",
        ),
        (lambda df: df.assign(continuity_segment_id=None), "contains nulls"),
    ],
)
def test_contract_violations_are_reported(mutate, message: str) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(BarFrameError, match=message):
        validate_bar_frame(mutate(make_bars(START, 20)))


def test_ensure_as_of_raises_on_future_rows() -> None:
    bars = make_bars(START, 10)
    as_of = bars[BAR_DATE].iloc[4].date()
    with pytest.raises(AsOfViolation, match="5 row"):
        ensure_as_of(bars, as_of)


def test_ensure_as_of_accepts_bar_dated_exactly_as_of() -> None:
    bars = make_bars(START, 10)
    ensure_as_of(bars, bars[BAR_DATE].iloc[-1].date())


def test_ensure_as_of_accepts_empty_frame() -> None:
    ensure_as_of(make_bars(START, 5).iloc[0:0], START)


@given(periods=st.integers(min_value=1, max_value=300), offset=st.integers(-10, 450))
def test_slice_as_of_is_exact(periods: int, offset: int) -> None:
    """Sliced output contains every row <= as_of and nothing after it."""
    bars = make_bars(START, periods, freq="D")
    as_of = START + timedelta(days=offset)
    sliced = slice_as_of(bars, as_of)
    ensure_as_of(sliced, as_of)
    expected = int((bars[BAR_DATE] <= pd.Timestamp(as_of)).sum())
    assert len(sliced) == expected


def test_slice_as_of_returns_a_copy() -> None:
    bars = make_bars(START, 10)
    sliced = slice_as_of(bars, bars[BAR_DATE].iloc[-1].date())
    sliced.loc[sliced.index[0], "close"] = -1.0
    assert bars["close"].iloc[0] != -1.0
