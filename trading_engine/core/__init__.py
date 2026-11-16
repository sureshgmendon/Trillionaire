"""
Trillionaire Trading Engine - Core Package
Exports pub/sub event bus and event data contracts.
"""

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
    "EventBus",
    "bus",
    "TickEvent",
    "CandleCloseEvent",
    "AnomalyAlertEvent",
    "SignalEvent",
    "OrderUpdateEvent",
    "RiskAlertEvent",
]
