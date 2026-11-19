"""
Trillionaire Research Lab - Synthetic Correlated 1-Minute OHLCV Generator
Generates realistic multi-year 1-minute historical datasets incorporating:
- Geometric Brownian Motion with sector index beta correlation
- Intraday U-shaped volume curve (open rush, lunch lull, close surge)
- Opening-range (first 15-30 min) volatility clustering and spikes
- High >= max(Open, Close) and Low <= min(Open, Close) consistency
"""

import os
import tempfile
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo
from loguru import logger
import numpy as np
import polars as pl

from config.settings import settings
from shared.constants import IST_TZ

DATA_STORE_DIR = Path(__file__).resolve().parent.parent / "data_store"
try:
    DATA_STORE_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    pass

FALLBACK_DATA_STORE_DIR = (
    Path("/tmp/trillionaire_data_store") if os.name != "nt" else Path(tempfile.gettempdir()) / "trillionaire_data_store"
)

# NSE Intraday trading day specs: 09:15 to 15:30 = 375 minutes
MINUTES_PER_DAY = 375
TRADING_START_TIME = time(9, 15)


def generate_intraday_volume_profile(minutes: int = MINUTES_PER_DAY) -> np.ndarray:
    """
    Simulates the characteristic Indian equity U-shaped volume distribution:
    - Peak in the opening 45 minutes (09:15 - 10:00)
    - Trough during midday lunch period (11:30 - 13:30)
    - Volume surge into square-off / closing (14:15 - 15:30)
    """
    t = np.linspace(0, 1, minutes)
    # Quadratic curve + opening surge exponential decay
    u_curve = 3.5 * (t - 0.5) ** 2 + 0.35
    opening_surge = 1.8 * np.exp(-t * 12.0)
    closing_surge = 1.2 * np.exp((t - 1.0) * 10.0)
    profile = u_curve + opening_surge + closing_surge
    return profile / np.sum(profile)


def generate_intraday_volatility_profile(minutes: int = MINUTES_PER_DAY) -> np.ndarray:
    """
    Simulates intraday volatility clustering:
    - Opening range volatility multiplier (1.8x to 2.5x in first 15-30 min)
    - Compressed mid-day volatility
    - Moderately elevated end-of-day volatility
    """
    t = np.linspace(0, 1, minutes)
    vol_mult = 1.0 + 1.8 * np.exp(-t * 15.0) + 0.6 * np.exp((t - 1.0) * 8.0)
    return vol_mult


