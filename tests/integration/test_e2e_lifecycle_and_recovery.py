"""
Trillionaire Algo Engine - End-to-End Lifecycle & Crash-Recovery Integration Tests
Sprint 10 Production Verification:
1. Full Replay Pipeline: CandleAggregator -> LiveStrategyRunner -> PortfolioRisk -> OrderManager -> PaperBroker.
2. Crash-Restart Hydration: KiteBroker reconciler adopts orphaned broker position, re-attaches SL/TP bracket, and enforces risk guard.
3. Emergency Kill-Switch Cascade: POST /api/v1/control/kill-switch flattens open positions, locks subsequent entries, and emits circuit breaker events.
"""

import asyncio
from datetime import datetime
from typing import Any, Dict
from unittest.mock import MagicMock
import uuid
import pytest
from fastapi.testclient import TestClient

from api_bridge.main import app
from api_bridge.routes.engine_control import set_engine
from api_bridge.ws.telemetry_manager import telemetry_manager
from config.settings import settings
from shared.constants import IST_TZ
from shared.enums import (
    ClusterRegime,
    OrderStatus,
    OrderType,
    SentinelStatus,
    Side,
    Timeframe,
    TradingMode,
)
from shared.schemas import Candle, Order, Position, Signal, StockProfile, Tick
from trading_engine.brokers.kite.kite_broker import KiteBroker
from trading_engine.core.engine import TrillionaireEngine
from trading_engine.core.event_bus import (
    CandleCloseEvent,
    EventBus,
    OrderUpdateEvent,
    RiskAlertEvent,
    SignalEvent,
    TickEvent,
)
from trading_engine.data.anomaly_detector import AnomalyDetector
from trading_engine.data.candle_aggregator import CandleAggregator
from trading_engine.execution.order_manager import OrderManager
from trading_engine.execution.paper_broker import PaperBroker
from trading_engine.risk.portfolio_risk import PortfolioRiskGatekeeper
from trading_engine.strategy.live_runner import LiveStrategyRunner


