"""
Unit tests for Sprint 6:
1. Dynamic Position Sizer with Sentinel Multipliers, Risk-Per-Trade, and Leverage Constraints.
2. Hard Daily MTM Kill-Switch (-1.5%) Emergency Flatten & Lockout.
3. Profit-Locking Trailing Floor (+1.2% trigger, +0.6% floor liquidation).
4. Marketable Limit Orders, Rate Limiting (<= 10 orders/sec), and 15:10 IST Mandatory Square-Off.
5. 1R Breakeven Trailing Stop Advancement and Stop Loss / Target Execution.
6. LiveStrategyRunner Dual Confirmation & Sentinel/Anomaly Gating.
"""

from datetime import date, datetime, time
import pytest
import uuid

from shared.constants import IST_TZ
from shared.enums import (
    ClusterRegime,
    OrderStatus,
    OrderType,
    SentinelStatus,
    Side,
    Timeframe,
)
from shared.exceptions import RateLimitExceededException
from shared.schemas import (
    Candle,
    NewsSentinelTag,
    Order,
    Position,
    Signal,
    StockProfile,
    Tick,
)
from trading_engine.core.event_bus import (
    CandleCloseEvent,
    EventBus,
    OrderUpdateEvent,
    RiskAlertEvent,
    SignalEvent,
    TickEvent,
)
from trading_engine.data.anomaly_detector import AnomalyDetector
from trading_engine.execution.order_manager import OrderManager
from trading_engine.execution.paper_broker import PaperBroker
from trading_engine.risk.portfolio_risk import PortfolioRiskGatekeeper
from trading_engine.strategy.live_runner import LiveStrategyRunner


