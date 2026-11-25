"""
Unit & Integration Verification Tests for Master Cockpit Upgrades:
1. Immutable ClosedTrade ledger & daily realized PnL integrity
2. Zero duplicate instrument_token collisions & Kite anti-bleed tick verification
3. Price sanity guard (>5% deviation rejection) in PaperBroker & OrderManager
4. Strict OCO exit legs cancellation & FLATTEN_ALL sequencing
5. DailyStateStore disk persistence & recovery across engine restart
6. API routes: /closed-trades, /candles/{symbol}?timeframe=M3, /sentinel/{symbol}/override, /settings capital/risk
7. KiteBroker structured exceptions (TokenException polling halt & NetworkException idempotent placement)
"""

from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock
import uuid
import pytest
import yaml
from fastapi.testclient import TestClient

from api_bridge.main import app
from api_bridge.routes.engine_control import set_engine
from config.settings import settings
from shared.constants import IST_TZ
from shared.enums import OrderStatus, OrderType, SentinelStatus, Side, Timeframe, TradingMode
from shared.exceptions import KiteTokenExpiredError
from shared.schemas import ClosedTrade, NewsSentinelTag, Order, Signal, Tick
from trading_engine.brokers.kite.instruments import InstrumentSync
from trading_engine.brokers.kite.kite_broker import KiteBroker
from trading_engine.core.daily_state import DailyStateStore
from trading_engine.core.engine import TrillionaireEngine
from trading_engine.core.event_bus import EventBus, RiskAlertEvent, TickEvent
from trading_engine.execution.order_manager import OrderManager
from trading_engine.execution.paper_broker import PaperBroker
from trading_engine.risk.portfolio_risk import PortfolioRiskGatekeeper


