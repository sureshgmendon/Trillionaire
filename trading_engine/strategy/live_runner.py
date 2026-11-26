"""
Trillionaire Trading Engine - Real-Time Dual-Confirmation Strategy Runner
Orchestrates live intraday micro-strategies (TREND_MOMENTUM, MEAN_REVERSION, HIGH_RVOL_BREAKOUT).
Enforces:
1. Sentinel Pre-Market Gate: Blocks symbols flagged DO_NOT_TRADE, applies caution multipliers.
2. Anomaly Freeze Guard: Blocks MEAN_REVERSION entries during a 15-minute flash volatility freeze.
3. Dual Confirmation: Requires joint confluence between the equity and its parent sector index.
Emits SignalEvent onto the typed EventBus upon confirmation.
"""

from collections import deque
from datetime import datetime, time
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from loguru import logger

from config.settings import settings
from research_lab.backtester.strategies import (
    BaseMicroStrategy,
    get_strategy_for_regime,
)
from sentinel.news_triage.vision_filter import VisionTrapFilter
from sentinel.orchestrator import SentinelOrchestrator
from shared.constants import IST_TZ
from shared.enums import ClusterRegime, SentinelStatus, Side, Timeframe
from shared.schemas import Candle, NewsSentinelTag, Signal, StockProfile, Tick
from trading_engine.core.event_bus import (
    CandleCloseEvent,
    EventBus,
    RiskAlertEvent,
    SignalEvent,
    TickEvent,
    bus,
)
from trading_engine.data.anomaly_detector import AnomalyDetector

STOCK_PROFILES_DEFAULT_PATH = (
    Path(__file__).resolve().parents[2] / "research_lab" / "artifacts" / "stock_profiles.json"
)


