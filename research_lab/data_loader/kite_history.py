"""
Trillionaire Research Lab - Kite Historical Data Ingestion Pipeline
Batched historical 1-minute OHLCV downloader with 60-day chunking, IST normalization,
and high-performance Parquet storage queried via DuckDB.
"""

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
import time
from zoneinfo import ZoneInfo
import duckdb
from loguru import logger
import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq

from shared.constants import IST_TZ

# Directory for high-performance tick & candle parquet storage
DATA_STORE_DIR = Path(__file__).resolve().parent.parent / "data_store"
DATA_STORE_DIR.mkdir(parents=True, exist_ok=True)

# Kite Connect Historical API rate limit: Max 3 requests/sec
KITE_HISTORICAL_CHUNK_DAYS = 60
KITE_REQUEST_DELAY_SEC = 0.35


class KiteHistoricalDownloader:
    """Production downloader managing Kite Connect historical API pagination and limits."""

    def __init__(self, kite_client: Optional[Any] = None, data_dir: Path = DATA_STORE_DIR):
        self.kite = kite_client
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def get_parquet_path(self, symbol: str) -> Path:
        """Returns the canonical Parquet file path for a symbol."""
        clean_symbol = symbol.replace(":", "_").replace(" ", "_")
        return self.data_dir / f"{clean_symbol}_1m.parquet"

    def fetch_symbol_history_chunked(
        self,
        instrument_token: int,
        tradingsymbol: str,
        from_datetime: datetime,
        to_datetime: datetime,
        interval: str = "minute",
    ) -> pl.DataFrame:
        """
        Fetches multi-year historical candles by partitioning the window into 60-day chunks.
        Normalizes all timestamps to IST (Asia/Kolkata).
        """
        if self.kite is None:
            raise RuntimeError(
                "KiteConnect instance required for live historical downloads. "
                "Use SyntheticDataGenerator for offline/synthetic data generation."
            )

        if from_datetime.tzinfo is None:
            from_datetime = from_datetime.replace(tzinfo=IST_TZ)
        if to_datetime.tzinfo is None:
            to_datetime = to_datetime.replace(tzinfo=IST_TZ)

        all_records: List[Dict[str, Any]] = []
        chunk_start = from_datetime

        logger.info(
            f"Starting historical download for {tradingsymbol} ({instrument_token}) "
            f"from {from_datetime.date()} to {to_datetime.date()} in {KITE_HISTORICAL_CHUNK_DAYS}-day batches."
        )

        while chunk_start < to_datetime:
            chunk_end = min(chunk_start + timedelta(days=KITE_HISTORICAL_CHUNK_DAYS), to_datetime)
            logger.debug(f"Fetching chunk {chunk_start.date()} -> {chunk_end.date()} for {tradingsymbol}...")

            try:
                records = self.kite.historical_data(
                    instrument_token=instrument_token,
                    from_date=chunk_start.strftime("%Y-%m-%d %H:%M:%S"),
                    to_date=chunk_end.strftime("%Y-%m-%d %H:%M:%S"),
                    interval=interval,
                    continuous=False,
                    oi=True,
                )
                if records:
                    all_records.extend(records)
                time.sleep(KITE_REQUEST_DELAY_SEC)
            except Exception as e:
                logger.error(
                    f"Failed chunk {chunk_start.date()} to {chunk_end.date()} for {tradingsymbol}: {e}"
                )
                raise

            chunk_start = chunk_end + timedelta(minutes=1)

        if not all_records:
            logger.warning(f"No records retrieved for {tradingsymbol}")
            return pl.DataFrame()

        df = pl.DataFrame(all_records)
        # Standardize column naming
        df = df.rename({
            "date": "timestamp",
        })

        # Ensure IST timezone
        df = df.with_columns([
            pl.col("timestamp").dt.convert_time_zone("Asia/Kolkata"),
            pl.lit(tradingsymbol).alias("tradingsymbol"),
            pl.lit(instrument_token).alias("instrument_token"),
        ])

        return df

    def save_to_parquet(self, df: pl.DataFrame, symbol: str) -> Path:
        """Persists DataFrame to optimized Parquet with ZSTD compression."""
        target_path = self.get_parquet_path(symbol)
        df.write_parquet(target_path, compression="zstd")
        logger.info(f"Saved {len(df):,} candles for {symbol} to {target_path}")
        return target_path

    def load_from_parquet(self, symbol: str) -> pl.DataFrame:
        """Reads Parquet file for a symbol into a Polars DataFrame."""
        target_path = self.get_parquet_path(symbol)
        if not target_path.exists():
            raise FileNotFoundError(f"Parquet file not found: {target_path}")
        return pl.read_parquet(target_path)

    def query_with_duckdb(self, symbol: str, sql_filter: str = "") -> duckdb.DuckDBPyRelation:
        """Executes zero-copy analytical SQL query directly on the symbol's Parquet file."""
        target_path = self.get_parquet_path(symbol)
        if not target_path.exists():
            raise FileNotFoundError(f"Parquet file not found: {target_path}")

        path_str = str(target_path).replace("\\", "/")
        query = f"SELECT * FROM read_parquet('{path_str}')"
        if sql_filter:
            query += f" WHERE {sql_filter}"
        return duckdb.query(query)
