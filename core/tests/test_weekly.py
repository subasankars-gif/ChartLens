"""Weekly bars: ISO weeks labelled by the last actual session, never across a break (ADR-0014)."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal as D

import pytest
from hypothesis import given
from hypothesis import strategies as st

from chartlens_core.asof import AsOfViolation
from chartlens_core.bars import validate_bar_frame
from chartlens_core.quality import FIRST_SESSION, ContinuitySegment
from chartlens_core.weekly import (
    DailyBar,
    PartialReason,
    WeeklyBar,
    build_weekly,
    current_segment,
    monday,
    to_bar_frame,
    week_last_sessions,
)

MON = date(2024, 1, 22)


def weekdays(start: date, end: date, skip: tuple[date, ...] = ()) -> list[date]:
    out, d = [], start
    while d <= end:
        if d.weekday() < 5 and d not in skip:
            out.append(d)
        d += timedelta(days=1)
    return out


def bar(day: date, close: str = "100", **kw: str) -> DailyBar:
    c = D(close)
    return DailyBar(
        day,
        D(kw.get("open", close)),
        D(kw.get("high", str(c + 1))),
        D(kw.get("low", str(c - 1))),
        c,
        D(kw.get("volume", "10")),
        D(kw.get("raw", close)),
    )


def seg(start: date, cause: str = FIRST_SESSION) -> ContinuitySegment:
    return ContinuitySegment("S", start, cause)


CAL = weekdays(date(2024, 1, 1), date(2024, 3, 31))
LAST = week_last_sessions(CAL)


def test_a_week_aggregates_exactly_and_is_labelled_by_its_last_session() -> None:
    days = [
        bar(MON, "100", open="98", high="103"),
        bar(MON + timedelta(1), "101", low="95.5"),
        bar(MON + timedelta(2), "99.25", volume="7.5"),
        bar(MON + timedelta(3), "104", high="104.000001"),
        bar(MON + timedelta(4), "102", raw="510"),
    ]
    (w,) = build_weekly(days, [seg(MON)], LAST, as_of=MON + timedelta(4))
    assert (w.open, w.high, w.low, w.close) == (D("98"), D("104.000001"), D("95.5"), D("102"))
    assert w.volume == D("47.5") and w.raw_close == D("510") and w.trading_days == 5
    assert (w.iso_year, w.iso_week, w.week_start_date, w.week_end_date) == (
        2024,
        4,
        MON,
        MON + timedelta(6),
    )
    assert (w.first_session_date, w.last_session_date) == (MON, MON + timedelta(4))
    assert w.is_complete and w.partial_reason is None and w.continuity_segment_id == "S@2024-01-22"


def test_friday_holiday_week_ends_and_completes_on_thursday() -> None:
    fri = MON + timedelta(4)
    last = week_last_sessions([d for d in CAL if d != fri])
    days = [bar(d) for d in weekdays(MON, fri, skip=(fri,))]
    (w,) = build_weekly(days, [seg(MON)], last, as_of=fri - timedelta(1))
    assert w.last_session_date == fri - timedelta(1) and w.is_complete


def test_the_forming_week_is_incomplete() -> None:
    days = [bar(d) for d in weekdays(MON, MON + timedelta(9))]  # through next Wednesday
    bars = build_weekly(days, [seg(MON)], LAST, as_of=MON + timedelta(9))
    assert [b.is_complete for b in bars] == [True, False]
    assert bars[-1].last_session_date == MON + timedelta(9)


def test_a_break_inside_a_week_splits_it_and_neither_side_is_dropped() -> None:
    thu = MON + timedelta(3)
    days = [bar(d, "100") for d in weekdays(MON, thu - timedelta(1))] + [
        bar(d, "60") for d in weekdays(thu, MON + timedelta(9))
    ]
    segments = [seg(MON - timedelta(7)), seg(thu, "UNQUANTIFIED_ACTION")]
    bars = build_weekly(days, segments, LAST, as_of=MON + timedelta(9))
    pre, post, nxt = bars
    assert (pre.first_session_date, pre.last_session_date, pre.close) == (
        MON,
        thu - timedelta(1),
        D(100),
    )
    assert (post.first_session_date, post.last_session_date, post.open) == (
        thu,
        thu + timedelta(1),
        D(60),
    )
    assert pre.partial_reason == post.partial_reason == PartialReason.CONTINUITY_BREAK
    assert nxt.partial_reason is None
    assert pre.is_complete and post.is_complete and not nxt.is_complete
    assert (pre.iso_week, pre.week_start_date) == (post.iso_week, post.week_start_date)
    assert pre.continuity_segment_id == "S@2024-01-15"
    assert post.continuity_segment_id == nxt.continuity_segment_id == "S@2024-01-25"
    assert current_segment(bars) == [post, nxt]


def test_pre_break_side_is_complete_even_while_its_week_is_still_forming() -> None:
    wed = MON + timedelta(2)
    days = [bar(MON), bar(MON + timedelta(1)), bar(wed, "50")]
    pre, post = build_weekly(days, [seg(MON), seg(wed, "TRADING_GAP")], LAST, as_of=wed)
    assert pre.is_complete and not post.is_complete


def test_a_break_on_monday_does_not_make_partial_bars() -> None:
    nxt = MON + timedelta(7)
    days = [bar(d) for d in weekdays(MON, nxt + timedelta(4))]
    bars = build_weekly(days, [seg(MON), seg(nxt, "X")], LAST, as_of=nxt + timedelta(4))
    assert [b.partial_reason for b in bars] == [None, None]
    assert [b.continuity_segment_id for b in bars] == ["S@2024-01-22", "S@2024-01-29"]


def test_weekend_sessions_are_flagged() -> None:
    sat, sun = date(2024, 2, 3), date(2024, 11, 3)  # a Saturday session; a Sunday Muhurat
    cal = sorted({*CAL, sat, *weekdays(date(2024, 10, 28), date(2024, 11, 1)), sun})
    last = week_last_sessions(cal)
    days = [bar(d) for d in weekdays(date(2024, 1, 29), date(2024, 2, 2))] + [bar(sat)]
    days += [bar(d) for d in weekdays(date(2024, 10, 28), date(2024, 11, 1))] + [bar(sun, "120")]
    w1, w2 = build_weekly(days, [seg(days[0].day)], last, as_of=sun)
    assert (w1.special_sessions, w1.closes_on_special_session, w1.last_session_date) == (
        1,
        True,
        sat,
    )
    assert (w2.close, w2.closes_on_special_session, w2.trading_days) == (D(120), True, 6)


def test_iso_weeks_cross_the_new_year() -> None:
    cal = weekdays(date(2024, 12, 23), date(2025, 1, 10), skip=(date(2024, 12, 25),))
    days = [bar(d) for d in weekdays(date(2024, 12, 30), date(2025, 1, 3))]
    (w,) = build_weekly(days, [seg(days[0].day)], week_last_sessions(cal), as_of=date(2025, 1, 3))
    assert (w.iso_year, w.iso_week, w.trading_days) == (2025, 1, 5)


def test_inputs_after_as_of_or_off_calendar_are_refused() -> None:
    days = [bar(MON), bar(MON + timedelta(1))]
    with pytest.raises(AsOfViolation):
        build_weekly(days, [seg(MON)], LAST, as_of=MON)
    with pytest.raises(AsOfViolation):
        build_weekly(days[:1], [seg(MON), seg(MON + timedelta(1), "X")], LAST, as_of=MON)
    with pytest.raises(ValueError, match="not a scheduled session"):
        build_weekly(
            [bar(date(2024, 1, 27))], [seg(date(2024, 1, 27))], LAST, as_of=date(2024, 2, 1)
        )
    with pytest.raises(ValueError, match="cover the first"):
        build_weekly(days, [seg(MON + timedelta(1))], LAST, as_of=MON + timedelta(1))


def test_bar_frame_of_the_current_segment_satisfies_the_contract() -> None:
    thu = MON + timedelta(3)
    days = [bar(d) for d in weekdays(MON - timedelta(14), MON + timedelta(9))]
    bars = build_weekly(days, [seg(days[0].day), seg(thu, "X")], LAST, as_of=days[-1].day)
    frame = to_bar_frame(current_segment(bars))
    validate_bar_frame(frame)
    assert list(frame["is_complete"]) == [True, False]
    with pytest.raises(ValueError, match="continuity segments"):
        validate_bar_frame(to_bar_frame(bars))


# ----------------------------------------------------------------------------- properties

SESSIONS = weekdays(date(2023, 1, 2), date(2023, 12, 29))


@st.composite
def histories(draw: st.DrawFn) -> tuple[list[DailyBar], list[ContinuitySegment], date]:
    traded = sorted(draw(st.sets(st.sampled_from(SESSIONS), min_size=1, max_size=120)))
    breaks = sorted(draw(st.sets(st.sampled_from(traded[1:] or traded), max_size=6)) - {traded[0]})
    prices = st.decimals(D("0.01"), D("5000"), places=6, allow_nan=False, allow_infinity=False)
    days = []
    for d in traded:
        o, c, x, y = (draw(prices) for _ in range(4))
        days.append(
            DailyBar(d, o, max(o, c, x), min(o, c, y), c, draw(st.decimals(0, 10**9, places=4)), c)
        )
    as_of = draw(st.sampled_from([d for d in SESSIONS if d >= traded[-1]]))
    return days, [seg(traded[0]), *(seg(b, "X") for b in breaks)], as_of


@given(histories())
def test_weekly_bars_reconcile_exactly_and_never_cross_a_break(
    h: tuple[list[DailyBar], list[ContinuitySegment], date],
) -> None:
    days, segments, as_of = h
    bars = build_weekly(days, segments, week_last_sessions(SESSIONS), as_of)
    assert sum(b.trading_days for b in bars) == len(days)
    labels = [b.last_session_date for b in bars]
    assert labels == sorted(set(labels))
    starts = [s.start for s in segments]
    for b in bars:
        inside = [d for d in days if b.first_session_date <= d.day <= b.last_session_date]
        assert len(inside) == b.trading_days
        assert b.open == inside[0].open and b.close == inside[-1].close
        assert b.high == max(d.high for d in inside) and b.low == min(d.low for d in inside)
        assert b.volume == sum((d.volume for d in inside), D(0))
        assert monday(b.first_session_date) == monday(b.last_session_date) == b.week_start_date
        # never across a break: no segment start strictly inside the bar
        assert not any(b.first_session_date < s <= b.last_session_date for s in starts)
        own = max(s for s in starts if s <= b.first_session_date)
        assert b.continuity_segment_id == f"S@{own.isoformat()}"
    # only the bars of the final week can be incomplete
    incomplete = [b for b in bars if not b.is_complete]
    assert all(b.week_start_date == bars[-1].week_start_date for b in incomplete)
    assert not incomplete or incomplete == [bars[-1]]


@given(histories())
def test_building_is_deterministic_and_as_of_monotone(
    h: tuple[list[DailyBar], list[ContinuitySegment], date],
) -> None:
    days, segments, as_of = h
    last = week_last_sessions(SESSIONS)
    full = build_weekly(days, segments, last, as_of)
    assert full == build_weekly(list(days), list(segments), last, as_of)
    # Cutting history at an earlier session changes only the final week's bar(s).
    cut = days[len(days) // 2].day
    early_days = [d for d in days if d.day <= cut]
    early_segs = [s for s in segments if s.start <= cut]
    early = build_weekly(early_days, early_segs, last, cut)
    settled = [b for b in early if b.week_start_date < monday(cut)]
    assert settled == [b for b in full if b.week_start_date < monday(cut)]
    assert all(isinstance(b, WeeklyBar) for b in early)
