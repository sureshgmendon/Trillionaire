"""
Unit and integration tests for the Six Production Upgrades:
1. Windows Task Scheduler scripts configuration & logging.
2. Quiet Pre-Login Reconciler & Safe Pause/Resume Trading Toggle.
3. Daily Orders and Realized PnL Redis Persistence & Rehydration.
4. NSE Market Holiday Awareness (2026) & 5-Milestone Engine Readiness.
5. Token-Free News Fetching & Sentinel Summary endpoint.
6. Broad Market (NIFTY 50) Trend Filter on breakout signals.
"""

from datetime import date, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from fastapi.testclient import TestClient

from api_bridge.main import app
from config.settings import settings
from sentinel.calendar_guard.event_calendar import get_market_holiday_name
from sentinel.news_triage.news_fetcher import AsyncNewsFetcher, NewsItem
from shared.constants import IST_TZ
from shared.enums import ClusterRegime, Side, Timeframe, TradingMode
from shared.schemas import Candle, Order, Position, Signal, StockProfile, Tick
from trading_engine.brokers.kite.kite_broker import KiteBroker
from trading_engine.core.engine import TrillionaireEngine
from trading_engine.core.event_bus import CandleCloseEvent, EventBus, SignalEvent
from trading_engine.execution.order_manager import OrderManager
from trading_engine.execution.paper_broker import PaperBroker
from trading_engine.risk.portfolio_risk import PortfolioRiskGatekeeper
from trading_engine.strategy.live_runner import LiveStrategyRunner


# -----------------------------------------------------------------------------
# 1. NSE Market Holiday Guard & 2026 Calendar
# -----------------------------------------------------------------------------

def test_nse_holiday_calendar_2026():
    """Verifies that NSE 2026 trading holidays and weekends are correctly identified."""
    # Guru Nanak Jayanti
    assert get_market_holiday_name(date(2026, 11, 24)) == "Guru Nanak Jayanti"
    # Christmas
    assert get_market_holiday_name(date(2026, 12, 25)) == "Christmas"
    # Republic Day
    assert get_market_holiday_name(date(2026, 1, 26)) == "Republic Day"
    # Normal Tuesday trading day (March 10, 2026)
    assert get_market_holiday_name(date(2026, 3, 10)) is None
    # Saturday / Sunday
    assert get_market_holiday_name(date(2026, 3, 14)) == "Weekend (Saturday)"
    assert get_market_holiday_name(date(2026, 3, 15)) == "Weekend (Sunday)"


# -----------------------------------------------------------------------------
# 2. Quiet Pre-Login Reconciler
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_quiet_prelogin_reconciler_skips_when_token_unauthorized():
    """Verifies that KiteBroker.reconcile_positions does not throw or spam when token is unauthorized."""
    mock_kite = MagicMock()
    mock_kite.positions.side_effect = Exception("Incorrect api_key or access_token")

    bus = EventBus()
    om = OrderManager(event_bus=bus, broker=PaperBroker(event_bus=bus))
    broker = KiteBroker(kite_client=mock_kite, event_bus=bus, order_manager=om)

    res = await broker.reconcile_positions()
    assert res["status"] == "skipped"
    assert res["reason"] == "Token unauthorized"


# -----------------------------------------------------------------------------
# 3. Safe Pause / Resume Trading Toggle
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_safe_pause_resume_trading():
    """Verifies that pause_trading halts signal routing while keeping ticker alive, and resume re-arms."""
    bus = EventBus()
    mock_auth = MagicMock()
    mock_auth.get_valid_access_token.return_value = "tok"
    mock_auth.get_kite_client.return_value = MagicMock()

    engine = TrillionaireEngine(
        trading_mode=TradingMode.PAPER,
        event_bus=bus,
        auth_manager=mock_auth,
    )
    await engine.boot_premarket(trade_date=date(2026, 3, 10))
    await engine.start()

    # 1. Initially active
    assert engine.trading_paused is False
    assert engine.strategy_runner.is_paused is False

    # 2. Pause trading
    p_res = engine.pause_trading()
    assert p_res["status"] == "paused"
    assert engine.trading_paused is True
    assert engine.strategy_runner.is_paused is True

    # 3. Emit signal while paused - order manager should receive 0 orders
    sig = Signal(
        signal_id="sig-pause-test",
        tradingsymbol="RELIANCE",
        parent_index="NIFTY ENERGY",
        side=Side.BUY,
        entry_price=2500.0,
        stop_loss=2480.0,
        target_price=2550.0,
        confidence=0.9,
        strategy_name="TrendMomentumStrategy",
        generated_at_ist=datetime.now(IST_TZ),
    )
    await bus.publish(SignalEvent(signal=sig))
    assert len(engine.order_manager.open_positions) == 0

    # 4. Resume trading
    r_res = engine.resume_trading()
    assert r_res["status"] == "resumed"
    assert engine.trading_paused is False
    assert engine.strategy_runner.is_paused is False

    await engine.stop()


