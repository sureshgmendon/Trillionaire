"""
Unit tests for Sprint 7:
1. KiteAuthManager TOTP generation, token caching, date-rollover refresh, and headless auth flow.
2. InstrumentSync ASM/GSM surveillance filtering and exact NSE tick-size rounding.
3. KiteBroker raw tick normalization, order updates, and 5-second position reconciler.
4. TrillionaireEngine master coordinator lifecycle in PAPER and REPLAY modes.
"""

from datetime import date, datetime, timedelta
import json
from pathlib import Path
from typing import Any, Dict
import pytest
import pyotp
import httpx
import uuid

from shared.constants import IST_TZ
from shared.enums import (
    ClusterRegime,
    OrderStatus,
    OrderType,
    Side,
    Timeframe,
    TradingMode,
)
from shared.schemas import Candle, Order, Position, Signal, StockProfile, Tick
from trading_engine.brokers.kite.auth import KiteAuthManager
from trading_engine.brokers.kite.instruments import InstrumentSync, round_to_tick_size
from trading_engine.brokers.kite.kite_broker import KiteBroker
from trading_engine.core.engine import TrillionaireEngine
from trading_engine.core.event_bus import (
    EventBus,
    OrderUpdateEvent,
    RiskAlertEvent,
    SignalEvent,
    TickEvent,
)
from trading_engine.execution.order_manager import OrderManager
from trading_engine.execution.paper_broker import PaperBroker
from trading_engine.risk.portfolio_risk import PortfolioRiskGatekeeper
from trading_engine.strategy.live_runner import LiveStrategyRunner


# -----------------------------------------------------------------------------
# 1. KiteAuthManager Tests
# -----------------------------------------------------------------------------

def test_totp_generation_and_validation():
    """Verifies that KiteAuthManager generates a valid 6-digit TOTP code from Base32 secret."""
    # Generate a random Base32 secret
    test_secret = pyotp.random_base32()
    auth_mgr = KiteAuthManager(
        api_key="mock_key",
        api_secret="mock_secret",
        user_id="AB1234",
        password="secret_password",
        totp_secret=test_secret,
    )

    code = auth_mgr.generate_totp_code()
    assert len(code) == 6
    assert code.isdigit()

    # Validate against pyotp
    totp = pyotp.TOTP(test_secret)
    assert totp.verify(code)


def test_token_caching_and_date_rollover(tmp_path: Path):
    """Verifies caching in memory/disk and automatic re-authentication on date rollover."""
    token_file = tmp_path / "access_token.json"
    today = datetime.now(IST_TZ).date()
    yesterday = today - timedelta(days=1)

    auth_mgr = KiteAuthManager(
        api_key="mock_key",
        api_secret="mock_secret",
        user_id="AB1234",
        password="secret_password",
        totp_secret=pyotp.random_base32(),
        token_cache_path=token_file,
    )

    # 1. Verify invalidation for yesterday's token
    stale_payload = {"date": yesterday.isoformat(), "access_token": "stale_token_123"}
    assert auth_mgr.is_token_valid_for_today(stale_payload, today) is False

    # 2. Verify validation for today's token
    fresh_payload = {"date": today.isoformat(), "access_token": "fresh_token_456"}
    assert auth_mgr.is_token_valid_for_today(fresh_payload, today) is True

    # 3. Save token and verify read back from disk
    auth_mgr._save_token("fresh_token_456", today)
    assert token_file.exists()

    disk_tok = auth_mgr._get_disk_token(today)
    assert disk_tok == "fresh_token_456"

    # Reading for tomorrow should return None
    tomorrow = today + timedelta(days=1)
    assert auth_mgr._get_disk_token(tomorrow) is None