@pytest.fixture
def mock_profile() -> StockProfile:
    return StockProfile(
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


@pytest.fixture
def test_bus() -> EventBus:
    return EventBus(enable_redis_mirror=False)


# -----------------------------------------------------------------------------
# 1. Dynamic Position Sizing Tests
# -----------------------------------------------------------------------------

def test_dynamic_position_sizing_sentinel_multipliers(test_bus: EventBus):
    """Verifies that position sizer dynamically scales quantity by Sentinel multiplier."""
    risk_guard = PortfolioRiskGatekeeper(
        event_bus=test_bus,
        allocated_capital=100000.0,  # Rs. 1,00,000
        risk_per_trade_pct=0.5,      # 0.5% = Rs. 500 risk
        max_open_positions=3,
        max_mis_leverage=3.0,
    )

    signal = Signal(
        signal_id=str(uuid.uuid4()),
        tradingsymbol="RELIANCE",
        parent_index="NIFTY ENERGY",
        side=Side.BUY,
        entry_price=2500.0,
        stop_loss=2480.0,            # Rs. 20 risk per share
        target_price=2560.0,
        confidence=0.85,
        strategy_name="TrendMomentumStrategy",
        generated_at_ist=datetime(2026, 3, 10, 10, 15, tzinfo=IST_TZ),
    )

    # 1. Normal Sentinel (1.0x): Rs. 500 / Rs. 20 = 25 shares
    size_normal = risk_guard.calculate_position_size(signal, sentinel_multiplier=1.0)
    assert size_normal == 25

    # 2. Caution Sentinel (0.5x): Rs. 250 / Rs. 20 = 12 shares
    size_caution = risk_guard.calculate_position_size(signal, sentinel_multiplier=0.5)
    assert size_caution == 12

    # 3. Lockout Sentinel (0.0x): 0 shares
    size_lockout = risk_guard.calculate_position_size(signal, sentinel_multiplier=0.0)
    assert size_lockout == 0


def test_position_sizing_leverage_and_max_positions(test_bus: EventBus):
    """Verifies that position sizing respects max gross leverage and max open positions."""
    risk_guard = PortfolioRiskGatekeeper(
        event_bus=test_bus,
        allocated_capital=100000.0,
        risk_per_trade_pct=2.0,      # Rs. 2,000 risk
        max_open_positions=2,
        max_mis_leverage=3.0,        # Max total gross exposure = Rs. 3,00,000
    )

    valid_time = datetime(2026, 3, 10, 10, 30, tzinfo=IST_TZ)

    # Register position 1: Rs. 1,00,000 exposure
    pos1 = Position(
        tradingsymbol="INFY",
        side=Side.BUY,
        quantity=50,
        average_entry_price=2000.0,
        ltp=2000.0,
        unrealized_pnl=0.0,
        realized_pnl=0.0,
        stop_loss=1980.0,
        target_price=2060.0,
    )
    risk_guard.register_position(pos1)

    # Register position 2: Rs. 1,50,000 exposure
    pos2 = Position(
        tradingsymbol="TCS",
        side=Side.BUY,
        quantity=50,
        average_entry_price=3000.0,
        ltp=3000.0,
        unrealized_pnl=0.0,
        realized_pnl=0.0,
        stop_loss=2950.0,
        target_price=3100.0,
    )
    risk_guard.register_position(pos2)

    # Total exposure is now Rs. 2,50,000. Remaining leverage capacity is Rs. 50,000.
    # Attempting 3rd position should fail due to max_open_positions=2
    signal3 = Signal(
        signal_id=str(uuid.uuid4()),
        tradingsymbol="HDFCBANK",
        parent_index="NIFTY BANK",
        side=Side.BUY,
        entry_price=1500.0,
        stop_loss=1480.0,
        target_price=1560.0,
        confidence=0.85,
        strategy_name="TrendMomentumStrategy",
        generated_at_ist=valid_time,
    )
    size_exceeded = risk_guard.calculate_position_size(signal3, sentinel_multiplier=1.0)
    assert size_exceeded == 0

    # Close one position to free a slot
    risk_guard.close_position("TCS", realized_pnl=100.0)
    # Remaining exposure is 50 * 2000 = 1,00,000. Capacity = 2,00,000.
    # Risk budget: 2% of 100k = Rs. 2,000. Risk/share = 20 -> 100 shares.
    # Exposure of 100 shares * 1500 = 1,50,000 <= 2,00,000 capacity.
    size_allowed = risk_guard.calculate_position_size(signal3, sentinel_multiplier=1.0)
    assert size_allowed == 100


def test_position_sizing_timing_window(test_bus: EventBus):
    """Verifies that signals outside the 09:25 - 14:30 IST window are rejected."""
    risk_guard = PortfolioRiskGatekeeper(
        event_bus=test_bus,
        allocated_capital=100000.0,
    )

    # Pre-market signal (09:20 IST) -> rejected
    early_signal = Signal(
        signal_id=str(uuid.uuid4()),
        tradingsymbol="RELIANCE",
        parent_index="NIFTY ENERGY",
        side=Side.BUY,
        entry_price=2500.0,
        stop_loss=2480.0,
        target_price=2560.0,
        confidence=0.85,
        strategy_name="TrendMomentumStrategy",
        generated_at_ist=datetime(2026, 3, 10, 9, 20, tzinfo=IST_TZ),
    )
    assert risk_guard.calculate_position_size(early_signal) == 0

    # Late session signal (14:35 IST) -> rejected
    late_signal = Signal(
        signal_id=str(uuid.uuid4()),
        tradingsymbol="RELIANCE",
        parent_index="NIFTY ENERGY",
        side=Side.BUY,
        entry_price=2500.0,
        stop_loss=2480.0,
        target_price=2560.0,
        confidence=0.85,
        strategy_name="TrendMomentumStrategy",
        generated_at_ist=datetime(2026, 3, 10, 14, 35, tzinfo=IST_TZ),
    )
    assert risk_guard.calculate_position_size(late_signal) == 0


# -----------------------------------------------------------------------------
# 2. Hard Daily MTM Kill-Switch (-1.5%) Tests
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_hard_daily_mtm_kill_switch(test_bus: EventBus):
    """Verifies that breaching -1.5% daily drawdown trips circuit breaker and flattens."""
    flattened_reasons = []

    async def mock_flatten(reason: str):
        flattened_reasons.append(reason)

    risk_guard = PortfolioRiskGatekeeper(
        event_bus=test_bus,
        allocated_capital=100000.0,  # 1.5% max loss = Rs. 1,500
        max_daily_loss_pct=1.5,
        flatten_handler=mock_flatten,
    )

    # Position: 100 shares bought @ Rs. 2,000 (Total Rs. 2,00,000)
    pos = Position(
        tradingsymbol="RELIANCE",
        side=Side.BUY,
        quantity=100,
        average_entry_price=2000.0,
        ltp=2000.0,
        unrealized_pnl=0.0,
        realized_pnl=0.0,
        stop_loss=1970.0,
        target_price=2060.0,
    )
    risk_guard.register_position(pos)

    # Mild drawdown: price drops to 1990 (-Rs. 1,000 MTM)
    mild_tick = Tick(
        instrument_token=738561,
        tradingsymbol="RELIANCE",
        timestamp_ist=datetime(2026, 3, 10, 10, 30, tzinfo=IST_TZ),
        ltp=1990.0,
        volume=10000,
        bid=1989.9,
        ask=1990.1,
    )
    await risk_guard.process_tick(mild_tick)
    assert risk_guard.kill_switch_active is False
    assert len(flattened_reasons) == 0

    # Severe drawdown: price drops to 1980 (-Rs. 2,000 MTM > Rs. 1,500 limit)
    severe_tick = Tick(
        instrument_token=738561,
        tradingsymbol="RELIANCE",
        timestamp_ist=datetime(2026, 3, 10, 10, 35, tzinfo=IST_TZ),
        ltp=1980.0,
        volume=20000,
        bid=1979.9,
        ask=1980.1,
    )
    await risk_guard.process_tick(severe_tick)

    assert risk_guard.kill_switch_active is True
    assert len(flattened_reasons) == 1
    assert flattened_reasons[0] == "DAILY_LOSS_KILL_SWITCH_TRIPPED"

    # Further signals must be rejected
    sig = Signal(
        signal_id=str(uuid.uuid4()),
        tradingsymbol="INFY",
        parent_index="NIFTY IT",
        side=Side.BUY,
        entry_price=1500.0,
        stop_loss=1480.0,
        target_price=1550.0,
        confidence=0.85,
        strategy_name="TrendMomentumStrategy",
        generated_at_ist=datetime(2026, 3, 10, 11, 0, tzinfo=IST_TZ),
    )
    assert risk_guard.calculate_position_size(sig) == 0


# -----------------------------------------------------------------------------
# 3. Trailing Profit-Lock Floor (+1.2% / +0.6%) Tests
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_profit_lock_trailing_floor(test_bus: EventBus):
    """Verifies profit lock arming at +1.2% and floor liquidation when retracing to +0.6%."""
    flatten_calls = []

    async def mock_flatten(reason: str):
        flatten_calls.append(reason)

    risk_guard = PortfolioRiskGatekeeper(
        event_bus=test_bus,
        allocated_capital=100000.0,
        profit_lock_trigger_pct=1.2,  # +Rs. 1,200 triggers arming
        profit_lock_floor_pct=0.6,    # +Rs. 600 floor liquidation
        flatten_handler=mock_flatten,
    )

    pos = Position(
        tradingsymbol="TCS",
        side=Side.BUY,
        quantity=100,
        average_entry_price=3000.0,
        ltp=3000.0,
        unrealized_pnl=0.0,
        realized_pnl=0.0,
        stop_loss=2970.0,
        target_price=3100.0,
    )
    risk_guard.register_position(pos)

    # 1. Price rises to 3013 (+Rs. 1,300 MTM > Rs. 1,200 trigger)
    peak_tick = Tick(
        instrument_token=2953217,
        tradingsymbol="TCS",
        timestamp_ist=datetime(2026, 3, 10, 11, 0, tzinfo=IST_TZ),
        ltp=3013.0,
        volume=50000,
        bid=3012.9,
        ask=3013.1,
    )
    await risk_guard.process_tick(peak_tick)

    assert risk_guard.profit_lock_armed is True
    assert risk_guard.profit_lock_floor_hit is False
    assert len(flatten_calls) == 0

    # 2. Price retraces to 3005 (+Rs. 500 MTM <= Rs. 600 floor)
    retrace_tick = Tick(
        instrument_token=2953217,
        tradingsymbol="TCS",
        timestamp_ist=datetime(2026, 3, 10, 11, 30, tzinfo=IST_TZ),
        ltp=3005.0,
        volume=80000,
        bid=3004.9,
        ask=3005.1,
    )
    await risk_guard.process_tick(retrace_tick)

    assert risk_guard.profit_lock_floor_hit is True
    assert len(flatten_calls) == 1
    assert flatten_calls[0] == "PROFIT_LOCK_FLOOR_REACHED"


# -----------------------------------------------------------------------------
# 4. Marketable Limit Orders & Rate Limiter Tests
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_order_manager_marketable_limits_and_rate_limiter(test_bus: EventBus):
    """Verifies SEBI marketable limit buffer and rate limiter (<= 10 orders/sec)."""
    broker = PaperBroker(event_bus=test_bus)
    om = OrderManager(
        event_bus=test_bus,
        broker=broker,
        max_orders_per_sec=10,
        marketable_buffer_pct=0.001,
    )

    # Marketable limit price checks
    buy_lim = om.calculate_marketable_limit_price(Side.BUY, 2500.0)
    assert buy_lim == 2502.50  # 2500 * 1.001

    sell_lim = om.calculate_marketable_limit_price(Side.SELL, 2500.0)
    assert sell_lim == 2497.50  # 2500 * 0.999

    # Test rate limiter: submit 10 orders rapidly
    sig = Signal(
        signal_id=str(uuid.uuid4()),
        tradingsymbol="RELIANCE",
        parent_index="NIFTY ENERGY",
        side=Side.BUY,
        entry_price=2500.0,
        stop_loss=2480.0,
        target_price=2560.0,
        confidence=0.85,
        strategy_name="TrendMomentumStrategy",
        generated_at_ist=datetime(2026, 3, 10, 10, 0, tzinfo=IST_TZ),
    )

    for _ in range(10):
        await om.submit_signal_order(sig, quantity=5)

    assert len(om._order_timestamps) == 10

    # 11th order in the same second must raise RateLimitExceededException
    with pytest.raises(RateLimitExceededException):
        await om.submit_signal_order(sig, quantity=5)


# -----------------------------------------------------------------------------
# 5. Live Bracket & 1R Breakeven Trailing Stop Tests
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_1r_breakeven_trailing_stop_and_execution(test_bus: EventBus):
    """Verifies that reaching +1R profit advances stop loss to entry price (breakeven)."""
    broker = PaperBroker(event_bus=test_bus)
    risk_guard = PortfolioRiskGatekeeper(event_bus=test_bus, allocated_capital=100000.0)
    om = OrderManager(event_bus=test_bus, broker=broker, risk_gatekeeper=risk_guard)

    entry_price = 2000.0
    initial_sl = 1980.0     # 1R = Rs. 20
    target_price = 2060.0   # 3R = Rs. 60

    # Provide initial tick so marketable limit order fills immediately
    broker.set_latest_tick(
        Tick(
            instrument_token=738561,
            tradingsymbol="RELIANCE",
            timestamp_ist=datetime(2026, 3, 10, 10, 0, tzinfo=IST_TZ),
            ltp=entry_price,
            volume=5000,
            bid=1999.9,
            ask=2000.1,
        )
    )

    sig = Signal(
        signal_id=str(uuid.uuid4()),
        tradingsymbol="RELIANCE",
        parent_index="NIFTY ENERGY",
        side=Side.BUY,
        entry_price=entry_price,
        stop_loss=initial_sl,
        target_price=target_price,
        confidence=0.85,
        strategy_name="TrendMomentumStrategy",
        generated_at_ist=datetime(2026, 3, 10, 10, 0, tzinfo=IST_TZ),
    )

    # Submit and fill
    order = await om.submit_signal_order(sig, quantity=10)
    assert order is not None
    assert order.status == OrderStatus.FILLED
    assert "RELIANCE" in om.open_positions

    pos = om.open_positions["RELIANCE"]
    assert pos.stop_loss == initial_sl

    # Tick 1: Price rises to 2010 (+0.5R). Stop loss stays at initial SL.
    tick_half_r = Tick(
        instrument_token=738561,
        tradingsymbol="RELIANCE",
        timestamp_ist=datetime(2026, 3, 10, 10, 5, tzinfo=IST_TZ),
        ltp=2010.0,
        volume=8000,
        bid=2009.9,
        ask=2010.1,
    )
    await om.on_tick(TickEvent(tick=tick_half_r))
    assert pos.stop_loss == initial_sl

    # Tick 2: Price reaches 2021 (>= 2000 + 20 = 2020 -> +1R). Stop loss moves to BREAKEVEN (2000.0)!
    tick_1r = Tick(
        instrument_token=738561,
        tradingsymbol="RELIANCE",
        timestamp_ist=datetime(2026, 3, 10, 10, 10, tzinfo=IST_TZ),
        ltp=2021.0,
        volume=12000,
        bid=2020.9,
        ask=2021.1,
    )
    await om.on_tick(TickEvent(tick=tick_1r))
    assert pos.stop_loss == pos.average_entry_price

    # Tick 3: Price drops back to 1999.0 (below breakeven SL) -> Triggers exit!
    tick_sl = Tick(
        instrument_token=738561,
        tradingsymbol="RELIANCE",
        timestamp_ist=datetime(2026, 3, 10, 10, 15, tzinfo=IST_TZ),
        ltp=1999.0,
        volume=15000,
        bid=1998.9,
        ask=1999.1,
    )
    await om.on_tick(TickEvent(tick=tick_sl))

    # Position is closed at breakeven
    assert "RELIANCE" not in om.open_positions


# -----------------------------------------------------------------------------
# 6. LiveStrategyRunner Dual Confirmation & Gating Tests
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_live_strategy_runner_dual_confirmation(test_bus: EventBus, mock_profile: StockProfile):
    """Verifies that LiveStrategyRunner checks Sentinel, Anomaly, and Parent Index."""
    runner = LiveStrategyRunner(
        event_bus=test_bus,
        profiles=[mock_profile],
    )
    runner.seed_opening_range("RELIANCE", or_high=2500.0, or_low=2470.0)

    now_ist = datetime(2026, 3, 10, 10, 0, tzinfo=IST_TZ)

    # 1. Provide Parent Index (NIFTY ENERGY) M5 Candle
    idx_candle = Candle(
        tradingsymbol="NIFTY ENERGY",
        timeframe=Timeframe.M5,
        timestamp_ist=now_ist,
        open=30000.0,
        high=30100.0,
        low=29990.0,
        close=30080.0,
        volume=1000000,
        vwap=30020.0,
        ema_200=29800.0,
        atr_14=120.0,
        rvol=1.8,
    )
    await runner.on_candle_close(CandleCloseEvent(candle=idx_candle))

    # 2. Stock Candle (RELIANCE) breaking OR High (2500) with Price > VWAP and Price > EMA200
    stock_candle = Candle(
        tradingsymbol="RELIANCE",
        timeframe=Timeframe.M5,
        timestamp_ist=now_ist,
        open=2490.0,
        high=2515.0,
        low=2488.0,
        close=2512.0,  # > 2500 OR High, > VWAP, > EMA200
        volume=450000,
        vwap=2495.0,
        ema_200=2460.0,
        atr_14=15.0,
        rvol=2.1,
    )

    signal = await runner.on_candle_close(CandleCloseEvent(candle=stock_candle))
    assert signal is not None
    assert signal.tradingsymbol == "RELIANCE"
    assert signal.side == Side.BUY
    assert signal.entry_price == 2512.0
    assert signal.stop_loss < 2512.0
    assert signal.target_price > 2512.0


@pytest.mark.asyncio
async def test_live_strategy_runner_sentinel_lockout(test_bus: EventBus, mock_profile: StockProfile):
    """Verifies that a stock tagged DO_NOT_TRADE by Sentinel produces no signal."""
    runner = LiveStrategyRunner(
        event_bus=test_bus,
        profiles=[mock_profile],
    )
    runner.seed_opening_range("RELIANCE", or_high=2500.0, or_low=2470.0)

    # Mock Sentinel tag to DO_NOT_TRADE
    runner.sentinel._cached_tags["RELIANCE"] = NewsSentinelTag(
        tradingsymbol="RELIANCE",
        status=SentinelStatus.DO_NOT_TRADE,
        reason="Quarterly earnings blackout",
        risk_multiplier=0.0,
        updated_at_ist=datetime(2026, 3, 10, 8, 30, tzinfo=IST_TZ),
    )

    now_ist = datetime(2026, 3, 10, 10, 0, tzinfo=IST_TZ)

    # Seed parent index
    idx_candle = Candle(
        tradingsymbol="NIFTY ENERGY",
        timeframe=Timeframe.M5,
        timestamp_ist=now_ist,
        open=30000.0,
        high=30100.0,
        low=29990.0,
        close=30080.0,
        volume=1000000,
        vwap=30020.0,
        ema_200=29800.0,
        atr_14=120.0,
        rvol=1.8,
    )
    await runner.on_candle_close(CandleCloseEvent(candle=idx_candle))

    stock_candle = Candle(
        tradingsymbol="RELIANCE",
        timeframe=Timeframe.M5,
        timestamp_ist=now_ist,
        open=2490.0,
        high=2515.0,
        low=2488.0,
        close=2512.0,
        volume=450000,
        vwap=2495.0,
        ema_200=2460.0,
        atr_14=15.0,
        rvol=2.1,
    )

    signal = await runner.on_candle_close(CandleCloseEvent(candle=stock_candle))
    assert signal is None  # Blocked by Sentinel gate!
