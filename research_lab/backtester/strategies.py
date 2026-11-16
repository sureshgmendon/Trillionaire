"""
Trillionaire Research Lab - Dual-Confirmation Micro-Strategies
Implements regime-specific intraday strategies where every signal strictly requires
dual confirmation from both the Stock AND its Parent Sector Index:
1. TREND_MOMENTUM (Opening Range Breakout + Trend Continuation)
2. MEAN_REVERSION (VWAP + CPR Stretch Fade)
3. HIGH_RVOL_BREAKOUT (Institutional Accumulation / Distribution Rider)
"""

from abc import ABC, abstractmethod
from datetime import datetime, time
from typing import Any, Dict, Optional
import uuid

from shared.constants import ENTRY_WINDOW_END, ENTRY_WINDOW_START, IST_TZ
from shared.enums import ClusterRegime, Side
from shared.schemas import Signal, StockProfile


class BaseMicroStrategy(ABC):
    """Abstract base class for regime-specific dual-confirmation intraday strategies."""

    def __init__(self, name: str):
        self.name = name

    @abstractmethod
    def evaluate(
        self,
        stock_bar: Dict[str, Any],
        index_bar: Dict[str, Any],
        profile: StockProfile,
        prev_stock_bar: Optional[Dict[str, Any]] = None,
    ) -> Optional[Signal]:
        """
        Evaluates current 5-min or 1-min bar against entry conditions.
        Returns a Signal if all stock + parent index dual confirmations match.
        """
        pass

    @staticmethod
    def is_in_entry_window(bar_dt: datetime) -> bool:
        """Enforces algorithmic trading session window (09:25 <= time <= 14:30 IST)."""
        bar_time = bar_dt.time()
        return ENTRY_WINDOW_START <= bar_time <= ENTRY_WINDOW_END


class TrendMomentumStrategy(BaseMicroStrategy):
    """
    TREND_MOMENTUM: Opening Range + Trend Continuation
    - Long: Break above OR High + Price > VWAP + Price > EMA(200)
            Dual Confirmation: Parent index > its VWAP AND its EMA(20)
    - Short: Break below OR Low + Price < VWAP + Price < EMA(200)
            Dual Confirmation: Parent index < its VWAP AND its EMA(20)
    """

    def __init__(self):
        super().__init__("TrendMomentumStrategy")

    def evaluate(
        self,
        stock_bar: Dict[str, Any],
        index_bar: Dict[str, Any],
        profile: StockProfile,
        prev_stock_bar: Optional[Dict[str, Any]] = None,
    ) -> Optional[Signal]:
        dt: datetime = stock_bar["timestamp"]
        if not self.is_in_entry_window(dt):
            return None

        close = stock_bar["close"]
        vwap = stock_bar.get("vwap")
        ema_200 = stock_bar.get("ema_200")
        or_high = stock_bar.get("or_high")
        or_low = stock_bar.get("or_low")
        atr_14 = stock_bar.get("atr_14", 10.0)

        idx_close = index_bar["close"]
        idx_vwap = index_bar.get("vwap")
        idx_ema_20 = index_bar.get("ema_20")

        if None in (vwap, ema_200, or_high, or_low, idx_vwap, idx_ema_20):
            return None

        # Long Condition
        if close > or_high and close > vwap and close > ema_200:
            # Dual confirmation from Parent Index
            if idx_close > idx_vwap and idx_close > idx_ema_20:
                sl = close - profile.atr_multiplier_sl * atr_14
                tp = close + profile.atr_multiplier_tp * atr_14
                return Signal(
                    signal_id=str(uuid.uuid4()),
                    tradingsymbol=profile.tradingsymbol,
                    parent_index=profile.parent_index,
                    side=Side.BUY,
                    entry_price=round(close, 2),
                    stop_loss=round(sl, 2),
                    target_price=round(tp, 2),
                    confidence=0.85,
                    strategy_name=self.name,
                    generated_at_ist=dt,
                )

        # Short Condition
        if close < or_low and close < vwap and close < ema_200:
            # Dual confirmation from Parent Index
            if idx_close < idx_vwap and idx_close < idx_ema_20:
                sl = close + profile.atr_multiplier_sl * atr_14
                tp = close - profile.atr_multiplier_tp * atr_14
                return Signal(
                    signal_id=str(uuid.uuid4()),
                    tradingsymbol=profile.tradingsymbol,
                    parent_index=profile.parent_index,
                    side=Side.SELL,
                    entry_price=round(close, 2),
                    stop_loss=round(sl, 2),
                    target_price=round(tp, 2),
                    confidence=0.85,
                    strategy_name=self.name,
                    generated_at_ist=dt,
                )

        return None


