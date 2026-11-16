"""
Trillionaire Algo Engine - Unit Test Suite
Validates strict Pydantic v2 schemas, IST timezone conversions, namespace constants, and YAML configurations.
"""

from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo
import pytest
from pydantic import ValidationError

from config.settings import Settings
from shared.constants import (
    APP_NAME,
    BROKERAGE_CAP_INR,
    BROKERAGE_PCT,
    ENTRY_WINDOW_END,
    ENTRY_WINDOW_START,
    FORCE_SQUARE_OFF_TRIGGER,
    GST_RATE,
    IST_TZ,
    NSE_EXCHANGE_TURNOVER_RATE,
    REDIS_PREFIX,
    SEBI_TURNOVER_RATE,
    STAMP_DUTY_BUY_RATE,
    STREAM_ORDERS,
    STREAM_SIGNALS,
    STREAM_TICKS,
    STT_INTRADAY_SELL_RATE,
)
from shared.enums import (
    ClusterRegime,
    OrderStatus,
    OrderType,
    SentinelStatus,
    Side,
    Timeframe,
    TradingMode,
)
from shared.schemas import (
    Candle,
    NewsSentinelTag,
    Order,
    Position,
    Signal,
    StockProfile,
    Tick,
)


# ==============================================================================
# 1. CONSTANTS & NAMESPACE TESTS
# ==============================================================================

def test_constants_and_redis_namespaces():
    assert REDIS_PREFIX == "trillionaire:"
    assert APP_NAME == "Trillionaire Algo Engine"
    assert STREAM_TICKS.startswith(REDIS_PREFIX)
    assert STREAM_SIGNALS.startswith(REDIS_PREFIX)
    assert STREAM_ORDERS.startswith(REDIS_PREFIX)

    # Market timings
    assert ENTRY_WINDOW_START == time(9, 25, 0)
    assert ENTRY_WINDOW_END == time(14, 30, 0)
    assert FORCE_SQUARE_OFF_TRIGGER == time(15, 10, 0)

    # Statutory rates
    assert BROKERAGE_PCT == 0.0003
    assert BROKERAGE_CAP_INR == 20.0
    assert STT_INTRADAY_SELL_RATE == 0.00025
    assert NSE_EXCHANGE_TURNOVER_RATE == 0.0000297
    assert GST_RATE == 0.18
    assert SEBI_TURNOVER_RATE == 0.000001
    assert STAMP_DUTY_BUY_RATE == 0.00003


# ==============================================================================
# 2. ENUM INTEGRITY TESTS
# ==============================================================================

def test_enums_integrity():
    assert set(TradingMode) == {TradingMode.PAPER, TradingMode.LIVE, TradingMode.REPLAY}
    assert set(ClusterRegime) == {
        ClusterRegime.TREND_MOMENTUM,
        ClusterRegime.MEAN_REVERSION,
        ClusterRegime.HIGH_RVOL_BREAKOUT,
    }
    assert set(SentinelStatus) == {
        SentinelStatus.NORMAL,
        SentinelStatus.CAUTION_HALF_SIZE,
        SentinelStatus.DO_NOT_TRADE,
    }
    assert set(Side) == {Side.BUY, Side.SELL}
    assert set(OrderType) == {OrderType.LIMIT, OrderType.SL_LIMIT}
    assert set(OrderStatus) == {
        OrderStatus.PENDING_NEW,
        OrderStatus.OPEN,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
        OrderStatus.REJECTED,
    }
    assert Timeframe.M1.minutes == 1
    assert Timeframe.M5.minutes == 5
    assert Timeframe.M15.minutes == 15


# ==============================================================================
# 3. STRICT PYDANTIC V2 SCHEMAS & TIMEZONE TESTS
# ==============================================================================

def test_tick_schema_and_ist_conversion():
    # Pass naive datetime
    naive_dt = datetime(2026, 11, 16, 9, 30, 0)
    tick = Tick(
        instrument_token=738561,
        tradingsymbol="RELIANCE",
        timestamp_ist=naive_dt,
        ltp=2850.50,
        volume=150000,
        bid=2850.00,
        ask=2850.50,
        oi=0,
    )
    assert tick.timestamp_ist.tzinfo == IST_TZ
    assert tick.tradingsymbol == "RELIANCE"
    assert tick.ltp == 2850.50

    # Pass UTC datetime - must be converted to IST (+5:30)
    utc_dt = datetime(2026, 11, 16, 4, 0, 0, tzinfo=timezone.utc)
    tick_utc = Tick(
        instrument_token=738561,
        tradingsymbol="RELIANCE",
        timestamp_ist=utc_dt,
        ltp=2851.0,
        volume=150500,
        bid=2850.5,
        ask=2851.0,
    )
    assert tick_utc.timestamp_ist.hour == 9
    assert tick_utc.timestamp_ist.minute == 30
    assert tick_utc.timestamp_ist.tzinfo == IST_TZ

    # ISO string parsing
    tick_iso = Tick(
        instrument_token=738561,
        tradingsymbol="RELIANCE",
        timestamp_ist="2026-11-16T09:30:00+05:30",
        ltp=2850.50,
        volume=150000,
        bid=2850.00,
        ask=2850.50,
    )
    assert tick_iso.timestamp_ist.tzinfo == IST_TZ

    # Validation failure on negative price or volume
    with pytest.raises(ValidationError):
        Tick(
            instrument_token=738561,
            tradingsymbol="RELIANCE",
            timestamp_ist=naive_dt,
            ltp=-10.0,
            volume=100,
            bid=10.0,
            ask=10.0,
        )

    # Extra forbidden field
    with pytest.raises(ValidationError):
        Tick(
            instrument_token=738561,
            tradingsymbol="RELIANCE",
            timestamp_ist=naive_dt,
            ltp=2850.0,
            volume=100,
            bid=10.0,
            ask=10.0,
            unknown_arg="illegal",
        )