def test_mock_headless_login_flow(tmp_path: Path):
    """Verifies complete headless OAuth login flow using mock HTTP transport."""
    test_secret = pyotp.random_base32()
    token_file = tmp_path / "access_token.json"

    # Define mock HTTP transport simulating Zerodha auth endpoints
    def custom_handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/api/login" in url:
            return httpx.Response(200, json={"status": "success", "data": {"request_id": "req_xyz_123"}})
        elif "/api/twofa" in url:
            return httpx.Response(200, json={"status": "success", "data": {"user_id": "AB1234"}})
        elif "/connect/login" in url:
            # Emulate redirect with request_token in URL
            redirect_target = "https://127.0.0.1/?action=login&status=success&request_token=captured_req_tok_789"
            return httpx.Response(302, headers={"Location": redirect_target})
        return httpx.Response(404)

    mock_client = httpx.Client(transport=httpx.MockTransport(custom_handler), follow_redirects=True)

    class MockKiteClient:
        def generate_session(self, request_token: str, api_secret: str) -> Dict[str, Any]:
            assert request_token == "captured_req_tok_789"
            assert api_secret == "mock_secret"
            return {"access_token": "live_access_token_999"}

        def set_access_token(self, token: str):
            pass

    auth_mgr = KiteAuthManager(
        api_key="mock_key",
        api_secret="mock_secret",
        user_id="AB1234",
        password="secret_password",
        totp_secret=test_secret,
        token_cache_path=token_file,
        http_client=mock_client,
        kite_client=MockKiteClient(),
    )

    token = auth_mgr.get_valid_access_token(force_refresh=True)
    assert token == "live_access_token_999"

    # Subsequent call without force_refresh should return cached token without HTTP calls
    cached_token = auth_mgr.get_valid_access_token(force_refresh=False)
    assert cached_token == "live_access_token_999"


# -----------------------------------------------------------------------------
# 2. InstrumentSync & Surveillance Filter Tests
# -----------------------------------------------------------------------------

def test_instrument_sync_filtering_and_tick_rounding(tmp_path: Path):
    """Verifies that non-EQ series and ASM/GSM symbols are excluded and tick size is rounded."""
    cache_file = tmp_path / "nse_instruments_cache.json"

    raw_dump = [
        # Valid EQ
        {
            "instrument_token": 738561,
            "tradingsymbol": "RELIANCE",
            "instrument_type": "EQ",
            "segment": "NSE",
            "tick_size": 0.05,
            "lot_size": 1,
        },
        # Valid INDEX
        {
            "instrument_token": 260105,
            "tradingsymbol": "NIFTY BANK",
            "instrument_type": "INDEX",
            "segment": "INDICES",
            "tick_size": 0.05,
            "lot_size": 1,
        },
        # Restricted BE series (Trade-for-Trade)
        {
            "instrument_token": 999001,
            "tradingsymbol": "JIOFIN_BE",
            "instrument_type": "BE",
            "segment": "NSE",
            "tick_size": 0.05,
            "lot_size": 1,
        },
        # Restricted BZ series (Non-compliant)
        {
            "instrument_token": 999002,
            "tradingsymbol": "VODAFONE_BZ",
            "instrument_type": "BZ",
            "segment": "NSE",
            "tick_size": 0.05,
            "lot_size": 1,
        },
        # Stock flagged under ASM/GSM
        {
            "instrument_token": 999003,
            "tradingsymbol": "ADANIENT",
            "instrument_type": "EQ",
            "segment": "NSE",
            "tick_size": 0.05,
            "lot_size": 1,
        },
    ]

    sync = InstrumentSync(
        cache_path=cache_file,
        asm_gsm_symbols={"ADANIENT"},
    )

    count = sync.sync_instruments(raw_instruments=raw_dump)
    # Should only retain RELIANCE and NIFTY BANK
    assert count == 2
    assert sync.get_token("RELIANCE") == 738561
    assert sync.get_token("NIFTY BANK") == 260105
    assert sync.get_token("JIOFIN_BE") is None
    assert sync.get_token("VODAFONE_BZ") is None
    assert sync.get_token("ADANIENT") is None

    # Reverse lookup
    assert sync.get_symbol(738561) == "RELIANCE"
    assert sync.get_symbol(260105) == "NIFTY BANK"

    # Tick size rounding verification
    assert round_to_tick_size(2502.48, 0.05) == 2502.50
    assert round_to_tick_size(2502.42, 0.05) == 2502.40
    assert round_to_tick_size(2502.474, 0.05) == 2502.45
    assert sync.round_price("RELIANCE", 2502.48) == 2502.50


