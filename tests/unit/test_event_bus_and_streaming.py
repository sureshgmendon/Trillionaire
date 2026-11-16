"""
Trillionaire Trading Engine - Event Bus & Streaming Aggregator Unit Tests
Validates:
1. EventBus dispatching to multiple subscribers with strict fault isolation.
2. CandleAggregator rolling M1 and M5 bars at IST boundaries with streaming indicators (VWAP, EMA200, ATR14, RVOL).
3. AnomalyDetector triggering INSTITUTIONAL_SURGE on RVOL >= 4.0x and FLASH_VOLATILITY_FREEZE on >= 3.0x ATR spikes.
4. HistoricalTickReplayer streaming realistic ticks at MAX_SPEED and producing closed candles.
"""

import asyncio
from datetime import date, datetime, timedelta
from pathlib import Path
import pytest

from shared.constants import IST_TZ
from shared.enums import Timeframe
from shared.schemas import Candle, Tick
from trading_engine.core.event_bus import (
    AnomalyAlertEvent,
    CandleCloseEvent,
    EventBus,
    TickEvent,
)
from trading_engine.data.anomaly_detector import AnomalyDetector
from trading_engine.data.candle_aggregator import CandleAggregator
from trading_engine.data.tick_replayer import HistoricalTickReplayer


# ==============================================================================
# 1. EVENT BUS FAULT ISOLATION TESTS
# ==============================================================================

def test_event_bus_dispatch_and_fault_isolation():
    """Validates that EventBus delivers events to all subscribers even if one throws an exception."""
    async def _run_test():
        bus = EventBus(enable_redis_mirror=False)
        received_sub1 = []
        received_sub3 = []

        async def sub1(event: TickEvent):
            received_sub1.append(event)

        async def sub2_crashing(event: TickEvent):
            raise RuntimeError("Simulated subscriber crash")

        async def sub3(event: TickEvent):
            received_sub3.append(event)

        bus.subscribe(TickEvent, sub1)
        bus.subscribe(TickEvent, sub2_crashing)
        bus.subscribe(TickEvent, sub3)

        now = datetime(2026, 11, 16, 9, 15, 0, tzinfo=IST_TZ)
        tick = Tick(
            instrument_token=738561,
            tradingsymbol="RELIANCE",
            timestamp_ist=now,
            ltp=2850.0,
            volume=1000,
            bid=2849.5,
            ask=2850.5,
        )

        event = TickEvent(tick=tick)
        # Publishing must not raise RuntimeError from sub2
        await bus.publish(event)

        assert len(received_sub1) == 1
        assert len(received_sub3) == 1
        assert received_sub1[0].tick.ltp == 2850.0
        assert received_sub3[0].tick.ltp == 2850.0

    asyncio.run(_run_test())


# ==============================================================================
# 2. CANDLE AGGREGATOR & STREAMING INDICATORS TESTS
# ==============================================================================

def test_candle_aggregator_bar_rollover_and_indicators():
    """Validates M1 and M5 bar rollover at IST clock boundaries and indicator calculations."""
    async def _run_test():
        bus = EventBus(enable_redis_mirror=False)
        aggregator = CandleAggregator(
            event_bus=bus,
            timeframes=[Timeframe.M1, Timeframe.M5],
        )

        closed_candles: list[Candle] = []

        async def on_candle(event: CandleCloseEvent):
            closed_candles.append(event.candle)

        bus.subscribe(CandleCloseEvent, on_candle)

        # Baseline timestamp: 09:15:10
        t0 = datetime(2026, 11, 16, 9, 15, 10, tzinfo=IST_TZ)
        t1 = datetime(2026, 11, 16, 9, 15, 45, tzinfo=IST_TZ)
        t2_rollover_m1 = datetime(2026, 11, 16, 9, 16, 5, tzinfo=IST_TZ)
        t3_rollover_m5 = datetime(2026, 11, 16, 9, 20, 5, tzinfo=IST_TZ)

        # Tick 1 @ 09:15:10
        await aggregator.process_tick(
            Tick(
                instrument_token=408065,
                tradingsymbol="INFY",
                timestamp_ist=t0,
                ltp=1500.0,
                volume=100,
                bid=1499.5,
                ask=1500.5,
            )
        )

        # Tick 2 @ 09:15:45
        await aggregator.process_tick(
            Tick(
                instrument_token=408065,
                tradingsymbol="INFY",
                timestamp_ist=t1,
                ltp=1510.0,
                volume=300,
                bid=1509.5,
                ask=1510.5,
            )
        )

        assert len(closed_candles) == 0, "No bar should be closed yet before boundary rollover"

        # Tick 3 @ 09:16:05 -> Triggers 09:15 M1 bar closure
        await aggregator.process_tick(
            Tick(
                instrument_token=408065,
                tradingsymbol="INFY",
                timestamp_ist=t2_rollover_m1,
                ltp=1505.0,
                volume=400,
                bid=1504.5,
                ask=1505.5,
            )
        )

        assert len(closed_candles) == 1
        m1_candle = closed_candles[0]
        assert m1_candle.timeframe == Timeframe.M1
        assert m1_candle.tradingsymbol == "INFY"
        assert m1_candle.open == 1500.0
        assert m1_candle.high == 1510.0
        assert m1_candle.low == 1500.0
        assert m1_candle.close == 1510.0
        assert m1_candle.volume == 300
        assert m1_candle.vwap > 0
        assert m1_candle.ema_200 > 0
        assert m1_candle.atr_14 > 0
        assert m1_candle.rvol > 0

        # Tick 4 @ 09:20:05 -> Triggers 09:15-09:20 M5 bar closure (and additional M1 closures)
        await aggregator.process_tick(
            Tick(
                instrument_token=408065,
                tradingsymbol="INFY",
                timestamp_ist=t3_rollover_m5,
                ltp=1520.0,
                volume=1000,
                bid=1519.5,
                ask=1520.5,
            )
        )

        m5_candles = [c for c in closed_candles if c.timeframe == Timeframe.M5]
        assert len(m5_candles) == 1
        m5_candle = m5_candles[0]
        assert m5_candle.timeframe == Timeframe.M5
        assert m5_candle.open == 1500.0

    asyncio.run(_run_test())


