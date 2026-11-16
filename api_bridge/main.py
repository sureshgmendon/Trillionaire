"""
Trillionaire API Bridge - FastAPI Main Application Gateway
Coordinates REST telemetry, operator control commands, and live WebSocket broadcasting:
1. Lifespan: Boots TrillionaireEngine, wires TelemetryManager, and manages graceful shutdown.
2. CORS: Configured for local Next.js Cockpit (http://localhost:3000).
3. Routing: Exposes /api/v1/status, /positions, /orders, /control/kill-switch, and /ws/stream.
"""

from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from api_bridge.routes.engine_control import router as control_router, set_engine
from api_bridge.routes.health import router as health_router
from api_bridge.ws.telemetry_manager import telemetry_manager
from config.settings import settings
from shared.constants import APP_NAME, APP_VERSION
from trading_engine.core.engine import TrillionaireEngine

# Engine singleton for API bridge
_engine_instance: TrillionaireEngine | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI Lifespan: Initializes TrillionaireEngine and sets up WebSocket telemetry."""
    global _engine_instance
    logger.info("Initializing Trillionaire API Bridge Lifespan...")

    from api_bridge.routes.engine_control import _global_engine
    if _global_engine is not None:
        _engine_instance = _global_engine
    else:
        _engine_instance = TrillionaireEngine(event_bus=telemetry_manager.bus)
        set_engine(_engine_instance)

    telemetry_manager.set_engine(_engine_instance)

    # Boot engine premarket routine if not already booted
    if not _engine_instance.is_booted:
        try:
            await _engine_instance.boot_premarket()
            await _engine_instance.start()
        except Exception as e:
            logger.warning(f"Engine auto-start encountered issue during boot (safe in test/dev): {e}")

    yield

    # Graceful shutdown
    logger.info("Shutting down Trillionaire API Bridge Lifespan...")
    if _engine_instance and _engine_instance.is_running:
        await _engine_instance.stop()


app = FastAPI(
    title=APP_NAME,
    version=APP_VERSION,
    description="Asynchronous Telemetry & Operator Control Gateway for Trillionaire Algo Engine",
    lifespan=lifespan,
)

# CORS configuration for Next.js 14 Dashboard
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:8000",
        "*",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount REST Routers
app.include_router(health_router, prefix="/api/v1")
app.include_router(control_router, prefix="/api/v1")


@app.websocket("/ws/stream")
async def websocket_stream_endpoint(websocket: WebSocket):
    """
    Real-Time WebSocket stream endpoint.
    Emits initial full-session SNAPSHOT packet upon connection, then streams
    live TICK, CANDLE, SIGNAL, ORDER, ANOMALY, and RISK packets.
    """
    await telemetry_manager.connect(websocket)
    try:
        while True:
            # Keep alive and receive any client pings / messages
            _ = await websocket.receive_text()
    except WebSocketDisconnect:
        telemetry_manager.disconnect(websocket)
    except Exception as e:
        logger.debug(f"WebSocket client loop exited: {e}")
        telemetry_manager.disconnect(websocket)


@app.get("/")
async def root():
    """Root metadata discovery endpoint."""
    return {
        "engine": APP_NAME,
        "version": APP_VERSION,
        "mode": settings.trading_mode.value,
        "active_tier": settings.active_capital_tier,
        "docs_url": "/docs",
        "ws_url": "/ws/stream",
    }
