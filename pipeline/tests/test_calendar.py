from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from nse_fakes import calendar_for

from chartlens_pipeline.calendar import (
    CalendarCoverageError,
    CalendarEvidence,
    CalendarYear,
    DataCalendar,
    all_days,
)
from chartlens_pipeline.calendar_derive import derive_calendar, to_toml
from chartlens_pipeline.providers.base import DownloadResult, DownloadStatus
from chartlens_pipeline.providers.nse import load_calendar

REPUBLIC_DAY_2024 = date(2024, 1, 26)  # Friday
SAT_SESSION_2024 = date(2024, 1, 20)  # special Saturday session (NSE DR drill)


@pytest.fixture
def cal() -> DataCalendar:
    return calendar_for([2024], holidays=[REPUBLIC_DAY_2024], special=[SAT_SESSION_2024])


def test_normal_weekday_is_a_session(cal: DataCalendar) -> None:
    assert cal.is_trading_day(date(2024, 1, 25))


def test_weekend_is_not_a_session(cal: DataCalendar) -> None:
    assert not cal.is_trading_day(date(2024, 1, 21))


def test_holiday_is_not_a_session(cal: DataCalendar) -> None:
    assert not cal.is_trading_day(REPUBLIC_DAY_2024)


def test_special_saturday_is_a_session(cal: DataCalendar) -> None:
    assert cal.is_trading_day(SAT_SESSION_2024)


def test_range_crossing_holiday_and_special_session(cal: DataCalendar) -> None:
    assert cal.expected_sessions(date(2024, 1, 19), date(2024, 1, 29)) == [
        date(2024, 1, 19),
        date(2024, 1, 20),
        date(2024, 1, 22),
        date(2024, 1, 23),
        date(2024, 1, 24),
        date(2024, 1, 25),
        date(2024, 1, 29),
    ]


def test_uncovered_year_raises_instead_of_assuming_weekdays(cal: DataCalendar) -> None:
    with pytest.raises(CalendarCoverageError, match="no data for 2023"):
        cal.is_trading_day(date(2023, 12, 29))
    with pytest.raises(CalendarCoverageError):
        cal.expected_sessions(date(2023, 12, 28), date(2024, 1, 2))


def test_inverted_range_is_rejected(cal: DataCalendar) -> None:
    with pytest.raises(ValueError, match="after end"):
        cal.expected_sessions(date(2024, 2, 1), date(2024, 1, 1))


@pytest.mark.parametrize(
    ("holidays", "special", "message"),
    [
        ([date(2024, 1, 27)], [], "holidays must be weekdays"),
        ([], [date(2024, 1, 26)], "special sessions must be weekend"),
        ([date(2023, 1, 26)], [], "listed under year 2024"),
    ],
)
def test_calendar_year_validation(holidays: list[date], special: list[date], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        CalendarYear(2024, CalendarEvidence.OFFICIAL, frozenset(holidays), frozenset(special))


def test_toml_round_trip_and_versioning(tmp_path: Path) -> None:
    path = tmp_path / "cal.toml"
    path.write_text(
        'exchange = "NSE"\nversion = "t1"\n[[years]]\nyear = 2024\nevidence = "derived"\n'
        'holidays = ["2024-01-26"]\nspecial_sessions = ["2024-01-20"]\n'
    )
    loaded = DataCalendar.from_toml(path)
    assert loaded.version.startswith("t1+")
    assert loaded.evidence(2024) is CalendarEvidence.DERIVED
    assert not loaded.is_trading_day(REPUBLIC_DAY_2024) and loaded.is_trading_day(SAT_SESSION_2024)
    path.write_text(path.read_text().replace("2024-01-26", "2024-01-25"))
    assert (
        DataCalendar.from_toml(path).version != loaded.version
    )  # content hash catches unbumped edits


def test_shipped_nse_calendar_matches_the_official_2026_list() -> None:
    nse = load_calendar()
    assert nse.evidence(2026) is CalendarEvidence.OFFICIAL
    for holiday in (date(2026, 1, 26), date(2026, 10, 2), date(2026, 12, 25)):
        assert not nse.is_trading_day(holiday)
    assert nse.is_trading_day(date(2026, 11, 8))  # Sunday Muhurat session
    assert nse.is_trading_day(date(2026, 9, 29))


# ----------------------------------------------------------------------------- derivation


class _Archive:
    source_dataset = "bhavcopy"
    parser_version = "test"

    def __init__(self, published: set[date], failing: set[date] = frozenset()) -> None:  # type: ignore[assignment]
        self.published, self.failing = published, failing

    def check_published(self, day: date) -> DownloadResult:
        if day in self.failing:
            return DownloadResult(DownloadStatus.FAILED, None, (), "503")
        status = DownloadStatus.FOUND if day in self.published else DownloadStatus.NOT_PUBLISHED
        return DownloadResult(status, None, ())


def test_derivation_classifies_closures_special_sessions_and_failures() -> None:
    days = [date(2024, 1, d) for d in range(15, 32)]
    published = {d for d in days if d.weekday() < 5 and d != REPUBLIC_DAY_2024} | {SAT_SESSION_2024}
    archive = _Archive(published - {date(2024, 1, 31)}, failing={date(2024, 1, 31)})
    years = derive_calendar(lambda: archive, days[0], days[-1], workers=2)  # type: ignore[arg-type,return-value]
    y = years[2024]
    assert y.holidays == [REPUBLIC_DAY_2024]
    assert y.special_sessions == [SAT_SESSION_2024]
    assert [d for d, _ in y.undetermined] == [date(2024, 1, 31)]  # never guessed
    assert 'holidays = ["2024-01-26"]' in to_toml(years, date(2026, 9, 30))


def test_implausible_evidence_is_refused() -> None:
    """Regression: HEAD requests once made every day of 2006 look published."""
    from chartlens_pipeline.calendar_derive import ImplausibleCalendarError

    days = [date(2006, 1, 1), date(2006, 12, 31)]
    everything = _Archive(set(all_days(*days)))
    years = derive_calendar(lambda: everything, *days, workers=2)  # type: ignore[arg-type,return-value]
    assert years[2006].implausible() == "365 sessions in one year"
    with pytest.raises(ImplausibleCalendarError):
        to_toml(years, date(2026, 9, 30))


def test_shipped_nse_calendar_covers_2006_to_2026_with_evidence() -> None:
    nse = load_calendar()
    assert nse.covered_years == list(range(2006, 2027))
    assert all(nse.evidence(y) is CalendarEvidence.DERIVED for y in range(2006, 2026))
    for y in range(2006, 2026):  # every derived year is plausible (ADR-0008 guard)
        assert 240 <= len(nse.expected_sessions(date(y, 1, 1), date(y, 12, 31))) <= 262
    assert not nse.is_trading_day(REPUBLIC_DAY_2024)
    for special in (SAT_SESSION_2024, date(2025, 2, 1), date(2026, 2, 1), date(2020, 2, 1)):
        assert nse.is_trading_day(special), special  # Budget days and DR sessions
    assert nse.note(date(2026, 2, 1)) == "Union Budget (Sunday session)"
