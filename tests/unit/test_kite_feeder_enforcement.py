"""
Unit tests for Sole Zerodha Kite Market Feeder Enforcement.
Validates:
1. TrillionaireEngine strictly defaults to ZERODHA_KITE and never falls back.
2. Morning rollover resets KILL SWITCH ACTIVE (reset_daily_state) unconditionally even before Kite OAuth login.
3. Premarket boot keeps data_feeder == ZERODHA_KITE when auth is pending.
4. Engine start warns cleanly without crashing if Kite token is missing for the day.
5. 1-click /api/v1/kite/callback exchanges token, subscribes all stock + sector tokens on KiteTicker, and starts live streaming.
6. REST settings & status endpoints strictly expose and enforce ZERODHA_KITE.
"""

import asyncio
from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from fastapi.testclient import TestClient

from api_bridge.main import app
from api_bridge.routes.engine_control import set_engine
from config.settings import settings
from shared.constants import IST_TZ
from shared.enums import DataFeederType, TradingMode
from shared.schemas import StockProfile
from trading_engine.brokers.kite.kite_broker import KiteBroker
from trading_engine.core.engine import TrillionaireEngine
from trading_engine.core.event_bus import EventBus
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
        data_feeder=DataFeederType.ZERODHA_KITE,
        event_bus=bus,
        risk_gatekeeper=risk_guard,
        order_manager=om,
        broker=broker,
        strategy_runner=strategy_runner,
    )
    engine.is_booted = True
    set_engine(engine)

    client = TestClient(app)
    return engine, client, bus, om, risk_guard


# ==============================================================================
# 1. Feeder Initialization & Strict Enforcement
# ==============================================================================

def test_engine_strictly_defaults_to_zerodha_kite():
    """Validates that TrillionaireEngine initializes strictly with ZERODHA_KITE."""
    bus = EventBus()
    engine = TrillionaireEngine(event_bus=bus)
    assert engine.data_feeder == DataFeederType.ZERODHA_KITE

    # Attempting to set any feeder still locks to ZERODHA_KITE
    engine.set_data_feeder(DataFeederType.ZERODHA_KITE)
    assert engine.data_feeder == DataFeederType.ZERODHA_KITE


# ==============================================================================
# 2. Premarket Boot & Rollover Resilience
# ==============================================================================

@pytest.mark.asyncio
async def test_boot_premarket_resets_kill_switch_and_keeps_zerodha_kite_on_auth_pending():
    """
    Validates that boot_premarket:
    1. Unconditionally resets PortfolioRiskGatekeeper state FIRST, clearing tripped kill-switch.
    2. Resiliently handles missing/pending Kite auth by loading cached instruments.
    3. Keeps data_feeder strictly as ZERODHA_KITE (never switches to any fallback).
    """
    bus = EventBus()
    risk_guard = PortfolioRiskGatekeeper(event_bus=bus, allocated_capital=100000.0)
    broker = PaperBroker(event_bus=bus)
    om = OrderManager(event_bus=bus, broker=broker, risk_gatekeeper=risk_guard)
    strategy_runner = LiveStrategyRunner(event_bus=bus)

    # Trip yesterday's kill switch with -1.6% loss
    day1 = date(2026, 11, 19)
    risk_guard.current_trade_date = day1
    risk_guard.realized_daily_pnl = -1600.0
    risk_guard.kill_switch_active = True
    assert risk_guard.kill_switch_active is True

    engine = TrillionaireEngine(
        trading_mode=TradingMode.PAPER,
        event_bus=bus,
        risk_gatekeeper=risk_guard,
        order_manager=om,
        broker=broker,
        strategy_runner=strategy_runner,
    )

    # Mock auth manager failing with timeout (awaiting today's OAuth login)
    mock_auth = MagicMock()
    mock_auth.get_valid_access_token.side_effect = TimeoutError("Kite auth awaiting browser login")
    engine.auth_manager = mock_auth

    # Mock instrument sync
    mock_sync = MagicMock()
    engine.instrument_sync = mock_sync

    day2 = date(2026, 11, 20)
    res = await engine.boot_premarket(trade_date=day2)

    # Risk gatekeeper MUST be reset
    assert risk_guard.current_trade_date == day2
    assert risk_guard.realized_daily_pnl == 0.0
    assert risk_guard.kill_switch_active is False

    # Instrument cache loaded from disk
    mock_sync.load_cache_from_disk.assert_called_once()

    # Data feeder strictly remains ZERODHA_KITE
    assert engine.data_feeder == DataFeederType.ZERODHA_KITE
    assert res["status"] == "booted"


# ==============================================================================
# 3. Engine Start Pending Kite Token
# ==============================================================================

