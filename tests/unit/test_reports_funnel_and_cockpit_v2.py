"""
Comprehensive tests for the 7 Cockpit, Reporting, & Architecture Upgrades:
1. Multi-tab XLSX workbooks (5-tab Evaluation + 2-tab PL Report), date-wise archiving YYYY/MM/YYYY-MM-DD/,
   and date-range GET /api/v1/reports/export aggregation.
2. Rolling funnel_logs buffer & GET /api/v1/funnel/logs stage filtering (ALL, EVAL, STOCK, SECTOR, SIGNAL).
3. Zero leftover gemini-2.5-pro references + VisionTrapFilter not polluting SENTINEL NEWSWIRE.
4. Cycle-safe /api/v1/sentinel/autopsy serialization + live-toggle-aware suggestions + AUTOPSY_UPDATED broadcast.
5. 6-milestone engine_restarted readiness check & research_lab/artifacts/kite_access_token.json path.
"""

from datetime import date, datetime, timedelta
from io import BytesIO
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import uuid
import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from api_bridge.main import app
from api_bridge.routes.engine_control import set_engine
from config.settings import settings
from sentinel.news_triage.trade_autopsy import (
    generate_daily_trade_autopsy,
    load_latest_trade_autopsy,
    sanitize_json_safe,
)
from sentinel.news_triage.vision_filter import VisionTrapFilter
from sentinel.orchestrator import SentinelOrchestrator
from shared.constants import IST_TZ
from shared.enums import ClusterRegime, OrderStatus, OrderType, Side, Timeframe, TradingMode
from shared.schemas import Candle, ClosedTrade, Order, Signal, StockProfile
from trading_engine.brokers.kite.auth import DEFAULT_TOKEN_CACHE_FILE, KiteAuthManager
from trading_engine.core.engine import TrillionaireEngine
from trading_engine.core.event_bus import AutopsyUpdatedEvent, CandleCloseEvent, EventBus, RiskAlertEvent
from trading_engine.core.xlsx_reports import (
    EVAL_SHEET_NAMES,
    archive_daily_reports,
    date_folder_for,
)
from trading_engine.strategy.live_runner import LiveStrategyRunner


@pytest.mark.asyncio
async def test_funnel_logs_buffer_and_api_stage_filtering(monkeypatch):
    """Verifies LiveStrategyRunner logs EVAL/STOCK/SECTOR/SIGNAL and /api/v1/funnel/logs filters each tab."""
    monkeypatch.setattr(settings, "enable_vision_filter", False)
    monkeypatch.setattr(settings, "enable_trend_strategy", True)
    monkeypatch.setattr(settings, "entry_start_time_ist", "09:25")

    bus = EventBus()
    profile = StockProfile(
        tradingsymbol="ICICIBANK",
        instrument_token=1270529,
        parent_index="NIFTY BANK",
        index_token=260105,
        cluster_regime=ClusterRegime.TREND_MOMENTUM,
        beta_5y=1.1,
        adr_pct=2.0,
        atr_multiplier_sl=1.5,
        atr_multiplier_tp=3.0,
        min_rvol_threshold=1.2,
        oos_sharpe=1.5,
        oos_profit_factor=1.6,
        is_active=True,
    )
    runner = LiveStrategyRunner(event_bus=bus, profiles=[profile])
    runner.seed_opening_range("ICICIBANK", or_high=1100.0, or_low=1080.0)
    dt = datetime(2026, 6, 15, 10, 15, tzinfo=IST_TZ)

    runner._index_ema_20["NIFTY BANK"] = 48000.0
    runner._latest_candles[("NIFTY BANK", Timeframe.M5)] = Candle(
        tradingsymbol="NIFTY BANK",
        timeframe=Timeframe.M5,
        timestamp_ist=dt,
        open=48100.0,
        high=48200.0,
        low=48050.0,
        close=48180.0,
        volume=500000,
        vwap=48100.0,
        ema_200=47900.0,
        atr_14=120.0,
        rvol=1.5,
    )
    sig = await runner.on_candle_close(
        CandleCloseEvent(
            candle=Candle(
                tradingsymbol="ICICIBANK",
                timeframe=Timeframe.M5,
                timestamp_ist=dt,
                open=1102.0,
                high=1110.0,
                low=1101.0,
                close=1108.0,
                volume=80000,
                vwap=1098.0,
                ema_200=1090.0,
                atr_14=5.0,
                rvol=2.1,
            )
        )
    )
    assert sig is not None
    assert len(runner.funnel_logs) >= 4

    engine = TrillionaireEngine(trading_mode=TradingMode.PAPER, event_bus=bus, strategy_runner=runner)
    set_engine(engine)
    client = TestClient(app)

    all_res = client.get("/api/v1/funnel/logs?stage=ALL").json()
    assert all_res["counts"]["ALL"] >= 4
    assert all_res["counts"]["EVAL"] >= 1
    assert all_res["counts"]["STOCK"] >= 1
    assert all_res["counts"]["SECTOR"] >= 1
    assert all_res["counts"]["SIGNAL"] >= 1

    for st in ("EVAL", "STOCK", "SECTOR", "SIGNAL"):
        res = client.get(f"/api/v1/funnel/logs?stage={st}").json()
        assert len(res["logs"]) >= 1
        assert all(row["stage"] == st for row in res["logs"])


