"""
Trillionaire API Bridge - Routes Module
Exports health check and engine control routers.
"""

from api_bridge.routes.engine_control import router as control_router, get_engine, set_engine
from api_bridge.routes.health import router as health_router

__all__ = ["control_router", "health_router", "get_engine", "set_engine"]
