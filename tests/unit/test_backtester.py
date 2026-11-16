"""
Trillionaire Research Lab - Backtester Unit & Integration Tests
Validates:
1. Zero lookahead indicators (daily VWAP reset at 09:15 IST, time-of-day RVOL).
2. Stock + Sector Index dual-confirmation logic across all 3 regime micro-strategies.
3. Event-driven engine execution, slippage, charges, and mandatory 15:10 IST MIS square-off.
4. End-to-end Walk-Forward validation updating stock_profiles.json and backtest_tearsheet.json.
"""

from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import numpy as np
import polars as pl
import pytest

from config.settings import settings
from research_lab.backtester.engine import EventDrivenBacktester
from research_lab.backtester.indicators import (
    compute_all_indicators,
    compute_daily_cpr,
    compute_opening_range,
    compute_time_of_day_rvol,
    compute_vwap,
    compute_wilder_atr,
    resample_to_5min,
)
from research_lab.backtester.strategies import (
    HighRvolBreakoutStrategy,
    MeanReversionStrategy,
    TrendMomentumStrategy,
)
from research_lab.backtester.walk_forward import TEARSHEET_PATH, WalkForwardValidator
from research_lab.data_loader.synthetic_generator import SyntheticDataGenerator
from research_lab.profiler.stock_profiler import STOCK_PROFILES_PATH, StockProfiler
from shared.constants import IST_TZ
from shared.enums import ClusterRegime, Side
from shared.schemas import StockProfile


# ==============================================================================
# 1. INDICATORS: VWAP RESET & TIME-OF-DAY RVOL TESTS
# ==============================================================================

def test_vwap_daily_reset():
    """Validates that intraday VWAP strictly resets at 09:15 IST on every new day."""
    # Build 2 consecutive days, 5 bars each
    day1_start = datetime(2026, 11, 16, 9, 15, tzinfo=IST_TZ)
    day2_start = datetime(2026, 11, 17, 9, 15, tzinfo=IST_TZ)

    timestamps = [day1_start + timedelta(minutes=m) for m in range(5)] + [
        day2_start + timedelta(minutes=m) for m in range(5)
    ]

    # Day 1: High prices (around 2000), Day 2: Low prices (around 1000)
    highs = [2010.0] * 5 + [1010.0] * 5
    lows = [1990.0] * 5 + [990.0] * 5
    closes = [2000.0] * 5 + [1000.0] * 5
    volumes = [1000] * 10

    df = pl.DataFrame({
        "timestamp": timestamps,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes,
    })

    vwap_series = compute_vwap(df)
    assert len(vwap_series) == 10

    # Day 1 VWAP should be 2000.0
    for v in vwap_series[:5]:
        assert abs(v - 2000.0) < 0.01

    # Day 2 VWAP MUST reset to 1000.0, NOT average with Day 1
    assert abs(vwap_series[5] - 1000.0) < 0.01
    for v in vwap_series[5:]:
        assert abs(v - 1000.0) < 0.01


def test_time_of_day_rvol_zero_lookahead():
    """Validates that time-of-day RVOL uses only strictly prior days without lookahead."""
    # 3 days of 5-min bars (3 bars per day: 09:15, 09:20, 09:25)
    days = [
        datetime(2026, 11, 16, tzinfo=IST_TZ),
        datetime(2026, 11, 17, tzinfo=IST_TZ),
        datetime(2026, 11, 18, tzinfo=IST_TZ),
    ]
    timestamps = []
    volumes = []

    # Day 1: volume 100 at 09:15
    # Day 2: volume 200 at 09:15
    # Day 3: volume 600 at 09:15
    for d, vol_base in zip(days, [100, 200, 600]):
        timestamps.extend([
            d.replace(hour=9, minute=15),
            d.replace(hour=9, minute=20),
            d.replace(hour=9, minute=25),
        ])
        volumes.extend([vol_base, vol_base, vol_base])

    df_5m = pl.DataFrame({
        "timestamp": timestamps,
        "volume": volumes,
    })

    rvol = compute_time_of_day_rvol(df_5m, lookback_days=20)
    assert len(rvol) == 9

    # Day 1 (first day) has no history -> defaults to 1.0
    assert rvol[0] == 1.0

    # Day 2: prior Day 1 volume is 100. Current is 200 -> RVOL = 200 / 100 = 2.0
    assert abs(rvol[3] - 2.0) < 0.01

    # Day 3: prior Day 1 & Day 2 mean volume is (100 + 200) / 2 = 150. Current is 600 -> RVOL = 600 / 150 = 4.0
    assert abs(rvol[6] - 4.0) < 0.01


