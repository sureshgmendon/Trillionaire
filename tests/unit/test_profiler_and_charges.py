"""
Trillionaire Research Lab - Unit Test Suite
Validates:
1. Exact Zerodha MIS intraday statutory charge calculations down to the paisa (Long and Short).
2. Synthetic data generation and DuckDB zero-copy parquet querying.
3. Loading and validating research_lab/artifacts/stock_profiles.json into list[StockProfile].
"""

from pathlib import Path
import duckdb
import pytest

from research_lab.data_loader.kite_history import KiteHistoricalDownloader
from research_lab.data_loader.synthetic_generator import SyntheticDataGenerator
from research_lab.profiler.indian_charges import ChargesBreakdown, calculate_mis_net_pnl
from research_lab.profiler.stock_profiler import STOCK_PROFILES_PATH, StockProfiler
from shared.enums import ClusterRegime, Side
from shared.schemas import StockProfile


# ==============================================================================
# 1. ZERODHA MIS STATUTORY CHARGES CALCULATION TESTS
# ==============================================================================

def test_long_trade_charges_capped_brokerage():
    """
    Test BUY (Long) trade with turnover high enough to trigger the Rs. 20 brokerage cap per leg.
    Entry: Buy 100 shares @ Rs. 2500.00 (Buy Turnover = 2,50,000)
    Exit: Sell 100 shares @ Rs. 2550.00 (Sell Turnover = 2,55,000)
    Total Turnover = 5,05,000
    Gross PnL = Rs. 5000.00
    """
    res = calculate_mis_net_pnl(
        entry_price=2500.0,
        exit_price=2550.0,
        quantity=100,
        side=Side.BUY,
    )

    assert isinstance(res, ChargesBreakdown)
    assert res.side == Side.BUY
    assert res.quantity == 100
    assert res.buy_turnover == 250000.0
    assert res.sell_turnover == 255000.0
    assert res.total_turnover == 505000.0
    assert res.gross_pnl == 5000.0

    # Brokerage: min(20, 0.0003 * 250000) = 20, min(20, 0.0003 * 255000) = 20 -> 40.0
    assert res.brokerage == 40.0

    # STT: 0.025% on sell turnover (255,000) -> 63.75
    assert res.stt == 63.75

    # Exchange Txn: 0.00297% on total turnover (505,000) -> 15.00
    assert res.exchange_txn == round(0.0000297 * 505000, 2)

    # SEBI Charges: Rs. 10/crore on total turnover -> 0.51
    assert res.sebi_charges == round(0.000001 * 505000, 2)

    # Stamp Duty: 0.003% on buy turnover (250,000) -> 7.50
    assert res.stamp_duty == 7.50

    # GST: 18% on (Brokerage + Exchange + SEBI)
    expected_gst = round(0.18 * (40.0 + (0.0000297 * 505000) + (0.000001 * 505000)), 2)
    assert res.gst == expected_gst

    # Net PnL must be gross_pnl - total_charges
    assert res.net_pnl == round(res.gross_pnl - res.total_charges, 2)
    assert res.breakeven_points == round(res.total_charges / 100, 4)


def test_long_trade_charges_uncapped_brokerage():
    """
    Test small BUY trade where 0.03% is below the Rs. 20 cap.
    Entry: Buy 10 shares @ Rs. 1500 (Buy Turnover = 15,000, 0.03% = Rs. 4.50)
    Exit: Sell 10 shares @ Rs. 1510 (Sell Turnover = 15,100, 0.03% = Rs. 4.53)
    Total Brokerage = 4.50 + 4.53 = 9.03
    """
    res = calculate_mis_net_pnl(
        entry_price=1500.0,
        exit_price=1510.0,
        quantity=10,
        side=Side.BUY,
    )

    assert res.buy_turnover == 15000.0
    assert res.sell_turnover == 15100.0
    assert res.brokerage == 9.03
    assert res.gross_pnl == 100.0
    assert res.stt == round(0.00025 * 15100, 2)
    assert res.stamp_duty == round(0.00003 * 15000, 2)
    assert res.net_pnl > 0