class SyntheticDataGenerator:
    """Vectorized generator of realistic correlated intraday equity datasets."""

    def __init__(self, data_dir: Path = DATA_STORE_DIR, seed: int = 42):
        self.data_dir = self._resolve_writable_dir(data_dir)
        self.rng = np.random.default_rng(seed)
        self.vol_profile = generate_intraday_volatility_profile()
        self.vol_curve = generate_intraday_volume_profile()

    @classmethod
    def _resolve_writable_dir(cls, preferred_dir: Path) -> Path:
        """Ensures the directory is writable, falling back to FALLBACK_DATA_STORE_DIR if permissions are denied."""
        try:
            preferred_dir.mkdir(parents=True, exist_ok=True)
            if not os.access(preferred_dir, os.W_OK):
                raise PermissionError(f"Directory {preferred_dir} is not writable (os.W_OK is False)")
            probe = preferred_dir / ".perm_check"
            probe.touch(exist_ok=True)
            probe.unlink(missing_ok=True)
            return preferred_dir
        except (PermissionError, OSError) as e:
            fallback = FALLBACK_DATA_STORE_DIR
            fallback.mkdir(parents=True, exist_ok=True)
            logger.warning(
                f"[SYNTHETIC GENERATOR] Directory {preferred_dir} is not writable ({e}). "
                f"Falling back to {fallback}."
            )
            return fallback

    def get_parquet_path(self, symbol: str) -> Path:
        clean_symbol = symbol.replace(":", "_").replace(" ", "_")
        return self.data_dir / f"{clean_symbol}_1m.parquet"

    def _generate_trading_days(self, num_days: int, end_date: Optional[datetime] = None) -> List[datetime]:
        """Generates list of weekday dates going backwards from end_date."""
        if end_date is None:
            end_date = datetime.now(IST_TZ).replace(hour=15, minute=30, second=0, microsecond=0)
        elif end_date.tzinfo is None:
            end_date = end_date.replace(tzinfo=IST_TZ)

        dates: List[datetime] = []
        curr = end_date
        while len(dates) < num_days:
            if curr.weekday() < 5:  # Monday to Friday
                dates.append(curr.replace(hour=9, minute=15, second=0, microsecond=0))
            curr -= timedelta(days=1)
        dates.reverse()
        return dates

    def generate_index_series(
        self,
        index_name: str,
        num_days: int = 1250,  # ~5 years of trading days
        base_price: float = 24000.0,
        annual_volatility: float = 0.14,
    ) -> pl.DataFrame:
        """Generates 1-minute historical OHLCV series for a benchmark sector index."""
        days = self._generate_trading_days(num_days)
        total_bars = num_days * MINUTES_PER_DAY

        dt = 1.0 / (250 * MINUTES_PER_DAY)
        sigma_bar = annual_volatility * np.sqrt(dt)

        # Replicate daily intraday volatility profile across all days
        full_vol_mult = np.tile(self.vol_profile, num_days)
        random_shocks = self.rng.standard_normal(total_bars) * sigma_bar * full_vol_mult

        # Log returns -> price series
        log_returns = random_shocks
        prices = base_price * np.exp(np.cumsum(log_returns))

        # Build 1-minute OHLCV
        highs = prices * (1.0 + np.abs(self.rng.normal(0, 0.0006, total_bars)))
        lows = prices * (1.0 - np.abs(self.rng.normal(0, 0.0006, total_bars)))
        opens = np.roll(prices, 1)
        opens[0] = base_price
        closes = prices

        # Guarantee high >= max(open, close) and low <= min(open, close)
        highs = np.maximum(highs, np.maximum(opens, closes))
        lows = np.minimum(lows, np.minimum(opens, closes))

        base_daily_volume = 15_000_000
        daily_volume_mult = self.rng.uniform(0.7, 1.5, num_days)
        volumes = np.repeat(daily_volume_mult * base_daily_volume, MINUTES_PER_DAY) * np.tile(
            self.vol_curve, num_days
        )
        volumes = np.maximum(1, volumes.astype(np.int64))

        # Generate timestamps
        timestamps: List[datetime] = []
        for d in days:
            for m in range(MINUTES_PER_DAY):
                timestamps.append(d + timedelta(minutes=m))

        df = pl.DataFrame({
            "timestamp": timestamps,
            "tradingsymbol": [index_name] * total_bars,
            "open": opens.round(2),
            "high": highs.round(2),
            "low": lows.round(2),
            "close": closes.round(2),
            "volume": volumes,
            "oi": [0] * total_bars,
        }).with_columns(
            pl.col("timestamp").dt.convert_time_zone("Asia/Kolkata")
        )

        target_path = self.get_parquet_path(index_name)
        df.write_parquet(target_path, compression="zstd")
        logger.info(f"Generated {total_bars:,} synthetic bars for {index_name} -> {target_path}")
        return df

    def generate_correlated_stock_series(
        self,
        tradingsymbol: str,
        parent_index_symbol: str,
        beta: float = 1.0,
        base_price: float = 1500.0,
        adr_pct: float = 1.8,
        num_days: int = 1250,
        average_daily_volume: int = 2_500_000,
    ) -> pl.DataFrame:
        """
        Generates stock OHLCV correlated to its parent sector index:
        R_stock = beta * R_index + idiosyncratic_shock
        """
        # Ensure parent index exists
        index_parquet = self.get_parquet_path(parent_index_symbol)
        if not index_parquet.exists():
            _ = self.generate_index_series(parent_index_symbol, num_days=num_days)

        index_df = pl.read_parquet(index_parquet)
        if len(index_df) < num_days * MINUTES_PER_DAY:
            # Recreate with sufficient bars if needed
            index_df = self.generate_index_series(parent_index_symbol, num_days=num_days)

        # Slice matching length
        total_bars = num_days * MINUTES_PER_DAY
        index_close = index_df["close"].to_numpy()[-total_bars:]
        index_timestamps = index_df["timestamp"].to_list()[-total_bars:]

        index_returns = np.diff(np.log(index_close), prepend=np.log(index_close[0]))

        # Idiosyncratic component
        dt = 1.0 / (250 * MINUTES_PER_DAY)
        annual_stock_vol = (adr_pct / 100.0) * np.sqrt(250)
        stock_sigma_bar = annual_stock_vol * np.sqrt(dt)

        full_vol_mult = np.tile(self.vol_profile, num_days)
        idio_shocks = (
            self.rng.standard_normal(total_bars)
            * stock_sigma_bar
            * np.sqrt(max(0.05, 1.0 - 0.4 * min(1.0, beta)))
            * full_vol_mult
        )

        stock_returns = beta * index_returns + idio_shocks
        prices = base_price * np.exp(np.cumsum(stock_returns))

        bar_noise = np.abs(self.rng.normal(0, stock_sigma_bar * 0.8, total_bars))
        highs = prices * (1.0 + bar_noise)
        lows = prices * (1.0 - bar_noise)
        opens = np.roll(prices, 1)
        opens[0] = base_price
        closes = prices

        highs = np.maximum(highs, np.maximum(opens, closes))
        lows = np.minimum(lows, np.minimum(opens, closes))

        daily_volume_mult = self.rng.uniform(0.6, 1.7, num_days)
        daily_vol_per_bar = (average_daily_volume * daily_volume_mult)
        volumes = np.repeat(daily_vol_per_bar, MINUTES_PER_DAY) * np.tile(self.vol_curve, num_days)
        volumes = np.maximum(1, volumes.astype(np.int64))

        df = pl.DataFrame({
            "timestamp": index_timestamps,
            "tradingsymbol": [tradingsymbol] * total_bars,
            "open": opens.round(2),
            "high": highs.round(2),
            "low": lows.round(2),
            "close": closes.round(2),
            "volume": volumes,
            "oi": [0] * total_bars,
        }).with_columns(
            pl.col("timestamp").dt.convert_time_zone("Asia/Kolkata")
        )

        target_path = self.get_parquet_path(tradingsymbol)
        df.write_parquet(target_path, compression="zstd")
        logger.info(
            f"Generated {total_bars:,} synthetic bars for {tradingsymbol} "
            f"(Beta: {beta}, Parent: {parent_index_symbol}) -> {target_path}"
        )
        return df

    def generate_all_sector_universe(self, num_days: int = 1250) -> None:
        """Generates correlated synthetic historical data for all 25 stocks and 6 indices."""
        sector_data = settings.load_sector_map()
        indices = sector_data.get("benchmark_indices", {})
        stocks = sector_data.get("stocks", [])

        # 1. Generate Indices first
        for idx_name in indices.keys():
            logger.info(f"Synthesizing index series for {idx_name} ({num_days} days)...")
            self.generate_index_series(idx_name, num_days=num_days)

        # 2. Generate Stocks correlated to their parent index
        for stock in stocks:
            sym = stock["tradingsymbol"]
            parent_idx = stock["parent_index"]
            beta = float(stock.get("beta_5y", 1.0))
            adr_pct = float(stock.get("adr_pct", 1.8))
            logger.info(f"Synthesizing stock series for {sym} (Parent: {parent_idx}, Beta: {beta})...")
            self.generate_correlated_stock_series(
                tradingsymbol=sym,
                parent_index_symbol=parent_idx,
                beta=beta,
                adr_pct=adr_pct,
                num_days=num_days,
            )
