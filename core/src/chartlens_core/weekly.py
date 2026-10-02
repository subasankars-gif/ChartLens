"""Weekly bars from adjusted daily bars (ADR-0004, ADR-0014). Pure functions, no I/O.

Rules
-----
* Sessions are grouped by ISO week (Monday–Sunday). A bar is labelled by the **last
  actual session** in it (``last_session_date``, the bar-frame ``bar_date``).
* open = first session's open, high = max high, low = min low, close = last session's
  close, volume = sum of volumes. All exact ``Decimal`` arithmetic: a weekly bar
  reconciles exactly to the daily bars it was built from.
* **A bar never crosses a continuity boundary.** A break inside a week splits it into
  two bars, one per continuity segment, both marked ``partial_reason =
  CONTINUITY_BREAK``. Neither side is dropped.
* ``is_complete`` is decided from the trading calendar: a week is complete once its last
  *scheduled* session is on or before ``as_of``. The pre-break part of a split week is
  complete (the break closed it). Incomplete bars must never confirm anything.
* Non-regular sessions — every weekend session and weekday sessions the calendar marks
  (a weekday Muhurat session) — are counted in ``special_sessions``. A bar whose close
  comes from one says so in ``closes_on_special_session`` and ``closing_session_type``
  (MUHURAT, BUDGET, DR_DRILL, OTHER). Prices stay exactly as traded (ADR-0015).

The builder sees only what it is given and refuses data dated after ``as_of``: cutting
history and choosing the point-in-time adjustment is the caller's job (ADR-0006).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise
from typing import Final

import pandas as pd

from chartlens_core.asof import AsOfViolation
from chartlens_core.bars import (
    BAR_DATE,
    CLOSES_ON_SPECIAL_SESSION,
    IS_COMPLETE,
    SECURITY,
    SEGMENT,
)
from chartlens_core.quality import ContinuitySegment

WEEKLY_BUILDER_VERSION: Final = "weekly_v2"
"""Bump on any change to how weekly bars are formed (part of the weekly version).
v2: weekday non-regular sessions and the closing session's type (ADR-0015)."""


class PartialReason(StrEnum):
    CONTINUITY_BREAK = "CONTINUITY_BREAK"
    """A continuity break inside the week split it; this bar is one side of it."""


@dataclass(frozen=True, slots=True)
class DailyBar:
    """One adjusted session as the weekly builder needs it."""

    day: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    raw_close: Decimal
    """The close as traded (unadjusted), for absolute-price filters (ADR-0005)."""


@dataclass(frozen=True, slots=True)
class WeeklyBar:
    continuity_segment_id: str
    iso_year: int
    iso_week: int
    week_start_date: date
    """Monday of the ISO week (a calendar bound, not a session)."""
    week_end_date: date
    """Sunday of the ISO week (a calendar bound, not a session)."""
    first_session_date: date
    last_session_date: date
    """The bar's label: its last actual session."""
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    raw_close: Decimal
    trading_days: int
    is_complete: bool
    partial_reason: str | None
    special_sessions: int
    closes_on_special_session: bool
    closing_session_type: str | None
    """Type of the closing session when it is non-regular; None for a regular close."""


def monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


def week_last_sessions(sessions: Sequence[date]) -> dict[date, date]:
    """Monday of each ISO week → its last scheduled session, from a calendar's sessions."""
    out: dict[date, date] = {}
    for d in sessions:
        key = monday(d)
        if key not in out or d > out[key]:
            out[key] = d
    return out


