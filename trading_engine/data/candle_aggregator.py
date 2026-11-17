"""
Trillionaire Trading Engine - Real-Time Candle & Indicator Aggregator
Consumes Level-1 Tick streams and aggregates M1, M5, and M15 bars aligned strictly
to IST market clock boundaries (09:15:00, 09:20:00, etc.).
Maintains zero look-ahead incremental indicator state:
- Daily VWAP resetting at 09:15 IST
- Incremental EMA(20) and EMA(200)
- Wilder's ATR(14)
- Time-of-day RVOL against historical 20-day baselines
- 09:15-09:30 Opening Range tracking
"""

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from loguru import logger
import numpy as np
import polars as pl

from config.settings import settings
from shared.constants import IST_TZ
from shared.enums import Timeframe
from shared.schemas import Candle, Tick
from trading_engine.core.event_bus import CandleCloseEvent, EventBus, TickEvent, bus


@dataclass
class TimeframeState:
    """Incremental streaming state for a specific (symbol, timeframe) pair."""
    symbol: str
    timeframe: Timeframe
    current_bucket_start: Optional[datetime] = None
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    volume: int = 0
    ticks_count: int = 0

    # Incremental Technical Indicators
    current_trade_date: Optional[date] = None
    vwap_cum_pv: float = 0.0
    vwap_cum_vol: float = 0.0
    ema_20: Optional[float] = None
    ema_200: Optional[float] = None
    atr_14: float = 5.0
    prev_close: Optional[float] = None
    atr_warmup_count: int = 0

    # Time-of-day historical volume baselines (time_slot -> mean volume)
    rvol_baselines: Dict[time, float] = field(default_factory=dict)

    # Opening Range tracking (09:15 - 09:30 IST)
    or_high: Optional[float] = None
    or_low: Optional[float] = None
    or_frozen: bool = False


