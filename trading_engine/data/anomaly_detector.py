"""
Trillionaire Trading Engine - Live Anomaly & RVOL Shock Detector
Monitors closed M1 and M5 candles in real time for abnormal market phenomena:
1. INSTITUTIONAL_SURGE: Volume surge (RVOL >= 4.0x) signaling institutional accumulation/rider.
2. FLASH_VOLATILITY_FREEZE: Abnormal bar range (>= 3.0x ATR_1m) freezing mean-reversion entries
   for 15 minutes to avoid catching falling knives during sudden market shocks.
"""

from datetime import datetime, timedelta
from typing import Dict, List, Optional
from loguru import logger

from shared.constants import IST_TZ
from shared.enums import Timeframe
from shared.schemas import Candle
from trading_engine.core.event_bus import AnomalyAlertEvent, CandleCloseEvent, EventBus, bus

INSTITUTIONAL_SURGE_THRESHOLD = 4.0   # RVOL >= 4.0x
FLASH_VOLATILITY_RATIO = 3.0          # Range >= 3.0x ATR_14
FREEZE_DURATION_MINUTES = 15          # Cooldown freeze duration


class AnomalyDetector:
    """Real-time detector identifying abnormal microstructure volume and volatility spikes."""

    def __init__(self, event_bus: Optional[EventBus] = None):
        self.bus = event_bus or bus
        # symbol -> frozen_until datetime
        self._freeze_expiry: Dict[str, datetime] = {}
        # History of detected anomaly events
        self._alerts_history: List[AnomalyAlertEvent] = []

    def is_symbol_frozen(self, symbol: str, current_time: Optional[datetime] = None) -> bool:
        """
        Checks whether an instrument is under a temporary flash-volatility cooldown.
        Used by MEAN_REVERSION strategy to inhibit counter-trend entries.
        """
        if current_time is None:
            current_time = datetime.now(IST_TZ)
        elif current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=IST_TZ)

        freeze_until = self._freeze_expiry.get(symbol)
        if freeze_until is None:
            return False

        if current_time < freeze_until:
            return True
        else:
            # Cooldown expired
            del self._freeze_expiry[symbol]
            return False

    async def on_candle_close(self, event: CandleCloseEvent) -> None:
        """Async event bus listener for CandleCloseEvent."""
        await self.evaluate_candle(event.candle)

    async def evaluate_candle(self, candle: Candle) -> List[AnomalyAlertEvent]:
        """
        Evaluates a closed candle for institutional volume spikes or flash volatility.
        Returns list of generated AnomalyAlertEvents and broadcasts them on the EventBus.
        """
        symbol = candle.tradingsymbol
        tf = candle.timeframe
        dt = candle.timestamp_ist
        bar_range = candle.high - candle.low

        emitted_alerts: List[AnomalyAlertEvent] = []

        # 1. Institutional Volume Surge (RVOL >= 4.0x)
        if candle.rvol >= INSTITUTIONAL_SURGE_THRESHOLD:
            msg = (
                f"Institutional volume surge detected on {symbol} ({tf.value}): "
                f"RVOL={candle.rvol:.1f}x (Bar Vol={candle.volume:,})"
            )
            logger.info(f"[ANOMALY] {msg}")
            alert = AnomalyAlertEvent(
                symbol=symbol,
                kind="INSTITUTIONAL_SURGE",
                message=msg,
                timestamp_ist=dt,
                metrics={
                    "rvol": candle.rvol,
                    "volume": candle.volume,
                    "close": candle.close,
                    "timeframe": tf.value,
                },
            )
            emitted_alerts.append(alert)
            self._alerts_history.append(alert)
            await self.bus.publish(alert)

        # 2. Freak Volatility / Flash Crash Spike (Range >= 3.0 * ATR_14 on M1 or M5)
        if candle.atr_14 > 0 and bar_range >= (FLASH_VOLATILITY_RATIO * candle.atr_14):
            freeze_until = dt + timedelta(minutes=FREEZE_DURATION_MINUTES)
            self._freeze_expiry[symbol] = freeze_until
            ratio = round(bar_range / candle.atr_14, 2)
            msg = (
                f"Flash volatility spike detected on {symbol} ({tf.value}): "
                f"Range={bar_range:.2f} ({ratio}x ATR). Freezing mean-reversion entries for 15 mins."
            )
            logger.warning(f"[ANOMALY] {msg}")
            alert = AnomalyAlertEvent(
                symbol=symbol,
                kind="FLASH_VOLATILITY_FREEZE",
                message=msg,
                timestamp_ist=dt,
                metrics={
                    "range": round(bar_range, 2),
                    "atr_14": candle.atr_14,
                    "ratio": ratio,
                    "freeze_until": freeze_until.isoformat(),
                    "timeframe": tf.value,
                },
            )
            emitted_alerts.append(alert)
            self._alerts_history.append(alert)
            await self.bus.publish(alert)

        return emitted_alerts