def test_multitab_xlsx_archive_and_date_range_export(tmp_path, monkeypatch):
    """
    Verifies:
    1. archive_daily_reports writes YYYY/MM/YYYY-MM-DD/Trillionaire_Evaluation_Report & _PL_Report.xlsx.
    2. Trillionaire_Evaluation_Report has 5 tabs: All, Eval, Stock, Sector, Signal.
    3. Trillionaire_PL_Report has 2 tabs: PL Report (with TOTAL row) & Executed Orders (stock-grouped chronologically).
    4. GET /api/v1/reports/export merges historical archived days + today across preset/custom ranges.
    """
    monkeypatch.setenv("TRILLIONAIRE_REPORTS_DIR", str(tmp_path))

    yesterday = date(2026, 6, 14)
    y_logs = [
        {
            "id": "y-1",
            "date_ist": yesterday.isoformat(),
            "timestamp_ist": "09:30:00",
            "symbol": "ICICIBANK",
            "parent_index": "NIFTY BANK",
            "regime": "TREND_MOMENTUM",
            "stage": "EVAL",
            "passed": True,
            "close": 1105.0,
            "vwap": 1100.0,
            "ema200": 1090.0,
            "rvol": 1.8,
            "reason": "Evaluated M5 bar",
        },
        {
            "id": "y-2",
            "date_ist": yesterday.isoformat(),
            "timestamp_ist": "09:30:00",
            "symbol": "ICICIBANK",
            "parent_index": "NIFTY BANK",
            "regime": "TREND_MOMENTUM",
            "stage": "SIGNAL",
            "passed": True,
            "close": 1105.0,
            "vwap": 1100.0,
            "ema200": 1090.0,
            "rvol": 1.8,
            "reason": "Signal emitted",
        },
    ]
    y_trade = ClosedTrade(
        order_id="ex-1",
        symbol="ICICIBANK",
        product="MIS",
        side=Side.BUY,
        filled_qty=10,
        entry_price=1105.0,
        sl_price=1095.0,
        target_price=1125.0,
        actual_exit_price=1125.0,
        realized_pnl=185.0,
        pnl_pct=1.81,
        exit_reason="TARGET_HIT",
        entry_time=datetime(2026, 6, 14, 9, 30, tzinfo=IST_TZ),
        exit_time=datetime(2026, 6, 14, 10, 5, tzinfo=IST_TZ),
    )
    y_entry = Order(
        order_id="ord-1",
        tradingsymbol="ICICIBANK",
        side=Side.BUY,
        product="MIS",
        order_type=OrderType.LIMIT,
        quantity=10,
        filled_quantity=10,
        limit_price=1105.0,
        average_fill_price=1105.0,
        status=OrderStatus.FILLED,
        is_exit=False,
        created_at_ist=datetime(2026, 6, 14, 9, 30, tzinfo=IST_TZ),
    )
    y_exit = Order(
        order_id="ex-1",
        tradingsymbol="ICICIBANK",
        side=Side.SELL,
        product="MIS",
        order_type=OrderType.LIMIT,
        quantity=10,
        filled_quantity=10,
        limit_price=1125.0,
        average_fill_price=1125.0,
        status=OrderStatus.FILLED,
        is_exit=True,
        exit_reason="TARGET_HIT",
        created_at_ist=datetime(2026, 6, 14, 10, 5, tzinfo=IST_TZ),
    )

    paths = archive_daily_reports(
        trade_date=yesterday,
        funnel_logs=y_logs,
        closed_trades=[y_trade],
        open_positions=[],
        orders=[y_exit, y_entry],
    )
    expected_dir = date_folder_for(yesterday)
    assert paths["evaluation"] == expected_dir / "Trillionaire_Evaluation_Report_2026-06-14.xlsx"
    assert paths["pl"] == expected_dir / "Trillionaire_PL_Report_2026-06-14.xlsx"
    assert paths["evaluation"].exists() and paths["pl"].exists()

    wb_eval = load_workbook(paths["evaluation"])
    assert wb_eval.sheetnames == EVAL_SHEET_NAMES
    assert wb_eval["All"].max_row == 3
    assert wb_eval["Eval"].max_row == 2
    assert wb_eval["Signal"].max_row == 2
    assert wb_eval["Stock"].max_row == 1

    wb_pl = load_workbook(paths["pl"])
    assert wb_pl.sheetnames == ["PL Report", "Executed Orders"]
    assert wb_pl["PL Report"].cell(row=wb_pl["PL Report"].max_row, column=1).value == "TOTAL"
    # Executed Orders sorted chronologically within stock (BUY Entry before SELL Exit)
    assert wb_pl["Executed Orders"].cell(row=2, column=3).value == "BUY"
    assert wb_pl["Executed Orders"].cell(row=3, column=3).value == "SELL"

    # REST API custom range export across yesterday -> today
    bus = EventBus()
    engine = TrillionaireEngine(trading_mode=TradingMode.PAPER, event_bus=bus)
    set_engine(engine)
    client = TestClient(app)

    today_str = datetime.now(IST_TZ).date().isoformat()
    r_eval = client.get(
        f"/api/v1/reports/export?preset=custom&from_date=2026-06-14&to_date={today_str}&report_type=evaluation"
    )
    assert r_eval.status_code == 200
    assert "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" in r_eval.headers["content-type"]
    wb_api = load_workbook(BytesIO(r_eval.content))
    assert wb_api.sheetnames == EVAL_SHEET_NAMES
    assert wb_api["All"].max_row >= 3

    r_pl = client.get(
        f"/api/v1/reports/export?preset=custom&from_date=2026-06-14&to_date={today_str}&report_type=pl"
    )
    assert r_pl.status_code == 200
    wb_api_pl = load_workbook(BytesIO(r_pl.content))
    assert wb_api_pl.sheetnames == ["PL Report", "Executed Orders"]


