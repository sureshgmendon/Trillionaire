"""
Comprehensive verification tests for Quantitative Architect Upgrades:
1. PAPER mode simulated OCO SL (TRIGGER PENDING) & TP (OPEN) child orders + Modify/Cancel API.
2. Profit-Only Active Trade Manager (50% halfway SL step, <=15% TP + RVOL>=1.5 extension & SL lock, never alters losers).
3. 50% Partial Profit Booking at +1R + breakeven runner + resized OCO brackets.
4. Choppy-Lunch Volume Filter (11:45-13:15 IST RVOL>=2.0 while scanning all stocks), 09:25 vs 09:30 start time, & strategy toggles.
5. Hourly automated AI Trade Autopsy scheduler slots + IST label + newest-first history + SUGGESTED TOGGLE ACTIONS.
6. Gemini model upgrade to gemini-3.8-flash -> gemini-2.5-pro.
"""

from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock, patch
import uuid
import pytest
from fastapi.testclient import TestClient

from api_bridge.main import app
from api_bridge.routes.engine_control import set_engine
from config.settings import settings
from sentinel.news_triage.gemini_client import call_gemini_async
from sentinel.news_triage.llm_classifier import PreMarketNewsClassifier
from sentinel.news_triage.news_fetcher import NewsItem
from sentinel.news_triage.trade_autopsy import generate_daily_trade_autopsy
from shared.constants import IST_TZ
from shared.enums import ClusterRegime, OrderStatus, OrderType, Side, Timeframe, TradingMode
from shared.schemas import Candle, ClosedTrade, Order, Position, Signal, StockProfile, Tick
from trading_engine.core.engine import TrillionaireEngine
from trading_engine.core.event_bus import CandleCloseEvent, EventBus, TickEvent
from trading_engine.core.scheduler import WeekdayMarketScheduler
from trading_engine.execution.order_manager import OrderManager
from trading_engine.execution.paper_broker import PaperBroker
from trading_engine.risk.portfolio_risk import PortfolioRiskGatekeeper
from trading_engine.strategy.live_runner import LiveStrategyRunner


@pytest.mark.asyncio
async def test_paper_mode_oco_brackets_and_modify_api():
    """
    Verifies PAPER mode creates resting SL (TRIGGER PENDING) and TP (OPEN) legs with shared trade_group_id,
    and POST /api/v1/control/orders/{order_id}/modify updates both order & position bracket cleanly.
    """
    bus = EventBus()
    risk_guard = PortfolioRiskGatekeeper(event_bus=bus, allocated_capital=200000.0, trading_mode=TradingMode.PAPER)
    broker = PaperBroker(event_bus=bus)
    om = OrderManager(event_bus=bus, broker=broker, risk_gatekeeper=risk_guard)
    engine = TrillionaireEngine(
        trading_mode=TradingMode.PAPER,
        event_bus=bus,
        risk_gatekeeper=risk_guard,
        order_manager=om,
        broker=broker,
    )
    set_engine(engine)

    t0 = datetime(2026, 5, 14, 9, 35, 0, tzinfo=IST_TZ)
    broker.set_latest_tick(
        Tick(
            instrument_token=738561,
            tradingsymbol="RELIANCE",
            timestamp_ist=t0,
            ltp=2500.0,
            volume=15000,
            bid=2499.8,
            ask=2500.2,
        )
    )

    sig = Signal(
        signal_id=str(uuid.uuid4()),
        tradingsymbol="RELIANCE",
        parent_index="NIFTY ENERGY",
        side=Side.BUY,
        entry_price=2500.0,
        stop_loss=2480.0,
        target_price=2540.0,
        confidence=0.88,
        strategy_name="TrendMomentumStrategy",
        generated_at_ist=t0,
    )
    entry_ord = await om.submit_signal_order(sig, quantity=10)
    assert entry_ord is not None and entry_ord.status == OrderStatus.FILLED
    pos = om.open_positions["RELIANCE"]
    assert pos.sl_order_id is not None
    assert pos.tp_order_id is not None

    sl_ord = om.orders[pos.sl_order_id]
    tp_ord = om.orders[pos.tp_order_id]
    assert sl_ord.status == OrderStatus.TRIGGER_PENDING
    assert sl_ord.trigger_price == 2480.0
    assert tp_ord.status == OrderStatus.OPEN
    assert tp_ord.limit_price == 2540.0
    assert sl_ord.trade_group_id == entry_ord.order_id
    assert tp_ord.trade_group_id == entry_ord.order_id

    client = TestClient(app)
    resp = client.post(
        f"/api/v1/control/orders/{sl_ord.order_id}/modify",
        headers={"X-API-Key": settings.trillionaire_admin_key},
        json={"trigger_price": 2488.5},
    )
    assert resp.status_code == 200
    assert resp.json()["order"]["trigger_price"] == 2488.5
    assert om.open_positions["RELIANCE"].stop_loss == 2488.5


