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
from typing import Any, Dict, List, Optional, Tuple
import httpx
from loguru import logger
import numpy as np
import polars as pl

from config.settings import settings
from shared.constants import IST_TZ
from shared.enums import Timeframe
from shared.schemas import Candle, Tick
from trading_engine.core.event_bus import CandleCloseEvent, EventBus, TickEvent, bus

YAHOO_INDEX_MAP: Dict[str, str] = {
    "NIFTY 50": "^NSEI",
    "NIFTY BANK": "^NSEBANK",
    "NIFTY IT": "^CNXIT",
    "NIFTY AUTO": "^CNXAUTO",
    "NIFTY PHARMA": "^CNXPHARMA",
    "NIFTY ENERGY": "^CNXENERGY",
    "NIFTY FMCG": "^CNXFMCG",
    "NIFTY METAL": "^CNXMETAL",
}

YAHOO_SYMBOL_OVERRIDES: Dict[str, str] = {
    "TMPV": "TMPV.NS",
    "TATAMOTORS": "TMPV.NS",
    "MCDOWELL-N": "UNITDSPR.NS",
    "LTIM": "LTM.NS",
}


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
        kite_client: Optional[Any] = None,
        instrument_sync: Optional[Any] = None,
        fetch_live_fallback: bool = True,
    ) -> Dict[str, Dict[str, float]]:
        """
        Loads historical bars per symbol from research_lab/data_store/*.parquet
        with automatic live history fallback (via KiteConnect historical data or Yahoo Finance)
        for any missing instruments, so EMA(200), EMA(20), and ATR(14) are 100% warmed up.
        """
        store_path = data_dir or (Path(__file__).resolve().parent.parent.parent / "research_lab" / "data_store")
        target_symbols = list(symbols) if symbols is not None else settings.get_tracked_symbols()
        # Also include any parent index symbols
        sector_data = settings.load_sector_map()
        indices = list(sector_data.get("benchmark_indices", {}).keys())
        all_to_warm = list(dict.fromkeys(target_symbols + indices))

        warmed: Dict[str, Dict[str, float]] = {}

        if not store_path.exists():
            logger.warning(f"[WARMUP] Historical data store directory not found: {store_path}. Falling back to live history...")
            if fetch_live_fallback:
                return self.warmup_from_live_history(
                    symbols=all_to_warm,
                    kite_client=kite_client,
                    instrument_sync=instrument_sync,
                    data_dir=store_path,
                )
            return {}

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
                    "source": "parquet",
                }
            except Exception as e:
                logger.warning(f"[WARMUP] Error warming up {sym} from {parquet_file.name}: {e}")

        # Check for missing symbols that need live history fallback
        missing = [s for s in all_to_warm if s not in warmed]
        if missing and fetch_live_fallback:
            logger.info(f"[WARMUP] {len(missing)} instruments missing local parquet data. Initiating live history fallback...")
            live_warmed = self.warmup_from_live_history(
                symbols=missing,
                kite_client=kite_client,
                instrument_sync=instrument_sync,
                data_dir=store_path,
            )
            warmed.update(live_warmed)

        logger.info(f"[WARMUP COMPLETE] Pre-warmed indicators for {len(warmed)} instruments (Total requested: {len(all_to_warm)})")
        return warmed

    def warmup_from_live_history(
        self,
        symbols: List[str],
        kite_client: Optional[Any] = None,
        instrument_sync: Optional[Any] = None,
        days: int = 5,
        save_to_parquet: bool = True,
        data_dir: Optional[Path] = None,
    ) -> Dict[str, Dict[str, float]]:
        """
        Fetches the last 5 trading days of real 5-minute bars via KiteConnect or Yahoo Finance
        and seeds EMA(200), EMA(20), ATR(14), and RVOL baselines into TimeframeState.
        """
        warmed: Dict[str, Dict[str, float]] = {}
        if not symbols:
            return warmed

        days = max(5, days)
        logger.info(f"[LIVE HISTORY WARMUP] Fetching historical baseline bars for {len(symbols)} instruments (days={days})...")

        now_ist = datetime.now(IST_TZ)
        from_date = (now_ist - timedelta(days=days + 4)).date()
        to_date = now_ist.date()

        store_path = data_dir or (Path(__file__).resolve().parent.parent.parent / "research_lab" / "data_store")

        for sym in symbols:
            bars: Optional[List[Dict[str, Any]]] = None

            # 1. Attempt KiteConnect historical data if authenticated
            if kite_client is not None and instrument_sync is not None:
                token = instrument_sync.get_token(sym)
                if token:
                    try:
                        records = kite_client.historical_data(
                            instrument_token=token,
                            from_date=from_date,
                            to_date=to_date,
                            interval="5minute",
                        )
                        if records:
                            bars = records
                            logger.debug(f"[WARMUP KITE] Retrieved {len(bars)} 5m bars for {sym} (Token {token})")
                    except Exception as e:
                        logger.debug(f"[WARMUP KITE] Could not fetch Kite historical data for {sym}: {e}")

            # 2. Fallback to Yahoo Finance chart API
            if not bars:
                if sym in YAHOO_INDEX_MAP:
                    ysym = YAHOO_INDEX_MAP[sym]
                else:
                    ysym = YAHOO_SYMBOL_OVERRIDES.get(sym, f"{sym}.NS")

                try:
                    url = f"https://query2.finance.yahoo.com/v8/finance/chart/{ysym}?interval=5m&range={days}d"
                    with httpx.Client(timeout=4.0, headers={"User-Agent": "Mozilla/5.0"}) as client:
                        resp = client.get(url)
                        if resp.status_code == 200:
                            chart_data = resp.json().get("chart", {}).get("result", [])
                            if chart_data:
                                res_obj = chart_data[0]
                                timestamps = res_obj.get("timestamp", [])
                                quote = res_obj.get("indicators", {}).get("quote", [{}])[0]
                                c_list = quote.get("close", [])
                                h_list = quote.get("high", [])
                                l_list = quote.get("low", [])
                                v_list = quote.get("volume", [])

                                parsed_bars = []
                                for i in range(len(timestamps)):
                                    if i < len(c_list) and c_list[i] is not None:
                                        parsed_bars.append({
                                            "close": float(c_list[i]),
                                            "high": float(h_list[i] if i < len(h_list) and h_list[i] is not None else c_list[i]),
                                            "low": float(l_list[i] if i < len(l_list) and l_list[i] is not None else c_list[i]),
                                            "volume": int(v_list[i] if i < len(v_list) and v_list[i] is not None else 1),
                                        })
                                if parsed_bars:
                                    bars = parsed_bars
                except Exception as e:
                    logger.debug(f"[WARMUP YAHOO] Could not fetch Yahoo chart for {sym} ({ysym}): {e}")

            if not bars:
                continue

            # 3. Compute indicators from bars
            closes = [b["close"] for b in bars]
            highs = [b["high"] for b in bars]
            lows = [b["low"] for b in bars]
            volumes = [b.get("volume", 1) for b in bars]
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

            avg_vol = float(sum(volumes) / n) if n > 0 else 1000.0

            # 4. Seed TimeframeState
            for tf in self.timeframes:
                st = self.get_state(sym, tf)
                st.ema_200 = round(ema_200, 2)
                st.ema_20 = round(ema_20, 2)
                st.atr_14 = round(atr_14, 2)
                st.prev_close = round(prev_c, 2)
                st.rvol_baselines["DEFAULT"] = round(avg_vol, 1)

            warmed[sym] = {
                "ema_200": round(ema_200, 2),
                "ema_20": round(ema_20, 2),
                "atr_14": round(atr_14, 2),
                "prev_close": round(prev_c, 2),
                "bars_loaded": n,
                "source": "live_history",
            }

            # 5. Optionally save to parquet cache
            if save_to_parquet and store_path.exists():
                try:
                    opens = [float(b.get("open", b["close"])) for b in bars]
                    clean_sym = sym.replace(":", "_").replace(" ", "_")
                    df = pl.DataFrame(
                        {
                            "open": opens,
                            "high": [float(h) for h in highs],
                            "low": [float(l) for l in lows],
                            "close": [float(c) for c in closes],
                            "volume": [int(v) for v in volumes],
                        },
                        schema={
                            "open": pl.Float64,
                            "high": pl.Float64,
                            "low": pl.Float64,
                            "close": pl.Float64,
                            "volume": pl.Int64,
                        },
                        strict=False,
                    )
                    parquet_target = store_path / f"{clean_sym}_1m.parquet"
                    df.write_parquet(parquet_target)
                except Exception as e:
                    logger.debug(f"[WARMUP] Could not write cached parquet for {sym}: {e}")

        logger.info(f"[LIVE HISTORY COMPLETE] Pre-warmed {len(warmed)} instruments via live history")
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
