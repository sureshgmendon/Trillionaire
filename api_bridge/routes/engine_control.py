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
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from loguru import logger

from config.settings import settings
from api_bridge.ws.telemetry_manager import telemetry_manager
from research_lab.data_loader.synthetic_generator import DATA_STORE_DIR, FALLBACK_DATA_STORE_DIR, SyntheticDataGenerator
from shared.constants import DEFAULT_REPLAY_BASKET, IST_TZ
from shared.enums import Timeframe, TradingMode
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


class ModifyOrderRequest(BaseModel):
    trigger_price: Optional[float] = Field(default=None, gt=0, description="Updated SL trigger price")
    limit_price: Optional[float] = Field(default=None, gt=0, description="Updated limit price")


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
        "trading_paused": st.get("trading_paused", False),
        "capital_tier": st["capital_tier"],
        "allocated_capital": st["allocated_capital"],
        "paper_balance": st.get("paper_balance", st["allocated_capital"] + st["daily_realized_pnl"]),
        "risk_per_trade_pct": st.get("risk_per_trade_pct", engine.risk_gatekeeper.risk_per_trade_pct),
        "daily_mtm": st["daily_total_mtm"],
        "realized_pnl": st["daily_realized_pnl"],
        "unrealized_pnl": round(unrealized, 2),
        "circuit_breaker_tripped": st["kill_switch_active"],
        "profit_floor_armed": st["profit_lock_armed"],
        "token_expired": st.get("token_expired", False),
        "ticker_reconnecting": st.get("ticker_reconnecting", False),
        "open_positions_count": st["open_positions_count"],
        "closed_trades_count": st.get("closed_trades_count", len(engine.order_manager.closed_trades)),
        "active_stocks_count": len(engine.strategy_runner.profiles),
        "milestones": st.get("milestones", engine.get_lifecycle_milestones()),
        "funnel_metrics": st.get("funnel_metrics", {
            "candles_evaluated": 0,
            "passed_stock_rule": 0,
            "passed_sector_filter": 0,
            "signals_emitted": 0,
        }),
        "timestamp_ist": datetime.now(IST_TZ).isoformat(),
    }


@router.get("/engine/milestones")
async def get_engine_milestones(engine: TrillionaireEngine = Depends(get_engine)) -> Dict[str, Any]:
    """Returns structured 5-milestone pipeline readiness state (kite_auth, universe_warmed, ticker_connected, ticks_flowing, strategy_armed)."""
    return engine.get_lifecycle_milestones()


@router.get("/sentinel/summary")
async def get_sentinel_summary(engine: TrillionaireEngine = Depends(get_engine)) -> Dict[str, Any]:
    """Returns latest financial news headlines, Gemini brief, and tagged high-risk stocks."""
    return await engine.sentinel.get_sentinel_summary_async()


@router.get("/sentinel/autopsy")
async def get_trade_autopsy(
    auto_generate: bool = Query(True, description="Generate on-demand if no report exists yet today"),
    engine: TrillionaireEngine = Depends(get_engine),
) -> Optional[Dict[str, Any]]:
    """Returns cycle-safe latest Trade Autopsy coaching report + newest-first session history."""
    from sentinel.news_triage.trade_autopsy import (
        generate_daily_trade_autopsy,
        load_latest_trade_autopsy,
        load_trade_autopsy_history,
        sanitize_json_safe,
    )

    existing = load_latest_trade_autopsy()
    if existing is not None:
        clean = sanitize_json_safe({k: v for k, v in existing.items() if k != "history"})
        hist = existing.get("history") or load_trade_autopsy_history()
        clean["history"] = [
            sanitize_json_safe({k: v for k, v in h.items() if k != "history"})
            for h in (hist or [])
            if isinstance(h, dict)
        ]
        return sanitize_json_safe(clean)

    if not auto_generate:
        return None

    closed = list(getattr(engine.order_manager, "closed_trades", []))
    st = engine.get_status()
    report = await generate_daily_trade_autopsy(
        closed_trades=closed,
        funnel_metrics=st.get("funnel_metrics"),
        daily_mtm=float(st.get("daily_total_mtm", 0.0)),
        realized_pnl=float(st.get("daily_realized_pnl", 0.0)),
        allocated_capital=float(st.get("allocated_capital", 100000.0)),
        send_telegram=False,
    )
    return sanitize_json_safe(report)


@router.post("/sentinel/autopsy/run", dependencies=[Depends(verify_admin_api_key)])
async def run_trade_autopsy_now(engine: TrillionaireEngine = Depends(get_engine)) -> Dict[str, Any]:
    """Triggers immediate generation & persistence of today's Trade Autopsy coaching report + WS broadcast."""
    from sentinel.news_triage.trade_autopsy import generate_daily_trade_autopsy, sanitize_json_safe
    from trading_engine.core.event_bus import AutopsyUpdatedEvent

    closed = list(getattr(engine.order_manager, "closed_trades", []))
    st = engine.get_status()
    report = await generate_daily_trade_autopsy(
        closed_trades=closed,
        funnel_metrics=st.get("funnel_metrics"),
        daily_mtm=float(st.get("daily_total_mtm", 0.0)),
        realized_pnl=float(st.get("daily_realized_pnl", 0.0)),
        allocated_capital=float(st.get("allocated_capital", 100000.0)),
        send_telegram=True,
    )
    safe_report = sanitize_json_safe(report)
    if hasattr(engine, "bus") and engine.bus is not None:
        try:
            await engine.bus.publish(AutopsyUpdatedEvent(report=safe_report))
        except Exception as exc:
            logger.debug(f"[AUTOPSY WS BROADCAST] Skipped: {exc}")
    return safe_report