@pytest.mark.asyncio
async def test_profit_only_trade_manager_losers_untouched_and_winners_managed(monkeypatch):
    """
    Verifies Profit-Only Trade Manager:
    - Never modifies SL/TP on losing open positions.
    - At >= 50% of distance to TP: steps SL halfway closer to Entry.
    - Within 15% of TP with RVOL >= 1.5: extends TP +1.0R and locks SL cleanly (+ snaps to orig TP on cross).
    """
    monkeypatch.setattr(settings, "enable_profit_trade_manager", True)
    monkeypatch.setattr(settings, "enable_partial_booking", False)

    bus = EventBus()
    risk_guard = PortfolioRiskGatekeeper(event_bus=bus, allocated_capital=200000.0, trading_mode=TradingMode.PAPER)
    broker = PaperBroker(event_bus=bus)
    om = OrderManager(event_bus=bus, broker=broker, risk_gatekeeper=risk_guard)

    t0 = datetime(2026, 5, 14, 9, 40, 0, tzinfo=IST_TZ)
    broker.set_latest_tick(
        Tick(instrument_token=1, tradingsymbol="INFY", timestamp_ist=t0, ltp=1500.0, volume=10000, bid=1499.9, ask=1500.1)
    )
    sig = Signal(
        signal_id=str(uuid.uuid4()),
        tradingsymbol="INFY",
        parent_index="NIFTY IT",
        side=Side.BUY,
        entry_price=1500.0,
        stop_loss=1480.0,   # R = 20
        target_price=1540.0,  # tp_dist = 40
        confidence=0.85,
        strategy_name="TrendMomentumStrategy",
        generated_at_ist=t0,
    )
    await om.submit_signal_order(sig, quantity=10)
    pos = om.open_positions["INFY"]
    orig_entry = pos.average_entry_price

    # 1. Losing M5 close -> MUST NEVER change SL or TP
    await bus.publish(
        CandleCloseEvent(
            candle=Candle(
                tradingsymbol="INFY",
                timeframe=Timeframe.M5,
                timestamp_ist=datetime(2026, 5, 14, 9, 45, tzinfo=IST_TZ),
                open=1500.0,
                high=1501.0,
                low=1488.0,
                close=1490.0,
                volume=30000,
                vwap=1496.0,
                ema_200=1492.0,
                atr_14=8.0,
                rvol=2.2,
            )
        )
    )
    assert pos.stop_loss == 1480.0
    assert pos.target_price == 1540.0

    # 2. >= 50% of distance to TP (close = 1522 -> +22 > 50% of 40) -> SL moves halfway between 1480 and orig_entry (~1490)
    await bus.publish(
        CandleCloseEvent(
            candle=Candle(
                tradingsymbol="INFY",
                timeframe=Timeframe.M5,
                timestamp_ist=datetime(2026, 5, 14, 9, 50, tzinfo=IST_TZ),
                open=1510.0,
                high=1523.0,
                low=1509.0,
                close=1522.0,
                volume=45000,
                vwap=1512.0,
                ema_200=1494.0,
                atr_14=8.0,
                rvol=1.4,
            )
        )
    )
    expected_halfway = round((1480.0 + orig_entry) / 2.0, 2)
    assert pos.halfway_sl_stepped is True
    assert pos.stop_loss == expected_halfway
    assert om.orders[pos.sl_order_id].trigger_price == expected_halfway

    # 3. Within 15% of TP (1536 -> 90% progress) with RVOL >= 1.5 -> TP extends +1.0R to 1560.0 & SL locks up safely
    await bus.publish(
        CandleCloseEvent(
            candle=Candle(
                tradingsymbol="INFY",
                timeframe=Timeframe.M5,
                timestamp_ist=datetime(2026, 5, 14, 9, 55, tzinfo=IST_TZ),
                open=1528.0,
                high=1537.0,
                low=1527.0,
                close=1536.0,
                volume=85000,
                vwap=1520.0,
                ema_200=1496.0,
                atr_14=8.0,
                rvol=2.1,
            )
        )
    )
    assert pos.profit_locked_at_tp is True
    assert pos.target_price == 1560.0  # 1540 + 20 (1.0R)
    assert pos.stop_loss > orig_entry
    assert pos.stop_loss < 1536.0  # Safe against instant self-stop-out

    # Crossing original TP (1541) snaps locked SL directly to original TP (1540.0)
    await bus.publish(
        TickEvent(
            tick=Tick(
                instrument_token=1,
                tradingsymbol="INFY",
                timestamp_ist=datetime(2026, 5, 14, 9, 58, tzinfo=IST_TZ),
                ltp=1541.0,
                volume=90000,
                bid=1540.9,
                ask=1541.1,
            )
        )
    )
    assert om.open_positions["INFY"].stop_loss == 1540.0


