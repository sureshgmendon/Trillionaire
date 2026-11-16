/**
 * Trillionaire Cockpit - WebSocket Stream & Telemetry State Hook
 * Manages WebSocket lifecycle, auto-reconnection with exponential backoff,
 * initial REST hydration, and real-time event updates.
 */

"use client";

import { useEffect, useRef, useState, useCallback } from "react";
import {
  AnomalyAlert,
  Candle,
  EngineStatus,
  Order,
  Position,
  RiskAlert,
  Signal,
  StockProfile,
  TelemetryEnvelope,
  Tick,
} from "@/types/telemetry";

const API_BASE_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
const WS_BASE_URL = process.env.NEXT_PUBLIC_WS_URL || "ws://localhost:8000/ws/stream";
const ADMIN_KEY = process.env.NEXT_PUBLIC_ADMIN_KEY || "trillionaire-dev-secret-key-change-in-prod";

export type ConnectionStatus = "CONNECTED" | "RECONNECTING" | "OFFLINE";

export function useTrillionaireStream() {
  const [connectionStatus, setConnectionStatus] = useState<ConnectionStatus>("OFFLINE");
  const [status, setStatus] = useState<EngineStatus | null>(null);
  const [ticks, setTicks] = useState<Record<string, Tick>>({});
  const [candles, setCandles] = useState<Record<string, Candle[]>>({});
  const [signals, setSignals] = useState<Signal[]>([]);
  const [orders, setOrders] = useState<Order[]>([]);
  const [positions, setPositions] = useState<Position[]>([]);
  const [anomalies, setAnomalies] = useState<AnomalyAlert[]>([]);
  const [riskAlerts, setRiskAlerts] = useState<RiskAlert[]>([]);
  const [profiles, setProfiles] = useState<StockProfile[]>([]);
  const [selectedSymbol, setSelectedSymbol] = useState<string>("RELIANCE");

  const wsRef = useRef<WebSocket | null>(null);
  const reconnectAttemptsRef = useRef<number>(0);
  const reconnectTimerRef = useRef<NodeJS.Timeout | null>(null);

  // 1. Initial REST Hydration
  const fetchInitialState = useCallback(async () => {
    try {
      const [statusRes, posRes, ordRes, sentinelRes] = await Promise.allSettled([
        fetch(`${API_BASE_URL}/api/v1/status`),
        fetch(`${API_BASE_URL}/api/v1/positions`),
        fetch(`${API_BASE_URL}/api/v1/orders`),
        fetch(`${API_BASE_URL}/api/v1/sentinel`),
      ]);

      if (statusRes.status === "fulfilled" && statusRes.value.ok) {
        const s = await statusRes.value.json();
        setStatus(s);
      }
      if (posRes.status === "fulfilled" && posRes.value.ok) {
        const p = await posRes.value.json();
        setPositions(p);
      }
      if (ordRes.status === "fulfilled" && ordRes.value.ok) {
        const o = await ordRes.value.json();
        setOrders(o);
      }
      if (sentinelRes.status === "fulfilled" && sentinelRes.value.ok) {
        const profs = await sentinelRes.value.json();
        setProfiles(profs);
        if (profs.length > 0 && !selectedSymbol) {
          setSelectedSymbol(profs[0].tradingsymbol);
        }
      }
    } catch (err) {
      console.warn("[TELEMETRY REST] Initial fetch error:", err);
    }
  }, [selectedSymbol]);

  // 2. WebSocket Connection Lifecycle
  const connectWebSocket = useCallback(() => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      return;
    }

    try {
      setConnectionStatus((prev) => (prev === "CONNECTED" ? "RECONNECTING" : "OFFLINE"));
      const ws = new WebSocket(WS_BASE_URL);
      wsRef.current = ws;

      ws.onopen = () => {
        setConnectionStatus("CONNECTED");
        reconnectAttemptsRef.current = 0;
        console.info("[TELEMETRY WS] Stream connected to:", WS_BASE_URL);
      };

      ws.onmessage = (event) => {
        try {
          const envelope: TelemetryEnvelope = JSON.parse(event.data);
          handleEnvelope(envelope);
        } catch (parseErr) {
          console.error("[TELEMETRY WS] JSON parse error:", parseErr);
        }
      };

      ws.onclose = () => {
        setConnectionStatus("RECONNECTING");
        scheduleReconnect();
      };

      ws.onerror = () => {
        ws.close();
      };
    } catch (err) {
      console.error("[TELEMETRY WS] Initialization error:", err);
      scheduleReconnect();
    }
  }, []);

  const scheduleReconnect = useCallback(() => {
    if (reconnectTimerRef.current) {
      clearTimeout(reconnectTimerRef.current);
    }
    const delay = Math.min(1000 * Math.pow(1.5, reconnectAttemptsRef.current), 10000);
    reconnectAttemptsRef.current += 1;
    reconnectTimerRef.current = setTimeout(() => {
      connectWebSocket();
    }, delay);
  }, [connectWebSocket]);

  // 3. Process Incoming Telemetry Envelopes
  const handleEnvelope = useCallback((envelope: TelemetryEnvelope) => {
    switch (envelope.type) {
      case "SNAPSHOT": {
        const { payload } = envelope;
        if (payload.status) setStatus((prev) => ({ ...prev, ...payload.status } as EngineStatus));
        if (payload.latest_ticks) setTicks(payload.latest_ticks);
        if (payload.recent_signals) setSignals(payload.recent_signals);
        if (payload.recent_orders) setOrders(payload.recent_orders);
        if (payload.recent_risk_alerts) setRiskAlerts(payload.recent_risk_alerts);
        if (payload.recent_anomalies) setAnomalies(payload.recent_anomalies);
        break;
      }
      case "TICK": {
        const tick = envelope.payload;
        setTicks((prev) => ({ ...prev, [tick.tradingsymbol]: tick }));
        break;
      }
      case "CANDLE": {
        const candle = envelope.payload;
        const sym = candle.tradingsymbol;
        setCandles((prev) => {
          const existing = prev[sym] || [];
          const updated = [...existing.filter((c) => c.timestamp_ist !== candle.timestamp_ist), candle];
          // Keep last 150 candles
          return { ...prev, [sym]: updated.slice(-150) };
        });
        break;
      }
      case "SIGNAL": {
        const sig = envelope.payload;
        setSignals((prev) => [sig, ...prev.slice(0, 99)]);
        break;
      }
      case "ORDER": {
        const ord = envelope.payload;
        setOrders((prev) => [ord, ...prev.slice(0, 99)]);
        // Refetch positions on order fill or update
        fetch(`${API_BASE_URL}/api/v1/positions`)
          .then((r) => r.json())
          .then((p) => setPositions(p))
          .catch(() => {});
        fetch(`${API_BASE_URL}/api/v1/status`)
          .then((r) => r.json())
          .then((s) => setStatus(s))
          .catch(() => {});
        break;
      }
      case "ANOMALY": {
        const anom = envelope.payload;
        setAnomalies((prev) => [anom, ...prev.slice(0, 99)]);
        break;
      }
      case "RISK": {
        const risk = envelope.payload;
        setRiskAlerts((prev) => [risk, ...prev.slice(0, 99)]);
        break;
      }
      default:
        break;
    }
  }, []);

  // 4. Emergency Kill Switch Action
  const triggerEmergencyFlatten = useCallback(async (reason: string = "UI_MANUAL_OVERRIDE") => {
    try {
      const resp = await fetch(
        `${API_BASE_URL}/api/v1/control/kill-switch?reason=${encodeURIComponent(reason)}`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-API-Key": ADMIN_KEY,
          },
        }
      );
      if (resp.ok) {
        const data = await resp.json();
        // Immediately refresh positions & status
        setPositions([]);
        setStatus((prev) => (prev ? { ...prev, circuit_breaker_tripped: true } : null));
        return { success: true, message: `Emergency flatten executed: ${data.liquidated_orders_count} positions closed.` };
      } else {
        const err = await resp.json();
        return { success: false, message: err.detail || "Emergency flatten failed" };
      }
    } catch (e: any) {
      return { success: false, message: e.message || "Network error" };
    }
  }, []);

  // 5. Historical Replay Action
  const triggerReplay = useCallback(
    async (params: { trading_date?: string; speed?: string; symbols?: string[] }) => {
      try {
        const resp = await fetch(`${API_BASE_URL}/api/v1/control/replay`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-API-Key": ADMIN_KEY,
          },
          body: JSON.stringify(params),
        });
        if (resp.ok) {
          const data = await resp.json();
          return { success: true, message: `Replay completed: ${data.ticks_replayed} ticks replayed.` };
        } else {
          const err = await resp.json();
          return { success: false, message: err.detail || "Replay trigger failed" };
        }
      } catch (e: any) {
        return { success: false, message: e.message || "Network error" };
      }
    },
    []
  );

  useEffect(() => {
    fetchInitialState();
    connectWebSocket();

    return () => {
      if (reconnectTimerRef.current) {
        clearTimeout(reconnectTimerRef.current);
      }
      if (wsRef.current) {
        wsRef.current.close();
      }
    };
  }, [fetchInitialState, connectWebSocket]);

  return {
    connectionStatus,
    status,
    ticks,
    candles,
    signals,
    orders,
    positions,
    anomalies,
    riskAlerts,
    profiles,
    selectedSymbol,
    setSelectedSymbol,
    triggerEmergencyFlatten,
    triggerReplay,
    refreshState: fetchInitialState,
  };
}
