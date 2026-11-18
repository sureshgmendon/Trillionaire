"""
Unit tests for Free Live NSE Market Feeder and Runtime Feeder Switching.
Verifies:
1. FreeLiveMarketFeeder quote normalization and Tick schema validity.
2. EventBus TickEvent dispatch and in-memory caching.
3. Lifecycle management (start/stop).
4. Safety enforcement: FREE_LIVE feeder locks order execution to PaperBroker.
5. REST settings integration: GET/POST /api/v1/settings and /api/v1/status feeder synchronization.
"""

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock
import httpx
import pytest
from fastapi.testclient import TestClient

from api_bridge.main import app
from api_bridge.routes.engine_control import set_engine
from config.settings import settings
from shared.constants import IST_TZ
from shared.enums import DataFeederType, TradingMode
from shared.schemas import Tick
from trading_engine.core.engine import TrillionaireEngine
from trading_engine.core.event_bus import EventBus, TickEvent
from trading_engine.data.free_live_feeder import DEFAULT_SECTOR_INDICES, FreeLiveMarketFeeder
from trading_engine.execution.order_manager import OrderManager
from trading_engine.execution.paper_broker import PaperBroker
from trading_engine.risk.portfolio_risk import PortfolioRiskGatekeeper
from trading_engine.strategy.live_runner import LiveStrategyRunner


@pytest.fixture
def mock_engine_and_client():
    """Sets up an engine with admin-protected FastAPI TestClient."""
    bus = EventBus()
    risk_guard = PortfolioRiskGatekeeper(event_bus=bus, allocated_capital=100000.0)
    broker = PaperBroker(event_bus=bus)
    om = OrderManager(event_bus=bus, broker=broker, risk_gatekeeper=risk_guard)
    strategy_runner = LiveStrategyRunner(event_bus=bus)

    engine = TrillionaireEngine(
        trading_mode=TradingMode.PAPER,
        data_feeder=DataFeederType.FREE_LIVE,
        event_bus=bus,
        risk_gatekeeper=risk_guard,
        order_manager=om,
        broker=broker,
        strategy_runner=strategy_runner,
    )
    engine.is_booted = True
    set_engine(engine)

    client = TestClient(app)
    return engine, client, bus, om


# ==============================================================================
# 1. Feeder Initialization & Universe Mapping
# ==============================================================================

def test_free_feeder_universe_loading():
    """Validates that feeder automatically indexes sector indices and equities."""
    bus = EventBus()
    feeder = FreeLiveMarketFeeder(event_bus=bus)

    # 8 Benchmark Indices
    for idx_name, meta in DEFAULT_SECTOR_INDICES.items():
        assert idx_name in feeder.tracked_symbols
        assert feeder.tracked_symbols[idx_name]["yahoo_symbol"] == meta["yahoo"]
        assert feeder.tracked_symbols[idx_name]["token"] == meta["token"]
        assert feeder.tracked_symbols[idx_name]["is_index"] is True

    # Equities from sector_map.yaml
    assert "RELIANCE" in feeder.tracked_symbols
    assert feeder.tracked_symbols["RELIANCE"]["yahoo_symbol"] == "RELIANCE.NS"
    assert feeder.tracked_symbols["RELIANCE"]["token"] == 738561
    assert feeder.tracked_symbols["RELIANCE"]["is_index"] is False
    assert len(feeder.tracked_symbols) >= 100


# ==============================================================================
# 2. Quote Normalization & EventBus Publishing
# ==============================================================================

@pytest.mark.asyncio
async def test_free_feeder_quote_normalization_and_publishing():
    """Validates normalization of Yahoo Finance JSON payloads into validated Tick schemas."""
    bus = EventBus()
    received_ticks: list[Tick] = []

    async def on_tick(event: TickEvent):
        received_ticks.append(event.tick)

    bus.subscribe(TickEvent, on_tick)

    # Mock HTTP response matching Yahoo Finance v8 chart payload
    mock_payload = {
        "chart": {
            "result": [
                {
                    "meta": {
                        "regularMarketPrice": 2550.75,
                        "regularMarketVolume": 1500000,
                        "regularMarketTime": int(datetime.now(IST_TZ).timestamp()),
                    }
                }
            ]
        }
    }

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.json.return_value = mock_payload
    mock_client.get.return_value = mock_response

    custom_symbols = {
        "RELIANCE": {"token": 738561, "yahoo_symbol": "RELIANCE.NS", "is_index": False},
        "NIFTY 50": {"token": 256265, "yahoo_symbol": "^NSEI", "is_index": True},
    }

    feeder = FreeLiveMarketFeeder(
        event_bus=bus,
        symbols_map=custom_symbols,
        custom_client=mock_client,
    )
    feeder._client = mock_client

    ticks = await feeder.poll_once()

    assert len(ticks) == 2
    assert "RELIANCE" in ticks
    assert "NIFTY 50" in ticks

    rel_tick = ticks["RELIANCE"]
    assert isinstance(rel_tick, Tick)
    assert rel_tick.tradingsymbol == "RELIANCE"
    assert rel_tick.instrument_token == 738561
    assert rel_tick.ltp == 2550.75
    assert rel_tick.volume == 1500000
    assert rel_tick.bid > 0
    assert rel_tick.ask > rel_tick.bid

    # Verify EventBus dispatch
    await asyncio.sleep(0.05)
    assert len(received_ticks) == 2