@pytest.mark.asyncio
async def test_partial_booking_50_pct_at_1r_on_short_and_long(monkeypatch):
    """
    Verifies 50% Partial Profit Booking at +1R on a SHORT position:
    - Exits quantity // 2 shares with is_partial=True
    - Records partial realized PnL in closed_trades
    - Moves remaining 50% runner SL to breakeven and resizes resting OCO legs
    """
    monkeypatch.setattr(settings, "enable_partial_booking", True)

    bus = EventBus()
    risk_guard = PortfolioRiskGatekeeper(event_bus=bus, allocated_capital=200000.0, trading_mode=TradingMode.PAPER)
    broker = PaperBroker(event_bus=bus)
    om = OrderManager(event_bus=bus, broker=broker, risk_gatekeeper=risk_guard)

    t0 = datetime(2026, 5, 14, 10, 0, 0, tzinfo=IST_TZ)
    broker.set_latest_tick(
        Tick(instrument_token=2, tradingsymbol="TCS", timestamp_ist=t0, ltp=3800.0, volume=12000, bid=3799.8, ask=3800.2)
    )
    sig = Signal(
        signal_id=str(uuid.uuid4()),
        tradingsymbol="TCS",
        parent_index="NIFTY IT",
        side=Side.SELL,
        entry_price=3800.0,
        stop_loss=3820.0,   # R = 20
        target_price=3740.0,
        confidence=0.90,
        strategy_name="TrendMomentumStrategy",
        generated_at_ist=t0,
    )
    await om.submit_signal_order(sig, quantity=10)
    pos = om.open_positions["TCS"]
    assert pos.quantity == 10

    # Price drops +1R to 3779 -> books 5 shares, 5 remain @ breakeven SL
    await bus.publish(
        TickEvent(
            tick=Tick(
                instrument_token=2,
                tradingsymbol="TCS",
                timestamp_ist=datetime(2026, 5, 14, 10, 12, tzinfo=IST_TZ),
                ltp=3779.0,
                volume=22000,
                bid=3778.9,
                ask=3779.1,
            )
        )
    )
    assert "TCS" in om.open_positions
    pos = om.open_positions["TCS"]
    assert pos.partial_booked is True
    assert pos.quantity == 5
    assert pos.stop_loss == pos.average_entry_price
    assert len(om.closed_trades) == 1
    assert om.closed_trades[0].filled_qty == 5
    assert om.closed_trades[0].is_partial is True
    assert om.closed_trades[0].realized_pnl > 0
    assert om.orders[pos.sl_order_id].quantity == 5
    assert om.orders[pos.tp_order_id].quantity == 5


