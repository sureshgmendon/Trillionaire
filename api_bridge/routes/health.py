"""
Trillionaire API Bridge - Health Check Router
System health, engine uptime, and session status diagnostics.
"""

from datetime import datetime
from fastapi import APIRouter
from config.settings import settings
from shared.constants import APP_NAME, APP_VERSION, IST_TZ

router = APIRouter(prefix="/health", tags=["Health"])


@router.get("")
async def get_system_health():
    now_ist = datetime.now(IST_TZ)
    return {
        "status": "HEALTHY",
        "app_name": APP_NAME,
        "version": APP_VERSION,
        "environment": settings.environment,
        "trading_mode": settings.trading_mode.value,
        "active_capital_tier": settings.active_capital_tier,
        "current_time_ist": now_ist.isoformat(),
        "is_entry_allowed": settings.is_entry_allowed(now_ist),
        "is_force_square_off_due": settings.is_force_square_off_due(now_ist),
    }