@router.get("/funnel/logs")
async def get_funnel_logs(
    stage: str = Query("ALL", description="Stage filter: ALL, EVAL, STOCK, SECTOR, or SIGNAL"),
    symbol: Optional[str] = Query(None, description="Optional symbol filter"),
    limit: int = Query(500, ge=1, le=2500),
    engine: TrillionaireEngine = Depends(get_engine),
) -> Dict[str, Any]:
    """Returns structured M5 funnel evaluation logs and live counts across ALL/EVAL/STOCK/SECTOR/SIGNAL."""
    raw_logs = list(getattr(engine.strategy_runner, "funnel_logs", []))
    sym_filter = symbol.strip().upper() if symbol and symbol.strip() else None
    if sym_filter:
        raw_logs = [row for row in raw_logs if sym_filter in str(row.get("symbol", "")).upper()]

    counts = {
        "ALL": len(raw_logs),
        "EVAL": sum(1 for row in raw_logs if row.get("stage") == "EVAL"),
        "STOCK": sum(1 for row in raw_logs if row.get("stage") == "STOCK"),
        "SECTOR": sum(1 for row in raw_logs if row.get("stage") == "SECTOR"),
        "SIGNAL": sum(1 for row in raw_logs if row.get("stage") == "SIGNAL"),
    }

    stage_upper = stage.strip().upper() if stage else "ALL"
    filtered = (
        raw_logs
        if stage_upper == "ALL"
        else [row for row in raw_logs if str(row.get("stage") or "").upper() == stage_upper]
    )
    # Return newest-first slice capped to `limit`
    return {
        "counts": counts,
        "logs": list(reversed(filtered))[:limit],
    }


@router.get("/reports/export")
async def export_xlsx_reports(
    preset: str = Query("today", description="today|yesterday|this_week|this_month|quarterly|half_yearly|yearly|five_years|custom"),
    from_date: Optional[str] = Query(None, description="YYYY-MM-DD start date when preset=custom"),
    to_date: Optional[str] = Query(None, description="YYYY-MM-DD end date when preset=custom"),
    report_type: str = Query("evaluation", description="evaluation or pl"),
    engine: TrillionaireEngine = Depends(get_engine),
):
    """Exports multi-tab Excel (.xlsx) report combining historical archives and live session data."""
    from trading_engine.core.xlsx_reports import export_reports_xlsx

    rtype = report_type.strip().lower()
    if rtype not in ("evaluation", "pl"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="report_type must be 'evaluation' or 'pl'",
        )

    try:
        xlsx_bytes, filename, is_live, live_label = export_reports_xlsx(
            preset=preset,
            report_type=rtype,
            engine=engine,
            from_date=from_date,
            to_date=to_date,
        )
    except ValueError as ve:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(ve))

    return StreamingResponse(
        iter([xlsx_bytes]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Report-Is-Live": "true" if is_live else "false",
            "X-Report-Timestamp": live_label,
            "Access-Control-Expose-Headers": "Content-Disposition, X-Report-Is-Live, X-Report-Timestamp",
        },
    )


@router.get("/positions")
async def get_active_positions(engine: TrillionaireEngine = Depends(get_engine)) -> List[Dict[str, Any]]:
    """Returns active intraday positions with entry prices, SL/Target, LTP, and unrealized PnL."""
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


@router.get("/closed-trades")
async def get_closed_trades(engine: TrillionaireEngine = Depends(get_engine)) -> List[Dict[str, Any]]:
    """Returns immutable closed round-trip trades ledger for the current IST session."""
    return [
        trade.model_dump(mode="json")
        for trade in engine.order_manager.closed_trades
    ]


@router.get("/candles/{symbol}")
async def get_symbol_candles(
    symbol: str,
    timeframe: str = Query("M5", description="Candle timeframe: M1, M3, M5, or M15"),
    limit: int = Query(150, ge=10, le=300),
    engine: TrillionaireEngine = Depends(get_engine),
) -> List[Dict[str, Any]]:
    """Returns historical + live intraday candles with VWAP and EMA(200) for chart switching."""
    tf_map = {
        "M1": Timeframe.M1,
        "1M": Timeframe.M1,
        "M3": Timeframe.M3,
        "3M": Timeframe.M3,
        "M5": Timeframe.M5,
        "5M": Timeframe.M5,
        "M15": Timeframe.M15,
        "15M": Timeframe.M15,
    }
    tf = tf_map.get(timeframe.upper(), Timeframe.M5)
    kite_client = getattr(engine.broker, "kite", None)
    bars = engine.candle_aggregator.get_candles_for_symbol(
        symbol=symbol.upper(),
        timeframe=tf,
        limit=limit,
        kite_client=kite_client,
        instrument_sync=engine.instrument_sync,
    )
    return [b.model_dump(mode="json") for b in bars]