def test_candle_schema():
    candle = Candle(
        tradingsymbol="INFY",
        timeframe=Timeframe.M5,
        timestamp_ist=datetime(2026, 11, 16, 9, 35, 0),
        open=1520.0,
        high=1528.5,
        low=1518.0,
        close=1525.0,
        volume=45000,
        vwap=1523.40,
        ema_200=1510.20,
        atr_14=8.50,
        rvol=1.85,
    )
    assert candle.tradingsymbol == "INFY"
    assert candle.timeframe == Timeframe.M5
    assert candle.timestamp_ist.tzinfo == IST_TZ
    assert candle.rvol == 1.85


def test_stock_profile_schema():
    profile = StockProfile(
        tradingsymbol="HDFCBANK",
        instrument_token=341249,
        parent_index="NIFTY BANK",
        index_token=260105,
        cluster_regime=ClusterRegime.TREND_MOMENTUM,
        beta_5y=1.08,
        adr_pct=1.75,
        atr_multiplier_sl=1.5,
        atr_multiplier_tp=3.0,
        min_rvol_threshold=1.5,
        oos_sharpe=1.82,
        oos_profit_factor=1.95,
        is_active=True,
    )
    assert profile.tradingsymbol == "HDFCBANK"
    assert profile.cluster_regime == ClusterRegime.TREND_MOMENTUM
    assert profile.is_active is True


def test_sentinel_tag_schema():
    tag = NewsSentinelTag(
        tradingsymbol="TATAMOTORS",
        status=SentinelStatus.CAUTION_HALF_SIZE,
        reason="Upcoming quarterly earnings call at 14:00 IST",
        risk_multiplier=0.5,
        updated_at_ist=datetime(2026, 11, 16, 8, 45, 0),
    )
    assert tag.status == SentinelStatus.CAUTION_HALF_SIZE
    assert tag.risk_multiplier == 0.5
    assert tag.updated_at_ist.tzinfo == IST_TZ

    # Risk multiplier out of [0, 1] range must raise ValidationError
    with pytest.raises(ValidationError):
        NewsSentinelTag(
            tradingsymbol="TATAMOTORS",
            status=SentinelStatus.CAUTION_HALF_SIZE,
            reason="Test",
            risk_multiplier=1.5,
            updated_at_ist=datetime(2026, 11, 16, 8, 45, 0),
        )


def test_signal_schema():
    signal = Signal(
        signal_id="sig-uuid-1001",
        tradingsymbol="TCS",
        parent_index="NIFTY IT",
        side=Side.BUY,
        entry_price=4150.0,
        stop_loss=4120.0,
        target_price=4210.0,
        confidence=0.88,
        strategy_name="TrendMomentumBreakout",
        generated_at_ist=datetime(2026, 11, 16, 10, 15, 0),
    )
    assert signal.side == Side.BUY
    assert signal.confidence == 0.88
    assert signal.generated_at_ist.tzinfo == IST_TZ


def test_order_schema():
    order = Order(
        order_id="ord-uuid-2001",
        broker_order_id="2411160001234",
        signal_id="sig-uuid-1001",
        tradingsymbol="TCS",
        side=Side.BUY,
        product="MIS",
        order_type=OrderType.LIMIT,
        quantity=50,
        filled_quantity=50,
        limit_price=4150.0,
        average_fill_price=4150.0,
        status=OrderStatus.FILLED,
        created_at_ist=datetime(2026, 11, 16, 10, 15, 5),
    )
    assert order.product == "MIS"
    assert order.status == OrderStatus.FILLED
    assert order.created_at_ist.tzinfo == IST_TZ