# -----------------------------------------------------------------------------
# 4. Redis Order & Realized PnL Persistence & Rehydration
# -----------------------------------------------------------------------------

def test_rehydrate_from_redis_mocked():
    """Verifies that rehydrate_from_redis populates OrderManager orders and risk gatekeeper realized PnL."""
    import sys
    bus = EventBus()
    mock_auth = MagicMock()
    mock_auth.get_valid_access_token.return_value = "tok"
    mock_auth.get_kite_client.return_value = MagicMock()

    engine = TrillionaireEngine(trading_mode=TradingMode.PAPER, event_bus=bus, auth_manager=mock_auth)

    fake_orders = {
        "ord-1": '{"order_id": "ord-1", "tradingsymbol": "INFY", "side": "BUY", "product": "MIS", "quantity": 10, "status": "FILLED", "created_at_ist": "2026-03-10T09:30:00+05:30"}',
        "ord-2": '{"order_id": "ord-2", "tradingsymbol": "INFY", "side": "SELL", "product": "MIS", "quantity": 10, "status": "FILLED", "exit_reason": "TP_HIT", "created_at_ist": "2026-03-10T10:15:00+05:30"}'
    }

    mock_redis = MagicMock()
    mock_redis.hgetall.return_value = fake_orders
    mock_redis.get.return_value = "1540.50"

    mock_redis_module = MagicMock()
    mock_redis_module.from_url.return_value = mock_redis
    orig_redis = sys.modules.get("redis")
    sys.modules["redis"] = mock_redis_module

    try:
        res = engine.rehydrate_from_redis(trade_date=date(2026, 3, 10))
        assert res["orders_rehydrated"] == 2
        assert res["realized_pnl_rehydrated"] == 1540.50
        assert "ord-1" in engine.order_manager.orders
        assert "ord-2" in engine.order_manager.orders
        assert engine.risk_gatekeeper.realized_pnl == 1540.50
    finally:
        if orig_redis is not None:
            sys.modules["redis"] = orig_redis
        else:
            sys.modules.pop("redis", None)


# -----------------------------------------------------------------------------
# 5. 5-Milestone Engine Readiness Status
# -----------------------------------------------------------------------------

def test_5_milestones_structure():
    """Verifies get_lifecycle_milestones returns all 5 required milestones with valid schema."""
    bus = EventBus()
    mock_auth = MagicMock()
    mock_auth.get_valid_access_token.return_value = "tok"
    mock_auth.get_kite_client.return_value = MagicMock()

    engine = TrillionaireEngine(trading_mode=TradingMode.PAPER, event_bus=bus, auth_manager=mock_auth)
    milestones = engine.get_lifecycle_milestones()

    assert "summary_label" in milestones
    assert "milestones" in milestones
    assert milestones["total_count"] == 5

    milestone_ids = [m["id"] for m in milestones["milestones"]]
    assert milestone_ids == [
        "kite_auth",
        "universe_warmed",
        "ticker_connected",
        "ticks_flowing",
        "strategy_armed",
    ]


