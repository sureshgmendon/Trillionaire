import os
"""
Trillionaire API Bridge - Settings & Engine Lifecycle Control REST Endpoints
Provides administrative control over runtime configuration, credentials, capital tiers,
and engine startup/shutdown directly from the Next.js Cockpit.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
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
    data_feeder: Optional[DataFeederType] = Field(default=None, description="FREE_LIVE or ZERODHA_KITE")
    active_capital_tier: Optional[str] = Field(default=None, description="Tier key in capital_tiers.yaml")


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
        "available_capital_tiers": available_tiers,
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
            else:
                updates[field_name] = value

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

        # 4. Update trading mode on engine if updated
        if "trading_mode" in updates:
            new_mode = TradingMode(updates["trading_mode"])
            engine.set_trading_mode(new_mode)

        # 5. Update data feeder on engine if updated
        if "data_feeder" in updates:
            new_feeder = DataFeederType(updates["data_feeder"])
            engine.set_data_feeder(new_feeder)

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
    Boots pre-market routine (syncs instruments, warms indicators, Sentinel triage)
    and starts real-time streaming operations.
    """
    logger.info(f"[API LIFECYCLE] Engine START requested via REST API (boot_premarket={boot_premarket})")

    if boot_premarket:
        await engine.boot_premarket()

    if not engine.is_running:
        await engine.start()

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
