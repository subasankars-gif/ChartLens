"""5b-A amendment — the breakout bar's frozen volume evidence (ADR-0022 §3, §18.4).

The breakout event records the breakout bar's RVOL, its inputs and the indicator
layer's classification, from data through that bar only. It is evidence, never a
condition: no lifecycle decision reads it, and relevance consumes the recorded value
rather than reconstructing it."""

from __future__ import annotations

import ast
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from analysis_chain import context, random_bars, run_chain

from chartlens_core.config import AnalysisConfig
from chartlens_engine.causal import complete_bars, numeric
from chartlens_engine.indicators import IndicatorAnalyzer
from chartlens_engine.interfaces import run_analyzer
from chartlens_engine.patterns import RelevanceAnalyzer, relevance
from chartlens_engine.patterns.lifecycle import Lifecycle, VolumeSource
from chartlens_engine.patterns.model import BROKEN_OUT

SEEDS = [3, 9, 21]


@pytest.mark.parametrize("seed", SEEDS)
def test_every_breakout_event_and_only_it_carries_the_breakout_bar_volume(seed: int) -> None:
    bars = random_bars(700, seed)
    chain = run_chain(bars)
    rvol = chain.indicators.get("relative_volume")
    state = chain.indicators.get("volume_state")
    dates = [pd.Timestamp(d).date() for d in bars["bar_date"]]
    seen = 0
    for p in chain.patterns.patterns:
        for e in p.status_history:
            v = e.breakout_bar_volume
            if e.status not in BROKEN_OUT:
                assert v is None
                continue
            assert v is not None and v.bar_date == e.effective_date
            t = dates.index(v.bar_date)
            assert v.volume == float(bars["volume"].iloc[t])
            assert v.rvol == rvol.data[t] and v.classification == state.data[t]
            assert v.baseline_bars == state.params["baseline"] == rvol.params["baseline"]
            assert (v.expansion_threshold, v.contraction_threshold) == (
                state.params["expansion"],
                state.params["contraction"],
            )
            assert v.baseline_mean_volume == pytest.approx(
                float(bars["volume"].iloc[t - v.baseline_bars : t].mean())
            )
            assert v.rvol == pytest.approx(v.volume / v.baseline_mean_volume)
            assert f"indicators:relative_volume@{v.bar_date}" in v.evidence_refs
            seen += 1
    assert seen > 0


def test_later_bars_cannot_change_breakout_evidence() -> None:
    """Replace everything after a cut, volumes included: every breakout on or before the
    cut keeps exactly the same event, volume evidence included."""
    bars = random_bars(600, seed=21)
    cut = 400
    other = random_bars(600, seed=2121)
    scale = bars["close"].iloc[cut] / other["close"].iloc[cut]
    altered = bars.copy()
    for col in ("open", "high", "low", "close"):
        altered.loc[cut + 1 :, col] = other[col].iloc[cut + 1 :] * scale
    altered.loc[cut + 1 :, "volume"] = other["volume"].iloc[cut + 1 :] * 50
    end = pd.Timestamp(bars["bar_date"].iloc[cut]).date()

    def events(frame: pd.DataFrame) -> dict[str, object]:
        return {
            p.pattern_id: e
            for p in run_chain(frame).patterns.patterns
            for e in p.status_history
            if e.status in BROKEN_OUT and e.effective_date <= end
        }

    a, b = events(bars), events(altered)
    assert a and a == b