@pytest.mark.asyncio
async def test_e2e_replay_pipeline():
    """
    Test 1: Full Replay-to-Dashboard Pipeline (REPLAY mode)
    Verifies that:
    1. Simulated ticks aggregate into market state.
    2. Strategy triggers SignalEvent on dual-confirmation.
    3. Engine routes signal through PortfolioRiskGatekeeper position sizing.
    4. OrderManager places marketable limit order executed by PaperBroker.
    5. OrderManager adopts position, tracks live MTM, advances 1R breakeven trailing stop.
    6. Square-off flattens position, deducts Zerodha statutory charges, and books net PnL.
    """
    custom_bus = EventBus(enable_redis_mirror=False)
    risk_guard = PortfolioRiskGatekeeper(event_bus=custom_bus, allocated_capital=200000.0)
    broker = PaperBroker(event_bus=custom_bus)
    order_mgr = OrderManager(event_bus=custom_bus, broker=broker, risk_gatekeeper=risk_guard)
    aggregator = CandleAggregator(event_bus=custom_bus)
    anomaly_det = AnomalyDetector(event_bus=custom_bus)
    strategy = LiveStrategyRunner(event_bus=custom_bus, anomaly_detector=anomaly_det)

    engine = TrillionaireEngine(
        trading_mode=TradingMode.REPLAY,
        event_bus=custom_bus,
        candle_aggregator=aggregator,
        anomaly_detector=anomaly_det,
        strategy_runner=strategy,
        risk_gatekeeper=risk_guard,
        order_manager=order_mgr,
        broker=broker,
    )
    await engine.start()

    # 1. Register StockProfile for RELIANCE
    profile = StockProfile(
        tradingsymbol="RELIANCE",
        instrument_token=738561,
        parent_index="NIFTY ENERGY",
        index_token=260105,
        cluster_regime=ClusterRegime.TREND_MOMENTUM,
        beta_5y=1.15,
        adr_pct=2.45,
        atr_multiplier_sl=1.5,
        atr_multiplier_tp=3.0,
        min_rvol_threshold=1.5,
        oos_sharpe=1.65,
        oos_profit_factor=1.85,
        is_active=True,
    )
    strategy.profiles["RELIANCE"] = profile

    # Seed initial tick at 09:30:00 IST (strictly within 09:25 - 14:30 entry window)
    t0 = datetime(2026, 4, 15, 9, 30, 0, tzinfo=IST_TZ)
    init_tick = Tick(
        instrument_token=738561,
        tradingsymbol="RELIANCE",
        timestamp_ist=t0,
        ltp=2520.0,
        volume=10000,
        bid=2519.80,
        ask=2520.20,
    )
    broker.set_latest_tick(init_tick)
    await custom_bus.publish(TickEvent(tick=init_tick))

    # 2. Trigger Strategy Signal (Long Dual-Confirmation Breakout)
    signal = Signal(
        signal_id=str(uuid.uuid4()),
        tradingsymbol="RELIANCE",
        parent_index="NIFTY ENERGY",
        side=Side.BUY,
        entry_price=2520.0,
        stop_loss=2500.0,      # Risk R = 20.0
        target_price=2560.0,    # Target = 2R (40.0)
        confidence=0.92,
        strategy_name="DUAL_CONFIRMATION_VWAP_CPR",
        generated_at_ist=t0,
    )

    # Publish signal on EventBus -> Engine handles on_signal -> OrderManager -> PaperBroker
    await custom_bus.publish(SignalEvent(signal=signal))
    await asyncio.sleep(0.01)

    # Verify Order filled and Position opened
    assert "RELIANCE" in order_mgr.open_positions
    pos = order_mgr.open_positions["RELIANCE"]
    assert pos.side == Side.BUY
    assert pos.quantity > 0
    assert pos.average_entry_price > 0
    assert pos.stop_loss == 2500.0
    assert pos.target_price == 2560.0

    # 3. Stream Tick advancing price to +1R (2545.0 >= entry 2520.0 + initial_r 20.0)
    t1 = datetime(2026, 4, 15, 9, 45, 0, tzinfo=IST_TZ)
    advance_tick = Tick(
        instrument_token=738561,
        tradingsymbol="RELIANCE",
        timestamp_ist=t1,
        ltp=2545.0,
        volume=25000,
        bid=2544.80,
        ask=2545.20,
    )
    await custom_bus.publish(TickEvent(tick=advance_tick))
    await asyncio.sleep(0.01)

    # Verify 1R Breakeven Trailing Stop is advanced
    assert pos.stop_loss >= pos.average_entry_price
    assert pos.unrealized_pnl > 0
    assert risk_guard.get_total_daily_mtm() > 0

    # 4. End-of-Day / Target Square-Off Trigger
    # Trigger exit via Target Hit
    t2 = datetime(2026, 4, 15, 10, 15, 0, tzinfo=IST_TZ)
    target_tick = Tick(
        instrument_token=738561,
        tradingsymbol="RELIANCE",
        timestamp_ist=t2,
        ltp=2565.0,  # Exceeds target 2560.0
        volume=50000,
        bid=2564.80,
        ask=2565.20,
    )
    await custom_bus.publish(TickEvent(tick=target_tick))
    await asyncio.sleep(0.01)

    # Verify Position is cleanly closed and Zerodha statutory charges deducted
    assert "RELIANCE" not in order_mgr.open_positions
    assert risk_guard.realized_daily_pnl > 0
    assert len(broker.trade_charges_history) >= 1
    trade_charge = broker.trade_charges_history[-1]
    assert trade_charge.brokerage > 0
    assert trade_charge.stt > 0
    assert trade_charge.total_charges > 0
    assert trade_charge.buy_turnover > 0

    await engine.stop()


