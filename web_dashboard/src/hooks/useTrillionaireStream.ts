/**
 * Trillionaire Cockpit - WebSocket Stream & Telemetry State Hook
 * Manages WebSocket lifecycle, auto-reconnection with exponential backoff,
 * initial REST hydration, dynamic symbol/timeframe candle loading, and real-time state sync.
 */

"use client";

import { useEffect, useRef, useState, useCallback } from "react";
import {
  AnomalyAlert,
  Candle,
  ClosedTrade,
  EngineStatus,
  Order,
  Position,
  RiskAlert,
  SentinelSummary,
  Signal,
  StockProfile,
  TelemetryEnvelope,
  Tick,
  TimeframeCode,
} from "@/types/telemetry";

export function getApiBaseUrl(): string {
  const envUrl = process.env.NEXT_PUBLIC_API_URL;
  if (typeof window !== "undefined") {
    if (window.location.protocol === "https:") {
      if (!envUrl || envUrl.startsWith("http://32.236.106.39") || envUrl.startsWith("http://")) {
        return window.location.origin;
      }
    }
    if (envUrl && envUrl.trim() !== "") {
      return envUrl;
    }
    return window.location.origin;
  }
  return envUrl && envUrl.trim() !== "" ? envUrl : "https://trillionaire-trade.duckdns.org";
}

export function getWsBaseUrl(): string {
  const envWs = process.env.NEXT_PUBLIC_WS_URL;
  if (typeof window !== "undefined") {
    const isHttps = window.location.protocol === "https:";
    const wsProto = isHttps ? "wss:" : "ws:";
    if (isHttps) {
      if (!envWs || envWs.startsWith("ws://32.236.106.39") || envWs.startsWith("ws://")) {
        return `wss://${window.location.host}/ws/stream`;
      }
    }
    if (envWs && envWs.trim() !== "") {
      return envWs;
    }
    return `${wsProto}//${window.location.host}/ws/stream`;
  }
  return envWs && envWs.trim() !== "" ? envWs : "wss://trillionaire-trade.duckdns.org/ws/stream";
}

const ADMIN_KEY = process.env.NEXT_PUBLIC_ADMIN_KEY || "trillionaire-local-admin-123";

export type ConnectionStatus = "CONNECTED" | "RECONNECTING" | "OFFLINE";

