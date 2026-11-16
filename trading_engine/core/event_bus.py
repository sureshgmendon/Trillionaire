"""
Trillionaire Trading Engine - Real-Time Typed Asyncio Event Bus
Provides high-throughput in-process pub/sub event routing with subscriber error isolation
and optional mirroring to Redis Streams.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Coroutine, Dict, List, Optional, Type, TypeVar
from loguru import logger

from config.settings import settings
from shared.constants import (
    IST_TZ,
    STREAM_CANDLES,
    STREAM_ORDERS,
    STREAM_SIGNALS,
    STREAM_TICKS,
)
from shared.schemas import Candle, Order, Signal, Tick


# -----------------------------------------------------------------------------
# Event Data Contracts
# -----------------------------------------------------------------------------

@dataclass(frozen=True)
class TickEvent:
    """Dispatched upon receiving an incoming Level-1 tick from broker feed."""
    tick: Tick


@dataclass(frozen=True)
class CandleCloseEvent:
    """Dispatched at bar closure (M1, M5, M15) with fully populated indicators."""
    candle: Candle


@dataclass(frozen=True)
class AnomalyAlertEvent:
    """Dispatched when abnormal volume or freak volatility is detected."""
    symbol: str
    kind: str  # INSTITUTIONAL_SURGE, FLASH_VOLATILITY_FREEZE
    message: str
    timestamp_ist: datetime
    metrics: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SignalEvent:
    """Dispatched when a dual-confirmation strategy identifies a trade entry."""
    signal: Signal


@dataclass(frozen=True)
class OrderUpdateEvent:
    """Dispatched upon order creation, execution, rejection, or fill update."""
    order: Order


@dataclass(frozen=True)
class RiskAlertEvent:
    """Dispatched on portfolio drawdown, leverage limit, or sentinel blackout."""
    symbol: str
    message: str
    level: str  # WARNING, CRITICAL, CIRCUIT_BREAKER
    timestamp_ist: datetime = field(default_factory=lambda: datetime.now(IST_TZ))


T = TypeVar("T")
AsyncEventHandler = Callable[[Any], Coroutine[Any, Any, None]]
SyncEventHandler = Callable[[Any], None]
EventHandler = AsyncEventHandler | SyncEventHandler


# -----------------------------------------------------------------------------
# High-Throughput Event Bus
# -----------------------------------------------------------------------------

class EventBus:
    """High-throughput typed pub/sub bus isolating subscriber exceptions."""

    def __init__(self, enable_redis_mirror: bool = False):
        self._subscribers: Dict[Type[Any], List[EventHandler]] = {}
        self.enable_redis_mirror = enable_redis_mirror
        self._redis_client: Optional[Any] = None

    def subscribe(self, event_type: Type[T], handler: EventHandler) -> None:
        """Subscribes an async or sync callback to a specific event type."""
        handlers = self._subscribers.setdefault(event_type, [])
        if handler not in handlers:
            handlers.append(handler)
            logger.debug(f"Subscribed {handler.__name__ if hasattr(handler, '__name__') else handler} to {event_type.__name__}")

    def unsubscribe(self, event_type: Type[T], handler: EventHandler) -> None:
        """Removes an event subscriber."""
        if event_type in self._subscribers:
            handlers = self._subscribers[event_type]
            if handler in handlers:
                handlers.remove(handler)

    def clear(self) -> None:
        """Clears all registered subscribers."""
        self._subscribers.clear()

    async def publish(self, event: Any) -> None:
        """
        Asynchronously broadcasts an event to all registered subscribers.
        Ensures strict fault isolation: errors in one handler do not affect others.
        """
        event_cls = type(event)
        handlers = self._subscribers.get(event_cls, [])

        if not handlers:
            # Check for inheritance match if needed
            for registered_cls, registered_handlers in self._subscribers.items():
                if isinstance(event, registered_cls) and registered_cls is not event_cls:
                    handlers.extend(registered_handlers)

        for handler in handlers:
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(event)
                else:
                    res = handler(event)
                    if asyncio.iscoroutine(res):
                        await res
            except Exception as e:
                handler_name = getattr(handler, "__name__", str(handler))
                logger.error(
                    f"Exception in EventBus subscriber '{handler_name}' "
                    f"handling {event_cls.__name__}: {e}",
                    exc_info=True,
                )

        # Optional Redis Streams Mirroring
        if self.enable_redis_mirror:
            self._mirror_to_redis_stream(event)

    def _mirror_to_redis_stream(self, event: Any) -> None:
        """Asynchronously mirrors serialized event to corresponding Redis Stream."""
        try:
            if self._redis_client is None:
                import redis
                self._redis_client = redis.from_url(
                    settings.redis_url,
                    decode_responses=True,
                    socket_connect_timeout=0.2,
                    socket_timeout=0.2,
                )

            stream_name = None
            payload = {}

            if isinstance(event, TickEvent):
                stream_name = STREAM_TICKS
                payload = {"data": event.tick.model_dump_json()}
            elif isinstance(event, CandleCloseEvent):
                stream_name = STREAM_CANDLES
                payload = {"data": event.candle.model_dump_json()}
            elif isinstance(event, SignalEvent):
                stream_name = STREAM_SIGNALS
                payload = {"data": event.signal.model_dump_json()}
            elif isinstance(event, OrderUpdateEvent):
                stream_name = STREAM_ORDERS
                payload = {"data": event.order.model_dump_json()}

            if stream_name:
                self._redis_client.xadd(stream_name, payload, maxlen=10_000)
        except Exception as e:
            logger.debug(f"Redis stream mirroring skipped: {e}")


# Global singleton instance
bus = EventBus()
