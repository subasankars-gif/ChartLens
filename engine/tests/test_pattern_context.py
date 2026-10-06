"""Layer H, Phase 5b-B — pattern context (ADR-0022 §12).

Context is a snapshot of the facts available at the pattern's ``known_at``. A run as of
``known_at`` must produce exactly the context the full run shows, later bars can never
change it, and every object it cites must itself have been known by then: an object built
from old bars but only knowable later never enters the context (object-level contract)."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest
from analysis_chain import (
    SEG,
    SID,
    Chain,
    known_at_of,
    manual_swings,
    random_bars,
    run_chain,
    week,
)

from chartlens_core.config import AnalysisConfig, IndicatorConfig
from chartlens_engine.patterns import Pattern

HAND = AnalysisConfig(indicators=IndicatorConfig(atr_period=2))
Row = tuple[float, float, float, float]


def rows_from_closes(closes: list[float]) -> list[Row]:
    out: list[Row] = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i else c
        out.append((o, max(o, c) + 0.5, min(o, c) - 0.5, c))
    return out


def frame(rows: list[Row]) -> pd.DataFrame:
    o, h, lo, c = (list(x) for x in zip(*rows, strict=True))
    return pd.DataFrame(
        {
            "bar_date": pd.date_range(date(2010, 1, 8), periods=len(rows), freq="W-FRI"),
            "open": o,
            "high": h,
            "low": lo,
            "close": c,
            "volume": 1000.0,
            "security_id": SID,
            "continuity_segment_id": SEG,
        }
    ).astype({"open": "float64", "high": "float64", "low": "float64", "close": "float64"})


# A slow decline into a double bottom (lows at weeks 30 and 40, neckline week 35); an
# older swing low at 45.1 (week 12) is a support level near both lows.
DECLINE = [60.0 - 0.5 * i for i in range(30)]  # weeks 0–29: 60 → 45.5
BOTTOM = [46.0, 47.0, 48.0, 48.5, 48.0, 47.5, 47.0, 46.5, 46.0, 45.8, 46.0]  # weeks 30–40
SWINGS = [
    ("LOW", 12, 13, 45.1),
    ("LOW", 30, 31, 45.0),
    ("HIGH", 35, 36, 49.0),
    ("LOW", 40, 41, 45.3),
]


def _double(
    tail: list[float], swings: list[tuple[str, int, int, float]] = SWINGS
) -> tuple[Pattern, pd.DataFrame]:
    bars = frame(rows_from_closes(DECLINE + BOTTOM + tail))
    chain = run_chain(bars, HAND, manual_swings(bars, swings))
    (p,) = [p for p in chain.patterns.patterns if p.pattern_type == "DOUBLE_BOTTOM"]
    return p, bars


def test_context_facts_by_hand() -> None:
    p, _ = _double([47.0, 47.5, 48.0])
    ctx = p.context
    assert ctx is not None and ctx.as_of == p.known_at == week(41)
    prior = ctx.prior_move
    assert (prior.bars, prior.window_start, prior.window_end) == (26, week(4), week(29))
    assert (prior.highest_close, prior.highest_close_date) == (58.0, week(4))
    # the decline into the first low (45.0), in ATR at its bar
    assert prior.decline_into_atr is not None and prior.decline_into_atr > 5
    # The old swing low at 45.1 is support near both lows; the pattern's own swings are
    # never counted as levels near themselves.
    near = [(lv.key_point, lv.role, round(lv.price, 2)) for lv in ctx.levels_near]
    assert ("LOW_1", "SUPPORT", 45.1) in near and ("LOW_2", "SUPPORT", 45.1) in near
    own = set(p.depends_on)
    assert not [lv for lv in ctx.levels_near if lv.level_id.split(":LEVEL")[0] in own]
    assert ctx.divergence.presence == "ABSENT" and ctx.divergence.divergences == []
    # the trend state is the structure layer's as of known_at, never relabelled
    assert ctx.structure.since is None or ctx.structure.since <= p.known_at


def test_an_object_from_old_bars_discovered_later_never_enters_the_context() -> None:
    """A swing low at week 22 (45.4, near both lows) that is only confirmed at week 44,
    after the pattern is known at week 41. Its level's bars precede ``known_at`` but the
    level itself was not knowable then, so the week-41 context must not cite it, even
    though the full run has it."""
    late = (*SWINGS, ("LOW", 22, 44, 45.4))
    tail = [47.0, 47.5, 48.0, 48.2, 48.4, 48.6]
    p, bars = _double(tail, sorted(late, key=lambda s: s[1]))
    chain = run_chain(bars, HAND, manual_swings(bars, sorted(late, key=lambda s: s[1])))
    level = next(lv for lv in chain.levels.levels if lv.price == 45.4)
    assert level.bar_date < p.known_at < level.known_at == week(44)
    # had it been known, it would qualify: within the tolerance of LOW_2 (45.3)
    assert abs(level.price - 45.3) <= 0.5 * p.geometry.atr_d
    ctx = p.context
    assert ctx is not None
    assert level.level_id not in ctx.evidence_refs
    assert all(lv.level_id != level.level_id for lv in ctx.levels_near)
    # ...and the context is exactly the one without the late swing at all
    q, _ = _double(tail)
    assert ctx == q.context


def test_a_level_that_breaks_after_recognition_stays_support_in_the_context() -> None:
    """The level at 45.1 becomes resistance when price closes below it after week 41.
    The context is the week-41 snapshot: it still says SUPPORT."""
    p, bars = _double([47.0, 44.0, 43.0, 42.5])
    level = next(lv for lv in p.context.levels_near if round(lv.price, 2) == 45.1)  # type: ignore[union-attr]
    assert level.role == "SUPPORT"
    # ...while the full history shows the level broken afterwards.
    chain = run_chain(bars, HAND, manual_swings(bars, SWINGS))
    now = next(lv for lv in chain.levels.levels if lv.level_id == level.level_id)
    assert now.role == "RESISTANCE" and now.role_history[-1].date > p.known_at


def test_context_is_identical_when_run_as_of_known_at() -> None:
    p, bars = _double([47.0, 44.0, 43.0, 42.5])
    k = 41
    part = bars.iloc[: k + 1].copy()
    chain = run_chain(part, HAND, manual_swings(part, SWINGS))
    (q,) = [q for q in chain.patterns.patterns if q.pattern_type == "DOUBLE_BOTTOM"]
    assert q.context == p.context


@pytest.mark.parametrize("seed", [3, 9])
def test_replay_at_known_at_reproduces_every_context(seed: int) -> None:
    bars = random_bars(600, seed)
    full = run_chain(bars).patterns.patterns
    assert full and all(p.context is not None for p in full)
    dates = [pd.Timestamp(d).date() for d in bars["bar_date"]]
    for p in full[:: max(1, len(full) // 10)]:
        k = dates.index(p.known_at)
        part = {q.pattern_id: q for q in run_chain(bars.iloc[: k + 1].copy()).patterns.patterns}
        assert part[p.pattern_id].context == p.context


def test_later_bars_cannot_change_a_context() -> None:
    bars = random_bars(500, seed=21)
    cut = 330
    other = random_bars(500, seed=2121)
    scale = bars["close"].iloc[cut] / other["close"].iloc[cut]
    altered = bars.copy()
    for col in ("open", "high", "low", "close"):
        altered.loc[cut + 1 :, col] = other[col].iloc[cut + 1 :] * scale
    altered.loc[cut + 1 :, "volume"] = other["volume"].iloc[cut + 1 :]
    end = pd.Timestamp(bars["bar_date"].iloc[cut]).date()

    def contexts(frame_: pd.DataFrame) -> dict[str, object]:
        return {
            p.pattern_id: p.context
            for p in run_chain(frame_).patterns.patterns
            if p.known_at <= end
        }

    a, b = contexts(bars), contexts(altered)
    assert a and a == b


@pytest.mark.parametrize("seed", [4, 17])
def test_context_cites_only_what_was_known(seed: int) -> None:
    chain = run_chain(random_bars(700, seed))
    known = known_at_of(chain)
    known |= {lv.level_id: lv.known_at for lv in chain.levels.levels}
    cited = 0
    for p in chain.patterns.patterns:
        ctx = p.context
        assert ctx is not None and ctx.as_of == p.known_at
        for ref in ctx.evidence_refs:
            assert ref in known, ref
            assert known[ref] <= p.known_at, (p.pattern_id, ref)
            cited += 1
        for f in ctx.fibonacci:
            assert f.fib_known_at <= p.known_at
        prior = ctx.prior_move
        if prior.window_end is not None:
            assert prior.window_end < p.start_date
        if ctx.structure.since is not None:
            assert ctx.structure.since <= p.known_at
    assert cited > 0


def _observed(chain: Chain) -> list[tuple[str, date, date]]:
    """Every citable object: (id, the last bar it was built from, its own known_at)."""
    rows: list[tuple[str, date, date]] = []
    rows += [(lv.level_id, lv.bar_date, lv.known_at) for lv in chain.levels.levels]
    rows += [(d.divergence_id, d.date_end, d.known_at) for d in chain.divergence.divergences]
    rows += [(f.fib_id, f.counter_bar_date, f.known_at) for f in chain.fibonacci.structures]
    rows += [(e.event_id, e.bar_date, e.known_at) for e in chain.structure.events]
    rows += [(e.event_id, e.bar_date, e.known_at) for e in chain.volatility.events]
    return rows


@pytest.mark.parametrize("seed", [4, 17])
def test_objects_built_from_old_bars_but_known_later_are_never_cited(seed: int) -> None:
    """The object-level contract on random series, and proof it is not vacuous: objects
    whose bars all precede a pattern's ``known_at`` but which became known after it do
    occur, and none of them is ever cited."""
    chain = run_chain(random_bars(700, seed))
    objects = _observed(chain)
    late_seen = 0
    for p in chain.patterns.patterns:
        assert p.context is not None
        cited = set(p.context.evidence_refs)
        late = {i for i, last_bar, known in objects if last_bar <= p.known_at < known}
        late_seen += len(late)
        assert not cited & late, (p.pattern_id, sorted(cited & late))
    assert late_seen > 0


@pytest.mark.parametrize("seed", [4, 17, 33])
def test_divergence_presence_absence_and_not_applicable(seed: int) -> None:
    chain = run_chain(random_bars(900, seed))
    seen: set[str] = set()
    for p in chain.patterns.patterns:
        div = p.context.divergence  # type: ignore[union-attr]
        seen.add(div.presence)
        if p.direction == "NEUTRAL":
            assert (div.presence, div.not_applicable_reason) == (
                "NOT_APPLICABLE",
                "NEUTRAL_DIRECTION",
            )
        elif p.family in ("flag", "pennant"):
            assert (div.presence, div.not_applicable_reason) == (
                "NOT_APPLICABLE",
                "FINE_SWING_GEOMETRY",
            )
        if div.presence == "NOT_APPLICABLE":
            assert div.divergences == []
        else:
            assert div.not_applicable_reason is None
            active = [d for d in div.divergences if d.status in ("FORMING", "CONFIRMED")]
            assert (div.presence == "PRESENT") == bool(active)
    assert seen == {"PRESENT", "ABSENT", "NOT_APPLICABLE"}
