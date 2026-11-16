"use client";

import React, { useState } from "react";
import {
  Activity,
  AlertTriangle,
  ArrowDownRight,
  ArrowUpRight,
  CheckCircle,
  Clock,
  FileText,
  ListOrdered,
  Radio,
  Zap,
} from "lucide-react";
import {
  AnomalyAlert,
  Order,
  Position,
  RiskAlert,
  Signal,
} from "@/types/telemetry";

interface PositionsAndOrdersTabsProps {
  positions: Position[];
  orders: Order[];
  signals: Signal[];
  anomalies: AnomalyAlert[];
  riskAlerts: RiskAlert[];
}

export const PositionsAndOrdersTabs: React.FC<PositionsAndOrdersTabsProps> = ({
  positions,
  orders,
  signals,
  anomalies,
  riskAlerts,
}) => {
  const [activeTab, setActiveTab] = useState<"POSITIONS" | "ORDERS" | "STREAM">("POSITIONS");

  return (
    <div className="h-full flex flex-col bg-cockpit-card rounded-xl border border-cockpit-border overflow-hidden select-none">
      {/* Tab Navigation Header */}
      <div className="h-11 border-b border-cockpit-border px-3 flex items-center justify-between">
        <div className="flex items-center space-x-1">
          <button
            onClick={() => setActiveTab("POSITIONS")}
            className={`px-3 py-1.5 rounded-lg text-xs font-mono font-medium flex items-center space-x-1.5 transition ${
              activeTab === "POSITIONS"
                ? "bg-cockpit-surface text-white border border-cockpit-borderBright"
                : "text-cockpit-muted hover:text-white"
            }`}
          >
            <Activity className="h-3.5 w-3.5 text-cockpit-blue" />
            <span>Open Positions</span>
            <span className="text-[10px] px-1 rounded bg-cockpit-card text-cockpit-muted">
              {positions.length}
            </span>
          </button>

          <button
            onClick={() => setActiveTab("ORDERS")}
            className={`px-3 py-1.5 rounded-lg text-xs font-mono font-medium flex items-center space-x-1.5 transition ${
              activeTab === "ORDERS"
                ? "bg-cockpit-surface text-white border border-cockpit-borderBright"
                : "text-cockpit-muted hover:text-white"
            }`}
          >
            <ListOrdered className="h-3.5 w-3.5 text-cockpit-purple" />
            <span>Order Book</span>
            <span className="text-[10px] px-1 rounded bg-cockpit-card text-cockpit-muted">
              {orders.length}
            </span>
          </button>

          <button
            onClick={() => setActiveTab("STREAM")}
            className={`px-3 py-1.5 rounded-lg text-xs font-mono font-medium flex items-center space-x-1.5 transition ${
              activeTab === "STREAM"
                ? "bg-cockpit-surface text-white border border-cockpit-borderBright"
                : "text-cockpit-muted hover:text-white"
            }`}
          >
            <Radio className="h-3.5 w-3.5 text-cockpit-green animate-pulse" />
            <span>Live Stream</span>
            <span className="text-[10px] px-1 rounded bg-cockpit-card text-cockpit-muted">
              {signals.length + anomalies.length + riskAlerts.length}
            </span>
          </button>
        </div>
      </div>

      {/* Tab 1: Open Positions */}
      {activeTab === "POSITIONS" && (
        <div className="flex-1 overflow-y-auto">
          {positions.length === 0 ? (
            <div className="h-full flex flex-col items-center justify-center text-cockpit-muted text-xs font-mono space-y-1">
              <Activity className="h-6 w-6 text-cockpit-muted/40 mb-1" />
              <span>No active open positions.</span>
              <span className="text-[10px]">Scanning active cluster regimes...</span>
            </div>
          ) : (
            <table className="w-full text-left text-xs font-mono">
              <thead className="sticky top-0 bg-cockpit-surface/90 backdrop-blur-sm border-b border-cockpit-border text-[10px] text-cockpit-muted uppercase">
                <tr>
                  <th className="py-2 px-3">Symbol</th>
                  <th className="py-2 px-2">Side</th>
                  <th className="py-2 px-2 text-right">Qty</th>
                  <th className="py-2 px-2 text-right">Entry</th>
                  <th className="py-2 px-2 text-right">LTP</th>
                  <th className="py-2 px-3 text-right">Unrealized MTM</th>
                  <th className="py-2 px-2 text-right">Stop Loss</th>
                  <th className="py-2 px-3 text-right">Target</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-cockpit-border/40">
                {positions.map((pos) => {
                  const isProfit = pos.unrealized_pnl >= 0;
                  const isBuy = pos.side === "BUY";
                  // Check if SL is at or past entry (breakeven trailed)
                  const isBreakeven = isBuy
                    ? pos.stop_loss >= pos.average_entry_price
                    : pos.stop_loss <= pos.average_entry_price;

                  return (
                    <tr key={pos.tradingsymbol} className="hover:bg-cockpit-surface/50 transition">
                      <td className="py-2.5 px-3 font-bold text-white">{pos.tradingsymbol}</td>
                      <td className="py-2.5 px-2">
                        <span
                          className={`px-1.5 py-0.5 rounded text-[10px] font-bold ${
                            isBuy
                              ? "bg-cockpit-green/15 text-cockpit-green border border-cockpit-green/30"
                              : "bg-cockpit-red/15 text-cockpit-red border border-cockpit-red/30"
                          }`}
                        >
                          {pos.side}
                        </span>
                      </td>
                      <td className="py-2.5 px-2 text-right font-medium">{pos.quantity}</td>
                      <td className="py-2.5 px-2 text-right text-cockpit-muted">
                        ₹{pos.average_entry_price.toFixed(2)}
                      </td>
                      <td className="py-2.5 px-2 text-right font-bold text-white">
                        ₹{pos.ltp.toFixed(2)}
                      </td>
                      <td
                        className={`py-2.5 px-3 text-right font-bold ${
                          isProfit ? "text-cockpit-green" : "text-cockpit-red"
                        }`}
                      >
                        {isProfit ? "+" : ""}₹{pos.unrealized_pnl.toFixed(2)}
                      </td>
                      <td className="py-2.5 px-2 text-right">
                        <span className="text-cockpit-muted">₹{pos.stop_loss.toFixed(2)}</span>
                        {isBreakeven && (
                          <span className="ml-1 text-[9px] px-1 py-0.2 rounded bg-cockpit-green/20 text-cockpit-green font-bold">
                            1R-BE
                          </span>
                        )}
                      </td>
                      <td className="py-2.5 px-3 text-right text-cockpit-blue font-medium">
                        ₹{pos.target_price.toFixed(2)}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>
      )}

      {/* Tab 2: Order Book & Audit Log */}
      {activeTab === "ORDERS" && (
        <div className="flex-1 overflow-y-auto">
          {orders.length === 0 ? (
            <div className="h-full flex items-center justify-center text-cockpit-muted text-xs font-mono">
              <span>No orders recorded in current session.</span>
            </div>
          ) : (
            <table className="w-full text-left text-xs font-mono">
              <thead className="sticky top-0 bg-cockpit-surface/90 backdrop-blur-sm border-b border-cockpit-border text-[10px] text-cockpit-muted uppercase">
                <tr>
                  <th className="py-2 px-3">Time</th>
                  <th className="py-2 px-2">Symbol</th>
                  <th className="py-2 px-2">Side</th>
                  <th className="py-2 px-2">Type</th>
                  <th className="py-2 px-2 text-right">Qty</th>
                  <th className="py-2 px-2 text-right">Limit</th>
                  <th className="py-2 px-2 text-right">Filled</th>
                  <th className="py-2 px-3 text-center">Status</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-cockpit-border/40">
                {orders.map((ord) => {
                  const isBuy = ord.side === "BUY";
                  const timeStr = ord.created_at_ist
                    ? new Date(ord.created_at_ist).toLocaleTimeString("en-IN", {
                        hour12: false,
                        hour: "2-digit",
                        minute: "2-digit",
                        second: "2-digit",
                      })
                    : "--:--:--";

                  return (
                    <tr key={ord.order_id} className="hover:bg-cockpit-surface/50 transition">
                      <td className="py-2 px-3 text-cockpit-muted">{timeStr}</td>
                      <td className="py-2 px-2 font-bold text-white">{ord.tradingsymbol}</td>
                      <td className="py-2 px-2">
                        <span
                          className={`px-1.5 py-0.5 rounded text-[10px] font-bold ${
                            isBuy ? "text-cockpit-green" : "text-cockpit-red"
                          }`}
                        >
                          {ord.side}
                        </span>
                      </td>
                      <td className="py-2 px-2 text-[10px] text-cockpit-muted">
                        {ord.product} {ord.order_type}
                      </td>
                      <td className="py-2 px-2 text-right">{ord.quantity}</td>
                      <td className="py-2 px-2 text-right text-cockpit-muted">
                        ₹{ord.limit_price?.toFixed(2) || "—"}
                      </td>
                      <td className="py-2 px-2 text-right font-medium text-white">
                        {ord.average_fill_price ? `₹${ord.average_fill_price.toFixed(2)}` : "—"}
                      </td>
                      <td className="py-2 px-3 text-center">
                        <span
                          className={`px-1.5 py-0.5 rounded text-[10px] font-semibold uppercase ${
                            ord.status === "FILLED"
                              ? "bg-cockpit-green/15 text-cockpit-green"
                              : ord.status === "OPEN"
                              ? "bg-cockpit-blue/15 text-cockpit-blue"
                              : "bg-cockpit-red/15 text-cockpit-red"
                          }`}
                        >
                          {ord.status}
                        </span>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>
      )}

      {/* Tab 3: Live Telemetry Event Stream */}
      {activeTab === "STREAM" && (
        <div className="flex-1 overflow-y-auto p-3 space-y-2 text-xs font-mono">
          {signals.length === 0 && anomalies.length === 0 && riskAlerts.length === 0 ? (
            <div className="h-full flex items-center justify-center text-cockpit-muted text-xs">
              <span>Telemetry event stream is active and awaiting signals...</span>
            </div>
          ) : (
            <>
              {/* Risk Alerts */}
              {riskAlerts.map((r, i) => (
                <div
                  key={`risk-${i}`}
                  className="p-2 rounded bg-cockpit-red/10 border border-cockpit-red/30 text-cockpit-red flex items-start space-x-2"
                >
                  <AlertTriangle className="h-4 w-4 shrink-0 mt-0.5" />
                  <div>
                    <span className="font-bold uppercase tracking-wider">[{r.level}]</span>{" "}
                    <span>{r.message}</span>
                  </div>
                </div>
              ))}

              {/* Anomaly Alerts */}
              {anomalies.map((a, i) => (
                <div
                  key={`anom-${i}`}
                  className="p-2 rounded bg-cockpit-purple/10 border border-cockpit-purple/30 text-cockpit-purple flex items-start space-x-2"
                >
                  <Zap className="h-4 w-4 shrink-0 mt-0.5" />
                  <div>
                    <span className="font-bold">[{a.kind}] {a.symbol}:</span> {a.message}
                  </div>
                </div>
              ))}

              {/* Signals */}
              {signals.map((s) => (
                <div
                  key={s.signal_id}
                  className="p-2 rounded bg-cockpit-surface border border-cockpit-border flex items-center justify-between"
                >
                  <div className="flex items-center space-x-2">
                    {s.side === "BUY" ? (
                      <ArrowUpRight className="h-4 w-4 text-cockpit-green" />
                    ) : (
                      <ArrowDownRight className="h-4 w-4 text-cockpit-red" />
                    )}
                    <span className="font-bold text-white">{s.tradingsymbol}</span>
                    <span
                      className={`text-[10px] px-1 rounded font-bold ${
                        s.side === "BUY" ? "text-cockpit-green" : "text-cockpit-red"
                      }`}
                    >
                      {s.side}
                    </span>
                    <span className="text-[10px] text-cockpit-muted">@{s.entry_price}</span>
                  </div>
                  <div className="text-[10px] text-cockpit-muted flex items-center space-x-2">
                    <span>Conf: {(s.confidence * 100).toFixed(0)}%</span>
                    <span>•</span>
                    <span>SL: ₹{s.stop_loss}</span>
                    <span>•</span>
                    <span>TP: ₹{s.target_price}</span>
                  </div>
                </div>
              ))}
            </>
          )}
        </div>
      )}
    </div>
  );
};
