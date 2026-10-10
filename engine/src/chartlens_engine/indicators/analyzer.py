"""Layer A: weekly indicators as one analyzer (ADR-0019, ADR-0020 §A).

Runs every configured indicator once over the bars of one continuity segment and returns
them as series aligned to the bar dates. Values in warm-up are ``None``; the last bar's
values are ``provisional`` when that bar is the forming week. Nothing here confirms
anything — later layers read these series.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict

from chartlens_core.bars import BAR_DATE, IS_COMPLETE, BarFrameError, column
from chartlens_core.config import IndicatorConfig
from chartlens_engine.indicators import functions as f
from chartlens_engine.interfaces import AnalysisContext, AnalyzerResult

Value = float | str | None


class IndicatorSeries(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    """Unique within a result, e.g. ``sma_20``, ``macd_signal``, ``volume_state``."""
    family: str
    """``trend``, ``momentum``, ``volatility`` or ``volume``."""
    kind: Literal["numeric", "state"]
    params: dict[str, float | int]
    warmup_bars: int
    """Index of the first non-null value (= bars needed before one exists)."""
    aliases: tuple[str, ...] = ()
    """Other names for this same series (``20W`` for ``sma_20``): never a second calculation."""
    data: list[Value]
    """One per bar, aligned to :attr:`IndicatorResult.bar_dates`; ``None`` in warm-up."""


class IndicatorResult(AnalyzerResult):
    bar_dates: list[date]
    provisional: list[bool]
    """True for a bar that is still forming (``is_complete`` false): its values may change."""
    series: list[IndicatorSeries]

    def get(self, name: str) -> IndicatorSeries:
        for s in self.series:
            if s.name == name or name in s.aliases:
                return s
        raise KeyError(name)


def _numeric(values: f.Array) -> tuple[list[Value], int]:
    out: list[Value] = [None if np.isnan(v) else float(v) for v in values.tolist()]
    first = next((i for i, v in enumerate(out) if v is not None), len(out))
    return out, first


def _state(values: list[str | None]) -> tuple[list[Value], int]:
    first = next((i for i, v in enumerate(values) if v is not None), len(values))
    return list(values), first


class IndicatorAnalyzer:
    """Every weekly indicator, computed once per segment (ADR-0020 §A)."""

    name = "indicators"
    version = "1"

    def __init__(self, config: IndicatorConfig) -> None:
        self.config = config

    def analyze(self, bars: pd.DataFrame, context: AnalysisContext) -> IndicatorResult:
        prices = {c: column(bars, c).to_numpy(dtype=np.float64) for c in ("high", "low", "close")}
        volume = column(bars, "volume").to_numpy(dtype=np.float64)
        for name, arr in (*prices.items(), ("volume", volume)):
            if np.isnan(arr).any():
                raise BarFrameError(f"{name} contains NaN; the engine never guesses a price")
        high, low, close = prices["high"], prices["low"], prices["close"]
        cfg = self.config
        series: list[IndicatorSeries] = []

        def add(
            name: str,
            family: str,
            params: dict[str, float | int],
            computed: tuple[list[Value], int],
            *,
            kind: Literal["numeric", "state"] = "numeric",
            aliases: tuple[str, ...] = (),
        ) -> None:
            values, first = computed
            series.append(
                IndicatorSeries(
                    name=name,
                    family=family,
                    kind=kind,
                    params=params,
                    warmup_bars=first,
                    aliases=aliases,
                    data=values,
                )
            )

        # Trend
        for n in cfg.sma_periods:
            add(f"sma_{n}", "trend", {"period": n}, _numeric(f.sma(close, n)), aliases=(f"{n}W",))
        for n in cfg.ema_periods:
            add(f"ema_{n}", "trend", {"period": n}, _numeric(f.ema(close, n)))

        # Momentum
        add("rsi", "momentum", {"period": cfg.rsi_period}, _numeric(f.rsi(close, cfg.rsi_period)))
        macd_params: dict[str, float | int] = {
            "fast": cfg.macd_fast,
            "slow": cfg.macd_slow,
            "signal": cfg.macd_signal,
        }
        line, signal, hist = f.macd(close, cfg.macd_fast, cfg.macd_slow, cfg.macd_signal)
        add("macd", "momentum", macd_params, _numeric(line))
        add("macd_signal", "momentum", macd_params, _numeric(signal))
        add("macd_histogram", "momentum", macd_params, _numeric(hist))
        stoch_params: dict[str, float | int] = {
            "k": cfg.stochastic_k,
            "k_smoothing": cfg.stochastic_k_smoothing,
            "d": cfg.stochastic_d,
        }
        _, k_slow, d_line = f.stochastic(
            high, low, close, cfg.stochastic_k, cfg.stochastic_k_smoothing, cfg.stochastic_d
        )
        add("stochastic_k", "momentum", stoch_params, _numeric(k_slow))
        add("stochastic_d", "momentum", stoch_params, _numeric(d_line))
        add("roc", "momentum", {"period": cfg.roc_period}, _numeric(f.roc(close, cfg.roc_period)))

        # Volatility
        atr = f.atr(high, low, close, cfg.atr_period)
        add("atr", "volatility", {"period": cfg.atr_period}, _numeric(atr))
        add(
            "atr_percent",
            "volatility",
            {"period": cfg.atr_period},
            _numeric(f.atr_percent(atr, close)),
        )
        bb_params: dict[str, float | int] = {"period": cfg.bollinger_period, "k": cfg.bollinger_k}
        mid, upper, lower, width = f.bollinger(close, cfg.bollinger_period, cfg.bollinger_k)
        add("bollinger_middle", "volatility", bb_params, _numeric(mid))
        add("bollinger_upper", "volatility", bb_params, _numeric(upper))
        add("bollinger_lower", "volatility", bb_params, _numeric(lower))
        add("bollinger_bandwidth", "volatility", bb_params, _numeric(width))

        # Volume
        add(
            "volume_sma",
            "volume",
            {"period": cfg.volume_sma_period},
            _numeric(f.sma(volume, cfg.volume_sma_period)),
        )
        rvol = f.relative_volume(volume, cfg.rvol_baseline)
        add("relative_volume", "volume", {"baseline": cfg.rvol_baseline}, _numeric(rvol))
        add("obv", "volume", {}, _numeric(f.obv(close, volume)))
        add(
            "volume_trend",
            "volume",
            {
                "short": cfg.volume_trend_short,
                "long": cfg.volume_trend_long,
                "band": cfg.volume_trend_band,
            },
            _state(
                f.volume_trend(
                    volume, cfg.volume_trend_short, cfg.volume_trend_long, cfg.volume_trend_band
                )
            ),
            kind="state",
        )
        add(
            "volume_state",
            "volume",
            {
                "baseline": cfg.rvol_baseline,
                "expansion": cfg.rvol_expansion,
                "contraction": cfg.rvol_contraction,
            },
            _state(f.volume_state(rvol, cfg.rvol_expansion, cfg.rvol_contraction)),
            kind="state",
        )

        dates: list[date] = [d.date() for d in pd.DatetimeIndex(column(bars, BAR_DATE))]
        complete = (
            column(bars, IS_COMPLETE).tolist()
            if IS_COMPLETE in bars.columns
            else [True] * len(bars)
        )
        return IndicatorResult(
            analyzer=self.name,
            analyzer_version=self.version,
            context=context,
            bar_dates=dates,
            provisional=[not bool(c) for c in complete],
            series=series,
        )