class MeanReversionStrategy(BaseMicroStrategy):
    """
    MEAN_REVERSION: VWAP + CPR Stretch Fade
    - Long: Price <= VWAP - 1.5 * ATR(14) into CPR support zone + Bullish candle reversal
            Dual Confirmation: Parent index range-bound (|Index Close - Index VWAP| <= 1.0%)
    - Short: Price >= VWAP + 1.5 * ATR(14) into CPR resistance zone + Bearish candle reversal
            Dual Confirmation: Parent index range-bound (|Index Close - Index VWAP| <= 1.0%)
    """

    def __init__(self):
        super().__init__("MeanReversionStrategy")

    def evaluate(
        self,
        stock_bar: Dict[str, Any],
        index_bar: Dict[str, Any],
        profile: StockProfile,
        prev_stock_bar: Optional[Dict[str, Any]] = None,
    ) -> Optional[Signal]:
        dt: datetime = stock_bar["timestamp"]
        if not self.is_in_entry_window(dt):
            return None

        close = stock_bar["close"]
        open_p = stock_bar["open"]
        high = stock_bar["high"]
        low = stock_bar["low"]
        vwap = stock_bar.get("vwap")
        atr_14 = stock_bar.get("atr_14", 10.0)
        cpr_bottom = stock_bar.get("cpr_bottom")
        cpr_top = stock_bar.get("cpr_top")

        idx_close = index_bar["close"]
        idx_vwap = index_bar.get("vwap")

        if None in (vwap, cpr_bottom, cpr_top, idx_vwap):
            return None

        # Parent sector index must be range-bound (not trending aggressively > 1% away from VWAP)
        idx_divergence_pct = abs(idx_close - idx_vwap) / idx_vwap
        if idx_divergence_pct > 0.010:
            return None

        # Long Reversion: Oversold stretch >= 1.5 * ATR below VWAP
        if (vwap - close) >= (1.5 * atr_14):
            # Proximity to CPR support: Low penetrates or is near CPR zone
            if low <= (cpr_top * 1.005) and close >= (cpr_bottom * 0.990):
                # Bullish candle reversal: Green bar closing in upper half of range
                if close > open_p and close >= ((high + low) / 2.0):
                    sl = close - profile.atr_multiplier_sl * atr_14
                    tp = vwap  # Fade back to VWAP
                    if tp <= close:
                        tp = close + profile.atr_multiplier_tp * atr_14
                    return Signal(
                        signal_id=str(uuid.uuid4()),
                        tradingsymbol=profile.tradingsymbol,
                        parent_index=profile.parent_index,
                        side=Side.BUY,
                        entry_price=round(close, 2),
                        stop_loss=round(sl, 2),
                        target_price=round(tp, 2),
                        confidence=0.80,
                        strategy_name=self.name,
                        generated_at_ist=dt,
                    )

        # Short Reversion: Overbought stretch >= 1.5 * ATR above VWAP
        if (close - vwap) >= (1.5 * atr_14):
            # Proximity to CPR resistance: High penetrates or is near CPR zone
            if high >= (cpr_bottom * 0.995) and close <= (cpr_top * 1.010):
                # Bearish candle reversal: Red bar closing in lower half of range
                if close < open_p and close <= ((high + low) / 2.0):
                    sl = close + profile.atr_multiplier_sl * atr_14
                    tp = vwap  # Fade back to VWAP
                    if tp >= close:
                        tp = close - profile.atr_multiplier_tp * atr_14
                    return Signal(
                        signal_id=str(uuid.uuid4()),
                        tradingsymbol=profile.tradingsymbol,
                        parent_index=profile.parent_index,
                        side=Side.SELL,
                        entry_price=round(close, 2),
                        stop_loss=round(sl, 2),
                        target_price=round(tp, 2),
                        confidence=0.80,
                        strategy_name=self.name,
                        generated_at_ist=dt,
                    )

        return None