@router.get("/stream")
async def sse_telemetry_stream(engine: TrillionaireEngine = Depends(get_engine)):
    """Server-Sent Events fallback stream with buffering disabled."""
    import json as _json

    telemetry_manager.set_engine(engine)

    async def _event_gen():
        while True:
            snap = telemetry_manager.build_snapshot()
            yield f"data: {_json.dumps(snap)}\n\n"
            await asyncio.sleep(1.0)

    return StreamingResponse(
        _event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/sentinel/{symbol}/override", dependencies=[Depends(verify_admin_api_key)])
async def override_sentinel_symbol(
    symbol: str,
    reason: str = Query("Manual operator override: Sentinel lock cleared", description="Override reason"),
    engine: TrillionaireEngine = Depends(get_engine),
) -> Dict[str, Any]:
    """Manually unlocks a Sentinel DO_NOT_TRADE / LOCK badge for the current session."""
    clean_sym = symbol.upper()
    tag = engine.sentinel.override_symbol_lock(clean_sym, reason=reason)
    try:
        await telemetry_manager.broadcast_snapshot()
    except Exception:
        pass
    return {
        "status": "unlocked",
        "symbol": clean_sym,
        "sentinel": tag.model_dump(mode="json"),
    }


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


@router.post("/control/positions/{symbol}/exit", dependencies=[Depends(verify_admin_api_key)])
async def exit_single_position(
    symbol: str,
    reason: str = Query("UI_MANUAL_EXIT", description="Reason for exiting position"),
    engine: TrillionaireEngine = Depends(get_engine),
) -> Dict[str, Any]:
    """
    Closes a specific open position immediately.
    Requires X-API-Key header.
    """
    clean_sym = symbol.upper()
    if clean_sym not in engine.order_manager.open_positions:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No active open position found for {clean_sym}",
        )
    order = await engine.order_manager.exit_position(clean_sym, reason=reason)
    return {
        "status": "executed",
        "symbol": clean_sym,
        "reason": reason,
        "order": order.model_dump(mode="json") if order else None,
        "timestamp_ist": datetime.now(IST_TZ).isoformat(),
    }


@router.post("/control/orders/{order_id}/cancel", dependencies=[Depends(verify_admin_api_key)])
async def cancel_pending_order(
    order_id: str,
    engine: TrillionaireEngine = Depends(get_engine),
) -> Dict[str, Any]:
    """
    Cancels an active OPEN or TRIGGER PENDING order via attached broker and updates ledger state.
    Requires X-API-Key header.
    """
    from shared.enums import OrderStatus

    order = engine.order_manager.orders.get(order_id)
    if order is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Order {order_id} not found",
        )
    if order.status not in (OrderStatus.OPEN, OrderStatus.TRIGGER_PENDING, OrderStatus.PENDING_NEW):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Order {order_id} cannot be cancelled in status {order.status.value}",
        )

    cancelled = await engine.order_manager.broker.cancel_order(order_id)
    if cancelled is not None:
        order = cancelled
    order.status = OrderStatus.CANCELLED
    engine.order_manager.orders[order_id] = order
    engine.order_manager._persist_order_to_redis(order)
    engine.order_manager._notify_state_change()

    try:
        await telemetry_manager.broadcast_snapshot()
    except Exception:
        pass

    return {
        "status": "cancelled",
        "order_id": order_id,
        "order": order.model_dump(mode="json"),
        "timestamp_ist": datetime.now(IST_TZ).isoformat(),
    }


@router.post("/control/orders/{order_id}/modify", dependencies=[Depends(verify_admin_api_key)])
async def modify_pending_order(
    order_id: str,
    req: ModifyOrderRequest,
    engine: TrillionaireEngine = Depends(get_engine),
) -> Dict[str, Any]:
    """
    Modifies trigger_price / limit_price on an active OPEN or TRIGGER PENDING SL/TP child order
    and keeps the parent open Position bracket in sync.
    Requires X-API-Key header.
    """
    from shared.enums import OrderStatus

    order = engine.order_manager.orders.get(order_id)
    if order is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Order {order_id} not found",
        )
    if order.status not in (OrderStatus.OPEN, OrderStatus.TRIGGER_PENDING, OrderStatus.PENDING_NEW):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Order {order_id} cannot be modified in status {order.status.value}",
        )

    modified = await engine.order_manager.modify_order(
        order_id=order_id,
        trigger_price=req.trigger_price,
        limit_price=req.limit_price,
    )
    try:
        await telemetry_manager.broadcast_snapshot()
    except Exception:
        pass

    return {
        "status": "modified",
        "order_id": order_id,
        "order": modified.model_dump(mode="json") if modified else None,
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
