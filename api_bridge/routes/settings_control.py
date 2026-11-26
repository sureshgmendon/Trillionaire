import asyncio
import os
import signal
"""
Trillionaire API Bridge - Settings & Engine Lifecycle Control REST Endpoints
Provides administrative control over runtime configuration, credentials, capital tiers,
and engine startup/shutdown directly from the Next.js Cockpit.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from loguru import logger

from api_bridge.routes.engine_control import get_engine, verify_admin_api_key
from config.settings import persist_env_updates, reload_settings_from_env, settings
from shared.constants import IST_TZ
from shared.enums import DataFeederType, TradingMode
from trading_engine.core.engine import TrillionaireEngine



def _load_env_map() -> dict:

    env_map = {}

    for path in ["/opt/trillionaire/.env", ".env"]:

        try:

            with open(path, encoding="utf-8") as f:

                for ln in f:

                    ln = ln.strip()

                    if ln and not ln.startswith("#") and "=" in ln:

                        k, v = ln.split("=", 1)

                        env_map[k.strip()] = v.strip().strip('"').strip("'")

            if env_map:

                break

        except Exception:

            pass

    return env_map


router = APIRouter(prefix="", tags=["Settings & Lifecycle Control"])


def mask_secret(val: Optional[str], visible_chars: int = 4) -> str:
    """Masks sensitive API secrets for secure telemetry inspection."""
    if not val or val.startswith("your_") or len(val.strip()) == 0:
        return ""
    clean_val = val.strip()
    if len(clean_val) <= visible_chars:
        return "*" * len(clean_val)
    return clean_val[:visible_chars] + "****"


def is_credential_configured(val: Optional[str]) -> bool:
    """Determines if a credential string is non-empty and non-placeholder."""
    if not val:
        return False
    clean = val.strip()
    return len(clean) > 0 and not clean.startswith("your_")


# -----------------------------------------------------------------------------
# Request & Response Schemas
# -----------------------------------------------------------------------------

class UpdateSettingsRequest(BaseModel):
    """Payload schema for updating runtime settings and broker credentials."""
    kite_api_key: Optional[str] = Field(default=None, description="Zerodha Kite Connect API Key")
    kite_api_secret: Optional[str] = Field(default=None, description="Zerodha Kite Connect API Secret")
    kite_user_id: Optional[str] = Field(default=None, description="Zerodha Client Login User ID")
    kite_password: Optional[str] = Field(default=None, description="Zerodha Client Password")
    kite_totp_secret: Optional[str] = Field(default=None, description="Base32 TOTP Secret Key")
    gemini_api_key: Optional[str] = Field(default=None, description="Google Gemini API Key for Sentinel")
    telegram_bot_token: Optional[str] = Field(default=None, description="Telegram Bot API Token")
    telegram_chat_id: Optional[str] = Field(default=None, description="Telegram Chat ID")
    trading_mode: Optional[TradingMode] = Field(default=None, description="REPLAY, PAPER, or LIVE")
    data_feeder: Optional[DataFeederType] = Field(default=DataFeederType.ZERODHA_KITE, description="ZERODHA_KITE")
    active_capital_tier: Optional[str] = Field(default=None, description="Tier key in capital_tiers.yaml")
    allocated_capital: Optional[float] = Field(default=None, gt=0, description="Custom paper/live allocated capital override")
    risk_per_trade_pct: Optional[float] = Field(default=None, gt=0, le=10.0, description="Risk per trade percentage override")
    enable_trend_strategy: Optional[bool] = Field(default=None, description="Enable TREND_MOMENTUM strategy")
    enable_mean_reversion_strategy: Optional[bool] = Field(default=None, description="Enable MEAN_REVERSION strategy")
    enable_rvol_breakout_strategy: Optional[bool] = Field(default=None, description="Enable HIGH_RVOL_BREAKOUT strategy")
    enable_profit_trade_manager: Optional[bool] = Field(default=None, description="Enable Profit-Only Active Trade Manager")
    enable_partial_booking: Optional[bool] = Field(default=None, description="Enable 50% Partial Profit Booking at +1R")
    enable_lunch_volume_filter: Optional[bool] = Field(default=None, description="Require RVOL >= 2.0 between 11:45 and 13:15 IST")
    enable_vision_filter: Optional[bool] = Field(default=None, description="Enable Gemini Vision chart trap filter")
    entry_start_time_ist: Optional[str] = Field(default=None, description="Opening bell entry start time in IST ('09:25' or '09:30')")


class PushTokenRequest(BaseModel):
    """Payload schema for pushing a daily Kite access token or request token to the engine."""
    access_token: Optional[str] = Field(default=None, description="Zerodha Kite Connect access token")
    request_token: Optional[str] = Field(default=None, description="Zerodha Kite Connect request token to exchange")


# -----------------------------------------------------------------------------
# Settings Inspection & Mutation Endpoints
# -----------------------------------------------------------------------------

@router.get("/settings", dependencies=[Depends(verify_admin_api_key)])
async def get_runtime_settings(engine: TrillionaireEngine = Depends(get_engine)) -> Dict[str, Any]:
    """
    Returns current runtime configuration with all sensitive credentials masked.
    Protected by X-API-Key matching TRILLIONAIRE_ADMIN_KEY.
    """
    available_tiers = list(settings.load_capital_tiers().keys())

    env_map = _load_env_map()

    def _real(v: str) -> str:

        v = (v or "").strip()

        return "" if not v or v.startswith("your_") else v

    kite_key = _real(settings.kite_api_key) or _real(os.environ.get("KITE_API_KEY")) or _real(env_map.get("KITE_API_KEY"))

    kite_uid = _real(settings.kite_user_id) or _real(os.environ.get("KITE_USER_ID")) or _real(env_map.get("KITE_USER_ID"))

    totp_sec = _real(settings.kite_totp_secret) or _real(os.environ.get("KITE_TOTP_SECRET")) or _real(env_map.get("KITE_TOTP_SECRET"))

    
    return {
        "has_kite_key": is_credential_configured(settings.kite_api_key),
        "kite_api_key_masked": mask_secret(settings.kite_api_key),
        "kite_user_id": settings.kite_user_id if is_credential_configured(settings.kite_user_id) else "",
        "has_kite_secret": is_credential_configured(settings.kite_api_secret),
        "has_kite_password": is_credential_configured(settings.kite_password),
        "has_totp_secret": is_credential_configured(settings.kite_totp_secret),
        "has_gemini_key": is_credential_configured(settings.gemini_api_key),
        "has_telegram": is_credential_configured(settings.telegram_bot_token) and is_credential_configured(settings.telegram_chat_id),
        "trading_mode": engine.trading_mode.value,
        "data_feeder": engine.data_feeder.value,
        "active_capital_tier": engine.risk_gatekeeper.tier_name,
        "allocated_capital": engine.risk_gatekeeper.allocated_capital,
        "risk_per_trade_pct": engine.risk_gatekeeper.risk_per_trade_pct,
        "token_expired": bool(getattr(engine.broker, "token_expired", False)),
        "available_capital_tiers": available_tiers,
        "enable_trend_strategy": bool(getattr(settings, "enable_trend_strategy", True)),
        "enable_mean_reversion_strategy": bool(getattr(settings, "enable_mean_reversion_strategy", True)),
        "enable_rvol_breakout_strategy": bool(getattr(settings, "enable_rvol_breakout_strategy", True)),
        "enable_profit_trade_manager": bool(getattr(settings, "enable_profit_trade_manager", True)),
        "enable_partial_booking": bool(getattr(settings, "enable_partial_booking", True)),
        "enable_lunch_volume_filter": bool(getattr(settings, "enable_lunch_volume_filter", True)),
        "enable_vision_filter": bool(getattr(settings, "enable_vision_filter", True)),
        "entry_start_time_ist": str(getattr(settings, "entry_start_time_ist", "09:25")),
        "engine_running": engine.is_running,
        "engine_booted": engine.is_booted,
        "timestamp_ist": datetime.now(IST_TZ).isoformat(),
    }


@router.post("/settings", dependencies=[Depends(verify_admin_api_key)])
async def update_runtime_settings(
    req: UpdateSettingsRequest,
    engine: TrillionaireEngine = Depends(get_engine),
) -> Dict[str, Any]:
    """
    Persists updated fields safely into the root .env file, reloads settings in-memory,
    and updates active capital tier and trading mode on the engine.
    Blank/omitted fields do not overwrite existing credentials.
    """
    updates: Dict[str, Any] = {}

    # Map request fields, ignoring None or empty strings (so blank inputs don't overwrite saved secrets)
    for field_name, value in req.model_dump(exclude_unset=True).items():
        if value is not None:
            if isinstance(value, str):
                cleaned = value.strip()
                if len(cleaned) > 0:
                    updates[field_name] = cleaned
            elif isinstance(value, TradingMode):
                updates[field_name] = value.value
            elif isinstance(value, DataFeederType):
                updates[field_name] = value.value
            elif isinstance(value, bool):
                updates[field_name] = "true" if value else "false"
            else:
                updates[field_name] = value

    if "entry_start_time_ist" in updates:
        updates["no_entry_before"] = updates["entry_start_time_ist"]

    if updates:
        logger.info(f"[SETTINGS UPDATE] Persisting {len(updates)} updated fields to .env: {list(updates.keys())}")
        # 1. Persist to .env
        persist_env_updates(updates)

        # 2. Reload in-memory settings
        reload_settings_from_env()

        # 3. Update active capital tier on risk gatekeeper if updated
        if "active_capital_tier" in updates:
            tier_name = updates["active_capital_tier"]
            engine.risk_gatekeeper.set_capital_tier(tier_name)

        # 3b. Apply custom paper capital / risk per trade overrides after tier load
        if "allocated_capital" in updates:
            engine.risk_gatekeeper.allocated_capital = float(updates["allocated_capital"])
        if "risk_per_trade_pct" in updates:
            engine.risk_gatekeeper.risk_per_trade_pct = float(updates["risk_per_trade_pct"])

        # 4. Update trading mode on engine if updated
        if "trading_mode" in updates:
            new_mode = TradingMode(updates["trading_mode"])
            engine.set_trading_mode(new_mode)

        # 5. Update data feeder on engine if updated
        if "data_feeder" in updates:
            new_feeder = DataFeederType(updates["data_feeder"])
            engine.set_data_feeder(new_feeder)

        # 6. Persist daily state snapshot with updated capital & risk settings
        if hasattr(engine, "persist_daily_state"):
            engine.persist_daily_state()

    # Return refreshed settings
    return {
        "status": "saved",
        "message": f"Successfully updated {len(updates)} configuration parameters",
        "updated_keys": list(updates.keys()),
        "settings": await get_runtime_settings(engine=engine),
    }


# -----------------------------------------------------------------------------
# Engine Lifecycle Control Endpoints
# -----------------------------------------------------------------------------

@router.post("/control/engine/start", dependencies=[Depends(verify_admin_api_key)])
async def start_trading_engine(
    boot_premarket: bool = Query(True, description="Whether to execute pre-market boot if not yet booted"),
    engine: TrillionaireEngine = Depends(get_engine),
) -> Dict[str, Any]:
    """
    Stops any stale sessions, resets daily risk gatekeeper state / circuit breakers,
    boots pre-market routine (syncs instruments, warms indicators, Sentinel triage),
    starts real-time streaming operations, and broadcasts fresh telemetry snapshot.
    """
    logger.info(f"[API LIFECYCLE] Engine START requested via REST API (boot_premarket={boot_premarket})")

    # 1. Halt stale or existing session if running
    if engine.is_running:
        logger.info("[API LIFECYCLE] Halting existing engine session prior to restart.")
        await engine.stop()

    # 2. Reset daily risk gatekeeper state and clear kill-switch
    today = datetime.now(IST_TZ).date()
    if hasattr(engine, "risk_gatekeeper") and engine.risk_gatekeeper is not None:
        engine.risk_gatekeeper.reset_daily_state(trade_date=today)

    # 3. Boot premarket sequence
    if boot_premarket:
        try:
            await engine.boot_premarket(trade_date=today)
        except Exception as e:
            logger.warning(f"[API LIFECYCLE] Premarket boot encountered error: {e}. Continuing with engine start.")

    # 4. Start engine streaming
    if not engine.is_running:
        await engine.start()

    # 5. Broadcast fresh snapshot to all UI clients
    from api_bridge.ws.telemetry_manager import telemetry_manager
    try:
        await telemetry_manager.broadcast_snapshot()
    except Exception as e:
        logger.debug(f"[API LIFECYCLE] Snapshot broadcast error: {e}")

    return {
        "status": "started",
        "is_running": engine.is_running,
        "is_booted": engine.is_booted,
        "trading_mode": engine.trading_mode.value,
        "capital_tier": engine.risk_gatekeeper.tier_name,
        "allocated_capital": engine.risk_gatekeeper.allocated_capital,
        "monitored_stocks": len(engine.strategy_runner.profiles),
        "timestamp_ist": datetime.now(IST_TZ).isoformat(),
    }


@router.post("/control/engine/pause", dependencies=[Depends(verify_admin_api_key)])
async def pause_trading_engine(
    engine: TrillionaireEngine = Depends(get_engine),
) -> Dict[str, Any]:
    """
    Safely pauses strategy entry signal evaluations and order placement,
    while KiteTicker feed remains connected and streaming live ticks.
    """
    logger.info("[API CONTROL] Engine PAUSE requested via REST API")
    res = engine.pause_trading()
    from api_bridge.ws.telemetry_manager import telemetry_manager
    try:
        await telemetry_manager.broadcast_snapshot()
    except Exception as e:
        logger.debug(f"[API CONTROL] Snapshot broadcast error: {e}")
    return {
        **res,
        "is_running": engine.is_running,
        "timestamp_ist": datetime.now(IST_TZ).isoformat(),
    }


@router.post("/control/engine/resume", dependencies=[Depends(verify_admin_api_key)])
async def resume_trading_engine(
    engine: TrillionaireEngine = Depends(get_engine),
) -> Dict[str, Any]:
    """
    Resumes strategy entry signal evaluations and order placement.
    """
    logger.info("[API CONTROL] Engine RESUME requested via REST API")
    res = engine.resume_trading()
    from api_bridge.ws.telemetry_manager import telemetry_manager
    try:
        await telemetry_manager.broadcast_snapshot()
    except Exception as e:
        logger.debug(f"[API CONTROL] Snapshot broadcast error: {e}")
    return {
        **res,
        "is_running": engine.is_running,
        "timestamp_ist": datetime.now(IST_TZ).isoformat(),
    }


@router.post("/control/engine/stop", dependencies=[Depends(verify_admin_api_key)])
async def stop_trading_engine(
    engine: TrillionaireEngine = Depends(get_engine),
) -> Dict[str, Any]:
    """
    Gracefully halts trading operations, halts ticker feeds, and flattens any open positions.
    """
    logger.warning("[API LIFECYCLE] Engine STOP requested via REST API")

    if engine.is_running:
        await engine.stop()

    return {
        "status": "stopped",
        "is_running": engine.is_running,
        "is_booted": engine.is_booted,
        "trading_mode": engine.trading_mode.value,
        "timestamp_ist": datetime.now(IST_TZ).isoformat(),
    }


# -----------------------------------------------------------------------------
# 1-Click Zerodha Kite Connect Browser OAuth Login & Callback
# -----------------------------------------------------------------------------

@router.get("/kite/login")
async def kite_oauth_login() -> RedirectResponse:
    """
    Redirects user's browser to official Zerodha Kite Connect login screen.
    Uses KITE_API_KEY from configuration.
    """
    api_key = settings.kite_api_key
    if not api_key or api_key.startswith("your_") or len(api_key.strip()) == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="KITE_API_KEY is not configured. Please set your Kite Connect API key in Settings.",
        )
    kite_login_url = f"https://kite.zerodha.com/connect/login?v=3&api_key={api_key.strip()}"
    logger.info(f"[KITE OAUTH] Initiating 1-click browser login redirect -> {kite_login_url}")
    return RedirectResponse(url=kite_login_url, status_code=status.HTTP_302_FOUND)


async def _delayed_worker_sigterm(delay_sec: float = 1.0) -> None:
    """Waits briefly so the HTTP response flushes before triggering Docker restart via SIGTERM."""
    await asyncio.sleep(delay_sec)
    logger.warning("[WORKER RESPAWN] Sending SIGTERM so container restarts with fresh KiteTicker reactor.")
    os.kill(os.getpid(), signal.SIGTERM)


def _schedule_worker_respawn(delay_sec: float = 1.0) -> bool:
    """Schedules graceful SIGTERM worker respawn on the active asyncio event loop."""
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_delayed_worker_sigterm(delay_sec=delay_sec))
        return True
    except RuntimeError:
        return False


async def _handle_kite_oauth_callback(
    request_token: Optional[str],
    status_param: Optional[str],
    action: Optional[str],
    engine: TrillionaireEngine,
    response_format: Optional[str] = None,
) -> Any:
    """
    Exchanges request_token for Kite Connect access_token, persists to Redis & disk,
    switches data_feeder to ZERODHA_KITE, restarts engine if running, and redirects to dashboard.
    """
    logger.info(f"[KITE OAUTH CALLBACK] Received: status={status_param}, action={action}, has_token={bool(request_token)}")

    if status_param == "cancelled" or not request_token:
        logger.warning(f"[KITE OAUTH CALLBACK] Login cancelled or missing request_token (status={status_param})")
        return RedirectResponse(url="/?error=kite_login_cancelled", status_code=status.HTTP_302_FOUND)

    try:
        from trading_engine.brokers.kite.auth import KiteAuthManager

        auth_mgr = engine.auth_manager
        if auth_mgr is None:
            auth_mgr = KiteAuthManager()
            engine.auth_manager = auth_mgr

        today = datetime.now(IST_TZ).date()
        access_token = auth_mgr.exchange_and_save_request_token(request_token=request_token, today_date=today)
        logger.info(f"[KITE OAUTH CALLBACK] Successfully exchanged and saved access_token for {today}")

        # Persist DATA_FEEDER=ZERODHA_KITE into .env
        persist_env_updates({"DATA_FEEDER": DataFeederType.ZERODHA_KITE.value})
        reload_settings_from_env()

        # Update engine data feeder
        engine.set_data_feeder(DataFeederType.ZERODHA_KITE)

        kite_client = auth_mgr.get_kite_client()

        # Re-initialize instrument sync if needed
        if engine.instrument_sync is not None:
            try:
                engine.instrument_sync.kite = kite_client
                engine.instrument_sync.sync_instruments()
            except Exception as e:
                logger.warning(f"[KITE OAUTH CALLBACK] Instrument sync kite client assignment: {e}")

        # Resolve all 100 universe stocks + 8 parent sector indices tokens
        resolved_tokens = engine._resolve_subscribed_tokens()

        # Reconnect KiteTicker cleanly in-place without stopping the engine or flattening positions
        await engine.reconnect_kite_ticker(
            kite_client=kite_client,
            access_token=access_token,
            resolved_tokens=resolved_tokens,
        )

        # Broadcast telemetry snapshot to all UI clients
        from api_bridge.ws.telemetry_manager import telemetry_manager
        try:
            await telemetry_manager.broadcast_snapshot()
        except Exception:
            pass

        respawn_scheduled = _schedule_worker_respawn(delay_sec=1.0)
        if response_format == "json":
            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content={
                    "status": "ok",
                    "message": "Token saved; respawning engine worker for fresh KiteTicker reactor",
                    "respawn_scheduled": respawn_scheduled,
                },
            )
        return RedirectResponse(url="/?kite_auth=success", status_code=status.HTTP_302_FOUND)

    except Exception as e:
        logger.error(f"[KITE OAUTH CALLBACK ERROR] Token exchange failed: {e}")
        return RedirectResponse(url=f"/?error={str(e)}", status_code=status.HTTP_302_FOUND)


@router.get("/callback")
async def generic_oauth_callback(
    request_token: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    action: Optional[str] = Query(None),
    format: Optional[str] = Query(None),
    engine: TrillionaireEngine = Depends(get_engine),
) -> Any:
    """Zerodha Kite Connect redirect callback (/api/v1/callback)."""
    return await _handle_kite_oauth_callback(request_token, status, action, engine, response_format=format)


@router.get("/kite/callback")
async def kite_oauth_callback(
    request_token: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    action: Optional[str] = Query(None),
    format: Optional[str] = Query(None),
    engine: TrillionaireEngine = Depends(get_engine),
) -> Any:
    """Zerodha Kite Connect redirect callback (/api/v1/kite/callback)."""
    return await _handle_kite_oauth_callback(request_token, status, action, engine, response_format=format)


@router.get("/auth/callback")
async def auth_oauth_callback(
    request_token: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    action: Optional[str] = Query(None),
    format: Optional[str] = Query(None),
    engine: TrillionaireEngine = Depends(get_engine),
) -> Any:
    """Alternative Zerodha Kite Connect redirect callback (/api/v1/auth/callback)."""
    return await _handle_kite_oauth_callback(request_token, status, action, engine, response_format=format)


@router.post("/kite/push-token", dependencies=[Depends(verify_admin_api_key)])
async def push_kite_token(
    payload: PushTokenRequest,
    engine: TrillionaireEngine = Depends(get_engine),
) -> Dict[str, Any]:
    """
    Accepts daily Kite access_token or request_token from external automated runner (e.g. residential PC),
    caches in Redis and disk vault, initializes KiteTicker, subscribes universe tokens, and starts live streaming.
    Protected by X-API-Key matching TRILLIONAIRE_ADMIN_KEY.
    """
    if not payload.access_token and not payload.request_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Either access_token or request_token must be provided.",
        )

    from trading_engine.brokers.kite.auth import KiteAuthManager
    auth_mgr = engine.auth_manager
    if auth_mgr is None:
        auth_mgr = KiteAuthManager()
        engine.auth_manager = auth_mgr

    today = datetime.now(IST_TZ).date()

    if payload.request_token:
        try:
            access_token = auth_mgr.exchange_and_save_request_token(
                request_token=payload.request_token.strip(),
                today_date=today,
            )
            logger.info(f"[PUSH TOKEN] Successfully exchanged request_token for access_token on {today}")
        except Exception as e:
            logger.error(f"[PUSH TOKEN ERROR] Token exchange failed: {e}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Failed to exchange request_token: {str(e)}",
            )
    else:
        access_token = payload.access_token.strip()
        auth_mgr.save_access_token(access_token=access_token, today_date=today)
        logger.info(f"[PUSH TOKEN] Successfully saved external access_token for {today}")

    # Persist in-memory & .env
    settings.kite_access_token = access_token
    persist_env_updates({"KITE_ACCESS_TOKEN": access_token, "DATA_FEEDER": DataFeederType.ZERODHA_KITE.value})
    reload_settings_from_env()

    # Update engine data feeder
    engine.set_data_feeder(DataFeederType.ZERODHA_KITE)

    kite_client = auth_mgr.get_kite_client()

    # Sync instruments if needed
    if engine.instrument_sync is not None:
        try:
            engine.instrument_sync.kite = kite_client
            engine.instrument_sync.sync_instruments()
        except Exception as e:
            logger.warning(f"[PUSH TOKEN] Instrument sync kite client assignment: {e}")

    # Resolve all 100 universe stocks + 8 parent sector indices tokens & reconnect KiteTicker immediately
    resolved_tokens = engine._resolve_subscribed_tokens()
    await engine.reconnect_kite_ticker(
        kite_client=kite_client,
        access_token=access_token,
        resolved_tokens=resolved_tokens,
    )

    # Broadcast telemetry snapshot
    from api_bridge.ws.telemetry_manager import telemetry_manager
    try:
        await telemetry_manager.broadcast_snapshot()
    except Exception as e:
        logger.debug(f"[PUSH TOKEN] Snapshot broadcast: {e}")

    respawn_scheduled = _schedule_worker_respawn(delay_sec=1.0)

    return {
        "status": "ok",
        "message": "Kite token activated — token saved; respawning engine worker for fresh KiteTicker reactor",
        "respawn_scheduled": respawn_scheduled,
        "active_tokens": len(resolved_tokens),
        "engine_running": engine.is_running,
        "trading_mode": engine.trading_mode.value,
        "timestamp_ist": datetime.now(IST_TZ).isoformat(),
    }


@router.post("/auth/kite-token", dependencies=[Depends(verify_admin_api_key)])
async def push_kite_token_alias(
    payload: PushTokenRequest,
    engine: TrillionaireEngine = Depends(get_engine),
) -> Dict[str, Any]:
    """Alias for /api/v1/kite/push-token accepting fresh daily Kite token pushes."""
    return await push_kite_token(payload=payload, engine=engine)

