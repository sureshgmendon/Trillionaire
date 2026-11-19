"""
Unit tests for Settings & Credentials Manager, Engine Lifecycle Control,
Universe Expansion (100 Stocks), Parquet Pre-Warmup, and Strategy Funnel Metrics.
"""

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest
import yaml
from fastapi.testclient import TestClient

from api_bridge.main import app
from api_bridge.routes.engine_control import set_engine
from config.settings import settings, persist_env_updates, reload_settings_from_env
from shared.constants import IST_TZ
from shared.enums import ClusterRegime, Side, Timeframe, TradingMode
from shared.schemas import Candle, StockProfile, Tick
from trading_engine.core.engine import TrillionaireEngine
from trading_engine.core.event_bus import EventBus
from trading_engine.data.candle_aggregator import CandleAggregator
from trading_engine.execution.order_manager import OrderManager
from trading_engine.execution.paper_broker import PaperBroker
from trading_engine.risk.portfolio_risk import PortfolioRiskGatekeeper
from trading_engine.strategy.live_runner import LiveStrategyRunner


@pytest.fixture
def mock_engine_and_client():
    """Sets up an engine attached to FastAPI TestClient with admin key authentication."""
    bus = EventBus()
    risk_guard = PortfolioRiskGatekeeper(event_bus=bus, allocated_capital=100000.0)
    broker = PaperBroker(event_bus=bus)
    om = OrderManager(event_bus=bus, broker=broker, risk_gatekeeper=risk_guard)
    strategy_runner = LiveStrategyRunner(event_bus=bus)

    engine = TrillionaireEngine(
        trading_mode=TradingMode.PAPER,
        event_bus=bus,
        risk_gatekeeper=risk_guard,
        order_manager=om,
        broker=broker,
        strategy_runner=strategy_runner,
    )
    engine.is_booted = True
    set_engine(engine)

    client = TestClient(app)
    return engine, client, bus, strategy_runner, risk_guard


# ==============================================================================
# 1. Dashboard Settings API Tests
# ==============================================================================

def test_settings_get_requires_auth(mock_engine_and_client):
    _, client, _, _, _ = mock_engine_and_client
    # No auth header -> 403
    resp = client.get("/api/v1/settings")
    assert resp.status_code == 403

    # Invalid auth header -> 403
    resp_bad = client.get("/api/v1/settings", headers={"X-API-Key": "invalid_key"})
    assert resp_bad.status_code == 403


def test_settings_get_masks_credentials(mock_engine_and_client):
    _, client, _, _, _ = mock_engine_and_client
    admin_key = settings.trillionaire_admin_key
    resp = client.get("/api/v1/settings", headers={"X-API-Key": admin_key})
    assert resp.status_code == 200

    data = resp.json()
    assert "kite_api_key_masked" in data
    assert "has_kite_key" in data
    assert "has_kite_secret" in data
    assert "has_totp_secret" in data
    assert "trading_mode" in data
    assert "active_capital_tier" in data
    assert "engine_running" in data

    # Raw secrets must never appear in response
    assert "kite_password" not in data
    assert "kite_api_secret" not in data


def test_persist_env_updates_and_reload():
    """Validates updating .env file safely without clobbering other properties."""
    with tempfile.NamedTemporaryFile("w+", suffix=".env", delete=False) as f:
        f.write("# Sample configuration\nTRADING_MODE=PAPER\nACTIVE_CAPITAL_TIER=TIER_0_PAPER\nFOO=BAR\n")
        temp_env = Path(f.name)

    try:
        # Update trading mode and capital tier
        updates = {
            "TRADING_MODE": "LIVE",
            "ACTIVE_CAPITAL_TIER": "TIER_1_PILOT",
            "NEW_VAR": "TEST_VAL",
        }
        persist_env_updates(updates, env_path=temp_env)

        content = temp_env.read_text(encoding="utf-8")
        assert 'TRADING_MODE="LIVE"' in content or "TRADING_MODE=LIVE" in content
        assert 'ACTIVE_CAPITAL_TIER="TIER_1_PILOT"' in content or "ACTIVE_CAPITAL_TIER=TIER_1_PILOT" in content
        assert "FOO=BAR" in content
        assert 'NEW_VAR="TEST_VAL"' in content or "NEW_VAR=TEST_VAL" in content
        assert "# Sample configuration" in content

        # Reload settings using this temp env
        reload_settings_from_env(env_path=temp_env)
        assert settings.trading_mode == "LIVE"
        assert settings.active_capital_tier == "TIER_1_PILOT"
    finally:
        if temp_env.exists():
            os.remove(temp_env)


