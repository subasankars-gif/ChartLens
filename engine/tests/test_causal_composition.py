"""Causal composition across layers D–F (ADR-0019, ADR-0021 Phase 4 rules).

> A derived technical object cannot become knowable before the latest ``known_at`` of the
> information required to construct it.

Checked three ways: generically through ``depends_on`` on random series; on a scenario
where one swing becomes known on 2026-07-10 and Fibonacci, a divergence and a zone are
all built from it; and by prefix stability — running on the bars up to T gives exactly
the full run's objects known by T, with their histories as of T.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest
from analysis_chain import SEG, SID, Chain, derived, known_at_of, random_bars, run_chain
from hypothesis import given, settings
from hypothesis import strategies as st

from chartlens_core.config import AnalysisConfig, IndicatorConfig, SwingConfig


@pytest.mark.parametrize("seed", [3, 17, 29, 41])
def test_nothing_is_known_before_what_it_is_built_from(seed: int) -> None:
    chain = run_chain(random_bars(700, seed))
    known = known_at_of(chain)
    rows = derived(chain)
    assert rows
    checked = 0
    for object_id, known_at, depends_on in rows:
        for dep in depends_on:
            if dep in known:
                assert known_at >= known[dep], (object_id, dep)
                checked += 1
    assert checked > 50
    for z in chain.levels.zones:  # a zone is as late as its latest source
        assert z.known_at == max(s.known_at for s in z.sources)
        assert z.first_seen == min(s.known_at for s in z.sources)


@pytest.mark.parametrize("seed", [5, 6])
def test_every_object_stays_in_its_segment_and_after_its_bars(seed: int) -> None:
    chain = run_chain(random_bars(600, seed))
    for f in chain.fibonacci.structures:
        assert f.continuity_segment_id == SEG
        assert f.anchor_bar_date < f.counter_bar_date <= f.known_at
    for t in chain.levels.trendlines:
        assert all(x.bar_date <= x.known_at for x in t.touches)
        assert t.anchor_1_bar_date < t.anchor_2_bar_date <= t.known_at
    for d in chain.divergence.divergences:
        assert d.date_start < d.date_end <= d.known_at
        assert d.status_history[0].date == d.known_at
    for e in [*chain.volume.events, *chain.volatility.events, *chain.candles.events]:
        assert e.continuity_segment_id == SEG and e.known_at == e.bar_date


# ----------------------------------------------------------------- the July 10 scenario

# Weekly closes ending on Friday 2026-07-17. A sharp fall to a low, a rally, then a slow
# drift to a lower low on 2026-07-03 that a FRACTAL/MICRO swing confirms one bar later,
# on 2026-07-10. RSI is far less oversold at the second low: a regular bullish divergence.
_CLOSES = [
    *[100.0 - 0.2 * i for i in range(30)],  # 30 bars drifting from 100 to 94.2
    90.0, 84.0, 78.0, 72.0,  # a sharp fall
    80.0, 86.0, 90.0, 92.0, 91.0,  # a rally (swing high at 92)
    89.0, 87.0, 85.0, 83.0, 81.0, 79.0, 77.0, 75.0, 73.0, 71.0,  # a slow drift lower
    74.0, 78.0,  # the turn
]  # fmt: skip
_END = date(2026, 7, 17)
_JULY_10 = date(2026, 7, 10)


def _scenario(closes: list[float]) -> pd.DataFrame:
    dates = pd.date_range(end=_END, periods=len(_CLOSES), freq="W-FRI")[: len(closes)]
    c = pd.Series(closes, dtype="float64")
    frame = pd.DataFrame(
        {
            "bar_date": dates,
            "open": c.shift(1).fillna(c.iloc[0]),
            "high": c + 0.5,
            "low": c - 0.5,
            "close": c,
            "volume": 1000.0,
            "security_id": SID,
            "continuity_segment_id": SEG,
        }
    )
    frame["high"] = frame[["open", "close"]].max(axis=1) + 0.5
    frame["low"] = frame[["open", "close"]].min(axis=1) - 0.5
    return frame


_CFG = AnalysisConfig(
    indicators=IndicatorConfig(atr_period=2),
    swings=SwingConfig(primary_method="FRACTAL", primary_sensitivity="MICRO"),
)


def test_objects_built_from_a_swing_known_on_july_10_are_not_known_before_it() -> None:
    chain = run_chain(_scenario(_CLOSES), _CFG)
    low = next(s for s in chain.swings.primary() if s.known_at == _JULY_10)
    assert low.type == "LOW" and low.bar_date == date(2026, 7, 3)

    fibs = [f for f in chain.fibonacci.structures if low.swing_id in f.depends_on]
    divergences = [d for d in chain.divergence.divergences if low.swing_id in d.depends_on]
    zones = [z for z in chain.levels.zones if low.swing_id in z.depends_on]
    assert fibs and divergences and zones  # all three are built from it...
    assert any(d.type == "REGULAR_BULLISH" and d.indicator == "rsi" for d in divergences)
    for obj in [*fibs, *divergences, *zones]:
        assert obj.known_at >= _JULY_10  # ...and none is knowable before it

    # As of the week before, the swing does not exist, so nothing built from it does.
    before = run_chain(_scenario(_CLOSES[:-2]), _CFG)
    assert before.swings.known_by(date(2026, 7, 3))
    assert low.swing_id not in {s.swing_id for s in before.swings.swings}
    rows = derived(before)
    assert not [r for r in rows if low.swing_id in r[2]]


# ----------------------------------------------------------------------- prefix stability


def _project(full: Chain, end: date) -> dict[str, object]:
    return {
        "fibonacci": [x for f in full.fibonacci.structures if (x := f.as_of(end))],
        "trendlines": [x for t in full.levels.trendlines if (x := t.as_of(end))],
        "levels": [x for lv in full.levels.levels if (x := lv.as_of(end))],
        "divergences": [x for d in full.divergence.divergences if (x := d.as_of(end))],
        "volume": [e for e in full.volume.events if e.known_at <= end],
        "volatility": [e for e in full.volatility.events if e.known_at <= end],
        "candles": [e for e in full.candles.events if e.known_at <= end],
    }


def _events(part: Chain) -> dict[str, object]:
    return {
        "fibonacci": part.fibonacci.structures,
        "trendlines": part.levels.trendlines,
        "levels": part.levels.levels,
        "divergences": part.divergence.divergences,
        "volume": part.volume.events,
        "volatility": part.volatility.events,
        "candles": part.candles.events,
    }


@settings(max_examples=15, deadline=None)
@given(
    periods=st.integers(min_value=40, max_value=500),
    seed=st.integers(min_value=0, max_value=10_000),
    cuts=st.lists(st.floats(min_value=0.05, max_value=1.0), min_size=1, max_size=3),
)
def test_prefix_stability(periods: int, seed: int, cuts: list[float]) -> None:
    bars = random_bars(periods, seed)
    full = run_chain(bars)
    for cut in cuts:
        k = max(1, round(cut * periods))
        part = run_chain(bars.iloc[:k].copy())
        end = pd.Timestamp(bars["bar_date"].iloc[k - 1]).date()
        projected, got = _project(full, end), _events(part)
        for name in got:
            assert got[name] == projected[name], name
        assert part.fibonacci.current() == full.fibonacci.current(end)


def test_later_bars_cannot_change_what_was_known() -> None:
    bars = random_bars(420, seed=8)
    cut = 260
    other = random_bars(420, seed=808)
    scale = bars["close"].iloc[cut] / other["close"].iloc[cut]
    altered = bars.copy()
    for col in ("open", "high", "low", "close"):
        altered.loc[cut + 1 :, col] = other[col].iloc[cut + 1 :] * scale
    altered.loc[cut + 1 :, "volume"] = other["volume"].iloc[cut + 1 :]
    end = pd.Timestamp(bars["bar_date"].iloc[cut]).date()
    a, b = _project(run_chain(bars), end), _project(run_chain(altered), end)
    assert a == b


@pytest.mark.parametrize("seed", [1, 2])
def test_the_forming_week_changes_nothing_in_layers_d_to_f(seed: int) -> None:
    bars = random_bars(400, seed)
    complete = run_chain(bars)
    forming_bar = bars.iloc[[-1]].copy()
    forming_bar["bar_date"] = forming_bar["bar_date"] + pd.Timedelta(weeks=1)
    forming_bar["close"] = forming_bar["close"] * 1.4  # a huge move, still forming
    forming_bar["high"] = forming_bar["close"] * 1.01
    forming_bar["volume"] = forming_bar["volume"] * 10
    with_forming = pd.concat([bars.assign(is_complete=True), forming_bar.assign(is_complete=False)])
    forming = run_chain(with_forming.reset_index(drop=True))
    skip = {"context"}
    for name in ("fibonacci", "levels", "divergence", "volume", "volatility", "candles"):
        a = getattr(complete, name).model_dump(exclude=skip)
        b = getattr(forming, name).model_dump(exclude=skip)
        assert a == b, name


def test_deterministic() -> None:
    bars = random_bars(500, seed=21)
    a, b = run_chain(bars), run_chain(bars)
    for name in ("fibonacci", "levels", "divergence", "volume", "volatility", "candles"):
        assert getattr(a, name).model_dump_json() == getattr(b, name).model_dump_json()


def test_special_session_closes_make_phase_4_decisions_provisional() -> None:
    bars = random_bars(500, seed=14)
    rng = np.random.default_rng(14)
    special = rng.random(len(bars)) < 0.15
    chain = run_chain(bars.assign(closes_on_special_session=special))
    on = {pd.Timestamp(d).date(): bool(s) for d, s in zip(bars["bar_date"], special, strict=True)}
    entries = [
        *[e for f in chain.fibonacci.structures for e in f.status_history],
        *[e for t in chain.levels.trendlines for e in t.status_history],
        *[r for lv in chain.levels.levels for r in lv.role_history[1:]],
        *[e for d in chain.divergence.divergences for e in d.status_history],
    ]
    assert entries
    assert all(e.provisional == on[e.date] for e in entries)
    events = [*chain.volume.events, *chain.volatility.events, *chain.candles.events]
    assert any(e.provisional for e in events)
    assert all(e.provisional == on[e.bar_date] for e in events)