class HighRvolBreakoutStrategy(BaseMicroStrategy):
    """
    HIGH_RVOL_BREAKOUT: Institutional Accumulation / Distribution Rider
    - Long: RVOL >= min_rvol_threshold + Strong close in top 25% of bar range
            Dual Confirmation: Stock outperforming parent index return by >= +0.4%
    - Short: RVOL >= min_rvol_threshold + Strong close in bottom 25% of bar range
            Dual Confirmation: Stock underperforming parent index return by >= -0.4%
    """

    def __init__(self):
        super().__init__("HighRvolBreakoutStrategy")

    def evaluate(
        self,
        stock_bar: Dict[str, Any],
        index_bar: Dict[str, Any],
        profile: StockProfile,
        prev_stock_bar: Optional[Dict[str, Any]] = None,
    ) -> Optional[Signal]:
        dt: datetime = stock_bar["timestamp"]
        if not self.is_in_entry_window(dt):
            return None

        close = stock_bar["close"]
        open_p = stock_bar["open"]
        high = stock_bar["high"]
        low = stock_bar["low"]
        rvol = stock_bar.get("rvol", 1.0)
        atr_14 = stock_bar.get("atr_14", 10.0)

        # RVOL threshold check
        min_rvol = profile.min_rvol_threshold
        if rvol < min_rvol:
            return None

        bar_range = high - low
        if bar_range <= 0:
            return None

        # Relative performance against Parent Index
        stock_ret = (close - open_p) / open_p
        idx_open = index_bar["open"]
        idx_close = index_bar["close"]
        idx_ret = (idx_close - idx_open) / idx_open if idx_open > 0 else 0.0

        rel_outperformance = stock_ret - idx_ret

        # Long: Close in top 25% of bar + outperforming index by >= +0.4%
        is_top_25_pct = (close - low) >= (0.75 * bar_range)
        if is_top_25_pct and rel_outperformance >= 0.004:
            sl = close - profile.atr_multiplier_sl * atr_14
            tp = close + profile.atr_multiplier_tp * atr_14
            return Signal(
                signal_id=str(uuid.uuid4()),
                tradingsymbol=profile.tradingsymbol,
                parent_index=profile.parent_index,
                side=Side.BUY,
                entry_price=round(close, 2),
                stop_loss=round(sl, 2),
                target_price=round(tp, 2),
                confidence=0.90,
                strategy_name=self.name,
                generated_at_ist=dt,
            )

        # Short: Close in bottom 25% of bar + underperforming index by >= -0.4%
        is_bottom_25_pct = (high - close) >= (0.75 * bar_range)
        if is_bottom_25_pct and rel_outperformance <= -0.004:
            sl = close + profile.atr_multiplier_sl * atr_14
            tp = close - profile.atr_multiplier_tp * atr_14
            return Signal(
                signal_id=str(uuid.uuid4()),
                tradingsymbol=profile.tradingsymbol,
                parent_index=profile.parent_index,
                side=Side.SELL,
                entry_price=round(close, 2),
                stop_loss=round(sl, 2),
                target_price=round(tp, 2),
                confidence=0.90,
                strategy_name=self.name,
                generated_at_ist=dt,
            )

        return None


def get_strategy_for_regime(regime: ClusterRegime) -> BaseMicroStrategy:
    """Factory creating strategy implementation mapped to ClusterRegime."""
    if regime == ClusterRegime.TREND_MOMENTUM:
        return TrendMomentumStrategy()
    elif regime == ClusterRegime.MEAN_REVERSION:
        return MeanReversionStrategy()
    elif regime == ClusterRegime.HIGH_RVOL_BREAKOUT:
        return HighRvolBreakoutStrategy()
    else:
        return TrendMomentumStrategy()