# ==============================================================================
# 3. Feeder Lifecycle (Start & Stop)
# ==============================================================================

@pytest.mark.asyncio
async def test_free_feeder_start_and_stop():
    """Validates feeder lifecycle starting and stopping cleanly."""
    bus = EventBus()
    feeder = FreeLiveMarketFeeder(event_bus=bus, poll_interval_sec=0.1)

    assert feeder.is_running is False
    await feeder.start()
    assert feeder.is_running is True

    await asyncio.sleep(0.15)
    await feeder.stop()
    assert feeder.is_running is False


# ==============================================================================
# 4. Safety Lock: FREE_LIVE Routes via PaperBroker
# ==============================================================================

def test_free_live_safety_lock_enforcement():
    """
    Verifies that selecting FREE_LIVE feeder strictly forces order routing through
    PaperBroker even if trading_mode is set to LIVE.
    """
    bus = EventBus()
    risk_guard = PortfolioRiskGatekeeper(event_bus=bus)
    mock_live_broker = MagicMock()  # Simulates a live Kite broker instance
    om = OrderManager(event_bus=bus, broker=mock_live_broker, risk_gatekeeper=risk_guard)

    engine = TrillionaireEngine(
        trading_mode=TradingMode.LIVE,
        data_feeder=DataFeederType.FREE_LIVE,
        event_bus=bus,
        risk_gatekeeper=risk_guard,
        order_manager=om,
        broker=mock_live_broker,
    )

    # OrderManager broker MUST be overridden to PaperBroker for safety
    assert isinstance(engine.order_manager.broker, PaperBroker)

    # Dynamic switch: if user switches from ZERODHA_KITE back to FREE_LIVE
    engine.data_feeder = DataFeederType.ZERODHA_KITE
    engine.order_manager.broker = mock_live_broker

    # Switching to FREE_LIVE triggers safety lock
    engine.set_data_feeder(DataFeederType.FREE_LIVE)
    assert isinstance(engine.order_manager.broker, PaperBroker)
    assert engine.data_feeder == DataFeederType.FREE_LIVE


# ==============================================================================
# 5. REST Settings & Status Feeder Integration
# ==============================================================================

def test_settings_api_feeder_switch(mock_engine_and_client):
    """Validates GET/POST /api/v1/settings for data_feeder switching."""
    engine, client, _, _ = mock_engine_and_client
    admin_key = settings.trillionaire_admin_key

    # 1. GET /api/v1/settings returns data_feeder
    resp = client.get("/api/v1/settings", headers={"X-API-Key": admin_key})
    assert resp.status_code == 200
    data = resp.json()
    assert "data_feeder" in data
    assert data["data_feeder"] == "FREE_LIVE"

    # 2. GET /api/v1/status exposes data_feeder
    st_resp = client.get("/api/v1/status")
    assert st_resp.status_code == 200
    st_data = st_resp.json()
    assert "data_feeder" in st_data
    assert st_data["data_feeder"] == "FREE_LIVE"

    # 3. POST /api/v1/settings switches to ZERODHA_KITE
    post_resp = client.post(
        "/api/v1/settings",
        json={"data_feeder": "ZERODHA_KITE"},
        headers={"X-API-Key": admin_key},
    )
    assert post_resp.status_code == 200
    res_json = post_resp.json()
    assert res_json["status"] == "saved"
    assert res_json["settings"]["data_feeder"] == "ZERODHA_KITE"
    assert engine.data_feeder == DataFeederType.ZERODHA_KITE

    # 4. Switch back to FREE_LIVE
    back_resp = client.post(
        "/api/v1/settings",
        json={"data_feeder": "FREE_LIVE"},
        headers={"X-API-Key": admin_key},
    )
    assert back_resp.status_code == 200
    assert back_resp.json()["settings"]["data_feeder"] == "FREE_LIVE"
    assert engine.data_feeder == DataFeederType.FREE_LIVE