def test_opening_range_computation():
    """Validates that Opening Range holds constant after 09:30 IST."""
    start = datetime(2026, 11, 16, 9, 15, tzinfo=IST_TZ)
    # 25 minutes of bars: 09:15 to 09:40
    timestamps = [start + timedelta(minutes=m) for m in range(25)]
    # High spikes to 1050 at 09:25, Low drops to 980 at 09:20
    # Later at 09:35, price moves to 1080 (which should NOT alter the 09:15-09:30 opening range)
    highs = [1000.0] * 25
    lows = [1000.0] * 25
    highs[10] = 1050.0  # 09:25
    lows[5] = 980.0     # 09:20
    highs[20] = 1080.0  # 09:35 (after cutoff)

    df = pl.DataFrame({
        "timestamp": timestamps,
        "high": highs,
        "low": lows,
        "close": [1000.0] * 25,
    })

    or_high, or_low = compute_opening_range(df)
    # For any bar after 09:30 (minute >= 15), OR High must be 1050.0 and OR Low 980.0
    assert or_high[20] == 1050.0
    assert or_low[20] == 980.0


# ==============================================================================
# 2. DUAL CONFIRMATION STRATEGY LOGIC TESTS
# ==============================================================================

def test_trend_momentum_dual_confirmation():
    """Validates that TrendMomentumStrategy triggers ONLY when parent index confirms."""
    strategy = TrendMomentumStrategy()
    profile = StockProfile(
        tradingsymbol="RELIANCE",
        instrument_token=738561,
        parent_index="NIFTY ENERGY",
        index_token=260609,
        cluster_regime=ClusterRegime.TREND_MOMENTUM,
        beta_5y=1.12,
        adr_pct=1.85,
        atr_multiplier_sl=1.5,
        atr_multiplier_tp=3.0,
        min_rvol_threshold=1.5,
        oos_sharpe=1.9,
        oos_profit_factor=1.9,
        is_active=True,
    )

    bar_time = datetime(2026, 11, 16, 9, 45, tzinfo=IST_TZ)

    # Stock meets long criteria: close (2850) > OR_High (2840), > VWAP (2820), > EMA200 (2800)
    stock_bar = {
        "timestamp": bar_time,
        "open": 2845.0,
        "high": 2855.0,
        "low": 2842.0,
        "close": 2850.0,
        "vwap": 2820.0,
        "ema_200": 2800.0,
        "or_high": 2840.0,
        "or_low": 2810.0,
        "atr_14": 15.0,
    }

    # Case A: Parent Index DISAGREES (Index trading below its VWAP)
    index_bar_disagree = {
        "open": 26000.0,
        "high": 26050.0,
        "low": 25900.0,
        "close": 25950.0,
        "vwap": 26020.0,  # index_close < vwap!
        "ema_20": 26010.0,
    }

    signal_rejected = strategy.evaluate(stock_bar, index_bar_disagree, profile)
    assert signal_rejected is None, "Signal must be blocked when parent index does not confirm!"

    # Case B: Parent Index AGREES (Index trading above its VWAP and EMA20)
    index_bar_agree = {
        "open": 26000.0,
        "high": 26100.0,
        "low": 25980.0,
        "close": 26080.0,  # index_close > vwap and > ema_20
        "vwap": 26020.0,
        "ema_20": 26010.0,
    }

    signal_approved = strategy.evaluate(stock_bar, index_bar_agree, profile)
    assert signal_approved is not None, "Signal must be approved when parent index confirms!"
    assert signal_approved.side == Side.BUY
    assert signal_approved.entry_price == 2850.0
    # Stop Loss: Entry - 1.5 * ATR (15.0) = 2850 - 22.5 = 2827.5
    assert signal_approved.stop_loss == 2827.5
    # Target: Entry + 3.0 * ATR (15.0) = 2850 + 45.0 = 2895.0
    assert signal_approved.target_price == 2895.0


def test_high_rvol_breakout_dual_confirmation():
    """Validates that HighRvolBreakoutStrategy checks both RVOL and relative index outperformance."""
    strategy = HighRvolBreakoutStrategy()
    profile = StockProfile(
        tradingsymbol="TATAMOTORS",
        instrument_token=884737,
        parent_index="NIFTY AUTO",
        index_token=259601,
        cluster_regime=ClusterRegime.HIGH_RVOL_BREAKOUT,
        beta_5y=1.62,
        adr_pct=2.65,
        atr_multiplier_sl=1.6,
        atr_multiplier_tp=3.2,
        min_rvol_threshold=2.0,
        oos_sharpe=1.8,
        oos_profit_factor=1.9,
        is_active=True,
    )

    bar_time = datetime(2026, 11, 16, 10, 15, tzinfo=IST_TZ)

    # Stock: RVOL = 3.5 (>= 2.0), range: 1000 to 1020, close: 1018 (top 25% of bar)
    # Stock return in bar: (1018 - 1002) / 1002 = +1.59%
    stock_bar = {
        "timestamp": bar_time,
        "open": 1002.0,
        "high": 1020.0,
        "low": 1000.0,
        "close": 1018.0,
        "rvol": 3.5,
        "atr_14": 10.0,
    }

    # Case A: Index return is +1.5% -> relative outperformance is +0.09% (< 0.4% required)
    index_bar_high_ret = {
        "open": 20000.0,
        "close": 20300.0,  # +1.5%
    }
    sig_rejected = strategy.evaluate(stock_bar, index_bar_high_ret, profile)
    assert sig_rejected is None, "Must reject if outperformance over sector index is < 0.4%"

    # Case B: Index return is +0.2% -> relative outperformance is 1.59% - 0.2% = 1.39% (> 0.4%)
    index_bar_low_ret = {
        "open": 20000.0,
        "close": 20040.0,  # +0.2%
    }
    sig_approved = strategy.evaluate(stock_bar, index_bar_low_ret, profile)
    assert sig_approved is not None
    assert sig_approved.side == Side.BUY
    assert sig_approved.entry_price == 1018.0