class CandleAggregator:
    """Real-time aggregator producing multi-timeframe candles with streaming indicators."""

    def __init__(
        self,
        event_bus: Optional[EventBus] = None,
        timeframes: Optional[List[Timeframe]] = None,
    ):
        self.bus = event_bus or bus
        self.timeframes = timeframes or [Timeframe.M1, Timeframe.M5, Timeframe.M15]
        # (symbol, timeframe) -> TimeframeState
        self._states: Dict[Tuple[str, Timeframe], TimeframeState] = {}
        # symbol -> last cumulative volume from broker tick
        self._last_tick_volumes: Dict[str, int] = {}
        # symbol -> latest closed candles per timeframe
        self._latest_closed_candles: Dict[Tuple[str, Timeframe], Candle] = {}

    def get_state(self, symbol: str, timeframe: Timeframe) -> TimeframeState:
        key = (symbol, timeframe)
        if key not in self._states:
            self._states[key] = TimeframeState(symbol=symbol, timeframe=timeframe)
        return self._states[key]

    def seed_historical_baselines(
        self,
        symbol: str,
        initial_ema_200: Optional[float] = None,
        initial_ema_20: Optional[float] = None,
        initial_atr_14: Optional[float] = None,
        rvol_baselines: Optional[Dict[time, float]] = None,
    ) -> None:
        """Seeds initial indicator values and time-of-day RVOL baselines for an instrument."""
        for tf in self.timeframes:
            state = self.get_state(symbol, tf)
            if initial_ema_200 is not None:
                state.ema_200 = initial_ema_200
            if initial_ema_20 is not None:
                state.ema_20 = initial_ema_20
            if initial_atr_14 is not None:
                state.atr_14 = initial_atr_14
            if rvol_baselines is not None:
                state.rvol_baselines.update(rvol_baselines)

    def warmup_from_parquet(
        self,
        symbols: Optional[List[str]] = None,
        data_dir: Optional[Path] = None,
        bars_count: int = 250,
    ) -> Dict[str, Dict[str, float]]:
        """
        Loads the last 250 1-minute historical bars per symbol from research_lab/data_store/
        so EMA(200), EMA(20), and ATR(14) are 100% warmed up and ready right at 09:25 AM IST.
        """
        store_path = data_dir or (Path(__file__).resolve().parent.parent.parent / "research_lab" / "data_store")
        if not store_path.exists():
            logger.warning(f"[WARMUP] Historical data store directory not found: {store_path}")
            return {}

        target_symbols = list(symbols) if symbols is not None else settings.get_tracked_symbols()
        # Also include any parent index symbols
        sector_data = settings.load_sector_map()
        indices = list(sector_data.get("benchmark_indices", {}).keys())
        all_to_warm = list(dict.fromkeys(target_symbols + indices))

        warmed: Dict[str, Dict[str, float]] = {}

        for sym in all_to_warm:
            # Check filename candidates
            candidates = [
                store_path / f"{sym}_1m.parquet",
                store_path / f"{sym.replace(' ', '_')}_1m.parquet",
                store_path / f"{sym.replace('-', '_')}_1m.parquet",
            ]
            parquet_file = next((f for f in candidates if f.exists()), None)
            if parquet_file is None:
                continue

            try:
                df = pl.read_parquet(parquet_file, columns=["close", "high", "low"])
                if len(df) == 0:
                    continue

                tail_df = df.tail(bars_count)
                closes = tail_df["close"].to_numpy()
                highs = tail_df["high"].to_numpy()
                lows = tail_df["low"].to_numpy()

                n = len(closes)
                if n == 0:
                    continue

                alpha_200 = 2.0 / (200 + 1)
                alpha_20 = 2.0 / (20 + 1)
                alpha_atr = 1.0 / 14.0

                ema_200 = float(closes[0])
                ema_20 = float(closes[0])
                atr_14 = float(highs[0] - lows[0])
                prev_c = float(closes[0])

                for i in range(1, n):
                    c = float(closes[i])
                    h = float(highs[i])
                    l = float(lows[i])
                    ema_200 = c * alpha_200 + ema_200 * (1.0 - alpha_200)
                    ema_20 = c * alpha_20 + ema_20 * (1.0 - alpha_20)
                    tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
                    atr_14 = atr_14 * (1.0 - alpha_atr) + tr * alpha_atr
                    prev_c = c

                # Seed into state for all timeframes
                for tf in self.timeframes:
                    st = self.get_state(sym, tf)
                    st.ema_200 = round(ema_200, 2)
                    st.ema_20 = round(ema_20, 2)
                    st.atr_14 = round(atr_14, 2)
                    st.prev_close = round(prev_c, 2)

                warmed[sym] = {
                    "ema_200": round(ema_200, 2),
                    "ema_20": round(ema_20, 2),
                    "atr_14": round(atr_14, 2),
                    "prev_close": round(prev_c, 2),
                    "bars_loaded": n,
                }
            except Exception as e:
                logger.warning(f"[WARMUP] Error warming up {sym} from {parquet_file.name}: {e}")

        logger.info(f"[WARMUP COMPLETE] Pre-warmed indicators for {len(warmed)} instruments from historical parquet store")
        return warmed

    def _get_bucket_start_time(self, dt: datetime, timeframe: Timeframe) -> datetime:
        """
        Calculates IST market-aligned candle bucket start time.
        09:15 is the reference market open anchor.
        """
        dt_ist = dt.astimezone(IST_TZ) if dt.tzinfo else dt.replace(tzinfo=IST_TZ)
        mins_step = timeframe.minutes

        if mins_step == 1:
            return dt_ist.replace(second=0, microsecond=0)

        # Anchor to 09:15:00
        ref_minute = 15
        total_mins_since_midnight = dt_ist.hour * 60 + dt_ist.minute
        open_mins = 9 * 60 + ref_minute

        if total_mins_since_midnight < open_mins:
            # Pre-market: anchor to standard minute modulo
            bucket_min = (dt_ist.minute // mins_step) * mins_step
            return dt_ist.replace(minute=bucket_min, second=0, microsecond=0)

        elapsed = total_mins_since_midnight - open_mins
        bucket_index = elapsed // mins_step
        bucket_total_mins = open_mins + bucket_index * mins_step

        b_hour = bucket_total_mins // 60
        b_min = bucket_total_mins % 60
        return dt_ist.replace(hour=b_hour, minute=b_min, second=0, microsecond=0)

    async def on_tick_event(self, event: TickEvent) -> None:
        """Async event bus listener for TickEvent."""
        await self.process_tick(event.tick)

    async def process_tick(self, tick: Tick) -> List[CandleCloseEvent]:
        """
        Processes a Level-1 tick across all subscribed timeframes.
        Returns list of CandleCloseEvents emitted due to bar rollovers.
        """
        symbol = tick.tradingsymbol
        dt_ist = tick.timestamp_ist
        if dt_ist.tzinfo is None:
            dt_ist = dt_ist.replace(tzinfo=IST_TZ)
        else:
            dt_ist = dt_ist.astimezone(IST_TZ)

        # Calculate incremental tick volume
        last_cum_vol = self._last_tick_volumes.get(symbol)
        if last_cum_vol is not None and tick.volume >= last_cum_vol:
            tick_incremental_vol = tick.volume - last_cum_vol
        else:
            tick_incremental_vol = max(1, tick.volume if tick.volume > 0 else 1)
        self._last_tick_volumes[symbol] = tick.volume

        closed_events: List[CandleCloseEvent] = []

        for tf in self.timeframes:
            state = self.get_state(symbol, tf)
            bucket_start = self._get_bucket_start_time(dt_ist, tf)

            # Check if previous bar closed (rollover)
            if state.current_bucket_start is not None and bucket_start > state.current_bucket_start:
                # Finalize closed bar
                closed_candle = self._finalize_candle(state)
                close_event = CandleCloseEvent(candle=closed_candle)
                closed_events.append(close_event)
                self._latest_closed_candles[(symbol, tf)] = closed_candle

                # Publish onto EventBus immediately
                await self.bus.publish(close_event)

                # Initialize new bar
                state.current_bucket_start = bucket_start
                state.open = tick.ltp
                state.high = tick.ltp
                state.low = tick.ltp
                state.close = tick.ltp
                state.volume = tick_incremental_vol
                state.ticks_count = 1
            elif state.current_bucket_start is None:
                # First bar
                state.current_bucket_start = bucket_start
                state.open = tick.ltp
                state.high = tick.ltp
                state.low = tick.ltp
                state.close = tick.ltp
                state.volume = tick_incremental_vol
                state.ticks_count = 1
            else:
                # Update ongoing bar
                state.high = max(state.high, tick.ltp)
                state.low = min(state.low, tick.ltp)
                state.close = tick.ltp
                state.volume += tick_incremental_vol
                state.ticks_count += 1

            # Update Opening Range (09:15 - 09:30)
            self._update_opening_range(state, dt_ist, tick.ltp)

        return closed_events

    def _finalize_candle(self, state: TimeframeState) -> Candle:
        """Finalizes closed bar and updates streaming technical indicators."""
        bar_close_time = state.current_bucket_start + timedelta(minutes=state.timeframe.minutes)
        trade_date = bar_close_time.date()

        # 1. VWAP Reset & Accumulation
        if state.current_trade_date != trade_date:
            state.current_trade_date = trade_date
            state.vwap_cum_pv = 0.0
            state.vwap_cum_vol = 0.0
            state.or_high = None
            state.or_low = None
            state.or_frozen = False

        typical_price = (state.high + state.low + state.close) / 3.0
        bar_vol = max(1, state.volume)
        state.vwap_cum_pv += typical_price * bar_vol
        state.vwap_cum_vol += bar_vol
        vwap = round(state.vwap_cum_pv / state.vwap_cum_vol, 2)

        # 2. Incremental EMA(200)
        alpha_200 = 2.0 / (200 + 1)
        if state.ema_200 is None:
            state.ema_200 = state.close
        else:
            state.ema_200 = round(state.close * alpha_200 + state.ema_200 * (1.0 - alpha_200), 2)

        # 3. Incremental EMA(20)
        alpha_20 = 2.0 / (20 + 1)
        if state.ema_20 is None:
            state.ema_20 = state.close
        else:
            state.ema_20 = round(state.close * alpha_20 + state.ema_20 * (1.0 - alpha_20), 2)

        # 4. Wilder's ATR(14)
        if state.prev_close is None:
            tr = state.high - state.low
        else:
            tr = max(
                state.high - state.low,
                abs(state.high - state.prev_close),
                abs(state.low - state.prev_close),
            )
        state.prev_close = state.close

        alpha_atr = 1.0 / 14.0
        state.atr_14 = round(state.atr_14 * (1.0 - alpha_atr) + tr * alpha_atr, 2)

        # 5. Time-of-Day RVOL
        slot_time = state.current_bucket_start.time()
        baseline_vol = state.rvol_baselines.get(slot_time, 50_000.0)
        rvol = round(bar_vol / baseline_vol, 2) if baseline_vol > 0 else 1.0

        return Candle(
            tradingsymbol=state.symbol,
            timeframe=state.timeframe,
            timestamp_ist=bar_close_time,
            open=round(state.open, 2),
            high=round(state.high, 2),
            low=round(state.low, 2),
            close=round(state.close, 2),
            volume=bar_vol,
            vwap=vwap,
            ema_200=state.ema_200,
            atr_14=state.atr_14,
            rvol=rvol,
        )

    def _update_opening_range(self, state: TimeframeState, dt: datetime, price: float) -> None:
        """Tracks the 09:15-09:30 Opening Range High and Low, locking it after 09:30."""
        bar_time = dt.time()
        if bar_time < time(9, 15):
            return

        if bar_time <= time(9, 30):
            if state.or_high is None or price > state.or_high:
                state.or_high = price
            if state.or_low is None or price < state.or_low:
                state.or_low = price
        else:
            state.or_frozen = True

    def get_latest_candle(self, symbol: str, timeframe: Timeframe) -> Optional[Candle]:
        """Returns the most recent finalized candle for an instrument."""
        return self._latest_closed_candles.get((symbol, timeframe))

    def get_opening_range(self, symbol: str) -> Tuple[Optional[float], Optional[float]]:
        """Returns current Opening Range (OR_High, OR_Low) for symbol."""
        state = self.get_state(symbol, Timeframe.M1)
        return state.or_high, state.or_low
