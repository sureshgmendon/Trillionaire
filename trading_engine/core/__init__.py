"""
Trillionaire Trading Engine - Core Module
Provides high-throughput EventBus and TrillionaireEngine coordinator.
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

__all__ = [
    "TrillionaireEngine",
    "EventBus",
    "bus",
    "TickEvent",
    "CandleCloseEvent",
    "AnomalyAlertEvent",
    "SignalEvent",
    "OrderUpdateEvent",
    "RiskAlertEvent",
]
