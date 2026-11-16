"""
Trillionaire Algo Engine - Shared Enums
Type-safe enumeration definitions for trading execution, clustering, and risk management.
"""

from enum import Enum


class TradingMode(str, Enum):
    """Operational mode of the trading engine."""
    PAPER = "PAPER"
    LIVE = "LIVE"
    REPLAY = "REPLAY"


class ClusterRegime(str, Enum):
    """Statistical cluster classification regime for stock behavior."""
    TREND_MOMENTUM = "TREND_MOMENTUM"
    MEAN_REVERSION = "MEAN_REVERSION"
    HIGH_RVOL_BREAKOUT = "HIGH_RVOL_BREAKOUT"


class SentinelStatus(str, Enum):
    """News and event risk condition emitted by Sentinel Guard."""
    NORMAL = "NORMAL"
    CAUTION_HALF_SIZE = "CAUTION_HALF_SIZE"
    DO_NOT_TRADE = "DO_NOT_TRADE"


class Side(str, Enum):
    """Trading order or position side."""
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    """Broker execution order type."""
    LIMIT = "LIMIT"
    SL_LIMIT = "SL_LIMIT"


class OrderStatus(str, Enum):
    """Lifecycle state of an order."""
    PENDING_NEW = "PENDING_NEW"
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


class Timeframe(str, Enum):
    """Candlestick sampling intervals."""
    M1 = "M1"
    M5 = "M5"
    M15 = "M15"

    @property
    def minutes(self) -> int:
        mapping = {
            "M1": 1,
            "M5": 5,
            "M15": 15,
        }
        return mapping[self.value]