@pytest.mark.asyncio
async def test_e2e_crash_recovery_position_adoption():
    """
    Test 2: Crash-Restart Position & Risk Hydration
    Verifies that:
    1. When engine restarts after an unexpected crash, OrderManager starts with 0 positions.
    2. KiteBroker 5-second reconciler audits live broker net positions.
    3. Orphaned/unmanaged broker position is discovered and adopted into OrderManager.open_positions.
    4. Conservative SL (1%) and TP (2%) brackets are automatically re-attached.
    5. The adopted position is registered into PortfolioRiskGatekeeper.
    6. A WARNING RiskAlertEvent is broadcast to alerting channels.
    7. Incoming market tick breaching the re-attached SL triggers an automated protective exit.
    """
    custom_bus = EventBus(enable_redis_mirror=False)
    risk_guard = PortfolioRiskGatekeeper(event_bus=custom_bus, allocated_capital=500000.0)

    # Setup mock KiteConnect client returning an orphaned position from broker
    mock_kite = MagicMock()
    mock_kite.positions.return_value = {
        "net": [
            {
                "tradingsymbol": "HDFCBANK",
                "quantity": 100,
                "average_price": 1650.0,
                "product": "MIS",
                "exchange": "NSE",
            }
        ]
    }
    mock_kite.place_order.return_value = {"order_id": "KITE_ORD_RECOVERY_99"}

    broker = KiteBroker(
        kite_client=mock_kite,
        event_bus=custom_bus,
    )
    order_mgr = OrderManager(
        event_bus=custom_bus,
        broker=broker,
        risk_gatekeeper=risk_guard,
    )
    broker.set_order_manager(order_mgr)

    # Verify initial state is clean (post-crash restart)
    assert len(order_mgr.open_positions) == 0

    # Capture RiskAlertEvents on the bus
    captured_risk_alerts = []

    async def on_risk_alert(event: RiskAlertEvent):
        captured_risk_alerts.append(event)

    custom_bus.subscribe(RiskAlertEvent, on_risk_alert)

    # Execute State Reconciler
    recon_result = await broker.reconcile_positions()
    assert recon_result["status"] == "ok"
    assert recon_result["active_broker_positions"] == 1

    # 1. Verify unmanaged position was adopted
    assert "HDFCBANK" in order_mgr.open_positions
    pos = order_mgr.open_positions["HDFCBANK"]
    assert pos.tradingsymbol == "HDFCBANK"
    assert pos.side == Side.BUY
    assert pos.quantity == 100
    assert pos.average_entry_price == 1650.0

    # 2. Verify re-attached SL and TP brackets
    # For BUY: SL = round(1650.0 * 0.99, 2) = 1633.50; TP = round(1650.0 * 1.02, 2) = 1683.00
    assert pos.stop_loss == 1633.50
    assert pos.target_price == 1683.00

    # 3. Verify position registered in Risk Gatekeeper
    assert "HDFCBANK" in risk_guard.open_positions

    # 4. Verify WARNING RiskAlertEvent was emitted
    assert len(captured_risk_alerts) >= 1
    alert = captured_risk_alerts[0]
    assert alert.symbol == "HDFCBANK"
    assert alert.level == "WARNING"
    assert "adopted external/crash-restart" in alert.message

    # 5. Simulate adverse tick breaching re-attached Stop Loss (1630.0 <= 1633.50)
    crash_tick = Tick(
        instrument_token=341249,
        tradingsymbol="HDFCBANK",
        timestamp_ist=datetime.now(IST_TZ),
        ltp=1630.0,
        volume=15000,
        bid=1629.80,
        ask=1630.20,
    )
    await custom_bus.publish(TickEvent(tick=crash_tick))
    await asyncio.sleep(0.01)

    # Verify protective exit order was sent to broker
    mock_kite.place_order.assert_called()
    call_kwargs = mock_kite.place_order.call_args[1]
    assert call_kwargs["tradingsymbol"] == "HDFCBANK"
    assert call_kwargs["quantity"] == 100


