"""
Trillionaire Trading Engine - Data Package
Exports real-time multi-timeframe candle aggregator, anomaly shock detector,
and historical tick replayer.
"""

from trading_engine.data.anomaly_detector import AnomalyDetector
from trading_engine.data.candle_aggregator import CandleAggregator, TimeframeState
from trading_engine.data.tick_replayer import HistoricalTickReplayer

__all__ = [
    "CandleAggregator",
    "TimeframeState",
    "AnomalyDetector",
    "HistoricalTickReplayer",
]
