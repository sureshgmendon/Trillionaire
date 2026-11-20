"""
Trillionaire API Bridge - Engine Control & Telemetry REST Endpoints
Provides HTTP endpoints for operator dashboard telemetry, diagnostics, and emergency controls:
1. GET /status: Real-time MTM, capital tier, circuit breaker flags, and engine mode.
2. GET /positions & GET /orders: Active positions and session order history.
3. GET /profiles & GET /sentinel: Stock profiles merged with Sentinel risk tags.
4. POST /control/kill-switch: Emergency portfolio liquidation and entry lockout (Admin Protected).
5. POST /control/replay: Offline historical session simulation replay (Admin Protected).
"""

import asyncio
import os
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, Field
from loguru import logger

from config.settings import settings
from api_bridge.ws.telemetry_manager import telemetry_manager
from research_lab.data_loader.synthetic_generator import DATA_STORE_DIR, FALLBACK_DATA_STORE_DIR, SyntheticDataGenerator
from shared.constants import DEFAULT_REPLAY_BASKET, IST_TZ
from shared.enums import TradingMode
from trading_engine.core.engine import TrillionaireEngine
from trading_engine.data.tick_replayer import HistoricalTickReplayer

FALLBACK_DATA_STORE_DIR = (
    Path("/tmp/trillionaire_data_store") if os.name != "nt" else Path(tempfile.gettempdir()) / "trillionaire_data_store"
)


def get_writable_data_store_dir(preferred_dir: Path = DATA_STORE_DIR) -> Path:
    """
    Returns preferred_dir if it is writable.
    If not writable (e.g. host-mounted Docker volume owned by host UID),
    automatically falls back to /tmp/trillionaire_data_store.
    """
    try:
        preferred_dir.mkdir(parents=True, exist_ok=True)
        if not os.access(preferred_dir, os.W_OK):
            raise PermissionError(f"Directory {preferred_dir} is not writable (os.W_OK is False)")
        probe = preferred_dir / ".perm_check"
        probe.touch(exist_ok=True)
        probe.unlink(missing_ok=True)
        return preferred_dir
    except (PermissionError, OSError) as e:
        logger.warning(
            f"[DATA STORE PERMISSION] {preferred_dir} is not writable ({e}). "
            f"Falling back to {FALLBACK_DATA_STORE_DIR}."
        )
        FALLBACK_DATA_STORE_DIR.mkdir(parents=True, exist_ok=True)
        return FALLBACK_DATA_STORE_DIR


router = APIRouter(prefix="", tags=["Engine Control & Telemetry"])

# Global engine reference (populated by main.py lifespan or testing)
_global_engine: Optional[TrillionaireEngine] = None


def get_engine() -> TrillionaireEngine:
    """Dependency provider for TrillionaireEngine singleton."""
    global _global_engine
    if _global_engine is None:
        _global_engine = TrillionaireEngine()
    return _global_engine


def set_engine(engine: TrillionaireEngine) -> None:
    """Sets active engine instance."""
    global _global_engine
    _global_engine = engine


def verify_admin_api_key(
    x_api_key: Optional[str] = Header(None, alias="X-API-Key", description="Admin authorization key"),
) -> None:
    """Security dependency verifying X-API-Key header against TRILLIONAIRE_ADMIN_KEY."""
    expected_key = settings.trillionaire_admin_key
    if not x_api_key or x_api_key != expected_key:
        logger.warning(f"[AUTH REJECTED] Invalid X-API-Key provided: {x_api_key}")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: Invalid or missing X-API-Key header",
        )


# -----------------------------------------------------------------------------
# Request & Response Schemas
# -----------------------------------------------------------------------------

class ReplayRequest(BaseModel):
    trading_date: Optional[str] = Field(default=None, description="ISO date YYYY-MM-DD to replay")
    speed: str = Field(default="MAX_SPEED", description="Replay speed: 1x, 10x, 50x, or MAX_SPEED")
    symbols: Optional[List[str]] = Field(default=None, description="Subset of symbols to replay")


# -----------------------------------------------------------------------------
# Telemetry Endpoints (Read-Only)
# -----------------------------------------------------------------------------