# -----------------------------------------------------------------------------
# 6. Broad Market (NIFTY 50) Trend Filter
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_nifty_50_broad_market_regime_filter():
    """Verifies that LiveStrategyRunner blocks BUY when NIFTY 50 M5 candle is below VWAP."""
    bus = EventBus()
    runner = LiveStrategyRunner(event_bus=bus)

    profile = StockProfile(
        tradingsymbol="TCS",
        instrument_token=2953217,
        parent_index="NIFTY IT",
        index_token=257289,
        cluster_regime=ClusterRegime.TREND_MOMENTUM,
        beta_5y=1.0,
        adr_pct=2.0,
        atr_multiplier_sl=1.5,
        atr_multiplier_tp=3.0,
        min_rvol_threshold=1.5,
        oos_sharpe=1.6,
        oos_profit_factor=1.8,
        is_active=True,
    )
    runner._set_profiles([profile])

    now = datetime(2026, 3, 10, 10, 0, tzinfo=IST_TZ)

    # Seed Bearish NIFTY 50 (close < vwap)
    runner._latest_candles[("NIFTY 50", Timeframe.M5)] = Candle(
        tradingsymbol="NIFTY 50",
        timeframe=Timeframe.M5,
        timestamp_ist=now,
        open=24000.0,
        high=24010.0,
        low=23950.0,
        close=23960.0,  # Below VWAP
        volume=50000,
        vwap=24000.0,
        ema_200=23900.0,
        atr_14=40.0,
        rvol=1.5,
    )
    runner._index_ema_20["NIFTY 50"] = 24000.0

    # Seed Parent Sector Index NIFTY IT (Bullish)
    runner._latest_candles[("NIFTY IT", Timeframe.M5)] = Candle(
        tradingsymbol="NIFTY IT",
        timeframe=Timeframe.M5,
        timestamp_ist=now,
        open=38000.0,
        high=38200.0,
        low=37950.0,
        close=38150.0,
        volume=20000,
        vwap=38050.0,
        ema_200=37800.0,
        atr_14=80.0,
        rvol=1.8,
    )
    runner._index_ema_20["NIFTY IT"] = 38050.0

    # Setup Opening Range & CPR for TCS
    runner._opening_ranges["TCS"] = (3900.0, 3850.0)
    runner._cpr_levels["TCS"] = (3860.0, 3890.0)

    # TCS Bullish breakout candle (c > or_high, c > vwap, c > ema200)
    tcs_candle = Candle(
        tradingsymbol="TCS",
        timeframe=Timeframe.M5,
        timestamp_ist=now,
        open=3880.0,
        high=3920.0,
        low=3875.0,
        close=3915.0,  # > 3900 or_high
        volume=100000,
        vwap=3890.0,
        ema_200=3800.0,
        atr_14=15.0,
        rvol=2.0,
    )

    # Evaluate candle — should be blocked by Gate 4 (NIFTY 50 market trend filter)
    sig = await runner.on_candle_close(CandleCloseEvent(candle=tcs_candle))
    assert sig is None
    assert runner.funnel_metrics.get("blocked_by_market_filter", 0) >= 1


# -----------------------------------------------------------------------------
# 7. API Bridge Endpoints (Pause, Resume, Milestones, Sentinel Summary)
# -----------------------------------------------------------------------------

def test_api_endpoints():
    """Tests /api/v1/control/engine/pause, resume, milestones, and sentinel/summary endpoints."""
    client = TestClient(app)
    admin_hdr = {"X-API-Key": settings.trillionaire_admin_key}

    # 1. Pause
    pause_res = client.post("/api/v1/control/engine/pause", headers=admin_hdr)
    assert pause_res.status_code == 200
    assert pause_res.json()["trading_paused"] is True

    # 2. Resume
    resume_res = client.post("/api/v1/control/engine/resume", headers=admin_hdr)
    assert resume_res.status_code == 200
    assert resume_res.json()["trading_paused"] is False

    # 3. Milestones
    m_res = client.get("/api/v1/engine/milestones")
    assert m_res.status_code == 200
    assert "milestones" in m_res.json()

    # 4. Sentinel Summary
    s_res = client.get("/api/v1/sentinel/summary")
    assert s_res.status_code == 200
    assert "headlines" in s_res.json()
    assert "gemini_brief" in s_res.json()
