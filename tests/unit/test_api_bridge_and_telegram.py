"""
Unit tests for Sprint 8:
1. REST Telemetry Endpoints (/status, /positions, /orders, /profiles, /sentinel).
2. Admin-Protected Control Endpoints (/control/kill-switch, /control/replay).
3. WebSocket Streaming (/ws/stream) with SNAPSHOT hydration and real-time event broadcasting.
4. TelegramNotifier mobile alerts, trade fills, and remote /kill command authorization.
"""

from datetime import datetime
import json
from typing import Any, Dict
import pytest
import httpx
import uuid
from fastapi.testclient import TestClient

from api_bridge.main import app
from api_bridge.routes.engine_control import set_engine
from api_bridge.ws.telemetry_manager import telemetry_manager
from config.settings import settings
from shared.constants import IST_TZ
from shared.enums import ClusterRegime, OrderStatus, OrderType, Side, Timeframe, TradingMode
from shared.schemas import Order, Position, StockProfile, Tick
from trading_engine.core.engine import TrillionaireEngine
from trading_engine.core.event_bus import (
    EventBus,
    OrderUpdateEvent,
    RiskAlertEvent,
    TickEvent,
)
from trading_engine.core.telegram_bot import TelegramNotifier
from trading_engine.execution.order_manager import OrderManager
from trading_engine.execution.paper_broker import PaperBroker
from trading_engine.risk.portfolio_risk import PortfolioRiskGatekeeper
from trading_engine.strategy.live_runner import LiveStrategyRunner


@pytest.fixture
def mock_engine() -> TrillionaireEngine:
    """Creates a configured mock TrillionaireEngine in PAPER mode with an active position."""
    bus = telemetry_manager.bus
    risk_guard = PortfolioRiskGatekeeper(event_bus=bus, allocated_capital=100000.0)
    broker = PaperBroker(event_bus=bus)
    om = OrderManager(event_bus=bus, broker=broker, risk_gatekeeper=risk_guard)

    engine = TrillionaireEngine(
        trading_mode=TradingMode.PAPER,
        event_bus=bus,
        risk_gatekeeper=risk_guard,
        order_manager=om,
        broker=broker,
    )
    engine.is_running = True
    engine.is_booted = True

    # Seed an open position
    pos = Position(
        tradingsymbol="RELIANCE",
        side=Side.BUY,
        quantity=50,
        average_entry_price=2500.0,
        ltp=2520.0,
        unrealized_pnl=1000.0,
        realized_pnl=0.0,
        stop_loss=2480.0,
        target_price=2560.0,
    )
    om.open_positions["RELIANCE"] = pos
    risk_guard.register_position(pos)

    # Seed an order
    order = Order(
        order_id=str(uuid.uuid4()),
        tradingsymbol="RELIANCE",
        side=Side.BUY,
        product="MIS",
        order_type=OrderType.LIMIT,
        quantity=50,
        limit_price=2502.50,
        average_fill_price=2500.0,
        filled_quantity=50,
        status=OrderStatus.FILLED,
        created_at_ist=datetime.now(IST_TZ),
    )
    om.orders[order.order_id] = order

    # Connect to TelemetryManager and routes
    set_engine(engine)
    telemetry_manager.set_engine(engine)
    return engine


@pytest.fixture
def api_client(mock_engine: TrillionaireEngine):
    """Provides FastAPI TestClient wired to the mock engine."""
    with TestClient(app) as client:
        yield client


# -----------------------------------------------------------------------------
# 1. REST Telemetry Endpoints Tests
# -----------------------------------------------------------------------------

def test_get_status_endpoint(api_client: TestClient):
    """Verifies GET /api/v1/status returns accurate engine diagnostics."""
    response = api_client.get("/api/v1/status")
    assert response.status_code == 200
    data = response.json()

    assert data["mode"] == "PAPER"
    assert data["is_running"] is True
    assert data["is_booted"] is True
    assert data["allocated_capital"] == 100000.0
    assert data["open_positions_count"] == 1
    assert data["circuit_breaker_tripped"] is False
    assert "timestamp_ist" in data