def test_settings_post_updates_runtime_and_engine(mock_engine_and_client):
    engine, client, _, _, risk_guard = mock_engine_and_client
    admin_key = settings.trillionaire_admin_key

    try:
        # Call POST /api/v1/settings to change capital tier to TIER_1_MICRO
        payload = {
            "active_capital_tier": "TIER_1_MICRO",
            "trading_mode": "PAPER",
        }
        resp = client.post("/api/v1/settings", json=payload, headers={"X-API-Key": admin_key})
        assert resp.status_code == 200
        res_data = resp.json()
        assert res_data["status"] == "saved"

        # Verify risk_guard tier was updated
        assert risk_guard.tier_name == "TIER_1_MICRO"
        assert risk_guard.allocated_capital == 100000.0
    finally:
        # Reset back to TIER_0_PAPER
        client.post(
            "/api/v1/settings",
            json={"active_capital_tier": "TIER_0_PAPER", "trading_mode": "PAPER"},
            headers={"X-API-Key": admin_key},
        )


# ==============================================================================
# 2. Engine Start & Stop Control API Tests
# ==============================================================================

def test_engine_start_stop_controls(mock_engine_and_client):
    engine, client, _, _, _ = mock_engine_and_client
    admin_key = settings.trillionaire_admin_key

    # 1. Stop engine
    stop_resp = client.post("/api/v1/control/engine/stop", headers={"X-API-Key": admin_key})
    assert stop_resp.status_code == 200
    assert engine.is_running is False

    # 2. Start engine
    start_resp = client.post("/api/v1/control/engine/start", headers={"X-API-Key": admin_key})
    assert start_resp.status_code == 200
    assert engine.is_running is True

    # Check status endpoint reflects is_running
    status_resp = client.get("/api/v1/status")
    assert status_resp.status_code == 200
    assert status_resp.json()["is_running"] is True


# ==============================================================================
# 3. 100-Stock Universe & Stock Profiles Validation
# ==============================================================================

