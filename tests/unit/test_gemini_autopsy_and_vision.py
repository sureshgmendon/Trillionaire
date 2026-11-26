"""
Unit tests verifying:
1. Daily -1.5% drawdown circuit breaker bypassed in PAPER mode when disable_daily_loss_limit_in_paper=True,
   while strictly enforced in LIVE mode regardless of disable_daily_loss_limit_in_paper.
2. 03:35 PM IST Gemini Trade Autopsy scorecard & rule-based fallback generation + API routes.
3. Gemini Vision Chart Trap Filter PNG rendering, trap vetoing, and fail-open timeout/no-key behavior.
4. LiveStrategyRunner Gate 5 integration with VisionTrapFilter.
"""

import asyncio
from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock, patch
import uuid
import pytest
from fastapi.testclient import TestClient

from api_bridge.main import app
from api_bridge.routes.engine_control import set_engine
from config.settings import settings
from sentinel.news_triage.trade_autopsy import generate_daily_trade_autopsy
from sentinel.news_triage.vision_filter import VisionTrapFilter, render_m5_chart_png
from shared.constants import IST_TZ
from shared.enums import ClusterRegime, Side, Timeframe, TradingMode
from shared.schemas import Candle, ClosedTrade, Position, Signal, StockProfile, Tick
from trading_engine.core.engine import TrillionaireEngine
from trading_engine.core.event_bus import CandleCloseEvent, EventBus
from trading_engine.risk.portfolio_risk import PortfolioRiskGatekeeper
from trading_engine.strategy.live_runner import LiveStrategyRunner


@pytest.mark.asyncio
async def test_paper_mode_bypasses_daily_loss_limit_while_live_enforces():
    """
    Verifies:
    - In PAPER mode with disable_daily_loss_limit_in_paper=True, -2.0% drawdown does NOT trip kill-switch.
    - In LIVE mode even when disable_daily_loss_limit_in_paper=True, -2.0% drawdown strictly trips kill-switch.
    """
    assert settings.disable_daily_loss_limit_in_paper is True

    bus_paper = EventBus()
    paper_flattens = []

    async def on_paper_flatten(reason: str):
        paper_flattens.append(reason)

    paper_guard = PortfolioRiskGatekeeper(
        event_bus=bus_paper,
        allocated_capital=100000.0,
        max_daily_loss_pct=1.5,
        trading_mode=TradingMode.PAPER,
        disable_daily_loss_limit_in_paper=True,
        flatten_handler=on_paper_flatten,
    )
    paper_guard.register_position(
        Position(
            tradingsymbol="SBIN",
            side=Side.BUY,
            quantity=100,
            average_entry_price=800.0,
            ltp=800.0,
            unrealized_pnl=0.0,
            realized_pnl=0.0,
            stop_loss=760.0,
            target_price=840.0,
        )
    )
    await paper_guard.process_tick(
        Tick(
            instrument_token=779521,
            tradingsymbol="SBIN",
            timestamp_ist=datetime(2026, 4, 15, 11, 30, tzinfo=IST_TZ),
            ltp=780.0,  # -Rs. 2,000 (-2.0% > 1.5% limit)
            volume=50000,
            bid=779.9,
            ask=780.1,
        )
    )
    assert paper_guard.kill_switch_active is False
    assert paper_flattens == []

    # Same -2.0% drawdown in LIVE mode MUST trigger hard daily kill-switch regardless
    bus_live = EventBus()
    live_flattens = []

    async def on_live_flatten(reason: str):
        live_flattens.append(reason)

    live_guard = PortfolioRiskGatekeeper(
        event_bus=bus_live,
        allocated_capital=100000.0,
        max_daily_loss_pct=1.5,
        trading_mode=TradingMode.LIVE,
        disable_daily_loss_limit_in_paper=True,
        flatten_handler=on_live_flatten,
    )
    live_guard.register_position(
        Position(
            tradingsymbol="SBIN",
            side=Side.BUY,
            quantity=100,
            average_entry_price=800.0,
            ltp=800.0,
            unrealized_pnl=0.0,
            realized_pnl=0.0,
            stop_loss=760.0,
            target_price=840.0,
        )
    )
    await live_guard.process_tick(
        Tick(
            instrument_token=779521,
            tradingsymbol="SBIN",
            timestamp_ist=datetime(2026, 4, 15, 11, 30, tzinfo=IST_TZ),
            ltp=780.0,
            volume=50000,
            bid=779.9,
            ask=780.1,
        )
    )
    assert live_guard.kill_switch_active is True
    assert live_flattens == ["DAILY_LOSS_KILL_SWITCH_TRIPPED"]