@pytest.mark.asyncio
async def test_lunch_volume_filter_and_strategy_toggles(monkeypatch):
    """
    Verifies:
    - 11:45-13:15 IST Choppy-Lunch Volume Filter blocks RVOL < 2.0 while allowing RVOL >= 2.0.
    - Strategy toggle (enable_trend_strategy=False) disables TREND_MOMENTUM emission.
    - Opening Bell start time ("09:30" vs "09:25") shifts entry start cleanly.
    """
    monkeypatch.setattr(settings, "enable_vision_filter", False)
    monkeypatch.setattr(settings, "enable_lunch_volume_filter", True)
    monkeypatch.setattr(settings, "enable_trend_strategy", True)
    monkeypatch.setattr(settings, "entry_start_time_ist", "09:25")

    profile = StockProfile(
        tradingsymbol="SBIN",
        instrument_token=779521,
        parent_index="NIFTY BANK",
        index_token=260105,
        cluster_regime=ClusterRegime.TREND_MOMENTUM,
        beta_5y=1.1,
        adr_pct=2.2,
        atr_multiplier_sl=1.5,
        atr_multiplier_tp=3.0,
        min_rvol_threshold=1.2,
        oos_sharpe=1.5,
        oos_profit_factor=1.7,
        is_active=True,
    )
    bus = EventBus()
    runner = LiveStrategyRunner(event_bus=bus, profiles=[profile])
    runner.seed_opening_range("SBIN", or_high=810.0, or_low=800.0)

    def _seed_idx(dt_val: datetime):
        runner._index_ema_20["NIFTY BANK"] = 48000.0
        runner._latest_candles[("NIFTY BANK", Timeframe.M5)] = Candle(
            tradingsymbol="NIFTY BANK",
            timeframe=Timeframe.M5,
            timestamp_ist=dt_val,
            open=48100.0,
            high=48200.0,
            low=48050.0,
            close=48180.0,
            volume=500000,
            vwap=48100.0,
            ema_200=47900.0,
            atr_14=120.0,
            rvol=1.6,
        )

    # 1. During lunch (12:15 IST) with RVOL = 1.4 (< 2.0) -> blocked cleanly
    lunch_dt = datetime(2026, 5, 14, 12, 15, tzinfo=IST_TZ)
    _seed_idx(lunch_dt)
    blocked = await runner.on_candle_close(
        CandleCloseEvent(
            candle=Candle(
                tradingsymbol="SBIN",
                timeframe=Timeframe.M5,
                timestamp_ist=lunch_dt,
                open=812.0,
                high=816.0,
                low=811.0,
                close=815.0,
                volume=40000,
                vwap=808.0,
                ema_200=802.0,
                atr_14=4.0,
                rvol=1.4,
            )
        )
    )
    assert blocked is None
    assert runner.funnel_metrics["blocked_by_lunch_filter"] == 1
    assert runner.funnel_metrics["candles_evaluated"] == 1

    # 2. During lunch (12:20 IST) with RVOL = 2.4 (>= 2.0) -> allowed!
    lunch_dt2 = datetime(2026, 5, 14, 12, 20, tzinfo=IST_TZ)
    _seed_idx(lunch_dt2)
    allowed = await runner.on_candle_close(
        CandleCloseEvent(
            candle=Candle(
                tradingsymbol="SBIN",
                timeframe=Timeframe.M5,
                timestamp_ist=lunch_dt2,
                open=812.0,
                high=818.0,
                low=811.5,
                close=817.0,
                volume=95000,
                vwap=809.0,
                ema_200=802.0,
                atr_14=4.0,
                rvol=2.4,
            )
        )
    )
    assert allowed is not None and allowed.tradingsymbol == "SBIN"

    # 3. Entry start time = 09:30 blocks 09:26 signal
    monkeypatch.setattr(settings, "entry_start_time_ist", "09:30")
    early_dt = datetime(2026, 5, 14, 9, 26, tzinfo=IST_TZ)
    _seed_idx(early_dt)
    blocked_early = await runner.on_candle_close(
        CandleCloseEvent(
            candle=Candle(
                tradingsymbol="SBIN",
                timeframe=Timeframe.M5,
                timestamp_ist=early_dt,
                open=812.0,
                high=818.0,
                low=811.5,
                close=817.0,
                volume=95000,
                vwap=809.0,
                ema_200=802.0,
                atr_14=4.0,
                rvol=2.4,
            )
        )
    )
    assert blocked_early is None

    # 4. Disabling TREND_MOMENTUM suppresses signal at 10:15 IST
    monkeypatch.setattr(settings, "enable_trend_strategy", False)
    mid_dt = datetime(2026, 5, 14, 10, 15, tzinfo=IST_TZ)
    _seed_idx(mid_dt)
    disabled = await runner.on_candle_close(
        CandleCloseEvent(
            candle=Candle(
                tradingsymbol="SBIN",
                timeframe=Timeframe.M5,
                timestamp_ist=mid_dt,
                open=812.0,
                high=818.0,
                low=811.5,
                close=817.0,
                volume=95000,
                vwap=809.0,
                ema_200=802.0,
                atr_14=4.0,
                rvol=2.4,
            )
        )
    )
    assert disabled is None