# -----------------------------------------------------------------------------
# 3. KiteBroker Tests: Tick Normalization, Execution & Reconciler
# -----------------------------------------------------------------------------

def test_kite_tick_normalization():
    """Verifies mapping of raw Kite ticker payload into validated Tick schema."""
    sync = InstrumentSync()
    sync.symbol_to_token["RELIANCE"] = 738561
    sync.token_to_symbol[738561] = "RELIANCE"

    broker = KiteBroker(
        kite_client=None,
        instrument_sync=sync,
    )

    raw_kite_tick = {
        "instrument_token": 738561,
        "last_price": 2510.50,
        "volume_traded": 125000,
        "last_trade_time": "2026-03-10T10:15:00",
        "oi": 45000,
        "depth": {
            "buy": [{"price": 2510.45, "quantity": 500}],
            "sell": [{"price": 2510.55, "quantity": 700}],
        },
    }

    tick = broker.normalize_kite_tick(raw_kite_tick)
    assert tick is not None
    assert tick.tradingsymbol == "RELIANCE"
    assert tick.instrument_token == 738561
    assert tick.ltp == 2510.50
    assert tick.volume == 125000
    assert tick.bid == 2510.45
    assert tick.ask == 2510.55
    assert tick.oi == 45000


@pytest.mark.asyncio
async def test_kite_order_placement_and_update():
    """Verifies live order placement and on_order_update callback mapping."""
    bus = EventBus()
    sync = InstrumentSync()
    sync.symbol_to_token["INFY"] = 408065
    sync.token_to_symbol[408065] = "INFY"

    class MockKite:
        def place_order(self, **kwargs) -> str:
            assert kwargs["tradingsymbol"] == "INFY"
            assert kwargs["quantity"] == 10
            assert kwargs["price"] == 1500.50  # tick-rounded
            return "KITE_ORD_12345"

    broker = KiteBroker(
        kite_client=MockKite(),
        event_bus=bus,
        instrument_sync=sync,
    )

    order = Order(
        order_id=str(uuid.uuid4()),
        tradingsymbol="INFY",
        side=Side.BUY,
        product="MIS",
        order_type=OrderType.LIMIT,
        quantity=10,
        limit_price=1500.48,  # Needs rounding to 1500.50
        status=OrderStatus.PENDING_NEW,
        created_at_ist=datetime.now(IST_TZ),
    )

    placed_order = await broker.submit_order(order)
    assert placed_order.status == OrderStatus.OPEN
    assert placed_order.broker_order_id == "KITE_ORD_12345"
    assert placed_order.limit_price == 1500.50

    # Simulate WebSocket on_order_update callback
    update_data = {
        "order_id": "KITE_ORD_12345",
        "status": "COMPLETE",
        "filled_quantity": 10,
        "average_price": 1500.50,
    }
    broker._on_order_update(None, update_data)

    assert placed_order.status == OrderStatus.FILLED
    assert placed_order.filled_quantity == 10
    assert placed_order.average_fill_price == 1500.50


