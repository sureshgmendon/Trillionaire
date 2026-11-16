"""
Trillionaire API Bridge - WebSocket Module
Exports TelemetryManager for real-time WebSocket client streaming.
"""

from api_bridge.ws.telemetry_manager import TelemetryManager, telemetry_manager

__all__ = ["TelemetryManager", "telemetry_manager"]
