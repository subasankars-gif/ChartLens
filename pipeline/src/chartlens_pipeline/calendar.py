"""Trading calendars (ADR-0008).

A calendar answers: which dates are sessions? The ingestion layer uses it to tell
*"expected session, data missing"* apart from *"not a trading day"*, and the weekly
builder (Milestone 4) will use it to decide whether a week is complete.

Calendars are **data, not rules**: every covered year lists its weekday closures
and its weekend sessions explicitly, with the evidence behind them. A date in a
year the calendar does not cover raises :class:`CalendarCoverageError` — the
calendar never falls back to "Monday–Friday", because that assumption is exactly
what makes a missing file look like a normal day (or a holiday look like missing data).
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Protocol


class CalendarCoverageError(LookupError):
    """The calendar has no data for the requested year."""


class CalendarEvidence(StrEnum):
    OFFICIAL = "official"
    """Published holiday list from the exchange."""
    DERIVED = "derived"
    """Inferred from which daily files the exchange actually published (see ADR-0008)."""


class TradingCalendar(Protocol):
    exchange: str
    version: str

    def is_trading_day(self, day: date) -> bool: ...

    def expected_sessions(self, start: date, end: date) -> list[date]:
        """All sessions in ``[start, end]``, ascending."""
        ...

    def evidence(self, year: int) -> CalendarEvidence: ...


@dataclass(frozen=True)
class CalendarYear:
    year: int
    evidence: CalendarEvidence
    holidays: frozenset[date] = frozenset()
    """Weekdays (Mon–Fri) on which the exchange did not trade."""
    special_sessions: frozenset[date] = frozenset()
    """Weekend dates on which the exchange did trade (Budget days, Muhurat, drills)."""
    notes: Mapping[date, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for day in (*self.holidays, *self.special_sessions):
            if day.year != self.year:
                raise ValueError(f"{day} listed under year {self.year}")
        weekend_holidays = sorted(d for d in self.holidays if d.weekday() >= 5)
        if weekend_holidays:
            raise ValueError(f"holidays must be weekdays (weekends are closed anyway): {weekend_holidays}")
        weekday_specials = sorted(d for d in self.special_sessions if d.weekday() < 5)
        if weekday_specials:
            raise ValueError(f"special sessions must be weekend dates: {weekday_specials}")


class DataCalendar:
    """Calendar backed by explicit per-year data."""

    def __init__(self, exchange: str, years: Iterable[CalendarYear], version: str) -> None:
        self.exchange = exchange
        self.version = version
        self._years: dict[int, CalendarYear] = {}
        for y in years:
            if y.year in self._years:
                raise ValueError(f"year {y.year} listed twice")
            self._years[y.year] = y

    @property
    def covered_years(self) -> list[int]:
        return sorted(self._years)

    def _year(self, year: int) -> CalendarYear:
        try:
            return self._years[year]
        except KeyError:
            covered = f"{min(self._years)}–{max(self._years)}" if self._years else "none"
            raise CalendarCoverageError(
                f"{self.exchange} calendar has no data for {year} (covered: {covered})"
            ) from None

    def is_trading_day(self, day: date) -> bool:
        y = self._year(day.year)
        if day in y.special_sessions:
            return True
        return day.weekday() < 5 and day not in y.holidays

    def expected_sessions(self, start: date, end: date) -> list[date]:
        if start > end:
            raise ValueError(f"start {start} is after end {end}")
        return [d for d in _days(start, end) if self.is_trading_day(d)]

    def evidence(self, year: int) -> CalendarEvidence:
        return self._year(year).evidence

    def note(self, day: date) -> str | None:
        return self._year(day.year).notes.get(day)

    # ------------------------------------------------------------------ persistence

    @classmethod
    def from_toml(cls, path: Path) -> DataCalendar:
        """Load a calendar data file. The version is the file's declared version plus a
        content hash, so an edit that forgets to bump the version is still detectable."""
        raw = path.read_bytes()
        data = tomllib.loads(raw.decode())
        years: list[CalendarYear] = []
        for entry in data.get("years", []):
            notes = {date.fromisoformat(k): v for k, v in entry.get("notes", {}).items()}
            years.append(
                CalendarYear(
                    year=int(entry["year"]),
                    evidence=CalendarEvidence(entry["evidence"]),
                    holidays=frozenset(_dates(entry.get("holidays", []))),
                    special_sessions=frozenset(_dates(entry.get("special_sessions", []))),
                    notes=notes,
                )
            )
        digest = hashlib.sha256(raw).hexdigest()[:8]
        return cls(data["exchange"], years, version=f"{data['version']}+{digest}")

    def fingerprint(self) -> str:
        payload = {
            str(y.year): [sorted(map(str, y.holidays)), sorted(map(str, y.special_sessions))]
            for y in self._years.values()
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:12]


def _dates(values: Iterable[object]) -> Iterator[date]:
    for v in values:
        yield v if isinstance(v, date) else date.fromisoformat(str(v))


def _days(start: date, end: date) -> Iterator[date]:
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def all_days(start: date, end: date) -> list[date]:
    return list(_days(start, end))