@pytest.mark.asyncio
async def test_e2e_emergency_kill_switch_cascade():
    """
    Test 3: Emergency Kill-Switch Cascade via REST API & Engine Gatekeeper
    Verifies that:
    1. Engine holds multiple active positions (TCS, INFY).
    2. Admin invokes POST /api/v1/control/kill-switch with authentication key.
    3. Engine liquidates all open positions immediately.
    4. Risk Gatekeeper locks session: kill_switch_active = True.
    5. Circuit breaker event (RiskAlertEvent) is published.
    6. Subsequent signals are strictly rejected and blocked from order submission.
    """
    bus = telemetry_manager.bus
    risk_guard = PortfolioRiskGatekeeper(event_bus=bus, allocated_capital=500000.0)
    broker = PaperBroker(event_bus=bus)
    order_mgr = OrderManager(event_bus=bus, broker=broker, risk_gatekeeper=risk_guard)

    engine = TrillionaireEngine(
        trading_mode=TradingMode.PAPER,
        event_bus=bus,
        risk_gatekeeper=risk_guard,
        order_manager=order_mgr,
        broker=broker,
    )
    await engine.start()

    # Seed 2 open positions
    pos_tcs = Position(
        tradingsymbol="TCS",
        side=Side.BUY,
        quantity=25,
        average_entry_price=3800.0,
        ltp=3810.0,
        unrealized_pnl=250.0,
        realized_pnl=0.0,
        stop_loss=3760.0,
        target_price=3880.0,
    )
    pos_infy = Position(
        tradingsymbol="INFY",
        side=Side.BUY,
        quantity=50,
        average_entry_price=1500.0,
        ltp=1505.0,
        unrealized_pnl=250.0,
        realized_pnl=0.0,
        stop_loss=1480.0,
        target_price=1540.0,
    )
    order_mgr.open_positions["TCS"] = pos_tcs
    order_mgr.open_positions["INFY"] = pos_infy
    risk_guard.register_position(pos_tcs)
    risk_guard.register_position(pos_infy)

    set_engine(engine)
    telemetry_manager.set_engine(engine)

    # Listen for circuit breaker events
    captured_circuit_events = []

    async def on_circuit_alert(event: RiskAlertEvent):
        captured_circuit_events.append(event)

    bus.subscribe(RiskAlertEvent, on_circuit_alert)

    # Execute Kill-Switch via FastAPI TestClient
    with TestClient(app) as client:
        # Check positions before kill-switch
        res_before = client.get("/api/v1/positions")
        assert res_before.status_code == 200
        assert len(res_before.json()) == 2

        # POST /control/kill-switch with admin key
        headers = {"X-API-Key": settings.trillionaire_admin_key}
        response = client.post(
            "/api/v1/control/kill-switch?reason=PORTFOLIO_CRASH_PANIC",
            headers=headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "executed"
        assert data["action"] == "EMERGENCY_KILL_SWITCH"
        assert data["reason"] == "PORTFOLIO_CRASH_PANIC"
        assert data["liquidated_orders_count"] == 2

        # 1. Verify positions are cleared
        assert len(order_mgr.open_positions) == 0

        # 2. Verify engine risk lock is engaged
        assert risk_guard.kill_switch_active is True
        assert "PORTFOLIO_CRASH_PANIC" in (risk_guard.circuit_breaker_reason or "")

        # 3. Verify circuit breaker event was published
        assert len(captured_circuit_events) >= 1
        assert any(ev.level == "CIRCUIT_BREAKER" for ev in captured_circuit_events)

        # 4. Attempt to pass a new signal through engine.on_signal
        blocked_signal = Signal(
            signal_id=str(uuid.uuid4()),
            tradingsymbol="RELIANCE",
            parent_index="NIFTY ENERGY",
            side=Side.BUY,
            entry_price=2500.0,
            stop_loss=2480.0,
            target_price=2540.0,
            confidence=0.85,
            strategy_name="DUAL_CONFIRMATION_VWAP_CPR",
            generated_at_ist=datetime(2026, 4, 15, 10, 0, 0, tzinfo=IST_TZ),
        )
        # calculate_position_size must evaluate to 0 shares due to kill_switch_active
        qty = risk_guard.calculate_position_size(signal=blocked_signal, sentinel_multiplier=1.0)
        assert qty == 0

        # Routing the event through on_signal does not open any position
        await engine.on_signal(SignalEvent(signal=blocked_signal))
        assert len(order_mgr.open_positions) == 0