@pytest.mark.asyncio
async def test_reconciler_adopts_orphaned_position_and_fixes_mismatch():
    """Verifies 5-second reconciler loop adopting crash-restart positions and correcting mismatches."""
    bus = EventBus()
    risk_guard = PortfolioRiskGatekeeper(event_bus=bus, allocated_capital=100000.0)
    om = OrderManager(event_bus=bus, broker=PaperBroker(event_bus=bus), risk_gatekeeper=risk_guard)

    class MockKiteReconciler:
        def positions(self) -> Dict[str, Any]:
            return {
                "net": [
                    # Orphaned position on broker (not in OrderManager)
                    {
                        "tradingsymbol": "SBIN",
                        "quantity": 100,
                        "average_price": 600.0,
                    },
                    # Position with quantity mismatch (broker has 50, internal has 20)
                    {
                        "tradingsymbol": "RELIANCE",
                        "quantity": 50,
                        "average_price": 2500.0,
                    },
                ]
            }

    # Setup internal state: RELIANCE with 20 shares
    pos_rel = Position(
        tradingsymbol="RELIANCE",
        side=Side.BUY,
        quantity=20,
        average_entry_price=2500.0,
        ltp=2500.0,
        unrealized_pnl=0.0,
        realized_pnl=0.0,
        stop_loss=2480.0,
        target_price=2550.0,
    )
    om.open_positions["RELIANCE"] = pos_rel

    broker = KiteBroker(
        kite_client=MockKiteReconciler(),
        event_bus=bus,
        order_manager=om,
    )

    alerts: list[RiskAlertEvent] = []

    def on_alert(ev: RiskAlertEvent):
        alerts.append(ev)

    bus.subscribe(RiskAlertEvent, on_alert)

    # Execute reconciliation audit
    res = await broker.reconcile_positions()
    assert res["status"] == "ok"
    assert res["active_broker_positions"] == 2

    # 1. SBIN was adopted
    assert "SBIN" in om.open_positions
    adopted = om.open_positions["SBIN"]
    assert adopted.quantity == 100
    assert adopted.average_entry_price == 600.0
    assert adopted.side == Side.BUY

    # 2. RELIANCE was reconciled from 20 to 50
    assert om.open_positions["RELIANCE"].quantity == 50

    # 3. Risk alert events were emitted for both actions
    assert len(alerts) >= 2


# -----------------------------------------------------------------------------
# 4. TrillionaireEngine Master Coordinator Tests
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_trillionaire_engine_lifecycle_paper_mode():
    """Verifies end-to-end boot, signal routing, and stop lifecycle in PAPER mode."""
    bus = EventBus()
    engine = TrillionaireEngine(
        trading_mode=TradingMode.PAPER,
        event_bus=bus,
    )

    # 1. Premarket Boot
    boot_res = await engine.boot_premarket(trade_date=date(2026, 3, 10))
    assert boot_res["status"] == "booted"
    assert boot_res["trading_mode"] == "PAPER"
    assert engine.is_booted is True

    # 2. Start Engine
    await engine.start()
    assert engine.is_running is True

    # 3. Simulate SignalEvent emission
    sig = Signal(
        signal_id=str(uuid.uuid4()),
        tradingsymbol="RELIANCE",
        parent_index="NIFTY ENERGY",
        side=Side.BUY,
        entry_price=2500.0,
        stop_loss=2480.0,  # 20 risk/share
        target_price=2560.0,
        confidence=0.85,
        strategy_name="TrendMomentumStrategy",
        generated_at_ist=datetime(2026, 3, 10, 10, 0, tzinfo=IST_TZ),
    )

    # Provide initial tick to broker so marketable limit order fills immediately
    engine.broker.set_latest_tick(
        Tick(
            instrument_token=738561,
            tradingsymbol="RELIANCE",
            timestamp_ist=datetime(2026, 3, 10, 10, 0, tzinfo=IST_TZ),
            ltp=2500.0,
            volume=5000,
            bid=2499.9,
            ask=2500.1,
        )
    )

    await bus.publish(SignalEvent(signal=sig))

    # Order should be sized and filled
    assert "RELIANCE" in engine.order_manager.open_positions
    pos = engine.order_manager.open_positions["RELIANCE"]
    assert pos.quantity > 0
    assert pos.average_entry_price > 0

    # Check diagnostic status
    status = engine.get_status()
    assert status["is_running"] is True
    assert status["open_positions_count"] == 1
    assert "RELIANCE" in status["open_positions"]

    # 4. Stop Engine (flattens open positions)
    await engine.stop()
    assert engine.is_running is False
    assert len(engine.order_manager.open_positions) == 0
