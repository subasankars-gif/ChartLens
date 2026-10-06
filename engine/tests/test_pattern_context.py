"""Layer H, Phase 5b-B — pattern context (ADR-0022 §12).

Context is a snapshot of the facts available at the pattern's ``known_at``. A run as of
``known_at`` must produce exactly the context the full run shows, later bars can never
change it, and every object it cites must already have been known."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest
from analysis_chain import SEG, SID, known_at_of, manual_swings, random_bars, run_chain, week

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


def _double(tail: list[float]) -> tuple[Pattern, pd.DataFrame]:
    bars = frame(rows_from_closes(DECLINE + BOTTOM + tail))
    chain = run_chain(bars, HAND, manual_swings(bars, SWINGS))
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
    assert ctx.divergence_ids == ()


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
        prior = ctx.prior_move
        if prior.window_end is not None:
            assert prior.window_end < p.start_date
        if ctx.structure.since is not None:
            assert ctx.structure.since <= p.known_at
        if p.direction == "NEUTRAL":
            assert ctx.divergence_ids == ()
    assert cited > 0