def test_position_schema():
    position = Position(
        tradingsymbol="TCS",
        side=Side.BUY,
        quantity=50,
        average_entry_price=4150.0,
        ltp=4175.0,
        unrealized_pnl=1250.0,
        realized_pnl=0.0,
        stop_loss=4120.0,
        target_price=4210.0,
    )
    assert position.quantity == 50
    assert position.unrealized_pnl == 1250.0


# ==============================================================================
# 4. CONFIGURATION LOADING & MARKET WINDOW TESTS
# ==============================================================================

def test_capital_tiers_loading():
    settings = Settings()
    tiers = settings.load_capital_tiers()

    expected_tiers = ["TIER_0_PAPER", "TIER_1_MICRO", "TIER_2_GROWTH", "TIER_3_SCALE"]
    for tier in expected_tiers:
        assert tier in tiers, f"Expected {tier} in capital_tiers.yaml"

    tier0 = tiers["TIER_0_PAPER"]
    assert tier0["allocated_capital"] == 100000.0
    assert tier0["max_daily_loss_pct"] == 1.5
    assert tier0["risk_per_trade_pct"] == 0.5
    assert tier0["profit_lock_trigger_pct"] == 1.2
    assert tier0["profit_lock_floor_pct"] == 0.6
    assert tier0["max_open_positions"] == 3
    assert tier0["max_mis_leverage"] == 3.0
    assert tier0["is_live"] is False

    tier1 = tiers["TIER_1_MICRO"]
    assert tier1["allocated_capital"] == 100000.0
    assert tier1["max_open_positions"] == 3

    tier2 = tiers["TIER_2_GROWTH"]
    assert tier2["allocated_capital"] == 500000.0
    assert tier2["max_open_positions"] == 5

    tier3 = tiers["TIER_3_SCALE"]
    assert tier3["allocated_capital"] == 1000000.0
    assert tier3["max_open_positions"] == 8

    # Active tier config fetching
    active_config = settings.get_active_capital_tier_config()
    assert active_config["allocated_capital"] == 100000.0


def test_sector_map_loading():
    settings = Settings()
    sector_data = settings.load_sector_map()

    assert "benchmark_indices" in sector_data
    indices = sector_data["benchmark_indices"]
    assert "NIFTY 50" in indices
    assert "NIFTY BANK" in indices
    assert "NIFTY IT" in indices
    assert "NIFTY AUTO" in indices
    assert "NIFTY PHARMA" in indices
    assert "NIFTY ENERGY" in indices

    stocks = sector_data["stocks"]
    assert len(stocks) == 25, f"Expected 25 stocks in sector_map.yaml, found {len(stocks)}"

    tracked_symbols = settings.get_tracked_symbols()
    assert len(tracked_symbols) == 25
    expected_symbols = [
        "RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK", "SBIN",
        "TATAMOTORS", "M&M", "SUNPHARMA", "LT", "BHARTIARTL", "ITC"
    ]
    for sym in expected_symbols:
        assert sym in tracked_symbols, f"Expected {sym} to be present in tracked symbols"


def test_market_session_windows():
    settings = Settings(
        no_entry_before="09:25",
        no_entry_after="14:30",
        force_square_off="15:10",
    )

    # 1. Early morning (09:15) - Market open, but algo entry NOT allowed
    dt_early = datetime(2026, 11, 16, 9, 15, 0, tzinfo=IST_TZ)
    assert settings.is_entry_allowed(dt_early) is False
    assert settings.is_force_square_off_due(dt_early) is False

    # 2. Algo window active (09:30) - Entries allowed
    dt_active = datetime(2026, 11, 16, 9, 30, 0, tzinfo=IST_TZ)
    assert settings.is_entry_allowed(dt_active) is True
    assert settings.is_force_square_off_due(dt_active) is False

    # 3. Midday (12:00) - Entries allowed
    dt_midday = datetime(2026, 11, 16, 12, 0, 0, tzinfo=IST_TZ)
    assert settings.is_entry_allowed(dt_midday) is True
    assert settings.is_force_square_off_due(dt_midday) is False

    # 4. Afternoon cutoff (14:35) - Entries ceased
    dt_afternoon = datetime(2026, 11, 16, 14, 35, 0, tzinfo=IST_TZ)
    assert settings.is_entry_allowed(dt_afternoon) is False
    assert settings.is_force_square_off_due(dt_afternoon) is False

    # 5. Mandatory liquidation (15:10) - Force square off triggered
    dt_square_off = datetime(2026, 11, 16, 15, 10, 0, tzinfo=IST_TZ)
    assert settings.is_entry_allowed(dt_square_off) is False
    assert settings.is_force_square_off_due(dt_square_off) is True

    # 6. Post market (15:35) - Force square off remains true
    dt_post = datetime(2026, 11, 16, 15, 35, 0, tzinfo=IST_TZ)
    assert settings.is_entry_allowed(dt_post) is False
    assert settings.is_force_square_off_due(dt_post) is True
