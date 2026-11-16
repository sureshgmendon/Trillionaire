"""
Trillionaire Research Lab - Walk-Forward Validator & Profile Updater
Splits multi-year historical data into In-Sample (Years 1-3) and Out-of-Sample (Years 4-5) windows.
Evaluates realistic Out-of-Sample metrics (after slippage & Zerodha MIS taxes) and updates
research_lab/artifacts/stock_profiles.json and backtest_tearsheet.json.
"""

from datetime import datetime
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from loguru import logger
import polars as pl

from config.settings import settings
from research_lab.backtester.engine import BacktestSummary, EventDrivenBacktester
from research_lab.backtester.indicators import compute_all_indicators
from research_lab.data_loader.kite_history import KiteHistoricalDownloader
from research_lab.profiler.stock_profiler import STOCK_PROFILES_PATH, StockProfiler
from shared.schemas import StockProfile

ARTIFACTS_DIR = Path(__file__).resolve().parent.parent / "artifacts"
TEARSHEET_PATH = ARTIFACTS_DIR / "backtest_tearsheet.json"


class WalkForwardValidator:
    """Anti-overfitting walk-forward engine calibrating out-of-sample profitability."""

    def __init__(
        self,
        artifacts_dir: Path = ARTIFACTS_DIR,
        data_downloader: Optional[KiteHistoricalDownloader] = None,
        initial_capital: float = 100_000.0,
    ):
        self.artifacts_dir = artifacts_dir
        self.downloader = data_downloader or KiteHistoricalDownloader()
        self.initial_capital = initial_capital

    @staticmethod
    def split_data(
        df: pl.DataFrame, is_ratio: float = 0.6
    ) -> Tuple[pl.DataFrame, pl.DataFrame]:
        """
        Chronologically partitions dataset into:
        - In-Sample (e.g. first 60% of dates)
        - Out-of-Sample (e.g. remaining 40% of dates)
        """
        df_sorted = df.sort("timestamp")
        total_rows = len(df_sorted)
        split_idx = int(total_rows * is_ratio)
        in_sample = df_sorted[:split_idx]
        out_of_sample = df_sorted[split_idx:]
        return in_sample, out_of_sample

    def run_walk_forward_evaluation(
        self,
        stock_profiles: Optional[List[StockProfile]] = None,
        is_ratio: float = 0.6,
        min_profit_factor: float = 1.15,
    ) -> Dict[str, Any]:
        """
        Runs walk-forward backtest for all stocks in the active profile universe.
        Updates stock_profiles.json with validated out-of-sample metrics.
        Exports backtest_tearsheet.json.
        """
        # Load all existing profiles to preserve the full universe
        all_profiles = StockProfiler.load_profiles(STOCK_PROFILES_PATH)
        profile_map = {p.tradingsymbol: p for p in all_profiles}

        if stock_profiles is None:
            stock_profiles = all_profiles

        tearsheet_records: List[Dict[str, Any]] = []

        total_portfolio_gross = 0.0
        total_portfolio_charges = 0.0
        total_portfolio_net = 0.0
        total_portfolio_trades = 0

        logger.info(
            f"Initiating Walk-Forward Validation on {len(stock_profiles)} symbols "
            f"(Split ratio: {is_ratio * 100:.0f}% IS / {(1 - is_ratio) * 100:.0f}% OOS)..."
        )

        for profile in stock_profiles:
            sym = profile.tradingsymbol
            parent_idx = profile.parent_index

            try:
                # Load raw datasets
                stock_raw = self.downloader.load_from_parquet(sym)
                index_raw = self.downloader.load_from_parquet(parent_idx)
            except FileNotFoundError:
                logger.warning(f"Parquet files not found for {sym} or {parent_idx}. Skipping.")
                continue

            # Compute zero lookahead technical indicators
            stock_augmented = compute_all_indicators(stock_raw)
            index_augmented = compute_all_indicators(index_raw)

            # Split into In-Sample and Out-of-Sample
            _, stock_oos = self.split_data(stock_augmented, is_ratio=is_ratio)
            _, index_oos = self.split_data(index_augmented, is_ratio=is_ratio)

            # Run realistic backtest on Out-of-Sample partition
            backtester = EventDrivenBacktester(
                capital=self.initial_capital,
                risk_per_trade_pct=0.5,
                max_daily_loss_pct=1.5,
                max_mis_leverage=3.0,
                slippage_pct=0.0002,  # 2 bps adverse fill
            )
            summary: BacktestSummary = backtester.run(
                stock_df_with_indicators=stock_oos,
                index_df_with_indicators=index_oos,
                profile=profile,
            )

            # Evaluate out-of-sample profitability gate
            is_active = (summary.profit_factor >= min_profit_factor) and (summary.net_pnl > 0)

            # Calibrate realistic Sharpe & Profit Factor
            oos_sharpe = max(0.0, summary.sharpe_ratio) if summary.total_trades >= 5 else profile.oos_sharpe
            oos_pf = max(0.0, summary.profit_factor) if summary.total_trades >= 5 else profile.oos_profit_factor

            updated_profile = StockProfile(
                tradingsymbol=profile.tradingsymbol,
                instrument_token=profile.instrument_token,
                parent_index=profile.parent_index,
                index_token=profile.index_token,
                cluster_regime=profile.cluster_regime,
                beta_5y=profile.beta_5y,
                adr_pct=profile.adr_pct,
                atr_multiplier_sl=profile.atr_multiplier_sl,
                atr_multiplier_tp=profile.atr_multiplier_tp,
                min_rvol_threshold=profile.min_rvol_threshold,
                oos_sharpe=round(oos_sharpe, 2),
                oos_profit_factor=round(oos_pf, 2),
                is_active=is_active,
            )
            profile_map[sym] = updated_profile

            # Aggregate portfolio statistics
            total_portfolio_gross += summary.gross_pnl
            total_portfolio_charges += summary.total_charges
            total_portfolio_net += summary.net_pnl
            total_portfolio_trades += summary.total_trades

            tearsheet_records.append({
                "tradingsymbol": sym,
                "parent_index": parent_idx,
                "cluster_regime": profile.cluster_regime.value,
                "total_trades": summary.total_trades,
                "win_rate_pct": summary.win_rate_pct,
                "gross_pnl": summary.gross_pnl,
                "total_charges": summary.total_charges,
                "net_pnl": summary.net_pnl,
                "profit_factor": summary.profit_factor,
                "sharpe_ratio": summary.sharpe_ratio,
                "max_drawdown_pct": summary.max_drawdown_pct,
                "is_active_validated": is_active,
            })

        final_profiles = list(profile_map.values())
        # Save updated stock_profiles.json preserving all symbols
        with open(STOCK_PROFILES_PATH, "w", encoding="utf-8") as f:
            json.dump([p.model_dump() for p in final_profiles], f, indent=2)

        # Save backtest_tearsheet.json
        tearsheet = {
            "evaluation_time_ist": datetime.now().isoformat(),
            "in_sample_ratio": is_ratio,
            "out_of_sample_ratio": round(1.0 - is_ratio, 2),
            "portfolio_summary": {
                "total_trades": total_portfolio_trades,
                "gross_pnl_inr": round(total_portfolio_gross, 2),
                "total_statutory_charges_inr": round(total_portfolio_charges, 2),
                "net_realized_pnl_inr": round(total_portfolio_net, 2),
                "active_symbols_count": sum(1 for p in final_profiles if p.is_active),
                "total_universe_count": len(final_profiles),
            },
            "stock_performance": tearsheet_records,
        }

        with open(TEARSHEET_PATH, "w", encoding="utf-8") as f:
            json.dump(tearsheet, f, indent=2)

        logger.info(
            f"Walk-Forward complete: {tearsheet['portfolio_summary']['active_symbols_count']}/"
            f"{len(final_profiles)} symbols active. Tearsheet saved to {TEARSHEET_PATH}"
        )

        return tearsheet