class LiveStrategyRunner:
    """Real-time strategy runner consuming M5 candle events and generating trade signals."""

    def __init__(
        self,
        event_bus: Optional[EventBus] = None,
        profiles: Optional[Dict[str, StockProfile] | List[StockProfile]] = None,
        sentinel_orchestrator: Optional[SentinelOrchestrator] = None,
        anomaly_detector: Optional[AnomalyDetector] = None,
        profiles_path: Path = STOCK_PROFILES_DEFAULT_PATH,
        vision_filter: Optional[VisionTrapFilter] = None,
    ):
        self.bus = event_bus or bus
        self.sentinel = sentinel_orchestrator or SentinelOrchestrator()
        self.anomaly_detector = anomaly_detector
        self.profiles_path = profiles_path
        self.vision_filter = vision_filter or VisionTrapFilter()

        # Stock profiles: tradingsymbol -> StockProfile
        self.profiles: Dict[str, StockProfile] = {}
        if profiles:
            self._set_profiles(profiles)
        else:
            self.load_profiles_from_disk()

        # Latest closed candles: (symbol, Timeframe) -> Candle
        self._latest_candles: Dict[Tuple[str, Timeframe], Candle] = {}
        # Rolling 60 M5 candle history per symbol for Gemini Vision chart trap inspection
        self._m5_history: Dict[str, deque[Candle]] = {}
        # Previous closed candles for trend change checks
        self._prev_candles: Dict[str, Dict[str, Any]] = {}

        # Opening Range tracking: symbol -> (or_high, or_low)
        self._opening_ranges: Dict[str, Tuple[float, float]] = {}
        # CPR levels: symbol -> (cpr_bottom, cpr_top)
        self._cpr_levels: Dict[str, Tuple[float, float]] = {}

        # Streaming EMA(20) tracking for symbols and indices
        self._index_ema_20: Dict[str, float] = {}
        self._stock_ema_20: Dict[str, float] = {}

        # Latest ticks: symbol -> Tick
        self._latest_ticks: Dict[str, Tick] = {}

        # Cache strategy instances per ClusterRegime
        self._strategies: Dict[ClusterRegime, BaseMicroStrategy] = {
            ClusterRegime.TREND_MOMENTUM: get_strategy_for_regime(ClusterRegime.TREND_MOMENTUM),
            ClusterRegime.MEAN_REVERSION: get_strategy_for_regime(ClusterRegime.MEAN_REVERSION),
            ClusterRegime.HIGH_RVOL_BREAKOUT: get_strategy_for_regime(ClusterRegime.HIGH_RVOL_BREAKOUT),
        }

        # Generated signals history
        self.emitted_signals: List[Signal] = []

        # Safe pause / resume state
        self.is_paused: bool = False

        # Real-time funnel metrics for dashboard telemetry
        self.funnel_metrics: Dict[str, int] = {
            "candles_evaluated": 0,
            "passed_stock_rule": 0,
            "passed_sector_filter": 0,
            "blocked_by_market_filter": 0,
            "blocked_by_lunch_filter": 0,
            "blocked_by_vision_filter": 0,
            "signals_emitted": 0,
        }

        # Wire up event subscriptions
        self._register_subscribers()

    def _register_subscribers(self) -> None:
        """Subscribes handler callbacks to EventBus."""
        self.bus.subscribe(CandleCloseEvent, self.on_candle_close)
        self.bus.subscribe(TickEvent, self.on_tick)

    def _set_profiles(self, profiles: Dict[str, StockProfile] | List[StockProfile]) -> None:
        if isinstance(profiles, list):
            self.profiles = {p.tradingsymbol: p for p in profiles}
        else:
            self.profiles = dict(profiles)
        logger.info(f"LiveStrategyRunner initialized with {len(self.profiles)} stock profiles")

    def load_profiles_from_disk(self, path: Optional[Path] = None) -> bool:
        """Loads stock profiles from JSON artifact."""
        target_path = path or self.profiles_path
        if not target_path.exists():
            logger.warning(f"Stock profiles file not found: {target_path}")
            return False

        try:
            with open(target_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.profiles = {item["tradingsymbol"]: StockProfile(**item) for item in data}
            logger.info(f"Loaded {len(self.profiles)} stock profiles from {target_path}")
            return True
        except Exception as e:
            logger.error(f"Failed to load stock profiles from {target_path}: {e}")
            return False

    def seed_cpr(self, symbol: str, cpr_bottom: float, cpr_top: float) -> None:
        """Seeds daily CPR levels (Pivot, BC, TC) for an instrument."""
        self._cpr_levels[symbol] = (round(min(cpr_bottom, cpr_top), 2), round(max(cpr_bottom, cpr_top), 2))

    def seed_opening_range(self, symbol: str, or_high: float, or_low: float) -> None:
        """Seeds the 09:15-09:30 Opening Range high and low."""
        self._opening_ranges[symbol] = (round(or_high, 2), round(or_low, 2))

    def seed_index_ema_20(self, index_symbol: str, ema_20: float) -> None:
        """Seeds or overrides parent sector index EMA(20)."""
        self._index_ema_20[index_symbol] = round(ema_20, 2)

    def get_latest_candle(self, symbol: str, timeframe: Timeframe = Timeframe.M5) -> Optional[Candle]:
        """Returns the most recent closed candle for a symbol and timeframe."""
        return self._latest_candles.get((symbol, timeframe))

    def get_sentinel_tag(self, symbol: str) -> NewsSentinelTag:
        """Retrieves active Sentinel risk assessment for a symbol."""
        return self.sentinel.get_effective_risk_multiplier(symbol)

    async def on_tick(self, event: TickEvent) -> None:
        """Tracks live ticks and dynamically discovers opening range levels (09:15 - 09:30 IST)."""
        tick = event.tick
        sym = tick.tradingsymbol
        self._latest_ticks[sym] = tick

        tick_time = tick.timestamp_ist.time()
        # Track 09:15 - 09:30 Opening Range
        if time(9, 15) <= tick_time <= time(9, 30):
            current_or = self._opening_ranges.get(sym)
            if current_or is None:
                self._opening_ranges[sym] = (tick.ltp, tick.ltp)
            else:
                or_h = max(current_or[0], tick.ltp)
                or_l = min(current_or[1], tick.ltp)
                self._opening_ranges[sym] = (or_h, or_l)

    async def on_candle_close(self, event: CandleCloseEvent) -> Optional[Signal]:
        """
        EventBus handler for CandleCloseEvent.
        Evaluates dual-confirmation micro-strategies upon M5 candle closures.
        """
        candle = event.candle
        sym = candle.tradingsymbol
        tf = candle.timeframe

        # Cache candle
        self._latest_candles[(sym, tf)] = candle

        # Maintain incremental EMA(20)
        alpha_20 = 2.0 / (20 + 1)
        if sym not in self._index_ema_20:
            self._index_ema_20[sym] = candle.vwap if candle.vwap else candle.close
        else:
            self._index_ema_20[sym] = round(candle.close * alpha_20 + self._index_ema_20[sym] * (1.0 - alpha_20), 2)

        # Micro-strategies strictly evaluate on M5 interval
        if tf != Timeframe.M5:
            return None

        m5_hist = self._m5_history.setdefault(sym, deque(maxlen=60))
        m5_hist.append(candle)

        # Check if trading strategy evaluation is paused
        if self.is_paused:
            logger.debug(f"[STRATEGY PAUSED] Skipping signal evaluation for {sym}")
            return None

        # Check if instrument is in tracked profiles
        profile = self.profiles.get(sym)
        if profile is None or not profile.is_active:
            return None

        # Track evaluated candles in signal funnel
        self.funnel_metrics["candles_evaluated"] += 1

        # Strategy Regime Toggles (persisted in Settings & Keys)
        if profile.cluster_regime == ClusterRegime.TREND_MOMENTUM and not getattr(settings, "enable_trend_strategy", True):
            logger.debug(f"[STRATEGY TOGGLE] TREND_MOMENTUM disabled; skipping {sym}")
            return None
        if profile.cluster_regime == ClusterRegime.MEAN_REVERSION and not getattr(settings, "enable_mean_reversion_strategy", True):
            logger.debug(f"[STRATEGY TOGGLE] MEAN_REVERSION disabled; skipping {sym}")
            return None
        if profile.cluster_regime == ClusterRegime.HIGH_RVOL_BREAKOUT and not getattr(settings, "enable_rvol_breakout_strategy", True):
            logger.debug(f"[STRATEGY TOGGLE] HIGH_RVOL_BREAKOUT disabled; skipping {sym}")
            return None

        # 1. Gate 1: Pre-Market Sentinel Risk Assessment
        tag = self.get_sentinel_tag(sym)
        if tag.status == SentinelStatus.DO_NOT_TRADE or tag.risk_multiplier <= 0.0:
            logger.info(
                f"[GATE 1 BLOCKED] {sym} is locked out by Sentinel: {tag.status.value} - {tag.reason}"
            )
            return None

        # 2. Gate 2: Flash Volatility Anomaly Freeze Guard (MEAN_REVERSION only)
        if profile.cluster_regime == ClusterRegime.MEAN_REVERSION:
            if self.anomaly_detector and self.anomaly_detector.is_symbol_frozen(sym, candle.timestamp_ist):
                logger.info(
                    f"[GATE 2 BLOCKED] {sym} MEAN_REVERSION is temporarily frozen due to flash volatility"
                )
                return None

        # 3. Gate 3: Dual Confirmation with Parent Sector Index
        parent_index = profile.parent_index
        idx_candle = self._latest_candles.get((parent_index, Timeframe.M5))
        if idx_candle is None:
            logger.debug(
                f"Parent index '{parent_index}' M5 candle not yet available for confirmation of {sym}"
            )
            return None

        # Build strategy evaluation bar payloads
        or_range = self._opening_ranges.get(sym, (candle.high, candle.low))
        cpr_range = self._cpr_levels.get(sym, (round(candle.close * 0.995, 2), round(candle.close * 1.005, 2)))

        stock_bar: Dict[str, Any] = {
            "timestamp": candle.timestamp_ist,
            "open": candle.open,
            "high": candle.high,
            "low": candle.low,
            "close": candle.close,
            "volume": candle.volume,
            "vwap": candle.vwap,
            "ema_200": candle.ema_200,
            "ema_20": candle.close,  # Streaming candle approximation
            "atr_14": candle.atr_14,
            "rvol": candle.rvol,
            "or_high": or_range[0],
            "or_low": or_range[1],
            "cpr_bottom": cpr_range[0],
            "cpr_top": cpr_range[1],
        }

        idx_ema_20 = self._index_ema_20.get(
            parent_index, idx_candle.vwap if idx_candle.vwap is not None else idx_candle.close * 0.999
        )

        index_bar: Dict[str, Any] = {
            "timestamp": idx_candle.timestamp_ist,
            "open": idx_candle.open,
            "high": idx_candle.high,
            "low": idx_candle.low,
            "close": idx_candle.close,
            "volume": idx_candle.volume,
            "vwap": idx_candle.vwap,
            "ema_200": idx_candle.ema_200,
            "ema_20": idx_ema_20,
            "atr_14": idx_candle.atr_14,
            "rvol": idx_candle.rvol,
        }

        # Select mapped micro-strategy
        strategy = self._strategies.get(profile.cluster_regime)
        if strategy is None:
            strategy = get_strategy_for_regime(profile.cluster_regime)
            self._strategies[profile.cluster_regime] = strategy

        # Track stock rule & sector filter passes for telemetry funnel
        passed_stock = False
        passed_sector = False
        c = stock_bar["close"]
        v = stock_bar.get("vwap")
        e200 = stock_bar.get("ema_200")
        oh = stock_bar.get("or_high")
        ol = stock_bar.get("or_low")

        if profile.cluster_regime == ClusterRegime.TREND_MOMENTUM:
            long_stk = oh is not None and v is not None and e200 is not None and c > oh and c > v and c > e200
            short_stk = ol is not None and v is not None and e200 is not None and c < ol and c < v and c < e200
            passed_stock = long_stk or short_stk
            if passed_stock and idx_candle.vwap is not None:
                passed_sector = (long_stk and idx_candle.close > idx_candle.vwap and idx_candle.close > idx_ema_20) or (
                    short_stk and idx_candle.close < idx_candle.vwap and idx_candle.close < idx_ema_20
                )
        elif profile.cluster_regime == ClusterRegime.MEAN_REVERSION:
            cb, ct = cpr_range
            a14 = stock_bar.get("atr_14", 5.0)
            long_stk = v is not None and c <= (v - 1.5 * a14) and c >= cb
            short_stk = v is not None and c >= (v + 1.5 * a14) and c <= ct
            passed_stock = long_stk or short_stk
            if passed_stock and idx_candle.vwap is not None and idx_candle.vwap > 0:
                passed_sector = abs(idx_candle.close - idx_candle.vwap) / idx_candle.vwap <= 0.01
        elif profile.cluster_regime == ClusterRegime.HIGH_RVOL_BREAKOUT:
            rv = stock_bar.get("rvol", 1.0)
            min_rv = profile.min_rvol_threshold
            long_stk = oh is not None and rv >= min_rv and c > oh
            short_stk = ol is not None and rv >= min_rv and c < ol
            passed_stock = long_stk or short_stk
            if passed_stock and stock_bar.get("open") and idx_candle.open:
                stk_ret = (c - stock_bar["open"]) / stock_bar["open"]
                idx_ret = (idx_candle.close - idx_candle.open) / idx_candle.open
                passed_sector = abs(stk_ret - idx_ret) >= 0.004

        if passed_stock:
            self.funnel_metrics["passed_stock_rule"] += 1
        if passed_sector:
            self.funnel_metrics["passed_sector_filter"] += 1

        prev_bar = self._prev_candles.get(sym)
        signal = strategy.evaluate(
            stock_bar=stock_bar,
            index_bar=index_bar,
            profile=profile,
            prev_stock_bar=prev_bar,
        )

        # Update previous bar cache
        self._prev_candles[sym] = stock_bar

        if signal is not None:
            # Choppy-Lunch Volume Filter: Keep scanning 100 stocks, require RVOL >= 2.0 for new entries in 11:45-13:15 IST
            bar_ist = (
                candle.timestamp_ist.astimezone(IST_TZ)
                if candle.timestamp_ist.tzinfo
                else candle.timestamp_ist.replace(tzinfo=IST_TZ)
            )
            bar_t = bar_ist.time()
            if getattr(settings, "enable_lunch_volume_filter", True) and time(11, 45) <= bar_t <= time(13, 15):
                if float(candle.rvol or 0.0) < 2.0:
                    self.funnel_metrics["blocked_by_lunch_filter"] = (
                        self.funnel_metrics.get("blocked_by_lunch_filter", 0) + 1
                    )
                    logger.info(
                        f"[LUNCH VOLUME FILTER] {sym} {signal.side.value} blocked in 11:45-13:15 IST window "
                        f"(RVOL={float(candle.rvol or 0.0):.2f} < 2.00)"
                    )
                    return None

            # Gate 4: Broad Market (NIFTY 50) Trend Filter
            nifty_candle = self._latest_candles.get(("NIFTY 50", Timeframe.M5))
            if nifty_candle is not None and nifty_candle.vwap is not None and nifty_candle.vwap > 0:
                nifty_ema_20 = self._index_ema_20.get("NIFTY 50", nifty_candle.vwap)
                if signal.side == Side.BUY and (nifty_candle.close < nifty_candle.vwap or nifty_candle.close < nifty_ema_20):
                    self.funnel_metrics["blocked_by_market_filter"] = self.funnel_metrics.get("blocked_by_market_filter", 0) + 1
                    logger.info(
                        f"[MARKET FILTER BLOCKED] BUY signal on {sym} blocked: NIFTY 50 below VWAP/EMA20 "
                        f"(Close={nifty_candle.close:.2f}, VWAP={nifty_candle.vwap:.2f}, EMA20={nifty_ema_20:.2f})"
                    )
                    return None
                elif signal.side == Side.SELL and (nifty_candle.close > nifty_candle.vwap or nifty_candle.close > nifty_ema_20):
                    self.funnel_metrics["blocked_by_market_filter"] = self.funnel_metrics.get("blocked_by_market_filter", 0) + 1
                    logger.info(
                        f"[MARKET FILTER BLOCKED] SELL signal on {sym} blocked: NIFTY 50 above VWAP/EMA20 "
                        f"(Close={nifty_candle.close:.2f}, VWAP={nifty_candle.vwap:.2f}, EMA20={nifty_ema_20:.2f})"
                    )
                    return None

            # Gate 5: Pre-Order Gemini Vision Chart Trap Filter (only runs on confirmed signals when enabled)
            if getattr(settings, "enable_vision_filter", True):
                vision_res = await self.vision_filter.evaluate_signal(
                    signal=signal,
                    candles=list(self._m5_history.get(sym, [candle])),
                    or_levels=or_range,
                )
                signal.metadata["vision_filter"] = vision_res
                decision_label = "APPROVED" if vision_res.get("approved", True) else "VETOED"
                vision_msg = (
                    f"[VISION FILTER] {decision_label} {sym} {signal.side.value} "
                    f"(Conf={vision_res.get('confidence', 0.0):.2f}): {vision_res.get('reason', '')}"
                )
                logger.info(vision_msg)
                await self.bus.publish(
                    RiskAlertEvent(
                        symbol=sym,
                        level="INFO" if vision_res.get("approved", True) else "WARNING",
                        message=vision_msg,
                        timestamp_ist=candle.timestamp_ist,
                    )
                )
                if not vision_res.get("approved", True):
                    self.funnel_metrics["blocked_by_vision_filter"] = (
                        self.funnel_metrics.get("blocked_by_vision_filter", 0) + 1
                    )
                    return None

            self.funnel_metrics["signals_emitted"] += 1
            if not passed_stock:
                self.funnel_metrics["passed_stock_rule"] += 1
            if not passed_sector:
                self.funnel_metrics["passed_sector_filter"] += 1

            logger.info(
                f"[SIGNAL GENERATED] {signal.strategy_name} -> {signal.tradingsymbol} {signal.side.value} "
                f"@ {signal.entry_price} (SL: {signal.stop_loss}, TP: {signal.target_price}, Conf: {signal.confidence})"
            )
            self.emitted_signals.append(signal)
            # Broadcast SignalEvent on EventBus
            await self.bus.publish(SignalEvent(signal=signal))
            return signal

        return None
