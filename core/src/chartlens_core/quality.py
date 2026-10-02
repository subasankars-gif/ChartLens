"""Data-quality findings, ``usable_from`` and status (ADR-0012). Pure functions, no I/O.

A *finding* is one observation about the data, with a dimension, a severity and the
date range it concerns. Findings that **break continuity** (an unquantified corporate
action, a factor the prices contradict, months without trading) mean no technical
analysis may span that date.

``usable_from`` is the earliest date from which a security's technical history is
considered reliable under the current methodology: the first session on/after the latest
continuity break (or the first session, if there is none). It is point-in-time — as of T
only findings dated ≤ T count — so the engine can ask the question as of any date.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from chartlens_core.domain import DataQualityStatus


class Dimension(StrEnum):
    SOURCE = "SOURCE"
    IDENTITY = "IDENTITY"
    PRICE = "PRICE"
    CORPORATE_ACTION = "CORPORATE_ACTION"
    CALENDAR = "CALENDAR"


class Severity(StrEnum):
    INFO = "INFO"
    """Recorded for traceability; never affects status."""
    WARN = "WARN"
    """Needs review; the history remains usable."""
    FAIL = "FAIL"
    """The data cannot be trusted at all (e.g. integrity failure)."""


@dataclass(frozen=True, order=True)
class Finding:
    security_id: str | None
    """None for market-wide findings (a missing session affects every security)."""
    start: date | None
    """First date concerned. For a continuity break: the first session after the break."""
    end: date | None
    dimension: Dimension
    severity: Severity
    code: str
    breaks_continuity: bool = False
    detail: str = ""
    evidence: str = ""
    """Pointer to the artefact that supports the finding (record keys, event, file hash)."""

    def __post_init__(self) -> None:
        if self.breaks_continuity and self.start is None:
            raise ValueError(f"a continuity break needs a date: {self}")

    def known_as_of(self, as_of: date | None) -> bool:
        return as_of is None or self.start is None or self.start <= as_of


def usable_from(
    first_date: date | None, findings: Iterable[Finding], as_of: date | None = None
) -> date | None:
    """First session on/after the latest continuity break known as of ``as_of``."""
    if first_date is None or (as_of is not None and first_date > as_of):
        return None
    breaks = [
        f.start
        for f in findings
        if f.breaks_continuity and f.start is not None and f.known_as_of(as_of)
    ]
    return max([first_date, *breaks])


def status(
    first_date: date | None,
    last_date: date | None,
    findings: Iterable[Finding],
    as_of: date | None = None,
) -> tuple[DataQualityStatus, date | None]:
    """(status, usable_from) as of ``as_of``.

    NOT_USABLE: a FAIL finding, or no session on/after ``usable_from``.
    USABLE_WITH_WARNINGS: a non-breaking WARN finding concerning the usable window.
    """
    relevant = [f for f in findings if f.known_as_of(as_of)]
    since = usable_from(first_date, relevant, as_of)
    end = last_date if as_of is None or last_date is None else min(last_date, as_of)
    if since is None or end is None or since > end:
        return DataQualityStatus.NOT_USABLE, since
    if any(f.severity is Severity.FAIL for f in relevant):
        return DataQualityStatus.NOT_USABLE, since
    for f in relevant:
        if f.severity is Severity.WARN and not f.breaks_continuity:
            concerns = f.end or f.start
            if concerns is None or concerns >= since:
                return DataQualityStatus.USABLE_WITH_WARNINGS, since
    return DataQualityStatus.USABLE, since
