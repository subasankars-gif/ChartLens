"""Layer C — market structure (ADR-0020 §C): labels, BOS/CHoCH and trend states by hand,
built only from the primary confirmed swings, and causal like every layer."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from chartlens_core.config import IndicatorConfig, StructureConfig, SwingConfig
from chartlens_core.domain import Timeframe
from chartlens_core.testing import make_bars
from chartlens_engine.indicators import IndicatorAnalyzer
from chartlens_engine.interfaces import AnalysisContext, run_analyzer
from chartlens_engine.structure import StructureAnalyzer, StructureResult
from chartlens_engine.structure.analyzer import _transition
from chartlens_engine.swings import SwingAnalyzer, SwingPoint, SwingResult

SID = "SEC-ST"
SEG = f"{SID}@2010-01-08"
# Hand fixtures read the primary swings as FRACTAL / MICRO with ATR(2), so every pivot
# and ATR value can be worked out on paper. Production defaults are exercised below.
HAND_IND = IndicatorConfig(atr_period=2)
HAND_SWINGS = SwingConfig(primary_method="FRACTAL", primary_sensitivity="MICRO")
HAND_STRUCT = StructureConfig(range_width_atr=1.0)

# An advance in steps, a lower swing, then a decline: highs = close + 0.5, lows = close − 0.5
HAND_CLOSES = [10, 12, 11, 14, 13, 16, 15, 18, 17, 15, 16, 13, 14, 11]


def bars_from_closes(closes: list[float], **extra: object) -> pd.DataFrame:
    c = [float(x) for x in closes]
    frame = pd.DataFrame(
        {
            "bar_date": pd.date_range(date(2010, 1, 8), periods=len(c), freq="W-FRI"),
            "open": c,
            "high": [x + 0.5 for x in c],
            "low": [x - 0.5 for x in c],
            "close": c,
            "volume": [1000.0] * len(c),
            "security_id": SID,
            "continuity_segment_id": SEG,
        }
    )
    return frame.assign(**extra) if extra else frame


def context(bars: pd.DataFrame) -> AnalysisContext:
    return AnalysisContext(
        security_id=SID,
        timeframe=Timeframe.WEEKLY,
        as_of=pd.Timestamp(bars["bar_date"].iloc[-1]).date(),
        methodology_hash="test",
        continuity_segment_id=SEG,
    )


def analyze(
    bars: pd.DataFrame,
    ind: IndicatorConfig = HAND_IND,
    sw: SwingConfig = HAND_SWINGS,
    st_cfg: StructureConfig = HAND_STRUCT,
    swings: SwingResult | None = None,
) -> StructureResult:
    ctx = context(bars)
    indicators = run_analyzer(IndicatorAnalyzer(ind), bars, ctx)
    swing_result = swings or run_analyzer(SwingAnalyzer(sw, indicators), bars, ctx)
    return run_analyzer(StructureAnalyzer(st_cfg, indicators, swing_result), bars, ctx)


def week(i: int) -> date:
    return (pd.Timestamp(date(2010, 1, 8)) + pd.Timedelta(weeks=i)).date()


# ----------------------------------------------------------------------------- by hand


def test_labels_by_hand() -> None:
    result = analyze(bars_from_closes(HAND_CLOSES))
    got = [(lb.type, lb.bar_date, lb.label, lb.known_at) for lb in result.labels]
    # In the order they became known; the first high (week 1) and low (week 2) have no
    # predecessor to compare with.
    assert got == [
        ("HIGH", week(3), "HH", week(4)),  # 14.5 vs 12.5
        ("LOW", week(4), "HL", week(5)),  # 12.5 vs 10.5
        ("HIGH", week(5), "HH", week(6)),
        ("LOW", week(6), "HL", week(7)),
        ("HIGH", week(7), "HH", week(8)),
        ("LOW", week(9), "EQL", week(10)),  # 14.5 vs 14.5: equal, not higher
        ("HIGH", week(10), "LH", week(11)),
        ("LOW", week(11), "LL", week(12)),
        ("HIGH", week(12), "LH", week(13)),
    ]


def test_bos_and_choch_by_hand() -> None:
    result = analyze(bars_from_closes(HAND_CLOSES))
    got = [(e.kind, e.direction, e.bar_date, e.level) for e in result.events]
    assert got == [
        ("BOS", "UP", week(3), 12.5),  # first break: close 14 above the week-1 high
        ("BOS", "UP", week(5), 14.5),
        ("BOS", "UP", week(7), 16.5),
        ("CHoCH", "DOWN", week(11), 14.5),  # close 13 below the week-9 low: against the trend
        ("BOS", "DOWN", week(13), 12.5),  # close 11 below the week-11 low: confirms it
    ]
    choch = result.events[3]
    assert choch.known_at == choch.bar_date
    assert (choch.prior_regime, choch.prior_state) == ("UP", "WEAKENING_UPTREND")
    assert choch.level_swing_id.endswith(f"LOW:{week(9)}")
    # The week-10 high only becomes known with week 11 itself, after the break is judged:
    # the invalidation is the swing high known before the break, week 7's 18.5.
    assert choch.invalidation.level == 18.5
    assert choch.confirmation.threshold is not None and choch.confirmation.threshold < 14.5
    assert all(e.continuity_segment_id == SEG and not e.provisional for e in result.events)


def test_trend_states_by_hand() -> None:
    result = analyze(bars_from_closes(HAND_CLOSES))
    got = [(s.state, s.since) for s in result.trend_history]
    states = [g for i, g in enumerate(got) if i == 0 or got[i - 1][0] != g[0]]
    assert states == [
        ("RANGE", week(0)),  # no regime before the first break
        ("WEAKENING_UPTREND", week(3)),  # regime up, no HH/HL pair yet
        ("STRONG_UPTREND", week(5)),  # latest high HH and latest low HL
        ("WEAKENING_UPTREND", week(10)),  # the latest low is only equal
        ("TRANSITION", week(11)),  # CHoCH down, unconfirmed
        ("STRONG_DOWNTREND", week(13)),  # BOS down: LH and LL
    ]
    assert result.trend is not None and result.trend.state == "STRONG_DOWNTREND"
    assert result.state_as_of(week(12)).state == "TRANSITION"  # type: ignore[union-attr]


def test_the_regime_table() -> None:
    assert _transition(None, None, "UP") == ("BOS", "UP", None)
    assert _transition("UP", None, "UP") == ("BOS", "UP", None)
    assert _transition("UP", None, "DOWN") == ("CHoCH", "UP", "DOWN")
    assert _transition("UP", "DOWN", "DOWN") == ("BOS", "DOWN", None)  # confirms the CHoCH
    assert _transition("UP", "DOWN", "UP") == ("BOS", "UP", None)  # the transition failed


# ----------------------------------------------------------------------------- rules


def manual_swings(bars: pd.DataFrame, swings: list[tuple[str, int, int, float]]) -> SwingResult:
    dates = [pd.Timestamp(d).date() for d in bars["bar_date"]]
    points = [
        SwingPoint(
            swing_id=f"{SEG}:FRACTAL:MICRO:{kind}:{dates[bar]}",
            security_id=SID,
            timeframe="WEEKLY",
            continuity_segment_id=SEG,
            method="FRACTAL",
            sensitivity="MICRO",
            type=kind,  # type: ignore[arg-type]
            confirmed=True,
            bar_date=dates[bar],
            known_at=dates[known],
            bar_index=bar,
            price=price,
            bars_from_previous=None,
            price_change=None,
            atr_change=None,
            strength=None,
        )
        for kind, bar, known, price in swings
    ]
    return SwingResult(
        analyzer="swings",
        analyzer_version="1",
        context=context(bars),
        primary_method="FRACTAL",
        primary_sensitivity="MICRO",
        swings=points,
        pending=[],
    )


def test_a_level_cannot_be_broken_by_the_bar_that_makes_it_known() -> None:
    bars = bars_from_closes([10, 10, 10, 11, 10, 20, 20])
    swings = manual_swings(bars, [("HIGH", 3, 5, 11.5)])  # known only at week 5
    result = analyze(bars, swings=swings)
    assert [e.bar_date for e in result.events] == [week(6)]  # not week 5


def test_only_the_primary_swings_build_structure() -> None:
    bars = make_bars(date(2010, 1, 4), 300, freq="W-FRI", seed=6).assign(
        security_id=SID, continuity_segment_id=SEG
    )
    ctx = context(bars)
    indicators = run_analyzer(IndicatorAnalyzer(IndicatorConfig()), bars, ctx)
    swings = run_analyzer(SwingAnalyzer(SwingConfig(), indicators), bars, ctx)
    only_primary = swings.model_copy(update={"swings": swings.primary(), "pending": []})
    full = run_analyzer(StructureAnalyzer(StructureConfig(), indicators, swings), bars, ctx)
    trimmed = run_analyzer(
        StructureAnalyzer(StructureConfig(), indicators, only_primary), bars, ctx
    )
    assert full.events == trimmed.events and full.labels == trimmed.labels
    assert (full.swing_method, full.swing_sensitivity) == ("ATR", "INTERMEDIATE")
    other = run_analyzer(
        StructureAnalyzer(
            StructureConfig(),
            indicators,
            run_analyzer(
                SwingAnalyzer(
                    SwingConfig(primary_method="FRACTAL", primary_sensitivity="MAJOR"),
                    indicators,
                ),
                bars,
                ctx,
            ),
        ),
        bars,
        ctx,
    )
    assert other.swing_method == "FRACTAL" and other.labels != full.labels


def test_the_forming_week_cannot_break_structure() -> None:
    closes = [*HAND_CLOSES[:11], 13.0]  # week 11 would be the CHoCH...
    complete = bars_from_closes(closes, is_complete=[True] * 11 + [False])
    forming = analyze(complete)
    assert all(e.bar_date != week(11) for e in forming.events)  # ...but it is still forming
    done = analyze(bars_from_closes(closes, is_complete=[True] * 12))
    choch = next(e for e in done.events if e.bar_date == week(11))
    assert choch.known_at == week(11)  # known when the week completes, never earlier


def test_a_special_session_close_makes_the_break_provisional_until_a_regular_week() -> None:
    special = [False] * len(HAND_CLOSES)
    special[11] = True  # the CHoCH week closes on a special session
    result = analyze(bars_from_closes(HAND_CLOSES, closes_on_special_session=special))
    choch = next(e for e in result.events if e.kind == "CHoCH")
    assert choch.provisional
    at_choch = result.state_as_of(week(11))
    after = result.state_as_of(week(12))
    assert at_choch is not None and at_choch.provisional
    assert after is not None and not after.provisional  # a regular week held it


def test_range_overrides_direction() -> None:
    wide = analyze(bars_from_closes(HAND_CLOSES), st_cfg=StructureConfig(range_width_atr=50))
    assert wide.trend is not None and wide.trend.state == "RANGE"
    assert wide.events  # breaks are still recorded; only the state reads RANGE


# ----------------------------------------------------------------------------- causality


def random_bars(periods: int, seed: int) -> pd.DataFrame:
    return make_bars(date(2010, 1, 4), periods, freq="W-FRI", seed=seed).assign(
        security_id=SID, continuity_segment_id=SEG
    )


def defaults(bars: pd.DataFrame) -> StructureResult:
    return analyze(bars, IndicatorConfig(), SwingConfig(), StructureConfig())


@settings(max_examples=20, deadline=None)
@given(
    periods=st.integers(min_value=30, max_value=400),
    seed=st.integers(min_value=0, max_value=10_000),
    cuts=st.lists(st.floats(min_value=0.05, max_value=1.0), min_size=1, max_size=4),
)
def test_prefix_stability(periods: int, seed: int, cuts: list[float]) -> None:
    bars = random_bars(periods, seed)
    full = defaults(bars)
    for cut in cuts:
        k = max(1, round(cut * periods))
        part = defaults(bars.iloc[:k].copy())
        end = pd.Timestamp(bars["bar_date"].iloc[k - 1]).date()
        assert part.events == [e for e in full.events if e.known_at <= end]
        assert part.labels == [lb for lb in full.labels if lb.known_at <= end]
        assert part.trend_history == [s for s in full.trend_history if s.since <= end]
        assert part.trend == full.state_as_of(end)


def test_later_bars_cannot_change_known_structure() -> None:
    bars = random_bars(320, seed=44)
    cut = 200
    other = random_bars(320, seed=4242)
    scale = bars["close"].iloc[cut] / other["close"].iloc[cut]
    altered = bars.copy()
    for col in ("open", "high", "low", "close"):
        altered.loc[cut + 1 :, col] = other[col].iloc[cut + 1 :] * scale
    end = pd.Timestamp(bars["bar_date"].iloc[cut]).date()
    a, b = defaults(bars), defaults(altered)
    assert [e for e in a.events if e.known_at <= end] == [e for e in b.events if e.known_at <= end]
    assert a.state_as_of(end) == b.state_as_of(end)


def test_deterministic() -> None:
    bars = random_bars(400, seed=12)
    assert defaults(bars).model_dump_json() == defaults(bars).model_dump_json()


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_every_event_is_a_complete_bar_known_on_its_own_date(seed: int) -> None:
    result = defaults(random_bars(500, seed))
    assert all(e.known_at == e.bar_date for e in result.events)
    assert all(lb.bar_date <= lb.known_at for lb in result.labels)