@router.get("/status")
async def get_engine_status(engine: TrillionaireEngine = Depends(get_engine)) -> Dict[str, Any]:
    """Returns real-time engine diagnostics, MTM, and circuit breaker status."""
    st = engine.get_status()
    risk_guard = engine.risk_gatekeeper
    unrealized = sum(pos.unrealized_pnl for pos in engine.order_manager.open_positions.values())

    return {
        "mode": st["trading_mode"],
        "data_feeder": st.get("data_feeder", "ZERODHA_KITE"),
        "is_running": st["is_running"],
        "is_booted": st["is_booted"],
        "capital_tier": st["capital_tier"],
        "allocated_capital": st["allocated_capital"],
        "daily_mtm": st["daily_total_mtm"],
        "realized_pnl": st["daily_realized_pnl"],
        "unrealized_pnl": round(unrealized, 2),
        "circuit_breaker_tripped": st["kill_switch_active"],
        "profit_floor_armed": st["profit_lock_armed"],
        "open_positions_count": st["open_positions_count"],
        "active_stocks_count": len(engine.strategy_runner.profiles),
        "funnel_metrics": st.get("funnel_metrics", {
            "candles_evaluated": 0,
            "passed_stock_rule": 0,
            "passed_sector_filter": 0,
            "signals_emitted": 0,
        }),
        "timestamp_ist": datetime.now(IST_TZ).isoformat(),
    }


@router.get("/positions")
async def get_active_positions(engine: TrillionaireEngine = Depends(get_engine)) -> List[Dict[str, Any]]:
    """Returns active intraday positions with entry prices, LTP, and unrealized PnL."""
    return [
        pos.model_dump(mode="json")
        for pos in engine.order_manager.open_positions.values()
    ]


@router.get("/orders")
async def get_session_orders(engine: TrillionaireEngine = Depends(get_engine)) -> List[Dict[str, Any]]:
    """Returns complete list of session orders from OrderManager."""
    return [
        order.model_dump(mode="json")
        for order in engine.order_manager.orders.values()
    ]


@router.get("/profiles")
async def get_active_profiles(engine: TrillionaireEngine = Depends(get_engine)) -> List[Dict[str, Any]]:
    """Returns statistical stock microstructure profiles."""
    return [
        profile.model_dump(mode="json")
        for profile in engine.strategy_runner.profiles.values()
    ]


@router.get("/sentinel")
async def get_sentinel_risk_tags(engine: TrillionaireEngine = Depends(get_engine)) -> List[Dict[str, Any]]:
    """Returns active stock profiles merged with today's Sentinel news & macro risk tags."""
    results = []
    for sym, profile in engine.strategy_runner.profiles.items():
        tag = engine.sentinel.get_effective_risk_multiplier(sym)
        merged = profile.model_dump(mode="json")
        merged["sentinel"] = tag.model_dump(mode="json")
        results.append(merged)
    return results


# -----------------------------------------------------------------------------
# Control Endpoints (Mutating - Protected by Admin Key)
# -----------------------------------------------------------------------------

@router.post("/control/kill-switch", dependencies=[Depends(verify_admin_api_key)])
async def trigger_emergency_kill_switch(
    reason: str = Query("API_KILL_SWITCH", description="Reason for triggering kill-switch"),
    engine: TrillionaireEngine = Depends(get_engine),
) -> Dict[str, Any]:
    """
    Emergency circuit breaker: Immediately liquidates all open positions and locks session.
    Requires X-API-Key header.
    """
    logger.critical(f"[API CONTROL] Emergency kill switch invoked via REST API: {reason}")
    flattened_orders = await engine.emergency_flatten(reason=reason)
    return {
        "status": "executed",
        "action": "EMERGENCY_KILL_SWITCH",
        "reason": reason,
        "liquidated_orders_count": len(flattened_orders),
        "timestamp_ist": datetime.now(IST_TZ).isoformat(),
    }


def _resolve_replay_symbols(symbols: Optional[List[str]]) -> List[str]:
    """Resolves target symbols defaulting to multi-sector basket with parent indices."""
    if not symbols:
        return list(DEFAULT_REPLAY_BASKET)

    sector_data = settings.load_sector_map()
    needed_indices = set()
    for s in sector_data.get("stocks", []):
        if s["tradingsymbol"] in symbols:
            needed_indices.add(s["parent_index"])

    resolved = list(symbols)
    for idx in needed_indices:
        if idx not in resolved:
            resolved.append(idx)
    return resolved


def _is_missing_parquets(data_dir: Path, symbols: Optional[List[str]]) -> bool:
    """Checks whether data_dir has no parquets or is missing requested symbols."""
    if not data_dir.exists():
        return True
    parquet_files = list(data_dir.glob("*.parquet"))
    if not parquet_files:
        return True
    check_syms = _resolve_replay_symbols(symbols)
    for sym in check_syms:
        clean = sym.replace(":", "_").replace(" ", "_")
        candidates = [
            data_dir / f"{clean}_1m.parquet",
            data_dir / f"{sym}_1m.parquet",
            data_dir / f"{sym.replace('-', '_')}_1m.parquet",
        ]
        if not any(c.exists() for c in candidates):
            return True
    return False