@pytest.mark.parametrize("seed", [3, 9])
def test_replay_at_the_breakout_bar_reproduces_the_evidence(seed: int) -> None:
    bars = random_bars(600, seed)
    full = run_chain(bars).patterns.patterns
    dates = [pd.Timestamp(d).date() for d in bars["bar_date"]]
    checked = 0
    for p in full[:: max(1, len(full) // 10)]:
        e = next((e for e in p.status_history if e.status in BROKEN_OUT), None)
        if e is None:
            continue
        t = dates.index(e.effective_date)
        part = {q.pattern_id: q for q in run_chain(bars.iloc[: t + 1].copy()).patterns.patterns}
        q = part[p.pattern_id]
        replayed = next(x for x in q.status_history if x.status in BROKEN_OUT)
        assert replayed == e  # the whole event, volume evidence included
        checked += 1
    assert checked > 0


def test_the_evidence_is_never_a_lifecycle_condition(monkeypatch: pytest.MonkeyPatch) -> None:
    """With nonsense volume evidence, every status, date, reason, measured value, measured
    move and definition fit is unchanged: no lifecycle decision reads it."""
    bars = random_bars(700, seed=9)
    real = run_chain(bars).patterns.patterns
    original = Lifecycle.breakout_bar_volume

    def nonsense(self: Lifecycle, t: int) -> object:
        v = original(self, t)
        return v.model_copy(update={"rvol": 99.0, "classification": "CONTRACTION"})

    monkeypatch.setattr(Lifecycle, "breakout_bar_volume", nonsense)
    fake = run_chain(bars).patterns.patterns
    assert len(real) == len(fake)
    for p, q in zip(real, fake, strict=True):
        assert p.pattern_id == q.pattern_id and p.geometry == q.geometry
        assert p.definition_fit == q.definition_fit and p.context == q.context
        strip = {"breakout_bar_volume"}
        assert [e.model_dump(exclude=strip) for e in p.status_history] == [
            e.model_dump(exclude=strip) for e in q.status_history
        ]


def test_relevance_reads_the_recorded_evidence_not_the_indicators() -> None:
    """Flip every breakout's recorded classification to EXPANSION: the BREAKOUT_VOLUME
    tag follows the record, whatever the indicators say."""
    bars = random_bars(700, seed=9)
    chain = run_chain(bars)
    flipped = chain.patterns.model_copy(
        update={
            "patterns": [
                p.model_copy(
                    update={
                        "status_history": [
                            e
                            if e.breakout_bar_volume is None
                            else e.model_copy(
                                update={
                                    "breakout_bar_volume": e.breakout_bar_volume.model_copy(
                                        update={"classification": "EXPANSION"}
                                    )
                                }
                            )
                            for e in p.status_history
                        ]
                    }
                )
                for p in chain.patterns.patterns
            ]
        }
    )
    ctx = context(bars)
    rel = run_analyzer(
        RelevanceAnalyzer(AnalysisConfig().patterns, chain.indicators, flipped, chain.structure),
        bars,
        ctx,
    )
    broke = {
        p.pattern_id
        for p in chain.patterns.patterns
        if any(e.status in BROKEN_OUT for e in p.status_history)
    }
    tagged = {
        r.pattern_id
        for r in rel.relevance
        if any(t.tag == "BREAKOUT_VOLUME" for e in r.history for t in e.tags)
    }
    assert broke and broke <= tagged
    source = Path(relevance.__file__).read_text()
    names = {n.attr for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Attribute)}
    assert "volume_state" not in source and "relative_volume" not in source
    assert "breakout_bar_volume" in names


def test_warm_up_breakouts_record_missing_rvol_as_none() -> None:
    bars = random_bars(80, seed=4)
    ctx = context(bars)
    ind = run_analyzer(IndicatorAnalyzer(AnalysisConfig().indicators), bars, ctx)
    cb = complete_bars(bars, ctx, ind)
    vs = ind.get("volume_state")
    source = VolumeSource(
        rvol=numeric(ind, "relative_volume", cb.n),
        state=list(vs.data[: cb.n]),
        baseline=int(vs.params["baseline"]),
        expansion=float(vs.params["expansion"]),
        contraction=float(vs.params["contraction"]),
    )
    life = Lifecycle(AnalysisConfig().patterns, cb, np.full(cb.n, np.nan), "test", source)
    early = life.breakout_bar_volume(source.baseline - 1)
    assert (early.rvol, early.baseline_mean_volume, early.classification) == (None, None, None)
    ready = life.breakout_bar_volume(source.baseline + 5)
    assert ready.rvol is not None and ready.classification is not None
