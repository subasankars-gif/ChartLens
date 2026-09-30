"""Trading calendars.

A calendar answers one question for the rest of the pipeline: which dates are
sessions? The weekly builder needs it to know whether a week is complete, and
data quality needs it to know which sessions are missing.

:class:`HolidayCalendar` is a generic weekday-plus-exceptions calendar. Exchange
providers supply the holiday list and any special weekend sessions (NSE, for
example, occasionally trades on a Saturday for the Union Budget, and holds a
Muhurat session that can fall on a Sunday).
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from datetime import date, timedelta
from typing import Protocol


class TradingCalendar(Protocol):
    exchange: str

    def is_session(self, day: date) -> bool: ...

    def sessions(self, start: date, end: date) -> list[date]:
        """All sessions in ``[start, end]``, ascending."""
        ...


class HolidayCalendar:
    """Monday–Friday sessions, minus holidays, plus explicitly listed special sessions."""

    def __init__(
        self,
        exchange: str,
        holidays: Iterable[date] = (),
        special_sessions: Iterable[date] = (),
    ) -> None:
        self.exchange = exchange
        self._holidays = frozenset(holidays)
        self._special = frozenset(special_sessions)
        overlap = self._holidays & self._special
        if overlap:
            raise ValueError(f"dates listed as both holiday and special session: {sorted(overlap)}")

    def is_session(self, day: date) -> bool:
        if day in self._special:
            return True
        return day.weekday() < 5 and day not in self._holidays

    def sessions(self, start: date, end: date) -> list[date]:
        return [d for d in _days(start, end) if self.is_session(d)]


def _days(start: date, end: date) -> Iterator[date]:
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)