def _generate_synthetic_universe_sync(data_dir: Path, symbols: Optional[List[str]] = None, num_days: int = 30) -> Path:
    """Synchronous generator executed safely in a worker thread via asyncio.to_thread."""
    target_dir = get_writable_data_store_dir(data_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    generator = SyntheticDataGenerator(data_dir=target_dir)
    target_dir = generator.data_dir
    generator.generate_all_sector_universe(num_days=num_days)

    syms_to_gen = _resolve_replay_symbols(symbols)
    stock_to_index = {s["tradingsymbol"]: s["parent_index"] for s in settings.load_sector_map().get("stocks", [])}
    for sym in syms_to_gen:
        if "NIFTY" in sym:
            continue
        clean = sym.replace(":", "_").replace(" ", "_")
        p_path = target_dir / f"{clean}_1m.parquet"
        if not p_path.exists():
            parent_idx = stock_to_index.get(sym, "NIFTY 50")
            generator.generate_correlated_stock_series(
                tradingsymbol=sym,
                parent_index_symbol=parent_idx,
                num_days=num_days,
            )
    return target_dir


@router.post("/control/replay", dependencies=[Depends(verify_admin_api_key)])
async def start_historical_replay(
    req: ReplayRequest,
    engine: TrillionaireEngine = Depends(get_engine),
) -> Dict[str, Any]:
    """
    Starts an offline historical session replay onto EventBus for dashboard visual validation.
    Auto-seeds realistic correlated synthetic Parquet files if data_store is empty or missing symbols.
    Requires X-API-Key header.
    """
    try:
        # Resolve target symbols: default to multi-sector basket if blank
        replay_symbols = _resolve_replay_symbols(req.symbols)

        # Resolve writable data directory (falls back to /tmp/trillionaire_data_store if permissions denied)
        effective_data_dir = get_writable_data_store_dir(DATA_STORE_DIR)

        # Check if parquet dataset is missing or incomplete
        if _is_missing_parquets(effective_data_dir, replay_symbols):
            logger.warning(
                f"[REPLAY AUTO-SEED] Parquet datasets missing in {effective_data_dir}. Auto-generating 30-day synthetic universe via worker thread..."
            )
            effective_data_dir = await asyncio.to_thread(_generate_synthetic_universe_sync, effective_data_dir, replay_symbols, 30)

        target_date: Optional[date] = None
        if req.trading_date:
            try:
                target_date = date.fromisoformat(req.trading_date)
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid trading_date format '{req.trading_date}', expected YYYY-MM-DD",
                )

        # Ensure engine is booted so stock profiles, indicators, and risk guard are initialized
        if not engine.is_booted:
            await engine.boot_premarket()

        # Connect telemetry broadcaster to this engine
        telemetry_manager.set_engine(engine)

        # Set replay flag so engine routes strategy signals during simulation
        engine.is_replaying = True

        replayer = HistoricalTickReplayer(event_bus=engine.bus, data_dir=effective_data_dir)
        logger.info(f"[API CONTROL] Starting replay for {len(replay_symbols)} instruments on {target_date or 'latest'} at {req.speed}...")

        async def _on_replay_progress(replayed_count: int, total_count: int) -> None:
            await telemetry_manager.broadcast_snapshot()

        total_ticks = await replayer.replay_session(
            trading_date=target_date,
            symbols=replay_symbols,
            speed=req.speed,
            progress_callback=_on_replay_progress,
        )

        # Flush any trailing candle buckets to evaluate final bars
        await engine.candle_aggregator.finalize_active_candles()

        # Final snapshot broadcast to ensure all dashboard tabs are fully updated
        await telemetry_manager.broadcast_snapshot()

        return {
            "status": "completed",
            "trading_date": target_date.isoformat() if target_date else "latest",
            "speed": req.speed,
            "ticks_replayed": total_ticks,
            "funnel_metrics": engine.funnel_metrics,
            "open_positions_count": len(engine.order_manager.open_positions),
            "timestamp_ist": datetime.now(IST_TZ).isoformat(),
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"[REPLAY FAILURE] Unhandled error during replay: {exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Replay execution failed: {str(exc)}",
        )
    finally:
        engine.is_replaying = False