@pytest.mark.asyncio
async def test_no_gemini_2_5_pro_and_clean_newswire(monkeypatch):
    """
    Verifies:
    1. Zero remaining 'gemini-2.5-pro' references in trading_engine/, sentinel/, api_bridge/, config/.
    2. VisionTrapFilter fail-open does NOT pollute sentinel.latest_headlines or emit FAIL-OPEN RiskAlertEvents.
    """
    repo_root = Path(__file__).resolve().parents[2]
    offenders = []
    for folder in ("trading_engine", "sentinel", "api_bridge", "config"):
        for py_file in (repo_root / folder).rglob("*.py"):
            content = py_file.read_text(encoding="utf-8", errors="ignore")
            if "gemini-2.5-pro" in content:
                offenders.append(str(py_file))
    assert offenders == [], f"Unexpected gemini-2.5-pro references found: {offenders}"

    bus = EventBus()
    published_alerts = []
    bus.subscribe(RiskAlertEvent, lambda e: published_alerts.append(e))

    sentinel = SentinelOrchestrator()
    sentinel.run_pre_market_triage()
    orig_headlines = list(sentinel.latest_headlines)

    profile = StockProfile(
        tradingsymbol="HDFCBANK",
        instrument_token=341249,
        parent_index="NIFTY BANK",
        index_token=260105,
        cluster_regime=ClusterRegime.TREND_MOMENTUM,
        beta_5y=1.0,
        adr_pct=1.8,
        atr_multiplier_sl=1.5,
        atr_multiplier_tp=3.0,
        min_rvol_threshold=1.2,
        oos_sharpe=1.4,
        oos_profit_factor=1.5,
        is_active=True,
    )
    monkeypatch.setattr(settings, "enable_vision_filter", True)
    monkeypatch.setattr(settings, "gemini_api_key", "")
    monkeypatch.setattr(settings, "entry_start_time_ist", "09:25")

    runner = LiveStrategyRunner(event_bus=bus, profiles=[profile], sentinel_orchestrator=sentinel)
    runner.seed_opening_range("HDFCBANK", or_high=1600.0, or_low=1580.0)
    dt = datetime(2026, 6, 15, 10, 15, tzinfo=IST_TZ)
    runner._index_ema_20["NIFTY BANK"] = 48000.0
    runner._latest_candles[("NIFTY BANK", Timeframe.M5)] = Candle(
        tradingsymbol="NIFTY BANK",
        timeframe=Timeframe.M5,
        timestamp_ist=dt,
        open=48100.0,
        high=48200.0,
        low=48050.0,
        close=48180.0,
        volume=500000,
        vwap=48100.0,
        ema_200=47900.0,
        atr_14=120.0,
        rvol=1.5,
    )
    sig = await runner.on_candle_close(
        CandleCloseEvent(
            candle=Candle(
                tradingsymbol="HDFCBANK",
                timeframe=Timeframe.M5,
                timestamp_ist=dt,
                open=1602.0,
                high=1610.0,
                low=1601.0,
                close=1608.0,
                volume=70000,
                vwap=1598.0,
                ema_200=1590.0,
                atr_14=5.0,
                rvol=2.1,
            )
        )
    )
    assert sig is not None
    assert sentinel.latest_headlines == orig_headlines
    assert published_alerts == []