# ==============================================================================
# 3. ANOMALY DETECTOR TESTS
# ==============================================================================

def test_anomaly_detector_shocks():
    """Validates institutional volume surge and flash volatility freeze triggers."""
    async def _run_test():
        bus = EventBus(enable_redis_mirror=False)
        detector = AnomalyDetector(event_bus=bus)

        alerts: list[AnomalyAlertEvent] = []

        async def on_alert(event: AnomalyAlertEvent):
            alerts.append(event)

        bus.subscribe(AnomalyAlertEvent, on_alert)

        now = datetime(2026, 11, 16, 10, 0, 0, tzinfo=IST_TZ)

        # 1. Normal Candle (RVOL = 1.2, Range = 1.0x ATR)
        normal_candle = Candle(
            tradingsymbol="SBIN",
            timeframe=Timeframe.M5,
            timestamp_ist=now,
            open=800.0,
            high=805.0,
            low=798.0,
            close=803.0,
            volume=50000,
            vwap=801.0,
            ema_200=795.0,
            atr_14=8.0,
            rvol=1.2,
        )
        await detector.evaluate_candle(normal_candle)
        assert len(alerts) == 0
        assert detector.is_symbol_frozen("SBIN", now) is False

        # 2. Institutional Surge Candle (RVOL = 4.5 >= 4.0x)
        surge_candle = Candle(
            tradingsymbol="SBIN",
            timeframe=Timeframe.M5,
            timestamp_ist=now + timedelta(minutes=5),
            open=803.0,
            high=810.0,
            low=802.0,
            close=809.0,
            volume=250000,
            vwap=803.0,
            ema_200=796.0,
            atr_14=8.0,
            rvol=4.5,
        )
        await detector.evaluate_candle(surge_candle)
        assert len(alerts) == 1
        assert alerts[0].kind == "INSTITUTIONAL_SURGE"
        assert alerts[0].symbol == "SBIN"
        assert alerts[0].metrics["rvol"] == 4.5

        # 3. Flash Volatility Freeze (1-min bar range 35.0 >= 3.0x ATR_14 of 8.0)
        flash_candle = Candle(
            tradingsymbol="SBIN",
            timeframe=Timeframe.M1,
            timestamp_ist=now + timedelta(minutes=6),
            open=809.0,
            high=835.0,
            low=800.0,
            close=832.0,
            volume=150000,
            vwap=805.0,
            ema_200=797.0,
            atr_14=8.0,
            rvol=2.0,
        )
        await detector.evaluate_candle(flash_candle)
        assert len(alerts) == 2
        assert alerts[1].kind == "FLASH_VOLATILITY_FREEZE"
        assert alerts[1].symbol == "SBIN"

        # Symbol must be frozen at current time
        assert detector.is_symbol_frozen("SBIN", now + timedelta(minutes=6)) is True
        # Freeze should persist at +10 minutes
        assert detector.is_symbol_frozen("SBIN", now + timedelta(minutes=16)) is True
        # Freeze must expire after 15 minutes (+22 minutes)
        assert detector.is_symbol_frozen("SBIN", now + timedelta(minutes=22)) is False

    asyncio.run(_run_test())


# ==============================================================================
# 4. HISTORICAL TICK REPLAYER END-TO-END TESTS
# ==============================================================================

def test_tick_replayer_simulation():
    """Validates replaying a historical session at MAX_SPEED and aggregating candles."""
    async def _run_test():
        bus = EventBus(enable_redis_mirror=False)
        aggregator = CandleAggregator(
            event_bus=bus,
            timeframes=[Timeframe.M1, Timeframe.M5],
        )
        replayer = HistoricalTickReplayer(event_bus=bus)

        # Wire aggregator to bus
        bus.subscribe(TickEvent, aggregator.on_tick_event)

        closed_candles: list[Candle] = []

        async def on_candle(event: CandleCloseEvent):
            closed_candles.append(event.candle)

        bus.subscribe(CandleCloseEvent, on_candle)

        # Replay RELIANCE session
        ticks_count = await replayer.replay_session(
            symbols=["RELIANCE"],
            speed="MAX_SPEED",
        )

        assert ticks_count > 0, "Tick replayer should have generated and replayed ticks"
        assert len(closed_candles) > 0, "Candle aggregator should have closed and emitted candles"

        # Check that both stock and parent index (NIFTY ENERGY) candles were produced
        symbols_emitted = {c.tradingsymbol for c in closed_candles}
        assert "RELIANCE" in symbols_emitted
        assert "NIFTY_ENERGY" in symbols_emitted or "NIFTY ENERGY" in symbols_emitted

        # Validate closed candle attributes
        for c in closed_candles[:10]:
            assert isinstance(c, Candle)
            assert c.open > 0
            assert c.high >= c.low
            assert c.close > 0
            assert c.volume > 0
            assert c.vwap > 0
            assert c.atr_14 > 0

    asyncio.run(_run_test())