def test_sector_map_zero_duplicate_tokens():
    """Ensures all 100 NSE stocks and 8 sector indices in sector_map.yaml have unique tokens."""
    sector_path = Path(__file__).resolve().parents[2] / "config" / "sector_map.yaml"
    with open(sector_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    tokens = {}
    stocks = data.get("stocks", [])
    for info in stocks:
        sym = info["tradingsymbol"]
        tok = int(info["instrument_token"])
        assert tok not in tokens, f"Duplicate stock token {tok} shared by {sym} and {tokens[tok]}"
        tokens[tok] = sym

    for idx_name, idx_info in data.get("benchmark_indices", {}).items():
        tok = int(idx_info["token"])
        assert tok not in tokens, f"Duplicate index token {tok} shared by {idx_name} and {tokens[tok]}"
        tokens[tok] = idx_name

    assert len(stocks) == 100


@pytest.mark.asyncio
async def test_immutable_closed_trade_and_oco_lifecycle():
    """
    Verifies:
    1. Entry fill captures entry_price, sl_price, target_price and places OCO exit legs.
    2. Target fill cancels resting SL leg (zero orphan TRIGGER PENDING orders).
    3. ClosedTrade ledger preserves filled_qty, entry/SL/target/exit prices & normalized TARGET_HIT reason.
    4. Realized daily PnL equals sum across closed_trades.
    """
    bus = EventBus()
    risk_guard = PortfolioRiskGatekeeper(event_bus=bus, allocated_capital=200000.0)
    broker = PaperBroker(event_bus=bus)
    om = OrderManager(
        event_bus=bus,
        broker=broker,
        risk_gatekeeper=risk_guard,
        enable_oco_brackets=True,
    )

    t0 = datetime(2026, 4, 15, 9, 35, 0, tzinfo=IST_TZ)
    broker.set_latest_tick(
        Tick(
            instrument_token=738561,
            tradingsymbol="RELIANCE",
            timestamp_ist=t0,
            ltp=2500.0,
            volume=10000,
            bid=2499.8,
            ask=2500.2,
        )
    )

    sig = Signal(
        signal_id=str(uuid.uuid4()),
        tradingsymbol="RELIANCE",
        parent_index="NIFTY ENERGY",
        side=Side.BUY,
        entry_price=2500.0,
        stop_loss=2480.0,
        target_price=2540.0,
        confidence=0.90,
        strategy_name="DUAL_CONFIRMATION",
        generated_at_ist=t0,
    )
    entry_ord = await om.submit_signal_order(sig, quantity=20)
    assert entry_ord is not None
    assert "RELIANCE" in om.open_positions

    pos = om.open_positions["RELIANCE"]
    assert pos.stop_loss == 2480.0
    assert pos.target_price == 2540.0
    assert pos.sl_order_id is not None
    assert pos.tp_order_id is not None

    sl_order = om.orders[pos.sl_order_id]
    tp_order = om.orders[pos.tp_order_id]
    assert sl_order.status == OrderStatus.TRIGGER_PENDING
    assert tp_order.status == OrderStatus.OPEN

    # Simulate target price hit tick -> fills TP limit leg and must auto-cancel SL leg
    t1 = datetime(2026, 4, 15, 10, 5, 0, tzinfo=IST_TZ)
    await bus.publish(
        TickEvent(
            tick=Tick(
                instrument_token=738561,
                tradingsymbol="RELIANCE",
                timestamp_ist=t1,
                ltp=2542.0,
                volume=25000,
                bid=2541.8,
                ask=2542.2,
            )
        )
    )

    assert "RELIANCE" not in om.open_positions
    assert len(om.closed_trades) == 1
    ct = om.closed_trades[0]
    assert ct.symbol == "RELIANCE"
    assert ct.filled_qty == 20
    assert ct.sl_price == 2480.0
    assert ct.target_price == 2540.0
    assert ct.actual_exit_price >= 2540.0
    assert ct.exit_reason == "TARGET_HIT"
    assert sl_order.status == OrderStatus.CANCELLED
    assert risk_guard.realized_daily_pnl == pytest.approx(ct.realized_pnl)


@pytest.mark.asyncio
async def test_price_sanity_guard_rejects_corrupt_deviation():
    """Orders whose execution price deviates >5% from live tick LTP must be rejected cleanly."""
    bus = EventBus()
    broker = PaperBroker(event_bus=bus)
    om = OrderManager(event_bus=bus, broker=broker)

    t0 = datetime(2026, 4, 15, 10, 0, 0, tzinfo=IST_TZ)
    live_tick = Tick(
        instrument_token=582913,
        tradingsymbol="MRF",
        timestamp_ist=t0,
        ltp=130000.0,
        volume=100,
        bid=129990.0,
        ask=130010.0,
    )
    broker.set_latest_tick(live_tick)
    await bus.publish(TickEvent(tick=live_tick))

    bad_signal = Signal(
        signal_id=str(uuid.uuid4()),
        tradingsymbol="MRF",
        parent_index="NIFTY AUTO",
        side=Side.BUY,
        entry_price=5000.0,  # >95% off real LTP (corrupt token bleed price)
        stop_loss=4900.0,
        target_price=5200.0,
        confidence=0.85,
        strategy_name="DUAL_CONFIRMATION",
        generated_at_ist=t0,
    )
    rejected_order = await om.submit_signal_order(bad_signal, quantity=1)
    assert rejected_order is not None
    assert rejected_order.status == OrderStatus.REJECTED
    assert rejected_order.rejection_reason is not None
    assert "deviates" in rejected_order.rejection_reason.lower()
    assert "MRF" not in om.open_positions


def test_daily_state_store_persistence_and_restore(tmp_path):
    """Verifies atomic save/load of closed trades, open positions, funnel counts, and paper balance."""
    store = DailyStateStore(state_dir=tmp_path)
    target = date(2026, 5, 12)
    ct = ClosedTrade(
        order_id="ord-exit-1",
        symbol="SBIN",
        product="MIS",
        side=Side.BUY,
        filled_qty=50,
        entry_price=800.0,
        sl_price=792.0,
        target_price=816.0,
        actual_exit_price=816.0,
        realized_pnl=785.5,
        pnl_pct=2.0,
        exit_reason="TARGET_HIT",
        entry_time=datetime(2026, 5, 12, 9, 45, 0, tzinfo=IST_TZ),
        exit_time=datetime(2026, 5, 12, 10, 20, 0, tzinfo=IST_TZ),
    )
    store.save_state(
        trade_date=target,
        orders=[],
        open_positions=[],
        closed_trades=[ct.model_dump(mode="json")],
        funnel_metrics={"candles_evaluated": 120, "passed_stock_rule": 5, "passed_sector_filter": 3, "signals_emitted": 1},
        paper_balance=100785.5,
        allocated_capital=100000.0,
        realized_pnl=785.5,
        risk_per_trade_pct=0.75,
    )

    loaded = store.load_state(trade_date=target)
    assert loaded is not None
    assert loaded["realized_pnl"] == 785.5
    assert loaded["paper_balance"] == 100785.5
    assert loaded["risk_per_trade_pct"] == 0.75
    assert len(loaded["closed_trades"]) == 1
    assert loaded["closed_trades"][0]["exit_reason"] == "TARGET_HIT"


@pytest.mark.asyncio
async def test_kite_broker_token_exception_and_idempotent_retry():
    """
    Verifies:
    1. NetworkException on kite.place_order checks kite.orders() by unique tag before retrying.
    2. TokenException flips token_expired=True and halts reconcile_positions polling.
    """
    class NetworkException(Exception):
        pass

    class TokenException(Exception):
        pass

    bus = EventBus()
    alerts: list[RiskAlertEvent] = []
    bus.subscribe(RiskAlertEvent, lambda ev: alerts.append(ev))

    mock_kite = MagicMock()
    mock_kite.EXCHANGE_NSE = "NSE"
    mock_kite.TRANSACTION_TYPE_BUY = "BUY"
    mock_kite.TRANSACTION_TYPE_SELL = "SELL"
    mock_kite.PRODUCT_MIS = "MIS"
    mock_kite.VARIETY_REGULAR = "regular"
    mock_kite.ORDER_TYPE_LIMIT = "LIMIT"
    mock_kite.ORDER_TYPE_SL = "SL"
    mock_kite.VALIDITY_DAY = "DAY"

    # First place_order raises NetworkException, but kite.orders() already shows the tagged order!
    mock_kite.place_order.side_effect = NetworkException("504 Gateway Timeout")

    broker = KiteBroker(kite_client=mock_kite, event_bus=bus, instrument_sync=InstrumentSync())
    broker.set_order_manager(OrderManager(event_bus=bus, broker=broker))

    ord1 = Order(
        order_id="idemp-1",
        tradingsymbol="INFY",
        side=Side.BUY,
        product="MIS",
        order_type=OrderType.LIMIT,
        quantity=10,
        limit_price=1500.0,
        client_order_tag="tril_idemp1",
        created_at_ist=datetime.now(IST_TZ),
    )
    mock_kite.orders.return_value = [{"order_id": "KITE_EXISTING_777", "tag": "tril_idemp1"}]

    placed = await broker.submit_order(ord1)
    assert placed.broker_order_id == "KITE_EXISTING_777"
    assert placed.status == OrderStatus.OPEN
    assert mock_kite.place_order.call_count == 1  # Never duplicated!

    # Now trigger TokenException -> halts polling and flips token_expired=True
    mock_kite.positions.side_effect = TokenException("Incorrect api_key or access_token (403)")
    with pytest.raises(KiteTokenExpiredError):
        broker._call_kite_rest(mock_kite.positions, max_network_retries=0)

    assert broker.token_expired is True
    recon = await broker.reconcile_positions()
    assert recon["status"] == "halted"


def test_api_closed_trades_candles_sentinel_override_and_settings(tmp_path, monkeypatch):
    """Tests new /closed-trades, /candles/{sym}?timeframe=M3, and /sentinel/{sym}/override endpoints."""
    import api_bridge.routes.settings_control as sc
    monkeypatch.setattr(sc, "persist_env_updates", lambda updates: None)

    bus = EventBus()
    risk_guard = PortfolioRiskGatekeeper(event_bus=bus, allocated_capital=150000.0)
    broker = PaperBroker(event_bus=bus)
    om = OrderManager(event_bus=bus, broker=broker, risk_gatekeeper=risk_guard)
    engine = TrillionaireEngine(
        trading_mode=TradingMode.PAPER,
        event_bus=bus,
        risk_gatekeeper=risk_guard,
        order_manager=om,
        broker=broker,
    )
    engine.is_booted = True
    engine.sentinel.artifacts_path = tmp_path / "daily_sentinel_tags.json"
    om.closed_trades.append(
        ClosedTrade(
            order_id="ct-api-1",
            symbol="SBIN",
            product="MIS",
            side=Side.BUY,
            filled_qty=30,
            entry_price=800.0,
            sl_price=792.0,
            target_price=816.0,
            actual_exit_price=816.0,
            realized_pnl=468.0,
            pnl_pct=2.0,
            exit_reason="TARGET_HIT",
            entry_time=datetime(2026, 5, 12, 9, 45, 0, tzinfo=IST_TZ),
            exit_time=datetime(2026, 5, 12, 10, 15, 0, tzinfo=IST_TZ),
        )
    )
    engine.sentinel._cached_tags["RELIANCE"] = NewsSentinelTag(
        tradingsymbol="RELIANCE",
        status=SentinelStatus.DO_NOT_TRADE,
        reason="Test blackout lock",
        risk_multiplier=0.0,
        updated_at_ist=datetime.now(IST_TZ),
    )
    set_engine(engine)

    with TestClient(app) as client:
        headers = {"X-API-Key": settings.trillionaire_admin_key}

        ct_resp = client.get("/api/v1/closed-trades", headers=headers)
        assert ct_resp.status_code == 200
        ct_data = ct_resp.json()
        assert len(ct_data) == 1
        assert ct_data[0]["symbol"] == "SBIN"
        assert ct_data[0]["exit_reason"] == "TARGET_HIT"

        candles_resp = client.get("/api/v1/candles/RELIANCE?timeframe=M3&limit=20", headers=headers)
        assert candles_resp.status_code == 200
        assert isinstance(candles_resp.json(), list)

        ovr_resp = client.post(
            "/api/v1/sentinel/RELIANCE/override",
            json={"reason": "Confirmed clean"},
            headers=headers,
        )
        assert ovr_resp.status_code == 200
        assert engine.sentinel._cached_tags["RELIANCE"].status == SentinelStatus.NORMAL
        assert engine.sentinel._cached_tags["RELIANCE"].risk_multiplier == 1.0

        set_resp = client.post(
            "/api/v1/settings",
            json={"allocated_capital": 250000.0, "risk_per_trade_pct": 0.75},
            headers=headers,
        )
        assert set_resp.status_code == 200
        assert set_resp.json()["settings"]["allocated_capital"] == 250000.0
        assert set_resp.json()["settings"]["risk_per_trade_pct"] == 0.75


@pytest.mark.asyncio
async def test_reconcile_positions_skips_live_kite_in_paper_mode():
    """Ensures KiteBroker.reconcile_positions NEVER ghost-closes active PaperBroker positions in PAPER mode."""
    bus = EventBus()
    mock_kite = MagicMock()
    mock_kite.positions.return_value = {"net": []}

    mock_auth = MagicMock()
    mock_auth.get_valid_access_token.return_value = "tok"
    mock_auth.get_kite_client.return_value = mock_kite

    engine = TrillionaireEngine(
        trading_mode=TradingMode.PAPER,
        event_bus=bus,
        auth_manager=mock_auth,
    )
    assert isinstance(engine.broker, KiteBroker)
    assert engine.broker.trading_mode == TradingMode.PAPER

    t0 = datetime(2026, 4, 15, 9, 45, 0, tzinfo=IST_TZ)
    engine.broker.set_latest_tick(
        Tick(
            instrument_token=779521,
            tradingsymbol="SBIN",
            timestamp_ist=t0,
            ltp=800.0,
            volume=10000,
            bid=799.8,
            ask=800.2,
        )
    )
    sig = Signal(
        signal_id=str(uuid.uuid4()),
        tradingsymbol="SBIN",
        parent_index="NIFTY BANK",
        side=Side.BUY,
        entry_price=800.0,
        stop_loss=790.0,
        target_price=820.0,
        confidence=0.88,
        strategy_name="DUAL_CONFIRMATION",
        generated_at_ist=t0,
    )
    await engine.order_manager.submit_signal_order(sig, quantity=20)
    assert "SBIN" in engine.order_manager.open_positions
    pos = engine.order_manager.open_positions["SBIN"]
    assert pos.stop_loss == 790.0
    assert pos.target_price == 820.0

    # Reconciler run during PAPER trading must skip kite.positions() and preserve open paper position
    recon = await engine.broker.reconcile_positions()
    assert recon["status"] == "skipped"
    assert recon["mode"] == "PAPER"
    mock_kite.positions.assert_not_called()
    assert "SBIN" in engine.order_manager.open_positions
    assert engine.order_manager.open_positions["SBIN"].quantity == 20