@pytest.mark.asyncio
async def test_autopsy_cycle_safe_live_toggles_and_ws_broadcast(tmp_path, monkeypatch):
    """
    Verifies:
    1. sanitize_json_safe breaks cyclic references without raising ValueError.
    2. Live-toggle-aware Autopsy NEVER suggests turning ON already-True toggles or 09:30 when already 09:30.
    3. POST /api/v1/sentinel/autopsy/run publishes AutopsyUpdatedEvent cleanly and returns 200 OK.
    """
    cyclic: dict = {"trade_date": "2026-06-15", "score": 1}
    cyclic["history"] = [cyclic]
    cleaned = sanitize_json_safe(cyclic)
    assert isinstance(json.dumps(cleaned), str)

    monkeypatch.setenv("TRILLIONAIRE_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "gemini_api_key", "")
    # Enable all positive toggles & set start time to 09:30, disable Mean Reversion -> zero redundant suggestions!
    monkeypatch.setattr(settings, "enable_lunch_volume_filter", True)
    monkeypatch.setattr(settings, "entry_start_time_ist", "09:30")
    monkeypatch.setattr(settings, "enable_profit_trade_manager", True)
    monkeypatch.setattr(settings, "enable_partial_booking", True)
    monkeypatch.setattr(settings, "enable_mean_reversion_strategy", False)
    monkeypatch.setattr(settings, "enable_vision_filter", True)

    trades = [
        ClosedTrade(
            order_id="loss-1",
            symbol="SBIN",
            product="MIS",
            side=Side.BUY,
            filled_qty=20,
            entry_price=800.0,
            sl_price=790.0,
            target_price=820.0,
            actual_exit_price=790.0,
            realized_pnl=-210.0,
            pnl_pct=-1.25,
            exit_reason="SL_HIT",
            entry_time=datetime(2026, 6, 15, 9, 32, tzinfo=IST_TZ),
            exit_time=datetime(2026, 6, 15, 12, 10, tzinfo=IST_TZ),
        )
    ]
    report = await generate_daily_trade_autopsy(
        closed_trades=trades,
        send_telegram=False,
        enable_redis=False,
    )
    assert report["suggested_toggle_actions"] == []

    # Verify broadcast on POST /api/v1/sentinel/autopsy/run
    bus = EventBus()
    received_events = []
    bus.subscribe(AutopsyUpdatedEvent, lambda ev: received_events.append(ev))
    engine = TrillionaireEngine(trading_mode=TradingMode.PAPER, event_bus=bus)
    set_engine(engine)
    client = TestClient(app)

    post_res = client.post(
        "/api/v1/sentinel/autopsy/run",
        headers={"X-API-Key": settings.trillionaire_admin_key},
    )
    assert post_res.status_code == 200
    assert len(received_events) == 1

    get_res = client.get("/api/v1/sentinel/autopsy")
    assert get_res.status_code == 200
    assert "history" in get_res.json()


@pytest.mark.asyncio
async def test_6th_milestone_engine_restarted_and_token_cache_path(tmp_path):
    """Verifies research_lab/artifacts/kite_access_token.json default path and 6-milestone engine_restarted logic."""
    assert DEFAULT_TOKEN_CACHE_FILE.parts[-3:] == ("research_lab", "artifacts", "kite_access_token.json")

    mgr = KiteAuthManager(token_cache_path=tmp_path / "artifacts" / "kite_access_token.json")
    mgr._save_token("fresh_tok_123", datetime.now(IST_TZ).date())
    assert mgr.token_cache_path.exists()

    bus = EventBus()
    mock_auth = MagicMock()
    mock_auth.get_valid_access_token.return_value = "fresh_tok_123"
    mock_auth.get_kite_client.return_value = MagicMock()

    engine = TrillionaireEngine(trading_mode=TradingMode.PAPER, event_bus=bus, auth_manager=mock_auth)
    m_before = {m["id"]: m for m in engine.get_lifecycle_milestones()["milestones"]}
    assert m_before["engine_restarted"]["status"] == "waiting"

    await engine.start()
    m_after = engine.get_lifecycle_milestones()
    assert m_after["total_count"] == 6
    after_map = {m["id"]: m for m in m_after["milestones"]}
    assert after_map["engine_restarted"]["status"] == "passed"