def test_100_stock_universe_and_profiles():
    """Validates config/sector_map.yaml contains 100 symbols across 8 sector indices."""
    sector_yaml = Path("config/sector_map.yaml")
    assert sector_yaml.exists(), "config/sector_map.yaml must exist"

    with open(sector_yaml, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    benchmark_indices = data.get("benchmark_indices", {})
    expected_indices = [
        "NIFTY 50",
        "NIFTY BANK",
        "NIFTY IT",
        "NIFTY AUTO",
        "NIFTY PHARMA",
        "NIFTY ENERGY",
        "NIFTY FMCG",
        "NIFTY METAL",
    ]
    for idx in expected_indices:
        assert idx in benchmark_indices, f"Index {idx} must be present in sector_map.yaml"

    stocks = data.get("stocks", [])
    assert len(stocks) == 100, f"Expected exactly 100 tracked stocks, found {len(stocks)}"

    # Validate stock_profiles.json
    profiles_json = Path("research_lab/artifacts/stock_profiles.json")
    assert profiles_json.exists(), "stock_profiles.json must exist"

    with open(profiles_json, "r", encoding="utf-8") as f:
        profiles_data = json.load(f)

    assert len(profiles_data) == 100, f"Expected 100 profiles in stock_profiles.json, found {len(profiles_data)}"
    profile_syms = {p["tradingsymbol"] for p in profiles_data}
    assert "UNITDSPR" in profile_syms, "Renamed symbol UNITDSPR must be present in stock_profiles.json"
    assert "LTM" in profile_syms, "Renamed symbol LTM must be present in stock_profiles.json"
    assert "TMPV" in profile_syms, "Demerged symbol TMPV must be present in stock_profiles.json"
    assert "MCDOWELL-N" not in profile_syms, "Legacy symbol MCDOWELL-N must not be in stock_profiles.json"
    assert "LTIM" not in profile_syms, "Legacy symbol LTIM must not be in stock_profiles.json"
    assert "TATAMOTORS" not in profile_syms, "Demerged symbol TATAMOTORS must be replaced by TMPV in stock_profiles.json"


# ==============================================================================
# 4. Parquet Warmup & Funnel Metrics Tests
# ==============================================================================

def test_candle_aggregator_warmup_from_parquet():
    """Validates warmup_from_parquet seeds EMA(200), EMA(20), and ATR(14)."""
    bus = EventBus()
    agg = CandleAggregator(event_bus=bus)

    # Warmup for RELIANCE and TCS
    warmed = agg.warmup_from_parquet(symbols=["RELIANCE", "TCS"], bars_count=250)
    assert "RELIANCE" in warmed
    assert "TCS" in warmed
    assert warmed["RELIANCE"]["ema_200"] > 0
    assert warmed["RELIANCE"]["bars_loaded"] > 0
    assert warmed["TCS"]["ema_200"] > 0

    # Verify state has seeded indicators
    rel_m1_state = agg.get_state("RELIANCE", Timeframe.M1)
    assert rel_m1_state.ema_200 > 0
    assert rel_m1_state.ema_20 > 0
    assert rel_m1_state.atr_14 > 0
    assert rel_m1_state.prev_close > 0


@pytest.mark.asyncio
async def test_strategy_runner_funnel_metrics(mock_engine_and_client):
    """Validates funnel_metrics increments upon candle evaluation."""
    engine, client, bus, runner, _ = mock_engine_and_client

    assert "candles_evaluated" in runner.funnel_metrics
    assert "passed_stock_rule" in runner.funnel_metrics
    assert "passed_sector_filter" in runner.funnel_metrics
    assert "signals_emitted" in runner.funnel_metrics

    initial_evaluated = runner.funnel_metrics["candles_evaluated"]

    # Create a profile and simulate candle evaluation
    profile = StockProfile(
        tradingsymbol="RELIANCE",
        instrument_token=738561,
        parent_index="NIFTY ENERGY",
        index_token=260105,
        cluster_regime=ClusterRegime.TREND_MOMENTUM,
        beta_5y=1.15,
        adr_pct=2.45,
        atr_multiplier_sl=1.5,
        atr_multiplier_tp=3.0,
        min_rvol_threshold=1.5,
        oos_sharpe=1.65,
        oos_profit_factor=1.85,
        is_active=True,
    )
    runner.profiles["RELIANCE"] = profile

    candle = Candle(
        tradingsymbol="RELIANCE",
        timeframe=Timeframe.M5,
        timestamp_ist=datetime.now(IST_TZ),
        open=2500.0,
        high=2515.0,
        low=2495.0,
        close=2510.0,
        volume=15000,
        vwap=2505.0,
        ema_200=2490.0,
        atr_14=12.5,
        rvol=1.8,
    )

    # Evaluate candle via on_candle_close
    from trading_engine.core.event_bus import CandleCloseEvent
    await runner.on_candle_close(CandleCloseEvent(candle=candle))
    assert runner.funnel_metrics["candles_evaluated"] == initial_evaluated + 1

    # Verify status API exposes funnel_metrics
    resp = client.get("/api/v1/status")
    assert resp.status_code == 200
    body = resp.json()
    assert "funnel_metrics" in body
    assert body["funnel_metrics"]["candles_evaluated"] >= 1


def test_warmup_from_live_history_when_data_store_empty(tmp_path: Path):
    """
    Verifies that when research_lab/data_store is empty or missing,
    CandleAggregator automatically falls back to live history retrieval (via Kite or Yahoo)
    and seeds EMA(200), EMA(20), ATR(14), and RVOL baselines.
    """
    bus = EventBus()
    agg = CandleAggregator(event_bus=bus)
    empty_store = tmp_path / "empty_store"
    empty_store.mkdir()

    # Mock Kite historical data
    mock_kite = MagicMock()
    mock_kite.historical_data.return_value = [
        {"date": "2026-03-01 09:15:00", "open": 2500.0, "high": 2510.0, "low": 2490.0, "close": 2505.0, "volume": 10000},
        {"date": "2026-03-01 09:20:00", "open": 2505.0, "high": 2515.0, "low": 2500.0, "close": 2510.0, "volume": 12000},
        {"date": "2026-03-01 09:25:00", "open": 2510.0, "high": 2520.0, "low": 2505.0, "close": 2518.0, "volume": 15000},
    ]

    mock_sync = MagicMock()
    mock_sync.get_token.return_value = 738561

    warmed = agg.warmup_from_parquet(
        symbols=["RELIANCE"],
        data_dir=empty_store,
        kite_client=mock_kite,
        instrument_sync=mock_sync,
        fetch_live_fallback=True,
    )

    assert "RELIANCE" in warmed
    assert warmed["RELIANCE"]["source"] == "live_history"
    assert warmed["RELIANCE"]["ema_200"] > 0
    assert warmed["RELIANCE"]["ema_20"] > 0
    assert warmed["RELIANCE"]["atr_14"] > 0
    assert warmed["RELIANCE"]["prev_close"] == 2518.0

    # Verify state in aggregator
    m5_state = agg.get_state("RELIANCE", Timeframe.M5)
    assert m5_state.ema_200 > 0
    assert m5_state.rvol_baselines["DEFAULT"] > 0


@pytest.mark.asyncio
async def test_live_m5_candle_close_increments_funnel_eval_on_boot(mock_engine_and_client):
    """
    Verifies that upon premarket boot and the very first live M5 candle close,
    LiveStrategyRunner increments funnel_metrics["candles_evaluated"] immediately.
    """
    engine, client, bus, runner, _ = mock_engine_and_client

    # Profile for RELIANCE
    profile = StockProfile(
        tradingsymbol="RELIANCE",
        instrument_token=738561,
        parent_index="NIFTY ENERGY",
        index_token=260105,
        cluster_regime=ClusterRegime.TREND_MOMENTUM,
        beta_5y=1.15,
        adr_pct=2.45,
        atr_multiplier_sl=1.5,
        atr_multiplier_tp=3.0,
        min_rvol_threshold=1.5,
        oos_sharpe=1.65,
        oos_profit_factor=1.85,
        is_active=True,
    )
    runner.profiles["RELIANCE"] = profile

    initial_eval = runner.funnel_metrics["candles_evaluated"]

    # Construct and publish an M5 candle close event
    from trading_engine.core.event_bus import CandleCloseEvent
    m5_candle = Candle(
        tradingsymbol="RELIANCE",
        timeframe=Timeframe.M5,
        timestamp_ist=datetime.now(IST_TZ),
        open=2500.0,
        high=2520.0,
        low=2495.0,
        close=2515.0,
        volume=25000,
        vwap=2510.0,
        ema_200=2490.0,
        atr_14=15.0,
        rvol=1.8,
    )
    await bus.publish(CandleCloseEvent(candle=m5_candle))

    # Funnel evaluated count must have incremented
    assert runner.funnel_metrics["candles_evaluated"] == initial_eval + 1

    # Verify status API exposes updated funnel_metrics
    resp = client.get("/api/v1/status")
    assert resp.status_code == 200
    status_data = resp.json()
    assert status_data["funnel_metrics"]["candles_evaluated"] >= 1


@pytest.mark.asyncio
async def test_engine_start_runs_boot_premarket_every_time(mock_engine_and_client):
    """
    Verifies that POST /api/v1/control/engine/start with boot_premarket=True
    invokes engine.boot_premarket() even if engine.is_booted is already True.
    """
    engine, client, _, _, _ = mock_engine_and_client
    admin_key = settings.trillionaire_admin_key

    # Ensure engine is marked booted
    engine.is_booted = True
    boot_called = False

    async def mock_boot(**kwargs):
        nonlocal boot_called
        boot_called = True
        return {"status": "booted"}

    engine.boot_premarket = mock_boot

    # Request start with boot_premarket=True (default)
    resp = client.post("/api/v1/control/engine/start?boot_premarket=true", headers={"X-API-Key": admin_key})
    assert resp.status_code == 200
    assert boot_called is True, "boot_premarket() must be invoked whenever boot_premarket=True"


@pytest.mark.asyncio
async def test_replay_auto_seeds_when_datastore_empty(mock_engine_and_client, tmp_path):
    """
    Verifies that POST /control/replay auto-generates 30 days of synthetic data
    when research_lab/data_store has no parquet files.
    """
    engine, client, _, _, _ = mock_engine_and_client
    admin_key = settings.trillionaire_admin_key

    generated_called = False

    def mock_generate(self, num_days=30):
        nonlocal generated_called
        generated_called = True
        assert num_days == 30

    with patch("research_lab.data_loader.synthetic_generator.SyntheticDataGenerator.generate_all_sector_universe", mock_generate):
        with patch("trading_engine.data.tick_replayer.HistoricalTickReplayer.replay_session", return_value=100):
            with patch("research_lab.data_loader.synthetic_generator.DATA_STORE_DIR", tmp_path):
                resp = client.post(
                    "/api/v1/control/replay",
                    headers={"X-API-Key": admin_key},
                    json={"speed": "MAX_SPEED"},
                )
                assert resp.status_code == 200
                assert generated_called is True, "Replay must auto-generate synthetic data when store is empty"


def test_warmup_from_live_history_210_bars(tmp_path: Path):
    """
    Validates warmup_from_live_history with 220 bars to ensure full EMA(200) warmup on Bar #1.
    """
    bus = EventBus()
    agg = CandleAggregator(event_bus=bus)

    # Mock 220 5-minute bars
    bars = []
    base_price = 2500.0
    for i in range(220):
        p = base_price + (i * 0.5)
        bars.append({
            "date": f"2026-03-01 09:{15 + (i % 60):02d}:00",
            "open": p,
            "high": p + 2.0,
            "low": p - 2.0,
            "close": p + 1.0,
            "volume": 5000 + i * 10,
        })

    mock_kite = MagicMock()
    mock_kite.historical_data.return_value = bars

    mock_sync = MagicMock()
    mock_sync.get_token.return_value = 738561

    warmed = agg.warmup_from_live_history(
        symbols=["RELIANCE"],
        kite_client=mock_kite,
        instrument_sync=mock_sync,
        days=5,
        save_to_parquet=False,
        data_dir=tmp_path,
    )

    assert "RELIANCE" in warmed
    assert warmed["RELIANCE"]["bars_loaded"] == 220
    assert warmed["RELIANCE"]["ema_200"] > 0
    assert warmed["RELIANCE"]["ema_20"] > 0
    assert warmed["RELIANCE"]["atr_14"] > 0

    st = agg.get_state("RELIANCE", Timeframe.M5)
    assert st.ema_200 == warmed["RELIANCE"]["ema_200"]
    assert st.ema_20 == warmed["RELIANCE"]["ema_20"]

