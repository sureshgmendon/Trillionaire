"""
Trillionaire Trading Engine - Core Module
Provides high-throughput EventBus, TrillionaireEngine coordinator, and TelegramNotifier.
"""

from trading_engine.core.engine import TrillionaireEngine
from trading_engine.core.event_bus import (
    AnomalyAlertEvent,
    CandleCloseEvent,
    EventBus,
    OrderUpdateEvent,
    RiskAlertEvent,
    SignalEvent,
    TickEvent,
    bus,
)
from trading_engine.core.telegram_bot import TelegramNotifier

__all__ = [
    "TrillionaireEngine",
    "TelegramNotifier",
    "EventBus",
    "bus",
    "TickEvent",
    "CandleCloseEvent",
    "AnomalyAlertEvent",
    "SignalEvent",
    "OrderUpdateEvent",
    "RiskAlertEvent",
]