@pytest.mark.asyncio
async def test_trade_autopsy_fallback_when_gemini_key_unset(tmp_path, monkeypatch):
    """Verifies structured Trade Autopsy generation & rule-based fallback when GEMINI_API_KEY is blank."""
    monkeypatch.setattr(settings, "gemini_api_key", "")
    trades = [
        ClosedTrade(
            order_id="o-win-1",
            symbol="RELIANCE",
            product="MIS",
            side=Side.BUY,
            filled_qty=20,
            entry_price=2500.0,
            sl_price=2480.0,
            target_price=2540.0,
            actual_exit_price=2540.0,
            realized_pnl=760.0,
            pnl_pct=1.6,
            exit_reason="TARGET_HIT",
            entry_time=datetime(2026, 5, 12, 9, 45, tzinfo=IST_TZ),
            exit_time=datetime(2026, 5, 12, 10, 30, tzinfo=IST_TZ),
        ),
        ClosedTrade(
            order_id="o-loss-1",
            symbol="TCS",
            product="MIS",
            side=Side.BUY,
            filled_qty=10,
            entry_price=3800.0,
            sl_price=3780.0,
            target_price=3840.0,
            actual_exit_price=3780.0,
            realized_pnl=-215.0,
            pnl_pct=-0.53,
            exit_reason="SL_HIT",
            entry_time=datetime(2026, 5, 12, 11, 45, tzinfo=IST_TZ),
            exit_time=datetime(2026, 5, 12, 12, 5, tzinfo=IST_TZ),
        ),
    ]
    out_file = tmp_path / "latest_trade_autopsy.json"
    report = await generate_daily_trade_autopsy(
        closed_trades=trades,
        funnel_metrics={"candles_evaluated": 150, "passed_stock_rule": 12, "passed_sector_filter": 6, "signals_emitted": 2},
        daily_mtm=545.0,
        realized_pnl=545.0,
        allocated_capital=100000.0,
        trade_date=date(2026, 5, 12),
        send_telegram=False,
        enable_redis=False,
        artifact_path=out_file,
    )
    assert out_file.exists()
    assert report["source"] == "rule_based_fallback"
    assert report["session_scorecard"]["total_trades"] == 2
    assert report["session_scorecard"]["win_rate_pct"] == 50.0
    assert report["session_scorecard"]["net_post_tax_pnl_inr"] == 545.0
    assert len(report["loss_and_trap_diagnosis"]) >= 1
    assert len(report["parameter_recommendations"]) >= 1
    assert "Session Scorecard" in report["markdown_report"]


def _sample_candles(symbol: str = "RELIANCE", count: int = 12):
    bars = []
    for i in range(count):
        px = 2500.0 + i * 1.5
        bars.append(
            Candle(
                tradingsymbol=symbol,
                timeframe=Timeframe.M5,
                timestamp_ist=datetime(2026, 5, 12, 9, 20 + i * 5 if 20 + i * 5 < 60 else 55, tzinfo=IST_TZ),
                open=px - 1.0,
                high=px + 2.0,
                low=px - 2.0,
                close=px + 0.5,
                volume=50000 + i * 1000,
                vwap=px - 1.0,
                ema_200=2490.0,
                atr_14=6.0,
                rvol=1.8,
            )
        )
    return bars


@pytest.mark.asyncio
async def test_vision_filter_png_veto_and_timeout_fail_open(monkeypatch):
    """
    Verifies:
    1. render_m5_chart_png produces a valid PNG header cleanly in-memory.
    2. VisionTrapFilter fails open when GEMINI_API_KEY is unset.
    3. VisionTrapFilter vetoes when Gemini returns approved=False.
    4. VisionTrapFilter strictly fails open on >3.5s asyncio timeout.
    """
    bars = _sample_candles("RELIANCE", 12)
    png = render_m5_chart_png(bars, "RELIANCE", Side.BUY, or_high=2515.0, or_low=2495.0)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")

    sig = Signal(
        signal_id=str(uuid.uuid4()),
        tradingsymbol="RELIANCE",
        parent_index="NIFTY ENERGY",
        side=Side.BUY,
        entry_price=2518.0,
        stop_loss=2502.0,
        target_price=2550.0,
        confidence=0.88,
        strategy_name="TrendMomentumStrategy",
        generated_at_ist=datetime(2026, 5, 12, 10, 30, tzinfo=IST_TZ),
    )

    vf = VisionTrapFilter(timeout_sec=0.1)

    # 1. Missing API key -> fail open
    monkeypatch.setattr(settings, "gemini_api_key", "")
    bypass = await vf.evaluate_signal(sig, bars)
    assert bypass["approved"] is True
    assert bypass["fail_open"] is True

    # 2. Gemini detects double-top bull trap -> veto signal
    monkeypatch.setattr(settings, "gemini_api_key", "AIza_fake_test_key")
    with patch(
        "sentinel.news_triage.vision_filter.call_gemini_async",
        new=AsyncMock(
            return_value={
                "ok": True,
                "model": "gemini-3.8-flash",
                "data": {"approved": False, "confidence": 0.91, "reason": "Double-top resistance exhaustion"},
            }
        ),
    ):
        veto = await vf.evaluate_signal(sig, bars)
        assert veto["approved"] is False
        assert veto["confidence"] == 0.91
        assert "Double-top" in veto["reason"]

    # 3. Gemini latency exceeds timeout -> fail open cleanly
    async def slow_gemini(*args, **kwargs):
        await asyncio.sleep(1.0)
        return {"ok": False}

    with patch("sentinel.news_triage.vision_filter.call_gemini_async", side_effect=slow_gemini):
        timeout_res = await vf.evaluate_signal(sig, bars, timeout_sec=0.05)
        assert timeout_res["approved"] is True
        assert timeout_res["fail_open"] is True
        assert "timed out" in timeout_res["reason"].lower()