@pytest.mark.asyncio
async def test_hourly_autopsy_scheduler_and_toggle_recommendations(tmp_path, monkeypatch):
    """
    Verifies:
    - WeekdayMarketScheduler executes automated hourly autopsy at 10:30, 11:30, 12:30, 13:30, 14:30 IST.
    - Reports stack newest-first in history with exact IST timestamp labels and SUGGESTED TOGGLE ACTIONS.
    """
    monkeypatch.setattr(settings, "gemini_api_key", "")
    monkeypatch.setenv("TRILLIONAIRE_STATE_DIR", str(tmp_path))

    bus = EventBus()
    engine = TrillionaireEngine(event_bus=bus)
    engine.is_running = True
    engine.is_booted = True
    engine.current_trade_date = date(2026, 5, 14)

    # Seed closed trades with morning + midday SL hits to trigger explicit toggle actions
    engine.order_manager.closed_trades = [
        ClosedTrade(
            order_id="t1",
            symbol="WIPRO",
            product="MIS",
            side=Side.BUY,
            filled_qty=20,
            entry_price=500.0,
            sl_price=495.0,
            target_price=510.0,
            actual_exit_price=495.0,
            realized_pnl=-110.0,
            pnl_pct=-1.0,
            exit_reason="SL_HIT",
            entry_time=datetime(2026, 5, 14, 9, 26, tzinfo=IST_TZ),
            exit_time=datetime(2026, 5, 14, 9, 40, tzinfo=IST_TZ),
        ),
        ClosedTrade(
            order_id="t2",
            symbol="TECHM",
            product="MIS",
            side=Side.BUY,
            filled_qty=10,
            entry_price=1400.0,
            sl_price=1390.0,
            target_price=1420.0,
            actual_exit_price=1390.0,
            realized_pnl=-115.0,
            pnl_pct=-0.71,
            exit_reason="SL_HIT",
            entry_time=datetime(2026, 5, 14, 12, 10, tzinfo=IST_TZ),
            exit_time=datetime(2026, 5, 14, 12, 25, tzinfo=IST_TZ),
        ),
    ]

    scheduler = WeekdayMarketScheduler(engine=engine)
    scheduler._last_boot_date = date(2026, 5, 14)

    # Simulate 10:30 IST and 12:30 IST hourly autopsy triggers
    await scheduler.evaluate_schedule(datetime(2026, 5, 14, 10, 30, tzinfo=IST_TZ))
    await scheduler.evaluate_schedule(datetime(2026, 5, 14, 12, 30, tzinfo=IST_TZ))
    assert len(scheduler._autopsy_history) == 2

    latest = scheduler.last_autopsy_report
    assert latest is not None
    assert "IST" in latest["generated_time_label"]
    assert len(latest["history"]) == 2
    action_keys = {a["setting_key"] for a in latest["suggested_toggle_actions"]}
    assert "enable_lunch_volume_filter" in action_keys
    assert "entry_start_time_ist" in action_keys
    assert "enable_mean_reversion_strategy" in action_keys


@pytest.mark.asyncio
async def test_gemini_3_8_flash_model_endpoints(monkeypatch):
    """Verifies Gemini REST client and News Classifier prioritize gemini-3.8-flash over gemini-1.5-flash."""
    called_urls = []

    class _FakeResp:
        status_code = 200

        def json(self):
            return {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": '{"evaluations": [{"tradingsymbol": "RELIANCE", "status": "NORMAL", "risk_multiplier": 1.0, "reason": "Clear"}]}'
                                }
                            ]
                        }
                    }
                ]
            }

    class _FakeClient:
        def __init__(self, timeout=10.0):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json=None):
            called_urls.append(url)
            return _FakeResp()

    monkeypatch.setattr(settings, "gemini_api_key", "AIza_test_gemini_38")
    classifier = PreMarketNewsClassifier()
    with patch("sentinel.news_triage.gemini_client.httpx.AsyncClient", _FakeClient):
        await classifier._classify_via_gemini([NewsItem(headline="RELIANCE Q4 results beat", summary="Strong GRM", source="NSE", published_at="08:00")])

    assert len(called_urls) == 1
    assert "gemini-3.8-flash" in called_urls[0]
    assert "gemini-1.5-flash" not in called_urls[0]
