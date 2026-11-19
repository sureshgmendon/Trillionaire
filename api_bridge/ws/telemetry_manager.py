"""
Trillionaire API Bridge - Real-Time WebSocket Telemetry Broadcaster
Maintains active browser WebSocket connections, subscribes to EventBus events,
serializes telemetry into standardized JSON envelopes, and streams real-time updates:
1. Concurrently broadcasts TICK, CANDLE, SIGNAL, ORDER, ANOMALY, and RISK envelopes.
2. Automatically prunes dead/disconnected client sockets without blocking the engine.
3. Maintains a rolling ring buffer of recent events and emits an instant SNAPSHOT upon connection.
"""

import asyncio
from collections import deque
from datetime import datetime
import json
from typing import Any, Dict, List, Optional, Set
from fastapi import WebSocket, WebSocketDisconnect
from loguru import logger

from shared.constants import IST_TZ
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


class TelemetryManager:
    """Manages active WebSockets, buffers telemetry history, and streams live event feeds."""

    def __init__(
        self,
        event_bus: Optional[EventBus] = None,
        max_buffer_len: int = 100,
        engine: Optional[Any] = None,
    ):
        self.bus = event_bus or bus
        self.max_buffer_len = max_buffer_len
        self.engine = engine

        # Active connected WebSocket clients
        self.active_connections: Set[WebSocket] = set()

        # Ring buffers for initial SNAPSHOT hydration
        self.signals_buffer: deque[Dict[str, Any]] = deque(maxlen=max_buffer_len)
        self.orders_buffer: deque[Dict[str, Any]] = deque(maxlen=max_buffer_len)
        self.risk_buffer: deque[Dict[str, Any]] = deque(maxlen=max_buffer_len)
        self.anomaly_buffer: deque[Dict[str, Any]] = deque(maxlen=max_buffer_len)
        self.latest_ticks: Dict[str, Dict[str, Any]] = {}
        self.latest_candles: Dict[str, Dict[str, Any]] = {}

        # Wire EventBus subscribers
        self._register_subscribers()

    def set_engine(self, engine: Any) -> None:
        """Attaches TrillionaireEngine instance for diagnostic snapshot queries."""
        self.engine = engine
        if hasattr(engine, "bus") and engine.bus != self.bus:
            self.bus = engine.bus
            self._register_subscribers()

    def _register_subscribers(self) -> None:
        """Subscribes handler callbacks to EventBus topics."""
        self.bus.subscribe(TickEvent, self.on_tick_event)
        self.bus.subscribe(CandleCloseEvent, self.on_candle_event)
        self.bus.subscribe(SignalEvent, self.on_signal_event)
        self.bus.subscribe(OrderUpdateEvent, self.on_order_event)
        self.bus.subscribe(AnomalyAlertEvent, self.on_anomaly_event)
        self.bus.subscribe(RiskAlertEvent, self.on_risk_event)

    async def connect(self, websocket: WebSocket) -> None:
        """Accepts a new client connection, tracks it, and sends initial SNAPSHOT."""
        await websocket.accept()
        self.active_connections.add(websocket)
        logger.info(f"[WS TELEMETRY] Client connected ({len(self.active_connections)} total active)")

        # Send instant hydration SNAPSHOT
        snapshot = self.build_snapshot()
        try:
            await websocket.send_text(json.dumps(snapshot))
        except Exception as e:
            logger.warning(f"[WS TELEMETRY] Failed to send initial snapshot: {e}")
            self.disconnect(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        """Removes a disconnected client socket from active registry."""
        self.active_connections.discard(websocket)
        logger.info(f"[WS TELEMETRY] Client disconnected ({len(self.active_connections)} active)")

    def build_snapshot(self) -> Dict[str, Any]:
        """Constructs full session history hydration packet for new dashboard tabs."""
        now_ist = datetime.now(IST_TZ).isoformat()
        engine_status = self.engine.get_status() if self.engine is not None else {}

        return {
            "type": "SNAPSHOT",
            "timestamp_ist": now_ist,
            "payload": {
                "status": engine_status,
                "latest_ticks": self.latest_ticks,
                "latest_candles": self.latest_candles,
                "recent_signals": list(self.signals_buffer),
                "recent_orders": list(self.orders_buffer),
                "recent_risk_alerts": list(self.risk_buffer),
                "recent_anomalies": list(self.anomaly_buffer),
            },
        }

    async def broadcast(self, envelope: Dict[str, Any]) -> None:
        """Broadcasts a JSON envelope concurrently to all connected sockets, pruning dead ones."""
        if not self.active_connections:
            return

        payload_str = json.dumps(envelope)
        dead_sockets: List[WebSocket] = []

        async def _safe_send(ws: WebSocket):
            try:
                await ws.send_text(payload_str)
            except (WebSocketDisconnect, RuntimeError, Exception):
                dead_sockets.append(ws)

        await asyncio.gather(*[_safe_send(ws) for ws in self.active_connections], return_exceptions=True)

        # Prune dead sockets
        for dead_ws in dead_sockets:
            self.disconnect(dead_ws)

    async def broadcast_snapshot(self) -> None:
        """Constructs and broadcasts a real-time SNAPSHOT envelope to all connected dashboard tabs."""
        snapshot = self.build_snapshot()
        await self.broadcast(snapshot)

    # -------------------------------------------------------------------------
    # EventBus Listeners
    # -------------------------------------------------------------------------

    async def on_tick_event(self, event: TickEvent) -> None:
        """Handles TickEvent: updates tick cache and broadcasts TICK envelope."""
        tick = event.tick
        tick_dict = tick.model_dump(mode="json")
        self.latest_ticks[tick.tradingsymbol] = tick_dict

        envelope = {
            "type": "TICK",
            "timestamp_ist": tick.timestamp_ist.isoformat(),
            "payload": tick_dict,
        }
        await self.broadcast(envelope)

    async def on_candle_event(self, event: CandleCloseEvent) -> None:
        """Handles CandleCloseEvent: updates candle cache and broadcasts CANDLE envelope."""
        candle = event.candle
        candle_dict = candle.model_dump(mode="json")
        key = f"{candle.tradingsymbol}_{candle.timeframe.value}"
        self.latest_candles[key] = candle_dict

        envelope = {
            "type": "CANDLE",
            "timestamp_ist": candle.timestamp_ist.isoformat(),
            "payload": candle_dict,
        }
        await self.broadcast(envelope)

    async def on_signal_event(self, event: SignalEvent) -> None:
        """Handles SignalEvent: appends to ring buffer and broadcasts SIGNAL envelope."""
        signal = event.signal
        sig_dict = signal.model_dump(mode="json")
        self.signals_buffer.append(sig_dict)

        envelope = {
            "type": "SIGNAL",
            "timestamp_ist": signal.generated_at_ist.isoformat(),
            "payload": sig_dict,
        }
        await self.broadcast(envelope)

    async def on_order_event(self, event: OrderUpdateEvent) -> None:
        """Handles OrderUpdateEvent: appends to ring buffer and broadcasts ORDER envelope."""
        order = event.order
        order_dict = order.model_dump(mode="json")
        self.orders_buffer.append(order_dict)

        envelope = {
            "type": "ORDER",
            "timestamp_ist": order.created_at_ist.isoformat(),
            "payload": order_dict,
        }
        await self.broadcast(envelope)

    async def on_anomaly_event(self, event: AnomalyAlertEvent) -> None:
        """Handles AnomalyAlertEvent: appends to buffer and broadcasts ANOMALY envelope."""
        anomaly_dict = {
            "symbol": event.symbol,
            "kind": event.kind,
            "message": event.message,
            "timestamp_ist": event.timestamp_ist.isoformat(),
            "metrics": event.metrics,
        }
        self.anomaly_buffer.append(anomaly_dict)

        envelope = {
            "type": "ANOMALY",
            "timestamp_ist": event.timestamp_ist.isoformat(),
            "payload": anomaly_dict,
        }
        await self.broadcast(envelope)

    async def on_risk_event(self, event: RiskAlertEvent) -> None:
        """Handles RiskAlertEvent: appends to buffer and broadcasts RISK envelope."""
        risk_dict = {
            "symbol": event.symbol,
            "level": event.level,
            "message": event.message,
            "timestamp_ist": event.timestamp_ist.isoformat(),
        }
        self.risk_buffer.append(risk_dict)

        envelope = {
            "type": "RISK",
            "timestamp_ist": event.timestamp_ist.isoformat(),
            "payload": risk_dict,
        }
        await self.broadcast(envelope)


# Singleton broadcaster instance
telemetry_manager = TelemetryManager()
