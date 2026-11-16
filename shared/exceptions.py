"""
Trillionaire Algo Engine - Domain Exceptions
Custom exception hierarchy for risk enforcement, market state, and broker execution.
"""


class TrillionaireBaseException(Exception):
    """Base exception for all Trillionaire algorithmic engine errors."""
    pass


class ConfigurationError(TrillionaireBaseException):
    """Raised when environment or YAML configuration fails validation."""
    pass


class RiskLimitBreachedException(TrillionaireBaseException):
    """Base class for risk violations."""
    pass


class MaxDailyLossBreachedException(RiskLimitBreachedException):
    """Raised when the portfolio drawdown breaches the maximum daily loss threshold."""
    pass


class RiskPerTradeExceededException(RiskLimitBreachedException):
    """Raised when a single order risks more than the allowed percentage of capital."""
    pass


class LeverageLimitExceededException(RiskLimitBreachedException):
    """Raised when requested margin exceeds the active capital tier leverage limit."""
    pass


class MaxPositionsExceededException(RiskLimitBreachedException):
    """Raised when an entry order would exceed max concurrent open positions."""
    pass


class SentinelLockoutException(TrillionaireBaseException):
    """Raised when Sentinel Guard flags a DO_NOT_TRADE status on an instrument."""
    pass


class MarketSessionClosedException(TrillionaireBaseException):
    """Raised when an order entry is attempted outside the valid algorithmic trading window."""
    pass


class OrderExecutionError(TrillionaireBaseException):
    """Raised when broker order placement, modification, or cancellation fails."""
    pass


class BrokerConnectionError(TrillionaireBaseException):
    """Raised when connection to Kite Connect WebSocket or REST fails."""
    pass


class DataFeedError(TrillionaireBaseException):
    """Raised when market data is stale, corrupted, or missing."""
    pass
