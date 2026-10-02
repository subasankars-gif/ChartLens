"""Exact, point-in-time price adjustment (ADR-0011). Pure functions, no I/O.

A *factor event* says: prices dated **before** ``ex_date`` are multiplied by ``factor``
(and volumes by ``volume_factor``) to be comparable with prices on and after it.
Factors are exact ``Fraction``s; nothing here uses floating point.

Point-in-time rule
------------------
An analysis *as of* date T applies only events with ``ex_date <= T``. An ex-date is
public on the ex-date (the price itself moves), so this needs no announcement
timestamps. The "latest" adjusted series is the special case T = +infinity, and any
earlier T is an exact rescaling of it:

    adjusted_as_of(T)[d] = raw[d] × Π{ f : d < f.ex_date ≤ T }

Rounding
--------
Adjusted prices are computed exactly (raw × cumulative fraction) and rounded **once**,
half-even, to ``ADJUSTED_PRICE_SCALE`` places — never by repeatedly rescaling an
already-rounded value, so the result cannot drift and is identical on every machine.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from fractions import Fraction
from itertools import pairwise
from typing import Final

ADJUSTED_PRICE_SCALE: Final = 6
ADJUSTED_VOLUME_SCALE: Final = 4


@dataclass(frozen=True, order=True)
class FactorEvent:
    ex_date: date
    factor: Fraction
    """Multiplier for prices dated before ``ex_date``. Must be > 0."""
    volume_factor: Fraction
    """Multiplier for volumes dated before ``ex_date`` (normally ``1 / factor``)."""
    action_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.factor <= 0 or self.volume_factor <= 0:
            raise ValueError(f"factors must be positive: {self}")


def combine(events: Iterable[FactorEvent]) -> list[FactorEvent]:
    """Merge events sharing an ex-date (e.g. bonus and split on one day) by multiplying."""
    merged: dict[date, FactorEvent] = {}
    for e in events:
        if e.ex_date in merged:
            m = merged[e.ex_date]
            merged[e.ex_date] = FactorEvent(
                e.ex_date,
                m.factor * e.factor,
                m.volume_factor * e.volume_factor,
                tuple(sorted({*m.action_ids, *e.action_ids})),
            )
        else:
            merged[e.ex_date] = e
    return [merged[d] for d in sorted(merged)]


def cumulative_factors(
    days: Sequence[date], events: Sequence[FactorEvent], as_of: date | None = None
) -> list[tuple[Fraction, Fraction]]:
    """(price, volume) cumulative factor for each date in ``days`` (ascending), as of ``as_of``.

    Linear in ``len(days) + len(events)``.
    """
    if any(b < a for a, b in pairwise(days)):
        raise ValueError("days must be ascending")
    applicable = sorted(e for e in combine(events) if as_of is None or e.ex_date <= as_of)
    result: list[tuple[Fraction, Fraction]] = [(Fraction(1), Fraction(1))] * len(days)
    price, volume = Fraction(1), Fraction(1)
    j = len(applicable) - 1
    # Walk backwards: every event whose ex_date is after day d applies to d.
    for i in range(len(days) - 1, -1, -1):
        while j >= 0 and applicable[j].ex_date > days[i]:
            price *= applicable[j].factor
            volume *= applicable[j].volume_factor
            j -= 1
        result[i] = (price, volume)
    return result


def apply(value: Decimal, factor: Fraction, scale: int) -> Decimal:
    """Exact ``value × factor`` rounded once, half-even, to ``scale`` decimal places.

    Pure integer arithmetic: the exact rational is formed and rounded a single time, so
    there is no intermediate rounding whatever the size of the factor's denominator.
    """
    if not value.is_finite() or factor <= 0:
        raise ValueError(f"cannot adjust {value} by {factor}")
    sign, digits, exponent = value.as_tuple()
    assert isinstance(exponent, int)
    mantissa = int("".join(map(str, digits)) or "0")
    num, den = mantissa * factor.numerator, factor.denominator
    shift = exponent + scale  # value × 10^scale = mantissa × 10^shift
    if shift >= 0:
        num *= 10**shift
    else:
        den *= 10**-shift
    q, r = divmod(num, den)
    if 2 * r > den or (2 * r == den and q % 2 == 1):
        q += 1
    return Decimal((sign, tuple(map(int, str(q))), -scale))


def adjust_price(raw: Decimal, factor: Fraction) -> Decimal:
    return apply(raw, factor, ADJUSTED_PRICE_SCALE)


def adjust_volume(raw: int, volume_factor: Fraction) -> Decimal:
    return apply(Decimal(raw), volume_factor, ADJUSTED_VOLUME_SCALE)


def fraction_text(f: Fraction) -> str:
    """Lossless text form for storage/audit, e.g. ``"1/5"``."""
    return f"{f.numerator}/{f.denominator}"


def parse_fraction(text: str) -> Fraction:
    num, _, den = text.partition("/")
    return Fraction(int(num), int(den or 1))
