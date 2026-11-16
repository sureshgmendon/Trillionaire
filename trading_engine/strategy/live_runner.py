"""
Trillionaire Trading Engine - Real-Time Dual-Confirmation Strategy Runner
Orchestrates live intraday micro-strategies (TREND_MOMENTUM, MEAN_REVERSION, HIGH_RVOL_BREAKOUT).
Enforces:
1. Sentinel Pre-Market Gate: Blocks symbols flagged DO_NOT_TRADE, applies caution multipliers.
2. Anomaly Freeze Guard: Blocks MEAN_REVERSION entries during a 15-minute flash volatility freeze.
3. Dual Confirmation: Requires joint confluence between the equity and its parent sector index.
Emits SignalEvent onto the typed EventBus upon confirmation.
"""

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
from sentinel.orchestrator import SentinelOrchestrator
from shared.constants import IST_TZ
from shared.enums import ClusterRegime, SentinelStatus, Timeframe
from shared.schemas import Candle, NewsSentinelTag, Signal, StockProfile, Tick
from trading_engine.core.event_bus import (
    CandleCloseEvent,
    EventBus,
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
    ):
        self.bus = event_bus or bus
        self.sentinel = sentinel_orchestrator or SentinelOrchestrator()
        self.anomaly_detector = anomaly_detector
        self.profiles_path = profiles_path

        # Stock profiles: tradingsymbol -> StockProfile
        self.profiles: Dict[str, StockProfile] = {}
        if profiles:
            self._set_profiles(profiles)
        else:
            self.load_profiles_from_disk()

        # Latest closed candles: (symbol, Timeframe) -> Candle
        self._latest_candles: Dict[Tuple[str, Timeframe], Candle] = {}
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

        # Check if instrument is in tracked profiles
        profile = self.profiles.get(sym)
        if profile is None or not profile.is_active:
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
            logger.info(
                f"[SIGNAL GENERATED] {signal.strategy_name} -> {signal.tradingsymbol} {signal.side.value} "
                f"@ {signal.entry_price} (SL: {signal.stop_loss}, TP: {signal.target_price}, Conf: {signal.confidence})"
            )
            self.emitted_signals.append(signal)
            # Broadcast SignalEvent on EventBus
            await self.bus.publish(SignalEvent(signal=signal))
            return signal

        return None
