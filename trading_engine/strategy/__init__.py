"""
Trillionaire Trading Engine - Strategy Module
Quantitative intraday strategies: Trend Momentum, Mean Reversion, and High-RVOL Breakout.
Exports LiveStrategyRunner for orchestrating live M5 candle evaluations.
"""

from trading_engine.strategy.live_runner import LiveStrategyRunner

__all__ = ["LiveStrategyRunner"]
