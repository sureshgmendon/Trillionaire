"""
Trillionaire Research Lab - Backtester Package
Exports technical indicators, dual-confirmation micro-strategies, event-driven engine,
and walk-forward optimization components.
"""

from research_lab.backtester.engine import BacktestSummary, EventDrivenBacktester, TradeRecord
from research_lab.backtester.indicators import (
    compute_all_indicators,
    compute_daily_cpr,
    compute_opening_range,
    compute_time_of_day_rvol,
    compute_vwap,
    compute_wilder_atr,
    resample_to_5min,
)
from research_lab.backtester.strategies import (
    BaseMicroStrategy,
    HighRvolBreakoutStrategy,
    MeanReversionStrategy,
    TrendMomentumStrategy,
    get_strategy_for_regime,
)
from research_lab.backtester.walk_forward import TEARSHEET_PATH, WalkForwardValidator

__all__ = [
    "EventDrivenBacktester",
    "TradeRecord",
    "BacktestSummary",
    "compute_all_indicators",
    "compute_vwap",
    "compute_wilder_atr",
    "compute_daily_cpr",
    "compute_time_of_day_rvol",
    "compute_opening_range",
    "resample_to_5min",
    "BaseMicroStrategy",
    "TrendMomentumStrategy",
    "MeanReversionStrategy",
    "HighRvolBreakoutStrategy",
    "get_strategy_for_regime",
    "WalkForwardValidator",
    "TEARSHEET_PATH",
]
