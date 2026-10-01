from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from fractions import Fraction

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from chartlens_core.adjustment import (
    FactorEvent,
    adjust_price,
    adjust_volume,
    combine,
    cumulative_factors,
    fraction_text,
    parse_fraction,
)

D0 = date(2020, 1, 1)


def ev(day: int, f: Fraction) -> FactorEvent:
    return FactorEvent(D0 + timedelta(days=day), f, 1 / f, (f"a{day}",))


def test_split_and_bonus_examples() -> None:
    days = [D0 + timedelta(days=i) for i in range(5)]
    split = ev(2, Fraction(1, 5))  # FV 10 → 2
    bonus = ev(4, Fraction(1, 2))  # 1:1
    cum = [p for p, _ in cumulative_factors(days, [split, bonus])]
    assert cum == [Fraction(1, 10), Fraction(1, 10), Fraction(1, 2), Fraction(1, 2), Fraction(1)]


def test_events_on_the_same_day_multiply() -> None:
    a, b = (
        ev(3, Fraction(1, 2)),
        FactorEvent(D0 + timedelta(days=3), Fraction(4, 5), Fraction(5, 4), ("b",)),
    )
    (m,) = combine([a, b])
    assert (
        m.factor == Fraction(2, 5)
        and m.volume_factor == Fraction(5, 2)
        and m.action_ids == ("a3", "b")
    )


def test_as_of_ignores_events_after_as_of() -> None:
    days = [D0 + timedelta(days=i) for i in range(10)]
    events = [ev(3, Fraction(1, 2)), ev(7, Fraction(1, 5))]
    as_of = D0 + timedelta(days=5)
    assert [p for p, _ in cumulative_factors(days, events, as_of)][:3] == [Fraction(1, 2)] * 3
    assert all(p == 1 for p, _ in cumulative_factors(days, events, as_of)[3:])


def test_exact_rounding_half_even_once() -> None:
    assert adjust_price(Decimal("100"), Fraction(1, 3)) == Decimal("33.333333")
    assert adjust_price(Decimal("0.0000025"), Fraction(1)) == Decimal("0.000002")  # half-even
    assert adjust_price(Decimal("3197.00"), Fraction(1, 5)) == Decimal("639.400000")
    assert adjust_volume(3, Fraction(3, 2)) == Decimal("4.5000")


def test_fraction_text_round_trip() -> None:
    assert parse_fraction(fraction_text(Fraction(23, 31))) == Fraction(23, 31)
    assert parse_fraction("7") == Fraction(7)


def test_non_positive_factor_is_rejected() -> None:
    with pytest.raises(ValueError):
        FactorEvent(D0, Fraction(0), Fraction(1))


def test_days_must_be_ascending() -> None:
    with pytest.raises(ValueError):
        cumulative_factors([D0, D0 - timedelta(days=1)], [])


# ----------------------------------------------------------------------------- properties

fractions_ = st.fractions(min_value=Fraction(1, 50), max_value=Fraction(10), max_denominator=200)
event_lists = st.lists(st.tuples(st.integers(1, 60), fractions_), max_size=6)


def _events(raw: list[tuple[int, Fraction]]) -> list[FactorEvent]:
    return [ev(d, f) for d, f in raw]


@settings(deadline=None)
@given(event_lists, st.integers(0, 70))
def test_as_of_result_never_depends_on_later_events(
    raw: list[tuple[int, Fraction]], cut: int
) -> None:
    """Point-in-time: adding any event after as_of cannot change anything as of as_of."""
    days = [D0 + timedelta(days=i) for i in range(70)]
    as_of = D0 + timedelta(days=cut)
    events = _events(raw)
    earlier = [e for e in events if e.ex_date <= as_of]
    assert cumulative_factors(days, events, as_of) == cumulative_factors(days, earlier, as_of)


@settings(deadline=None)
@given(event_lists, st.integers(0, 70))
def test_as_of_is_an_exact_rescaling_of_latest(raw: list[tuple[int, Fraction]], cut: int) -> None:
    """latest[d] = as_of[d] × Π(events after as_of that apply to d)."""
    days = [D0 + timedelta(days=i) for i in range(70)]
    as_of = D0 + timedelta(days=cut)
    events = _events(raw)
    latest = cumulative_factors(days, events)
    at = cumulative_factors(days, events, as_of)
    for d, (lp, _), (ap, _) in zip(days, latest, at, strict=True):
        later = Fraction(1)
        for e in combine(events):
            if e.ex_date > as_of and d < e.ex_date:
                later *= e.factor
        assert lp == ap * later


@settings(deadline=None)
@given(event_lists)
def test_ratios_within_an_unbroken_segment_are_unchanged(raw: list[tuple[int, Fraction]]) -> None:
    """Between consecutive ex-dates every row has the same factor, so returns are untouched."""
    days = [D0 + timedelta(days=i) for i in range(70)]
    cum = [p for p, _ in cumulative_factors(days, _events(raw))]
    ex_dates = {e.ex_date for e in _events(raw)}
    for i in range(1, len(days)):
        if days[i] not in ex_dates:
            assert cum[i] == cum[i - 1]


@settings(deadline=None)
@given(st.decimals(min_value=Decimal("0.05"), max_value=Decimal("99999"), places=2), fractions_)
def test_adjust_then_unadjust_is_within_rounding(raw: Decimal, f: Fraction) -> None:
    back = adjust_price(adjust_price(raw, f), 1 / f)
    # Each rounding is at most half a unit (1e-6 / 2); the first is magnified by 1/f.
    assert Fraction(abs(back - raw)) <= Fraction(1, 2_000_000) * (1 + 1 / f)
