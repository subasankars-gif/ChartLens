"""Layer F — evidence: divergence, volume, volatility and candles (ADR-0021 §F).

Everything here is evidence that later layers may cite. Nothing is a signal, and nothing
is combined into a score.
"""

from chartlens_engine.evidence.candles import CandleAnalyzer, CandleEvent, CandleResult
from chartlens_engine.evidence.divergence import Divergence, DivergenceAnalyzer, DivergenceResult
from chartlens_engine.evidence.volatility import (
    VolatilityAnalyzer,
    VolatilityEvent,
    VolatilityResult,
    VolatilityState,
)
from chartlens_engine.evidence.volume import (
    VolumeAnalyzer,
    VolumeEvent,
    VolumeResult,
    VolumeState,
)

__all__ = [
    "CandleAnalyzer",
    "CandleEvent",
    "CandleResult",
    "Divergence",
    "DivergenceAnalyzer",
    "DivergenceResult",
    "VolatilityAnalyzer",
    "VolatilityEvent",
    "VolatilityResult",
    "VolatilityState",
    "VolumeAnalyzer",
    "VolumeEvent",
    "VolumeResult",
    "VolumeState",
]