@pytest.mark.asyncio
async def test_live_strategy_runner_respects_vision_veto():
    """Verifies LiveStrategyRunner increments blocked_by_vision_filter and suppresses SignalEvent when vetoed."""
    bus = EventBus()
    mock_vision = MagicMock(spec=VisionTrapFilter)
    mock_vision.evaluate_signal = AsyncMock(
        return_value={"approved": False, "confidence": 0.85, "reason": "Vertical exhaustion trap", "fail_open": False}
    )

    runner = LiveStrategyRunner(event_bus=bus, vision_filter=mock_vision)
    runner.profiles = {
        "RELIANCE": StockProfile(
            tradingsymbol="RELIANCE",
            instrument_token=738561,
            parent_index="NIFTY ENERGY",
            index_token=261641,
            cluster_regime=ClusterRegime.TREND_MOMENTUM,
            beta_5y=1.1,
            adr_pct=2.2,
            atr_multiplier_sl=1.5,
            atr_multiplier_tp=3.0,
            min_rvol_threshold=1.5,
            oos_sharpe=1.5,
            oos_profit_factor=1.8,
            is_active=True,
        )
    }
    ts = datetime(2026, 5, 12, 10, 30, tzinfo=IST_TZ)
    runner._latest_candles[("NIFTY ENERGY", Timeframe.M5)] = Candle(
        tradingsymbol="NIFTY ENERGY",
        timeframe=Timeframe.M5,
        timestamp_ist=ts,
        open=38000.0,
        high=38200.0,
        low=37950.0,
        close=38180.0,
        volume=100000,
        vwap=38020.0,
        ema_200=37800.0,
        atr_14=120.0,
        rvol=1.4,
    )
    runner._index_ema_20["NIFTY ENERGY"] = 38000.0
    runner._opening_ranges["RELIANCE"] = (2510.0, 2490.0)

    mock_sig = Signal(
        signal_id=str(uuid.uuid4()),
        tradingsymbol="RELIANCE",
        parent_index="NIFTY ENERGY",
        side=Side.BUY,
        entry_price=2525.0,
        stop_loss=2510.0,
        target_price=2555.0,
        confidence=0.90,
        strategy_name="TrendMomentumStrategy",
        generated_at_ist=ts,
    )
    runner._strategies[ClusterRegime.TREND_MOMENTUM] = MagicMock(evaluate=MagicMock(return_value=mock_sig))

    emitted = await runner.on_candle_close(
        CandleCloseEvent(candle=_sample_candles("RELIANCE", 1)[0])
    )
    assert emitted is None
    assert runner.funnel_metrics["blocked_by_vision_filter"] == 1
    assert len(runner.emitted_signals) == 0


def test_autopsy_api_endpoints(monkeypatch):
    """Tests GET /api/v1/sentinel/autopsy and POST /api/v1/sentinel/autopsy/run."""
    monkeypatch.setattr(settings, "gemini_api_key", "")
    bus = EventBus()
    engine = TrillionaireEngine(trading_mode=TradingMode.PAPER, event_bus=bus)
    set_engine(engine)

    with TestClient(app) as client:
        run_resp = client.post(
            "/api/v1/sentinel/autopsy/run",
            headers={"X-API-Key": settings.trillionaire_admin_key},
        )
        assert run_resp.status_code == 200
        data = run_resp.json()
        assert data["source"] == "rule_based_fallback"
        assert "session_scorecard" in data

        get_resp = client.get("/api/v1/sentinel/autopsy")
        assert get_resp.status_code == 200
        assert get_resp.json()["trade_date"] == data["trade_date"]