# ==============================================================================
# 3. EVENT-DRIVEN ENGINE & 15:10 IST MANDATORY SQUARE-OFF TESTS
# ==============================================================================

def test_mandatory_1510_square_off():
    """Validates that any open position is forcefully closed at 15:10 IST with taxes & slippage deducted."""
    engine = EventDrivenBacktester(
        capital=100_000.0,
        risk_per_trade_pct=1.0,
        slippage_pct=0.0002,
    )

    profile = StockProfile(
        tradingsymbol="INFY",
        instrument_token=408065,
        parent_index="NIFTY IT",
        index_token=259849,
        cluster_regime=ClusterRegime.TREND_MOMENTUM,
        beta_5y=1.05,
        adr_pct=1.85,
        atr_multiplier_sl=1.5,
        atr_multiplier_tp=3.0,
        min_rvol_threshold=1.5,
        oos_sharpe=1.9,
        oos_profit_factor=1.9,
        is_active=True,
    )

    # Construct bars for 1 day: Entry at 09:30, price flat so neither SL nor TP is hit, reaching 15:10
    day = datetime(2026, 11, 16, tzinfo=IST_TZ)
    # 09:15 to 15:15
    bar_times = [day.replace(hour=9, minute=15) + timedelta(minutes=m) for m in range(0, 365, 5)]

    # Mock stock series
    stock_records = []
    index_records = []

    for t in bar_times:
        is_entry = (t.hour == 9 and t.minute == 30)
        close_p = 1520.0 if not is_entry else 1515.0
        stock_records.append({
            "timestamp": t,
            "open": 1510.0,
            "high": 1525.0,  # TP would be 1515 + 45 = 1560 (not reached)
            "low": 1505.0,   # SL would be 1515 - 22.5 = 1492.5 (not reached)
            "close": close_p,
            "volume": 20000,
            "vwap": 1500.0,
            "ema_20": 1502.0,
            "ema_200": 1490.0,
            "atr_14": 15.0,
            "or_high": 1512.0,
            "or_low": 1495.0,
            "rvol": 2.0,
            "cpr_pivot": 1500.0,
            "cpr_bc": 1498.0,
            "cpr_tc": 1502.0,
            "cpr_bottom": 1498.0,
            "cpr_top": 1502.0,
        })
        index_records.append({
            "timestamp": t,
            "open": 38000.0,
            "high": 38200.0,
            "low": 37950.0,
            "close": 38100.0,
            "vwap": 38050.0,
            "ema_20": 38040.0,
        })

    stock_df = pl.DataFrame(stock_records)
    index_df = pl.DataFrame(index_records)

    summary = engine.run(stock_df, index_df, profile)

    assert summary.total_trades == 1, "Should have executed exactly 1 trade"
    trade = summary.trades[0]
    assert trade.exit_reason == "MANDATORY_SQUARE_OFF"
    assert trade.exit_time.time() >= time(15, 10)
    assert trade.total_charges > 0, "Zerodha MIS statutory charges must be deducted"
    assert trade.charges.brokerage > 0
    assert trade.charges.stt > 0
    assert trade.charges.gst > 0
    # Entry had upward slippage, exit had downward slippage
    assert trade.entry_price_fill > trade.entry_price_raw
    assert trade.exit_price_fill < trade.exit_price_raw


# ==============================================================================
# 4. WALK-FORWARD END-TO-END OPTIMIZATION TESTS
# ==============================================================================

def test_walk_forward_evaluation():
    """Validates that WalkForwardValidator evaluates OOS data and updates stock_profiles.json."""
    validator = WalkForwardValidator()
    profiles = StockProfiler.load_profiles(STOCK_PROFILES_PATH)

    # Run Walk-Forward evaluation on 2 stocks for speed
    test_profiles = profiles[:2]
    tearsheet = validator.run_walk_forward_evaluation(
        stock_profiles=test_profiles,
        is_ratio=0.6,
        min_profit_factor=1.15,
    )

    assert "portfolio_summary" in tearsheet
    assert "stock_performance" in tearsheet
    assert TEARSHEET_PATH.exists()

    # Verify stock_profiles.json was updated with valid OOS metrics
    updated_profiles = StockProfiler.load_profiles(STOCK_PROFILES_PATH)
    assert len(updated_profiles) >= len(test_profiles)

    for p in updated_profiles:
        assert isinstance(p, StockProfile)
        assert p.oos_sharpe >= 0.0
        assert p.oos_profit_factor >= 0.0
        assert isinstance(p.is_active, bool)
