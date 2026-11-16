"""
Trillionaire API Bridge - FastAPI Main Application
ASGI Gateway exposing operator control, risk telemetry, and market state.
"""

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from config.settings import settings
from shared.constants import APP_NAME, APP_VERSION
from api_bridge.routes.health import router as health_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: validate configurations & initialize connection pools
    _ = settings.get_active_capital_tier_config()
    _ = settings.load_sector_map()
    yield
    # Shutdown: clean up broker WebSockets & connection pools


app = FastAPI(
    title=APP_NAME,
    version=APP_VERSION,
    description="Asynchronous Telemetry & Operator Control Gateway for Trillionaire Algo Engine",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health_router, prefix="/api/v1")


@app.get("/")
async def root():
    return {
        "engine": APP_NAME,
        "version": APP_VERSION,
        "mode": settings.trading_mode.value,
        "docs_url": "/docs",
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api_bridge.main:app", host="0.0.0.0", port=8000, reload=True)