def build_weekly(
    days: Sequence[DailyBar],
    segments: Sequence[ContinuitySegment],
    last_scheduled: Mapping[date, date],
    as_of: date,
    session_types: Mapping[date, str] | None = None,
) -> list[WeeklyBar]:
    """Weekly bars for one security, oldest first.

    ``days`` ascending, all on or before ``as_of``; ``segments`` the security's continuity
    segments known as of ``as_of`` (oldest first, the first starting on or before the first
    day); ``last_scheduled`` maps each ISO week's Monday to its last scheduled session;
    ``session_types`` maps non-regular sessions to their type (a weekend session missing
    from it is still non-regular, typed OTHER).
    """
    types = session_types or {}

    def kind(day: date) -> str | None:
        return types.get(day, "OTHER" if day.weekday() >= 5 else None)

    if not days:
        return []
    if any(b.day <= a.day for a, b in pairwise(days)):
        raise ValueError("daily bars must be strictly ascending by date")
    if days[-1].day > as_of:
        raise AsOfViolation(f"daily bar {days[-1].day} is after as_of={as_of}")
    starts = [s.start for s in segments]
    if not segments or starts != sorted(set(starts)) or starts[0] > days[0].day:
        raise ValueError("segments must be ascending and cover the first daily bar")
    if starts[-1] > as_of:
        raise AsOfViolation(f"continuity segment starting {starts[-1]} is after as_of={as_of}")

    # Group consecutive sessions by (segment, ISO week).
    groups: list[tuple[ContinuitySegment, list[DailyBar]]] = []
    seg_index = 0
    for bar in days:
        while seg_index + 1 < len(segments) and segments[seg_index + 1].start <= bar.day:
            seg_index += 1
        seg = segments[seg_index]
        if groups and groups[-1][0] is seg and monday(groups[-1][1][0].day) == monday(bar.day):
            groups[-1][1].append(bar)
        else:
            groups.append((seg, [bar]))

    weeks_split = {
        monday(a[1][0].day)
        for a, b in pairwise(groups)
        if monday(a[1][0].day) == monday(b[1][0].day)
    }
    out: list[WeeklyBar] = []
    for i, (seg, bars) in enumerate(groups):
        week = monday(bars[0].day)
        scheduled = last_scheduled.get(week)
        if scheduled is None or scheduled < bars[-1].day:
            raise ValueError(
                f"{bars[-1].day} is not a scheduled session of the calendar (week of {week})"
            )
        closed_by_break = i + 1 < len(groups) and monday(groups[i + 1][1][0].day) == week
        iso = bars[0].day.isocalendar()
        out.append(
            WeeklyBar(
                continuity_segment_id=seg.id,
                iso_year=iso.year,
                iso_week=iso.week,
                week_start_date=week,
                week_end_date=week + timedelta(days=6),
                first_session_date=bars[0].day,
                last_session_date=bars[-1].day,
                open=bars[0].open,
                high=max(b.high for b in bars),
                low=min(b.low for b in bars),
                close=bars[-1].close,
                volume=sum((b.volume for b in bars), Decimal(0)),
                raw_close=bars[-1].raw_close,
                trading_days=len(bars),
                is_complete=closed_by_break or scheduled <= as_of,
                partial_reason=str(PartialReason.CONTINUITY_BREAK) if week in weeks_split else None,
                special_sessions=sum(1 for b in bars if kind(b.day) is not None),
                closes_on_special_session=kind(bars[-1].day) is not None,
                closing_session_type=kind(bars[-1].day),
            )
        )
    return out


def current_segment(bars: Sequence[WeeklyBar]) -> list[WeeklyBar]:
    """The bars of the latest continuity segment (the one valid at the data's as_of)."""
    if not bars:
        return []
    last = bars[-1].continuity_segment_id
    return [b for b in bars if b.continuity_segment_id == last]


def to_bar_frame(bars: Sequence[WeeklyBar], security_id: str) -> pd.DataFrame:
    """Weekly bars as an engine bar frame (``chartlens_core.bars``): floats for computation,
    one row per bar labelled by its last session, with the security, segment, completeness
    and closing-session flags the engine's comparability guard needs."""
    frame = pd.DataFrame(
        {
            BAR_DATE: pd.to_datetime([b.last_session_date for b in bars]),
            SECURITY: [security_id] * len(bars),
            "open": [float(b.open) for b in bars],
            "high": [float(b.high) for b in bars],
            "low": [float(b.low) for b in bars],
            "close": [float(b.close) for b in bars],
            "volume": [float(b.volume) for b in bars],
            IS_COMPLETE: pd.Series([b.is_complete for b in bars], dtype=bool),
            SEGMENT: [b.continuity_segment_id for b in bars],
            "first_session_date": pd.to_datetime([b.first_session_date for b in bars]),
            "trading_days": [b.trading_days for b in bars],
            "partial_reason": [b.partial_reason for b in bars],
            CLOSES_ON_SPECIAL_SESSION: pd.Series(
                [b.closes_on_special_session for b in bars], dtype=bool
            ),
            "closing_session_type": [b.closing_session_type for b in bars],
            "raw_close": [float(b.raw_close) for b in bars],
        }
    )
    for name in ("open", "high", "low", "close", "volume", "raw_close"):
        frame[name] = frame[name].astype("float64")
    return frame