export function useTrillionaireStream() {
  const [connectionStatus, setConnectionStatus] = useState<ConnectionStatus>("OFFLINE");
  const [status, setStatus] = useState<EngineStatus | null>(null);
  const [ticks, setTicks] = useState<Record<string, Tick>>({});
  const [candles, setCandles] = useState<Record<string, Candle[]>>({});
  const [signals, setSignals] = useState<Signal[]>([]);
  const [orders, setOrders] = useState<Order[]>([]);
  const [positions, setPositions] = useState<Position[]>([]);
  const [closedTrades, setClosedTrades] = useState<ClosedTrade[]>([]);
  const [anomalies, setAnomalies] = useState<AnomalyAlert[]>([]);
  const [riskAlerts, setRiskAlerts] = useState<RiskAlert[]>([]);
  const [profiles, setProfiles] = useState<StockProfile[]>([]);
  const [sentinelSummary, setSentinelSummary] = useState<SentinelSummary | null>(null);
  const [selectedSymbol, setSelectedSymbol] = useState<string>("RELIANCE");
  const [selectedTimeframe, setSelectedTimeframe] = useState<TimeframeCode>("M5");

  const wsRef = useRef<WebSocket | null>(null);
  const reconnectAttemptsRef = useRef<number>(0);
  const reconnectTimerRef = useRef<NodeJS.Timeout | null>(null);

  // 1. Initial REST Hydration
  const fetchInitialState = useCallback(async () => {
    try {
      const apiBase = getApiBaseUrl();
      const [statusRes, posRes, ordRes, closedRes, sentinelRes, summaryRes] = await Promise.allSettled([
        fetch(`${apiBase}/api/v1/status`),
        fetch(`${apiBase}/api/v1/positions`),
        fetch(`${apiBase}/api/v1/orders`),
        fetch(`${apiBase}/api/v1/closed-trades`),
        fetch(`${apiBase}/api/v1/sentinel`),
        fetch(`${apiBase}/api/v1/sentinel/summary`, {
          headers: {
            "X-API-Key": ADMIN_KEY,
          },
        }),
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
      if (closedRes.status === "fulfilled" && closedRes.value.ok) {
        const ct = await closedRes.value.json();
        setClosedTrades(ct);
      }
      if (sentinelRes.status === "fulfilled" && sentinelRes.value.ok) {
        const profs = await sentinelRes.value.json();
        setProfiles(profs);
        if (profs.length > 0 && !selectedSymbol) {
          setSelectedSymbol(profs[0].tradingsymbol);
        }
      }
      if (summaryRes.status === "fulfilled" && summaryRes.value.ok) {
        const summ = await summaryRes.value.json();
        setSentinelSummary(summ);
      }
    } catch (err) {
      console.warn("[TELEMETRY REST] Initial fetch error:", err);
    }
  }, [selectedSymbol]);

  // 1b. Dynamic Candle Loader when Symbol or Timeframe switches
  const fetchCandlesForSymbol = useCallback(async (symbol: string, timeframe: TimeframeCode) => {
    if (!symbol) return;
    try {
      const apiBase = getApiBaseUrl();
      const res = await fetch(
        `${apiBase}/api/v1/candles/${encodeURIComponent(symbol)}?timeframe=${timeframe}&limit=150`
      );
      if (res.ok) {
        const bars: Candle[] = await res.json();
        const key = `${symbol}_${timeframe}`;
        setCandles((prev) => ({ ...prev, [key]: bars }));
      }
    } catch (e) {
      console.debug("[CANDLES FETCH] Skipped:", e);
    }
  }, []);

  useEffect(() => {
    fetchCandlesForSymbol(selectedSymbol, selectedTimeframe);
  }, [selectedSymbol, selectedTimeframe, fetchCandlesForSymbol]);

  // 2. WebSocket Connection Lifecycle
  const connectWebSocket = useCallback(() => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      return;
    }

    try {
      setConnectionStatus((prev) => (prev === "CONNECTED" ? "RECONNECTING" : "OFFLINE"));
      const wsUrl = getWsBaseUrl();
      const ws = new WebSocket(wsUrl);
      wsRef.current = ws;

      ws.onopen = () => {
        setConnectionStatus("CONNECTED");
        reconnectAttemptsRef.current = 0;
        console.info("[TELEMETRY WS] Stream connected to:", wsUrl);
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
        if (payload.status) {
          setStatus((prev) => ({ ...prev, ...payload.status } as EngineStatus));
        }
        if (payload.open_positions) {
          setPositions(payload.open_positions);
        }
        if (payload.closed_trades) {
          setClosedTrades(payload.closed_trades);
        }
        if (payload.latest_ticks && Object.keys(payload.latest_ticks).length > 0) {
          setTicks((prev) => ({ ...prev, ...payload.latest_ticks }));
        }
        if (payload.recent_signals) setSignals(payload.recent_signals);
        if (payload.recent_orders) setOrders(payload.recent_orders);
        if (payload.recent_risk_alerts) setRiskAlerts(payload.recent_risk_alerts);
        if (payload.recent_anomalies) setAnomalies(payload.recent_anomalies);
        if (payload.sentinel_tags) {
          const sTags = payload.sentinel_tags;
          setProfiles((prev) =>
            prev.map((p) => (sTags[p.tradingsymbol] ? { ...p, sentinel: sTags[p.tradingsymbol] } : p))
          );
        }
        break;
      }
      case "TICK": {
        const tick = envelope.payload;
        setTicks((prev) => ({ ...prev, [tick.tradingsymbol]: tick }));
        break;
      }
      case "CANDLE": {
        const candle = envelope.payload;
        const tf = (candle.timeframe || "M5") as TimeframeCode;
        const key = `${candle.tradingsymbol}_${tf}`;
        setCandles((prev) => {
          const existing = prev[key] || [];
          const updated = [...existing.filter((c) => c.timestamp_ist !== candle.timestamp_ist), candle];
          return { ...prev, [key]: updated.slice(-200) };
        });
        break;
      }
      case "SIGNAL": {
        const sig = envelope.payload;
        setSignals((prev) => [sig, ...prev.slice(0, 99)]);
        break;
      }
      case "ORDER":
      case "EXIT": {
        const ord = envelope.payload;
        setOrders((prev) => {
          const exists = prev.some((o) => o.order_id === ord.order_id);
          if (exists) {
            return prev.map((o) => (o.order_id === ord.order_id ? ord : o));
          }
          return [ord, ...prev.slice(0, 199)];
        });
        const apiBase = getApiBaseUrl();
        fetch(`${apiBase}/api/v1/positions`)
          .then((r) => r.json())
          .then((p) => setPositions(p))
          .catch(() => {});
        fetch(`${apiBase}/api/v1/closed-trades`)
          .then((r) => r.json())
          .then((ct) => setClosedTrades(ct))
          .catch(() => {});
        fetch(`${apiBase}/api/v1/status`)
          .then((r) => r.json())
          .then((s) => {
            if (s) setStatus(s);
          })
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

  // 4. Emergency Kill Switch / FLATTEN ALL Action
  const triggerEmergencyFlatten = useCallback(async (reason: string = "FLATTEN_ALL") => {
    try {
      const apiBase = getApiBaseUrl();
      const resp = await fetch(
        `${apiBase}/api/v1/control/kill-switch?reason=${encodeURIComponent(reason)}`,
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
        setPositions([]);
        setStatus((prev) => (prev ? { ...prev, circuit_breaker_tripped: true } : null));
        await fetchInitialState();
        return { success: true, message: `Emergency flatten executed: ${data.liquidated_orders_count} positions closed.` };
      } else {
        const err = await resp.json();
        return { success: false, message: err.detail || "Emergency flatten failed" };
      }
    } catch (e: any) {
      return { success: false, message: e.message || "Network error" };
    }
  }, [fetchInitialState]);

  // 5. Historical Replay Action
  const triggerReplay = useCallback(
    async (params: { trading_date?: string; speed?: string; symbols?: string[] }) => {
      try {
        const resp = await fetch(`${getApiBaseUrl()}/api/v1/control/replay`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-API-Key": ADMIN_KEY,
          },
          body: JSON.stringify(params),
        });
        if (resp.ok) {
          const data = await resp.json();
          await fetchInitialState();
          return { success: true, message: `Replay completed: ${data.ticks_replayed} ticks replayed.` };
        } else {
          let errorMsg = "Replay trigger failed";
          try {
            const err = await resp.json();
            errorMsg = err.detail || errorMsg;
          } catch {
            const text = await resp.text();
            errorMsg = text || `HTTP ${resp.status} ${resp.statusText}`;
          }
          return { success: false, message: errorMsg };
        }
      } catch (e: any) {
        return { success: false, message: e.message || "Network error" };
      }
    },
    [fetchInitialState]
  );

  // 6. Engine Lifecycle Actions
  const startEngine = useCallback(async () => {
    try {
      const resp = await fetch(`${getApiBaseUrl()}/api/v1/control/engine/start`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-API-Key": ADMIN_KEY,
        },
      });
      if (resp.ok) {
        await fetchInitialState();
      }
    } catch (e) {
      console.error("Failed to start engine:", e);
    }
  }, [fetchInitialState]);

  const pauseTrading = useCallback(async () => {
    try {
      const resp = await fetch(`${getApiBaseUrl()}/api/v1/control/engine/pause`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-API-Key": ADMIN_KEY,
        },
      });
      if (resp.ok) {
        await fetchInitialState();
      }
    } catch (e) {
      console.error("Failed to pause trading:", e);
    }
  }, [fetchInitialState]);

  const resumeTrading = useCallback(async () => {
    try {
      const resp = await fetch(`${getApiBaseUrl()}/api/v1/control/engine/resume`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-API-Key": ADMIN_KEY,
        },
      });
      if (resp.ok) {
        await fetchInitialState();
      }
    } catch (e) {
      console.error("Failed to resume trading:", e);
    }
  }, [fetchInitialState]);

  const stopEngine = useCallback(async () => {
    try {
      const resp = await fetch(`${getApiBaseUrl()}/api/v1/control/engine/stop`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-API-Key": ADMIN_KEY,
        },
      });
      if (resp.ok) {
        await fetchInitialState();
      }
    } catch (e) {
      console.error("Failed to stop engine:", e);
    }
  }, [fetchInitialState]);

  const overrideSentinelLock = useCallback(async (symbol: string) => {
    try {
      const resp = await fetch(
        `${getApiBaseUrl()}/api/v1/sentinel/${encodeURIComponent(symbol)}/override`,
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
        if (data.sentinel) {
          setProfiles((prev) =>
            prev.map((p) => (p.tradingsymbol === symbol ? { ...p, sentinel: data.sentinel } : p))
          );
        }
        return { success: true };
      }
      return { success: false };
    } catch {
      return { success: false };
    }
  }, []);

  const refreshSentinelSummary = useCallback(async () => {
    try {
      const res = await fetch(`${getApiBaseUrl()}/api/v1/sentinel/summary`, {
        headers: {
          "X-API-Key": ADMIN_KEY,
        },
      });
      if (res.ok) {
        const data = await res.json();
        setSentinelSummary(data);
      }
    } catch (e) {
      console.warn("[TELEMETRY REST] Sentinel summary refresh error:", e);
    }
  }, []);

  useEffect(() => {
    fetchInitialState();
    connectWebSocket();

    const sentinelInterval = setInterval(() => {
      refreshSentinelSummary();
    }, 5 * 60 * 1000);

    return () => {
      clearInterval(sentinelInterval);
      if (reconnectTimerRef.current) {
        clearTimeout(reconnectTimerRef.current);
      }
      if (wsRef.current) {
        wsRef.current.close();
      }
    };
  }, [fetchInitialState, connectWebSocket, refreshSentinelSummary]);

  // 7. Single Position Quick Exit Action
  const exitPosition = useCallback(
    async (symbol: string, reason: string = "MANUAL_EXIT") => {
      try {
        const apiBase = getApiBaseUrl();
        const resp = await fetch(
          `${apiBase}/api/v1/control/positions/${encodeURIComponent(symbol)}/exit?reason=${encodeURIComponent(reason)}`,
          {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "X-API-Key": ADMIN_KEY,
            },
          }
        );
        if (resp.ok) {
          const [pRes, ctRes, sRes] = await Promise.allSettled([
            fetch(`${apiBase}/api/v1/positions`),
            fetch(`${apiBase}/api/v1/closed-trades`),
            fetch(`${apiBase}/api/v1/status`),
          ]);
          if (pRes.status === "fulfilled" && pRes.value.ok) {
            setPositions(await pRes.value.json());
          }
          if (ctRes.status === "fulfilled" && ctRes.value.ok) {
            setClosedTrades(await ctRes.value.json());
          }
          if (sRes.status === "fulfilled" && sRes.value.ok) {
            setStatus(await sRes.value.json());
          }
          return { success: true, message: `Position ${symbol} exited successfully.` };
        } else {
          const err = await resp.json().catch(() => ({}));
          return { success: false, message: err.detail || `Failed to exit ${symbol}` };
        }
      } catch (e: any) {
        return { success: false, message: e.message || "Network error" };
      }
    },
    []
  );

  // 7b. Cancel a Pending OPEN / TRIGGER PENDING Order
  const cancelOrder = useCallback(async (orderId: string) => {
    try {
      const apiBase = getApiBaseUrl();
      const resp = await fetch(
        `${apiBase}/api/v1/control/orders/${encodeURIComponent(orderId)}/cancel`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-API-Key": ADMIN_KEY,
          },
        }
      );
      if (resp.ok) {
        const ordRes = await fetch(`${apiBase}/api/v1/orders`);
        if (ordRes.ok) {
          setOrders(await ordRes.json());
        }
        return { success: true, message: "Order cancelled." };
      }
      const err = await resp.json().catch(() => ({}));
      return { success: false, message: err.detail || "Failed to cancel order" };
    } catch (e: any) {
      return { success: false, message: e.message || "Network error" };
    }
  }, []);

  // 8. Dynamic Live Unrealized P&L Calculation across active positions
  const liveUnrealizedPnl = (function calculateLiveUnrealized() {
    if (!positions || positions.length === 0) {
      return 0.0;
    }
    let total = 0;
    for (const pos of positions) {
      const sym = (pos as any).symbol || pos.tradingsymbol;
      const avgPrice = (pos as any).avg_price ?? pos.average_entry_price ?? 0;
      const currentLtp = (sym && ticks[sym]?.ltp) ?? pos.ltp ?? avgPrice;
      const qty = pos.quantity ?? 0;
      const isBuy = (pos.side as any) === "BUY" || qty > 0;

      if (isBuy) {
        total += (currentLtp - avgPrice) * Math.abs(qty);
      } else {
        total += (avgPrice - currentLtp) * Math.abs(qty);
      }
    }
    return Math.round(total * 100) / 100;
  })();

  return {
    connectionStatus,
    status,
    ticks,
    candles,
    signals,
    orders,
    positions,
    closedTrades,
    anomalies,
    riskAlerts,
    profiles,
    sentinelSummary,
    selectedSymbol,
    setSelectedSymbol,
    selectedTimeframe,
    setSelectedTimeframe,
    liveUnrealizedPnl,
    exitPosition,
    cancelOrder,
    triggerEmergencyFlatten,
    triggerReplay,
    startEngine,
    stopEngine,
    pauseTrading,
    resumeTrading,
    overrideSentinelLock,
    refreshState: fetchInitialState,
    refreshSentinelSummary,
  };
}
