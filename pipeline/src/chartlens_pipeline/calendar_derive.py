"""Derive a trading calendar from what an exchange actually published (ADR-0008).

For each date in a range, ask the provider whether a daily file exists:

* weekday, no file anywhere (every location 404)  → closed (holiday)
* weekend, file exists                             → special session
* any location failed for another reason            → UNDETERMINED (never guessed)

Used to build calendar data for years with no official holiday list available.
Limitation, stated plainly: in derived years a session whose file NSE never
published is indistinguishable from a holiday. Where an official list exists
(``compare_with``), the derivation is checked against it.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date

from chartlens_pipeline.calendar import all_days
from chartlens_pipeline.providers.base import DailyBarSource, DownloadStatus

MAX_PLAUSIBLE_SESSIONS = 262
"""A full year has at most ~261 weekdays; more sessions than that means the evidence is bad."""
MAX_PLAUSIBLE_WEEKEND_SESSIONS = 6
"""Weekend sessions are rare (Budget days, Muhurat, DR drills) — a handful a year at most."""


@dataclass
class DerivedYear:
    year: int
    holidays: list[date] = field(default_factory=list)
    special_sessions: list[date] = field(default_factory=list)
    sessions: int = 0
    undetermined: list[tuple[date, str]] = field(default_factory=list)
    days_checked: int = 0

    def implausible(self) -> str | None:
        """Why this year's evidence cannot be trusted, if it cannot."""
        if self.sessions > MAX_PLAUSIBLE_SESSIONS:
            return f"{self.sessions} sessions in one year"
        if len(self.special_sessions) > MAX_PLAUSIBLE_WEEKEND_SESSIONS:
            return f"{len(self.special_sessions)} weekend sessions"
        if self.days_checked >= 360 and not self.holidays:
            return "no weekday closures in a full year"
        return None


def derive_calendar(
    make_source: Callable[[], DailyBarSource],
    start: date,
    end: date,
    *,
    workers: int = 4,
    progress: Callable[[int, int], None] | None = None,
) -> dict[int, DerivedYear]:
    days = all_days(start, end)
    sources: dict[int, DailyBarSource] = {}

    def check(day: date) -> tuple[date, DownloadStatus, str]:
        import threading

        tid = threading.get_ident()
        if tid not in sources:
            sources[tid] = make_source()
        result = sources[tid].check_published(day)
        return day, result.status, result.detail

    years: dict[int, DerivedYear] = defaultdict(lambda: DerivedYear(0))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for n, (day, status, detail) in enumerate(pool.map(check, days), start=1):
            y = years[day.year]
            y.year = day.year
            y.days_checked += 1
            weekend = day.weekday() >= 5
            if status is DownloadStatus.FOUND:
                y.sessions += 1
                if weekend:
                    y.special_sessions.append(day)
            elif status is DownloadStatus.NOT_PUBLISHED:
                if not weekend:
                    y.holidays.append(day)
            else:
                y.undetermined.append((day, detail))
            if progress and n % 250 == 0:
                progress(n, len(days))
    return dict(sorted(years.items()))


class ImplausibleCalendarError(ValueError):
    pass


def to_toml(years: dict[int, DerivedYear], derived_on: date) -> str:
    bad = {y.year: why for y in years.values() if (why := y.implausible())}
    if bad:
        raise ImplausibleCalendarError(f"refusing to emit implausible calendar data: {bad}")
    lines: list[str] = []
    for y in years.values():
        lines += [
            "[[years]]",
            f"year = {y.year}",
            'evidence = "derived"',
            f"# derived {derived_on}: {y.sessions} sessions, {len(y.holidays)} weekday closures, "
            f"{len(y.special_sessions)} weekend sessions, {len(y.undetermined)} undetermined",
            "holidays = [" + ", ".join(f'"{d}"' for d in y.holidays) + "]",
            "special_sessions = [" + ", ".join(f'"{d}"' for d in y.special_sessions) + "]",
            "",
        ]
    return "\n".join(lines)