def test_get_positions_and_orders_endpoints(api_client: TestClient):
    """Verifies GET /api/v1/positions and /orders return populated state."""
    # Positions
    pos_resp = api_client.get("/api/v1/positions")
    assert pos_resp.status_code == 200
    positions = pos_resp.json()
    assert len(positions) == 1
    assert positions[0]["tradingsymbol"] == "RELIANCE"
    assert positions[0]["quantity"] == 50
    assert positions[0]["average_entry_price"] == 2500.0

    # Orders
    ord_resp = api_client.get("/api/v1/orders")
    assert ord_resp.status_code == 200
    orders = ord_resp.json()
    assert len(orders) >= 1
    assert orders[0]["tradingsymbol"] == "RELIANCE"
    assert orders[0]["status"] == "FILLED"


def test_get_profiles_and_sentinel_endpoints(api_client: TestClient):
    """Verifies GET /api/v1/profiles and /sentinel return active universe and risk tags."""
    prof_resp = api_client.get("/api/v1/profiles")
    assert prof_resp.status_code == 200
    profiles = prof_resp.json()
    assert isinstance(profiles, list)

    sentinel_resp = api_client.get("/api/v1/sentinel")
    assert sentinel_resp.status_code == 200
    sentinel_data = sentinel_resp.json()
    assert isinstance(sentinel_data, list)
    if len(sentinel_data) > 0:
        assert "sentinel" in sentinel_data[0]
        assert "risk_multiplier" in sentinel_data[0]["sentinel"]


# -----------------------------------------------------------------------------
# 2. Control Endpoints Tests (Security & Kill-Switch)
# -----------------------------------------------------------------------------

def test_control_kill_switch_security_and_execution(api_client: TestClient, mock_engine: TrillionaireEngine):
    """Verifies that mutating control endpoints require valid X-API-Key and flatten positions."""
    # 1. Unauthorized call (no key) -> 403
    unauth_resp = api_client.post("/api/v1/control/kill-switch")
    assert unauth_resp.status_code == 403

    # 2. Unauthorized call (wrong key) -> 403
    wrong_key_resp = api_client.post(
        "/api/v1/control/kill-switch",
        headers={"X-API-Key": "wrong-secret-key"},
    )
    assert wrong_key_resp.status_code == 403

    # 3. Authorized call with admin key -> 200 and flattens positions
    admin_key = settings.trillionaire_admin_key
    assert len(mock_engine.order_manager.open_positions) == 1

    auth_resp = api_client.post(
        "/api/v1/control/kill-switch?reason=TEST_MANUAL_OVERRIDE",
        headers={"X-API-Key": admin_key},
    )
    assert auth_resp.status_code == 200
    res_data = auth_resp.json()
    assert res_data["status"] == "executed"
    assert res_data["action"] == "EMERGENCY_KILL_SWITCH"

    # Engine positions must now be flattened and kill switch tripped
    assert len(mock_engine.order_manager.open_positions) == 0
    assert mock_engine.risk_gatekeeper.kill_switch_active is True


# -----------------------------------------------------------------------------
# 3. WebSocket Streaming & Telemetry Tests
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_websocket_telemetry_snapshot_and_live_broadcast(mock_engine: TrillionaireEngine):
    """Verifies that /ws/stream delivers initial SNAPSHOT and streams real-time TICK envelopes."""
    bus = telemetry_manager.bus

    with TestClient(app) as client:
        with client.websocket_connect("/ws/stream") as websocket:
            # 1. First packet received must be SNAPSHOT
            initial_data = websocket.receive_text()
            packet = json.loads(initial_data)
            assert packet["type"] == "SNAPSHOT"
            assert "payload" in packet
            assert "recent_orders" in packet["payload"]

            # 2. Publish TickEvent onto EventBus
            test_tick = Tick(
                instrument_token=738561,
                tradingsymbol="RELIANCE",
                timestamp_ist=datetime(2026, 3, 10, 10, 30, tzinfo=IST_TZ),
                ltp=2525.0,
                volume=15000,
                bid=2524.9,
                ask=2525.1,
            )
            await bus.publish(TickEvent(tick=test_tick))

            # 3. Client receives live TICK packet
            live_data = websocket.receive_text()
            live_packet = json.loads(live_data)
            assert live_packet["type"] == "TICK"
            assert live_packet["payload"]["tradingsymbol"] == "RELIANCE"
            assert live_packet["payload"]["ltp"] == 2525.0