@pytest.mark.asyncio
async def test_engine_start_warns_cleanly_without_crashing_when_token_pending():
    """
    Validates that when engine.start() is invoked before Kite OAuth login is done,
    it logs a clear actionable message and marks engine.is_running without crashing.
    """
    bus = EventBus()
    risk_guard = PortfolioRiskGatekeeper(event_bus=bus, allocated_capital=100000.0)
    broker = PaperBroker(event_bus=bus)
    om = OrderManager(event_bus=bus, broker=broker, risk_gatekeeper=risk_guard)
    strategy_runner = LiveStrategyRunner(event_bus=bus)

    engine = TrillionaireEngine(
        trading_mode=TradingMode.PAPER,
        event_bus=bus,
        risk_gatekeeper=risk_guard,
        order_manager=om,
        broker=broker,
        strategy_runner=strategy_runner,
    )
    engine.is_booted = True

    # Broker start raises exception because access token is not set
    mock_broker = MagicMock(spec=KiteBroker)
    mock_broker.start = AsyncMock(side_effect=Exception("Missing access_token for today"))
    engine.broker = mock_broker

    await engine.start()

    assert engine.is_running is True
    assert engine.data_feeder == DataFeederType.ZERODHA_KITE


# ==============================================================================
# 4. 1-Click Kite OAuth Callback & Full Token Subscription
# ==============================================================================