def test_short_trade_charges():
    """
    Test SELL (Short) trade:
    Entry: Sell 100 shares @ Rs. 800.00 (Sell Turnover = 80,000)
    Exit: Buy back 100 shares @ Rs. 790.00 (Buy Turnover = 79,000)
    Total Turnover = 1,59,000
    Gross PnL = (800 - 790) * 100 = Rs. 1000.00
    STT charged on Entry (Sell leg = 80,000), Stamp Duty charged on Exit (Buy leg = 79,000).
    """
    res = calculate_mis_net_pnl(
        entry_price=800.0,
        exit_price=790.0,
        quantity=100,
        side=Side.SELL,
    )

    assert res.side == Side.SELL
    assert res.sell_turnover == 80000.0
    assert res.buy_turnover == 79000.0
    assert res.total_turnover == 159000.0
    assert res.gross_pnl == 1000.0

    # Brokerage: min(20, 24.0) + min(20, 23.70) = 20 + 20 = 40.0
    assert res.brokerage == 40.0

    # STT: 0.025% on sell turnover (80,000) -> 20.00
    assert res.stt == 20.00

    # Stamp Duty: 0.003% on buy turnover (79,000) -> 2.37
    assert res.stamp_duty == 2.37

    assert res.net_pnl == round(1000.0 - res.total_charges, 2)


def test_charges_invalid_inputs():
    with pytest.raises(ValueError):
        calculate_mis_net_pnl(entry_price=-100, exit_price=105, quantity=10, side=Side.BUY)

    with pytest.raises(ValueError):
        calculate_mis_net_pnl(entry_price=100, exit_price=105, quantity=0, side=Side.BUY)


# ==============================================================================
# 2. SYNTHETIC GENERATOR & DUCKDB PARQUET TESTS
# ==============================================================================

def test_synthetic_data_generation_and_duckdb_query(tmp_path: Path):
    generator = SyntheticDataGenerator(data_dir=tmp_path, seed=123)

    # 1. Generate 10-day synthetic index
    idx_df = generator.generate_index_series("TEST_INDEX", num_days=10, base_price=20000.0)
    assert len(idx_df) == 10 * 375
    assert set(idx_df.columns) == {
        "timestamp", "tradingsymbol", "open", "high", "low", "close", "volume", "oi"
    }

    # Verify high >= max(open, close) and low <= min(open, close)
    assert (idx_df["high"] >= idx_df["open"]).all()
    assert (idx_df["high"] >= idx_df["close"]).all()
    assert (idx_df["low"] <= idx_df["open"]).all()
    assert (idx_df["low"] <= idx_df["close"]).all()

    # 2. Generate correlated stock
    stock_df = generator.generate_correlated_stock_series(
        tradingsymbol="TEST_STOCK",
        parent_index_symbol="TEST_INDEX",
        beta=1.2,
        num_days=10,
    )
    assert len(stock_df) == 10 * 375

    # 3. Query via DuckDB zero-copy
    downloader = KiteHistoricalDownloader(data_dir=tmp_path)
    rel = downloader.query_with_duckdb("TEST_STOCK", sql_filter="volume > 0")
    count = rel.aggregate("count(*)").fetchall()[0][0]
    assert count == 10 * 375


# ==============================================================================
# 3. STOCK PROFILES ARTIFACT VERIFICATION
# ==============================================================================

def test_stock_profiles_artifact_validity():
    """Validates that research_lab/artifacts/stock_profiles.json strictly loads into list[StockProfile]."""
    assert STOCK_PROFILES_PATH.exists(), f"Missing required artifact: {STOCK_PROFILES_PATH}"

    profiles = StockProfiler.load_profiles(STOCK_PROFILES_PATH)
    assert len(profiles) == 100, f"Expected 100 profiles, found {len(profiles)}"

    for p in profiles:
        assert isinstance(p, StockProfile)
        assert isinstance(p.cluster_regime, ClusterRegime)
        assert p.instrument_token > 0
        assert p.index_token > 0
        assert isinstance(p.beta_5y, (int, float))
        assert p.adr_pct > 0
        assert p.atr_multiplier_sl > 0
        assert p.atr_multiplier_tp > 0
        assert p.min_rvol_threshold > 0
        assert p.oos_sharpe >= 0
        assert p.oos_profit_factor >= 0
        assert isinstance(p.is_active, bool)

    symbols = [p.tradingsymbol for p in profiles]
    assert "RELIANCE" in symbols
    assert "TCS" in symbols
    assert "HDFCBANK" in symbols
    assert "TMPV" in symbols
    assert "SUNPHARMA" in symbols
