"""
Trillionaire API Bridge - Engine Control & Telemetry REST Endpoints
Provides HTTP endpoints for operator dashboard telemetry, diagnostics, and emergency controls:
1. GET /status: Real-time MTM, capital tier, circuit breaker flags, and engine mode.
2. GET /positions & GET /orders: Active positions and session order history.
3. GET /profiles & GET /sentinel: Stock profiles merged with Sentinel risk tags.
4. POST /control/kill-switch: Emergency portfolio liquidation and entry lockout (Admin Protected).
5. POST /control/replay: Offline historical session simulation replay (Admin Protected).
"""

from datetime import date, datetime
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, Field
from loguru import logger

from config.settings import settings
from shared.constants import IST_TZ
from shared.enums import TradingMode
from trading_engine.core.engine import TrillionaireEngine
from trading_engine.data.tick_replayer import HistoricalTickReplayer

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


@router.post("/control/replay", dependencies=[Depends(verify_admin_api_key)])
async def start_historical_replay(
    req: ReplayRequest,
    engine: TrillionaireEngine = Depends(get_engine),
) -> Dict[str, Any]:
    """
    Starts an offline historical session replay onto EventBus for dashboard visual validation.
    Requires X-API-Key header.
    """
    target_date: Optional[date] = None
    if req.trading_date:
        try:
            target_date = date.fromisoformat(req.trading_date)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid trading_date format, expected YYYY-MM-DD")

    replayer = HistoricalTickReplayer(event_bus=engine.bus)
    logger.info(f"[API CONTROL] Starting replay on {target_date or 'latest'} at {req.speed}...")

    total_ticks = await replayer.replay_session(
        trading_date=target_date,
        symbols=req.symbols,
        speed=req.speed,
    )

    return {
        "status": "completed",
        "trading_date": target_date.isoformat() if target_date else "latest",
        "speed": req.speed,
        "ticks_replayed": total_ticks,
        "timestamp_ist": datetime.now(IST_TZ).isoformat(),
    }
