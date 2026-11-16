"""
Trillionaire Trading Engine - Historical Tick Replayer
Synthesizes intra-minute Level-1 ticks from 1-minute Parquet datasets and replays them
chronologically onto the EventBus at configurable speeds (1x, 10x, 50x, MAX_SPEED).
"""

import asyncio
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from loguru import logger
import polars as pl

from config.settings import settings
from research_lab.data_loader.kite_history import DATA_STORE_DIR
from shared.constants import IST_TZ
from shared.schemas import Tick
from trading_engine.core.event_bus import EventBus, TickEvent, bus


class HistoricalTickReplayer:
    """Replays historical intraday sessions by generating realistic intra-minute ticks."""

    def __init__(
        self,
        event_bus: Optional[EventBus] = None,
        data_dir: Path = DATA_STORE_DIR,
    ):
        self.bus = event_bus or bus
        self.data_dir = data_dir
        self._load_instrument_metadata()

    def _load_instrument_metadata(self) -> None:
        """Loads instrument tokens for universe stocks and benchmark indices."""
        sector_data = settings.load_sector_map()
        self.tokens: Dict[str, int] = {}

        for idx_name, idx_meta in sector_data.get("benchmark_indices", {}).items():
            self.tokens[idx_name] = int(idx_meta.get("token", 256265))

        for s in sector_data.get("stocks", []):
            self.tokens[s["tradingsymbol"]] = int(s.get("instrument_token", 738561))

    def _get_parquet_path(self, symbol: str) -> Path:
        clean = symbol.replace(":", "_").replace(" ", "_")
        return self.data_dir / f"{clean}_1m.parquet"

    def synthesize_ticks_for_bar(
        self,
        symbol: str,
        token: int,
        bar: Dict[str, Any],
        start_cum_volume: int,
    ) -> List[Tick]:
        """
        Synthesizes 4 realistic intra-minute ticks from an OHLCV bar:
        Open -> Extremum 1 -> Extremum 2 -> Close with realistic bid/ask spread.
        """
        dt: datetime = bar["timestamp"]
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=IST_TZ)
        else:
            dt = dt.astimezone(IST_TZ)

        open_p = bar["open"]
        high_p = bar["high"]
        low_p = bar["low"]
        close_p = bar["close"]
        bar_vol = max(4, bar.get("volume", 4))
        oi = bar.get("oi", 0)

        # Distribute volume across 4 ticks
        vol_step = bar_vol // 4
        cum_vol = start_cum_volume

        ticks: List[Tick] = []

        if close_p >= open_p:
            # Bullish bar sequence: Open (00s) -> Low (15s) -> High (35s) -> Close (50s)
            trajectory = [
                (open_p, dt.replace(second=0)),
                (low_p, dt.replace(second=15)),
                (high_p, dt.replace(second=35)),
                (close_p, dt.replace(second=50)),
            ]
        else:
            # Bearish bar sequence: Open (00s) -> High (15s) -> Low (35s) -> Close (50s)
            trajectory = [
                (open_p, dt.replace(second=0)),
                (high_p, dt.replace(second=15)),
                (low_p, dt.replace(second=35)),
                (close_p, dt.replace(second=50)),
            ]

        for price, tick_dt in trajectory:
            cum_vol += vol_step
            # 2 bps bid-ask spread
            half_spread = max(0.05, round(price * 0.0001, 2))
            bid = round(price - half_spread, 2)
            ask = round(price + half_spread, 2)

            tick = Tick(
                instrument_token=token,
                tradingsymbol=symbol,
                timestamp_ist=tick_dt,
                ltp=round(price, 2),
                volume=cum_vol,
                bid=bid,
                ask=ask,
                oi=oi,
            )
            ticks.append(tick)

        return ticks

    async def replay_session(
        self,
        trading_date: Optional[date] = None,
        symbols: Optional[List[str]] = None,
        speed: str = "MAX_SPEED",  # 1x, 10x, 50x, MAX_SPEED
    ) -> int:
        """
        Replays a historical trading day across specified symbols and parent indices.
        Interleaves ticks chronologically to maintain cross-sectional consistency.
        Returns total ticks replayed.
        """
        if symbols is None:
            symbols = settings.get_tracked_symbols()[:5]  # Default top 5 for simulation

        # Also include parent sector indices
        sector_data = settings.load_sector_map()
        needed_indices = set()
        for s in sector_data.get("stocks", []):
            if s["tradingsymbol"] in symbols:
                needed_indices.add(s["parent_index"])

        all_instruments = list(symbols) + list(needed_indices)
        all_ticks: List[Tick] = []

        logger.info(
            f"Loading session data for {len(all_instruments)} instruments on {trading_date or 'latest'}..."
        )

        for sym in all_instruments:
            p_path = self._get_parquet_path(sym)
            if not p_path.exists():
                logger.warning(f"Parquet missing for {sym} at {p_path}. Skipping.")
                continue

            df = pl.read_parquet(p_path).sort("timestamp")
            if len(df) == 0:
                continue

            # Filter for specific date if provided
            if trading_date is not None:
                df = df.filter(pl.col("timestamp").dt.date() == trading_date)
            else:
                # Default to the first available date in dataset
                first_date = df["timestamp"][0].date()
                df = df.filter(pl.col("timestamp").dt.date() == first_date)

            if len(df) == 0:
                continue

            token = self.tokens.get(sym, 738561)
            bars = df.to_dicts()
            cum_vol = 0

            for bar in bars:
                bar_ticks = self.synthesize_ticks_for_bar(sym, token, bar, start_cum_volume=cum_vol)
                cum_vol = bar_ticks[-1].volume
                all_ticks.extend(bar_ticks)

        # Sort all ticks chronologically
        all_ticks.sort(key=lambda t: t.timestamp_ist)

        total_ticks = len(all_ticks)
        logger.info(f"Replaying {total_ticks:,} synthetic ticks at {speed} speed...")

        delay = 0.0
        if speed == "1x":
            delay = 0.05
        elif speed == "10x":
            delay = 0.005
        elif speed == "50x":
            delay = 0.001

        for i, tick in enumerate(all_ticks):
            event = TickEvent(tick=tick)
            await self.bus.publish(event)

            if delay > 0:
                await asyncio.sleep(delay)
            elif (i % 200) == 0:
                # Yield control to event loop at MAX_SPEED
                await asyncio.sleep(0)

        logger.info(f"Session replay complete: {total_ticks:,} ticks published onto EventBus.")
        return total_ticks

    # Alias for replay_session
    replay_day = replay_session
