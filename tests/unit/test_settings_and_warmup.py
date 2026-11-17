"""
Unit tests for Settings & Credentials Manager, Engine Lifecycle Control,
Universe Expansion (100 Stocks), Parquet Pre-Warmup, and Strategy Funnel Metrics.
"""

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
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

    # Verify status API exposes funnel_metrics
    resp = client.get("/api/v1/status")
    assert resp.status_code == 200
    body = resp.json()
    assert "funnel_metrics" in body
    assert body["funnel_metrics"]["candles_evaluated"] >= 1