# -----------------------------------------------------------------------------
# 4. TelegramNotifier Mobile Alerts & Remote Command Tests
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_telegram_notifier_alerts_and_commands(mock_engine: TrillionaireEngine):
    """Verifies TelegramNotifier alert formatting and authorized /kill command execution."""
    sent_messages = []

    # Mock HTTP transport intercepting Telegram Bot API calls
    def mock_telegram_transport(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/sendMessage" in url:
            body = json.loads(request.content.decode("utf-8"))
            sent_messages.append(body)
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 101}})
        return httpx.Response(404)

    mock_http = httpx.AsyncClient(transport=httpx.MockTransport(mock_telegram_transport))

    bot = TelegramNotifier(
        bot_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
        chat_id="99887766",
        engine=mock_engine,
        event_bus=telemetry_manager.bus,
        http_client=mock_http,
    )

    # 1. Test Trade Fill Alert
    order = Order(
        order_id=str(uuid.uuid4()),
        tradingsymbol="TCS",
        side=Side.BUY,
        product="MIS",
        order_type=OrderType.LIMIT,
        quantity=25,
        limit_price=3000.0,
        average_fill_price=3000.0,
        filled_quantity=25,
        status=OrderStatus.FILLED,
        created_at_ist=datetime.now(IST_TZ),
    )
    delivered = await bot.send_trade_fill_alert(order)
    assert delivered is True
    assert len(sent_messages) == 1
    assert "TCS" in sent_messages[0]["text"]
    assert "Trade Filled" in sent_messages[0]["text"]

    # 2. Test Remote /status Command (Authorized chat ID)
    status_reply = await bot.handle_command("/status", sender_chat_id="99887766")
    assert "Engine Status Snapshot" in status_reply
    assert "PAPER" in status_reply

    # 3. Test Remote Command from Unauthorized sender
    unauth_reply = await bot.handle_command("/status", sender_chat_id="hackers_chat_id")
    assert "Unauthorized" in unauth_reply

    # 4. Test Remote /kill Command (Authorized chat ID)
    assert len(mock_engine.order_manager.open_positions) > 0
    kill_reply = await bot.handle_command("/kill", sender_chat_id="99887766")

    assert "EMERGENCY KILL SWITCH EXECUTED" in kill_reply
    assert len(mock_engine.order_manager.open_positions) == 0
    assert mock_engine.risk_gatekeeper.kill_switch_active is True

    # 5. Test Webhook Update Processing
    webhook_payload = {
        "update_id": 10001,
        "message": {
            "message_id": 1,
            "text": "/positions",
            "chat": {"id": 99887766},
        },
    }
    webhook_reply = await bot.process_webhook_update(webhook_payload)
    assert "Active Open Positions" in webhook_reply or "No open positions" in webhook_reply

    # 6. Test Risk Alert Event Push
    risk_ev = RiskAlertEvent(
        symbol="ALL",
        level="CIRCUIT_BREAKER",
        message="Daily loss -1.5% breached. Trading locked.",
    )
    await bot.on_risk_alert(risk_ev)
    assert len(sent_messages) >= 2
    assert "CIRCUIT BREAKER" in sent_messages[-1]["text"]

    await mock_http.aclose()


def test_control_replay_endpoint(api_client: TestClient):
    """Verifies POST /api/v1/control/replay validation and execution."""
    admin_key = settings.trillionaire_admin_key

    # Unauthorized call
    resp_unauth = api_client.post("/api/v1/control/replay", json={"speed": "MAX_SPEED"})
    assert resp_unauth.status_code == 403

    # Authorized call with invalid date format
    resp_bad_date = api_client.post(
        "/api/v1/control/replay",
        headers={"X-API-Key": admin_key},
        json={"trading_date": "not-a-date", "speed": "MAX_SPEED"},
    )
    assert resp_bad_date.status_code == 400

    # Authorized call with valid parameters
    resp_valid = api_client.post(
        "/api/v1/control/replay",
        headers={"X-API-Key": admin_key},
        json={"speed": "MAX_SPEED", "symbols": ["RELIANCE"]},
    )
    assert resp_valid.status_code == 200
    res_data = resp_valid.json()
    assert res_data["status"] == "completed"
    assert "ticks_replayed" in res_data