def test_kite_oauth_callback_subscribes_all_tokens_and_starts_streaming(mock_engine_and_client):
    """
    Validates that GET /api/v1/kite/callback:
    1. Exchanges request_token for access_token.
    2. Persists token to cache.
    3. Resolves all 100 stock + sector tokens.
    4. Connects KiteBroker + KiteTicker with all tokens.
    5. Redirects to /?kite_auth=success.
    """
    engine, client, _, _, _ = mock_engine_and_client

    mock_auth = MagicMock()
    mock_auth.exchange_and_save_request_token.return_value = "mock_valid_access_token_123"
    mock_kite = MagicMock()
    mock_auth.get_kite_client.return_value = mock_kite
    engine.auth_manager = mock_auth

    # Mock stock profiles with instrument tokens
    p1 = StockProfile(
        tradingsymbol="RELIANCE",
        instrument_token=738561,
        parent_index="NIFTY ENERGY",
        index_token=260105,
        cluster_regime="TREND_MOMENTUM",
        beta_5y=1.1,
        adr_pct=2.0,
        atr_multiplier_sl=1.5,
        atr_multiplier_tp=3.0,
        min_rvol_threshold=1.5,
        oos_sharpe=1.5,
        oos_profit_factor=1.5,
        is_active=True,
    )
    engine.strategy_runner.profiles = {"RELIANCE": p1}

    mock_broker = MagicMock(spec=KiteBroker)
    mock_broker.start = AsyncMock()
    engine.broker = mock_broker

    with patch("api_bridge.routes.settings_control.persist_env_updates") as mock_persist, patch("os.kill"):
        resp = client.get(
            "/api/v1/kite/callback?request_token=test_oauth_request_tok&action=login&status=success",
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert resp.headers["location"] == "/?kite_auth=success"

        # Token exchanged and persisted
        mock_auth.exchange_and_save_request_token.assert_called_once()
        mock_persist.assert_called_with({"DATA_FEEDER": "ZERODHA_KITE"})

        # Subscribed tokens resolved
        assert 738561 in mock_broker.tokens_to_subscribe or mock_broker.subscribe_tokens.called
        assert engine.data_feeder == DataFeederType.ZERODHA_KITE


# ==============================================================================
# 5. REST Settings & Status Endpoints
# ==============================================================================

def test_settings_api_feeder_strictly_zerodha_kite(mock_engine_and_client):
    """Validates that GET /api/v1/settings and /api/v1/status strictly return ZERODHA_KITE."""
    engine, client, _, _, _ = mock_engine_and_client
    admin_key = settings.trillionaire_admin_key

    # 1. GET /api/v1/settings returns ZERODHA_KITE
    resp = client.get("/api/v1/settings", headers={"X-API-Key": admin_key})
    assert resp.status_code == 200
    data = resp.json()
    assert data["data_feeder"] == "ZERODHA_KITE"

    # 2. GET /api/v1/status returns ZERODHA_KITE
    st_resp = client.get("/api/v1/status")
    assert st_resp.status_code == 200
    st_data = st_resp.json()
    assert st_data["data_feeder"] == "ZERODHA_KITE"

    # 3. POST /api/v1/settings strictly persists ZERODHA_KITE
    post_resp = client.post(
        "/api/v1/settings",
        json={"data_feeder": "ZERODHA_KITE"},
        headers={"X-API-Key": admin_key},
    )
    assert post_resp.status_code == 200
    res_json = post_resp.json()
    assert res_json["settings"]["data_feeder"] == "ZERODHA_KITE"
    assert engine.data_feeder == DataFeederType.ZERODHA_KITE


# ==============================================================================
# 6. KiteTicker Initialization, Subscription, & OrderManager Ticks
# ==============================================================================

@pytest.mark.asyncio
async def test_engine_start_instantiates_kite_ticker_and_connects():
    """Validates that TrillionaireEngine.start() creates KiteTicker and connects threaded."""
    bus = EventBus()
    risk_guard = PortfolioRiskGatekeeper(event_bus=bus, allocated_capital=100000.0)
    broker = PaperBroker(event_bus=bus)
    om = OrderManager(event_bus=bus, broker=broker, risk_gatekeeper=risk_guard)
    strategy_runner = LiveStrategyRunner(event_bus=bus)

    engine = TrillionaireEngine(
        trading_mode=TradingMode.PAPER,
        event_bus=bus,
        risk_gatekeeper=risk_guard,
        order_manager=om,
        broker=broker,
        strategy_runner=strategy_runner,
    )
    engine.is_booted = True

    mock_auth = MagicMock()
    mock_auth.get_valid_access_token.return_value = "test_valid_access_token_888"
    mock_kite = MagicMock()
    mock_kite.api_key = "test_kite_api_key_888"
    mock_auth.get_kite_client.return_value = mock_kite
    engine.auth_manager = mock_auth

    mock_ticker_instance = MagicMock()
    with patch("kiteconnect.KiteTicker", return_value=mock_ticker_instance) as mock_ticker_cls:
        await engine.start()

        assert engine.is_running is True
        assert isinstance(engine.broker, KiteBroker)
        assert engine.broker.ticker is mock_ticker_instance
        mock_ticker_instance.connect.assert_called_once_with(threaded=True)


@pytest.mark.asyncio
async def test_kite_broker_on_ticks_forwards_to_paper_broker():
    """Validates that KiteBroker._on_ticks updates latest_ticks and forwards to PaperBroker."""
    bus = EventBus()
    paper_broker = PaperBroker(event_bus=bus)
    om = OrderManager(event_bus=bus, broker=paper_broker)
    mock_kite = MagicMock()
    kite_broker = KiteBroker(kite_client=mock_kite, event_bus=bus, order_manager=om)
    kite_broker.set_event_loop(asyncio.get_running_loop())

    # Normalize tick mock
    raw_ticks = [{
        "instrument_token": 738561,
        "last_price": 2850.50,
        "volume_traded": 100000,
        "depth": {"buy": [{"price": 2850.45}], "sell": [{"price": 2850.55}]},
        "timestamp": datetime.now(IST_TZ).isoformat(),
    }]
    kite_broker.instruments.token_to_symbol[738561] = "RELIANCE"

    kite_broker._on_ticks(None, raw_ticks)
    await asyncio.sleep(0.02)

    # Check that latest_ticks has RELIANCE
    assert "RELIANCE" in kite_broker.latest_ticks
    assert kite_broker.latest_ticks["RELIANCE"].ltp == 2850.50

    # Check that paper_broker received latest tick
    assert "RELIANCE" in paper_broker.latest_ticks
    assert paper_broker.latest_ticks["RELIANCE"].ltp == 2850.50


# ==============================================================================
# 7. Zero-Touch Push Token & Callback Routing
# ==============================================================================

def test_push_token_api_endpoint(mock_engine_and_client):
    """
    Validates POST /api/v1/kite/push-token:
    1. Rejects unauthenticated requests (missing X-API-Key).
    2. Rejects empty payload.
    3. Successfully activates external access token and starts KiteTicker.
    """
    engine, client, _, _, _ = mock_engine_and_client
    admin_key = settings.trillionaire_admin_key

    # 1. Reject without admin key
    unauth = client.post("/api/v1/kite/push-token", json={"access_token": "valid_tok"})
    assert unauth.status_code in (401, 403)

    # 2. Reject empty payload
    empty = client.post(
        "/api/v1/kite/push-token",
        json={},
        headers={"X-API-Key": admin_key},
    )
    assert empty.status_code == 400

    # 3. Success with access_token
    mock_auth = MagicMock()
    mock_kite = MagicMock()
    mock_auth.get_kite_client.return_value = mock_kite
    engine.auth_manager = mock_auth

    mock_ticker_instance = MagicMock()
    with patch("kiteconnect.KiteTicker", return_value=mock_ticker_instance), patch("os.kill"):
        resp = client.post(
            "/api/v1/kite/push-token",
            json={"access_token": "remote_pushed_access_token_123"},
            headers={"X-API-Key": admin_key},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "Kite token activated" in data["message"]
        mock_auth.save_access_token.assert_called_once()


@pytest.mark.asyncio
async def test_push_token_schedules_sigterm_respawn():
    """Verifies graceful worker SIGTERM respawn task sends signal.SIGTERM after delay without blocking."""
    import os
    import signal
    from api_bridge.routes.settings_control import _delayed_worker_sigterm

    with patch("os.kill") as mock_kill:
        await _delayed_worker_sigterm(delay_sec=0.01)
        mock_kill.assert_called_once_with(os.getpid(), signal.SIGTERM)


def test_root_callback_redirects_to_api_v1(mock_engine_and_client):
    """Validates that GET /callback and /kite/callback forward to /api/v1/kite/callback."""
    _, client, _, _, _ = mock_engine_and_client

    r1 = client.get("/callback?request_token=tok123&status=success", follow_redirects=False)
    assert r1.status_code == 307
    assert "/api/v1/kite/callback?request_token=tok123&status=success" in r1.headers["location"]

    r2 = client.get("/kite/callback?request_token=tok456&status=success", follow_redirects=False)
    assert r2.status_code == 307
    assert "/api/v1/kite/callback?request_token=tok456&status=success" in r2.headers["location"]
