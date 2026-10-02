"""usable_from and status: point-in-time, breaks only move it forward (ADR-0012)."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from chartlens_core.domain import DataQualityStatus as Q
from chartlens_core.quality import Dimension, Finding, Severity, status, usable_from

FIRST, LAST = date(2010, 1, 4), date(2024, 12, 31)


def brk(day: date) -> Finding:
    return Finding("S", day, day, Dimension.CORPORATE_ACTION, Severity.WARN, "UNQ", True)


def warn(day: date | None) -> Finding:
    return Finding("S", day, day, Dimension.PRICE, Severity.WARN, "MOVE")


def test_no_breaks_means_usable_from_first_session() -> None:
    assert usable_from(FIRST, []) == FIRST
    assert status(FIRST, LAST, []) == (Q.USABLE, FIRST)


def test_latest_break_known_as_of_the_date_wins() -> None:
    f = [brk(date(2015, 3, 2)), brk(date(2021, 10, 22))]
    assert usable_from(FIRST, f) == date(2021, 10, 22)
    assert usable_from(FIRST, f, as_of=date(2020, 1, 1)) == date(2015, 3, 2)
    assert usable_from(FIRST, f, as_of=date(2014, 1, 1)) == FIRST
    assert usable_from(FIRST, f, as_of=date(2009, 1, 1)) is None  # not listed yet


def test_warnings_only_count_inside_the_usable_window() -> None:
    f = [warn(date(2012, 1, 1)), brk(date(2021, 10, 22))]
    assert status(FIRST, LAST, f) == (Q.USABLE, date(2021, 10, 22))
    assert status(FIRST, LAST, [*f, warn(date(2022, 1, 3))])[0] is Q.USABLE_WITH_WARNINGS
    assert status(FIRST, LAST, [warn(None)])[0] is Q.USABLE_WITH_WARNINGS  # undated: always


def test_failure_makes_the_security_not_usable() -> None:
    fail = Finding("S", None, None, Dimension.SOURCE, Severity.FAIL, "HASH")
    assert status(FIRST, LAST, [fail])[0] is Q.NOT_USABLE


def test_info_never_changes_status() -> None:
    info = Finding("S", date(2020, 1, 1), None, Dimension.IDENTITY, Severity.INFO, "SYMBOL")
    assert status(FIRST, LAST, [info])[0] is Q.USABLE


def test_a_break_needs_a_date() -> None:
    with pytest.raises(ValueError, match="needs a date"):
        Finding("S", None, None, Dimension.CALENDAR, Severity.WARN, "GAP", True)


@given(
    st.lists(st.integers(0, 5000), max_size=6),
    st.integers(0, 5000),
    st.integers(0, 5000),
)
def test_usable_from_is_monotonic_in_as_of(breaks: list[int], t1: int, t2: int) -> None:
    f = [brk(FIRST + timedelta(days=b)) for b in breaks]
    a, b = sorted((FIRST + timedelta(days=t1), FIRST + timedelta(days=t2)))
    ua, ub = usable_from(FIRST, f, a), usable_from(FIRST, f, b)
    assert ua is not None and ub is not None and ua <= ub <= b
