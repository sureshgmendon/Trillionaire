"use client";

import React, { useState, useEffect } from "react";
import {
  Activity,
  AlertTriangle,
  Clock,
  Play,
  Power,
  RefreshCw,
  Shield,
  Sliders,
  Square,
  TrendingDown,
  TrendingUp,
  Zap,
} from "lucide-react";
import { EngineStatus } from "@/types/telemetry";
import { ConnectionStatus } from "@/hooks/useTrillionaireStream";

interface TopTelemetryBarProps {
  connectionStatus: ConnectionStatus;
  status: EngineStatus | null;
  onEmergencyFlatten: () => Promise<{ success: boolean; message: string }>;
  onOpenReplayModal: () => void;
  onOpenSettingsModal: () => void;
  onStartEngine: () => Promise<void>;
  onStopEngine: () => Promise<void>;
  onRefresh: () => void;
}

export const TopTelemetryBar: React.FC<TopTelemetryBarProps> = ({
  connectionStatus,
  status,
  onEmergencyFlatten,
  onOpenReplayModal,
  onOpenSettingsModal,
  onStartEngine,
  onStopEngine,
  onRefresh,
}) => {
  const [istTime, setIstTime] = useState<string>("");
  const [showConfirmFlatten, setShowConfirmFlatten] = useState(false);
  const [isFlattening, setIsFlattening] = useState(false);
  const [flattenToast, setFlattenToast] = useState<string | null>(null);

  // Live IST Clock
  useEffect(() => {
    const updateTime = () => {
      const now = new Date();
      // Format to IST
      const istStr = now.toLocaleTimeString("en-IN", {
        timeZone: "Asia/Kolkata",
        hour12: false,
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      });
      setIstTime(istStr);
    };

    updateTime();
    const timer = setInterval(updateTime, 1000);
    return () => clearInterval(timer);
  }, []);

  const handleConfirmFlatten = async () => {
    setIsFlattening(true);
    const res = await onEmergencyFlatten();
    setIsFlattening(false);
    setShowConfirmFlatten(false);
    setFlattenToast(res.message);
    setTimeout(() => setFlattenToast(null), 5000);
  };

  const dailyMtm = status?.daily_mtm ?? 0;
  const realized = status?.realized_pnl ?? 0;
  const unrealized = status?.unrealized_pnl ?? 0;
  const allocatedCapital = status?.allocated_capital ?? 100000;
  const mtmPct = allocatedCapital > 0 ? (dailyMtm / allocatedCapital) * 100 : 0;
  const isProfit = dailyMtm >= 0;

  return (
    <>
      <header className="h-16 bg-cockpit-card border-b border-cockpit-border px-4 flex items-center justify-between select-none">
        {/* Brand & Connection State */}
        <div className="flex items-center space-x-3">
          <div className="flex items-center space-x-2">
            <div className="h-9 w-9 rounded-lg bg-gradient-to-br from-cockpit-blue/20 to-cockpit-purple/20 border border-cockpit-borderBright flex items-center justify-center">
              <Zap className="h-5 w-5 text-cockpit-blue animate-pulse-live" />
            </div>
            <div>
              <div className="flex items-center space-x-1.5">
                <span className="font-bold tracking-wider text-base text-white">
                  TRILLIONAIRE
                </span>
                <span className="text-[10px] font-mono px-1.5 py-0.5 rounded bg-cockpit-surface border border-cockpit-border text-cockpit-blue uppercase">
                  v0.1.0
                </span>
              </div>
              <div className="text-[11px] text-cockpit-muted flex items-center space-x-2">
                <span>NSE Algo Engine</span>
                <span>•</span>
                <div className="flex items-center space-x-1">
                  <div
                    className={`h-2 w-2 rounded-full ${
                      connectionStatus === "CONNECTED"
                        ? "bg-cockpit-green animate-ping"
                        : connectionStatus === "RECONNECTING"
                        ? "bg-cockpit-amber animate-pulse"
                        : "bg-cockpit-red"
                    }`}
                  />
                  <span
                    className={`text-[10px] font-mono uppercase font-medium ${
                      connectionStatus === "CONNECTED"
                        ? "text-cockpit-green"
                        : connectionStatus === "RECONNECTING"
                        ? "text-cockpit-amber"
                        : "text-cockpit-red"
                    }`}
                  >
                    {connectionStatus}
                  </span>
                </div>
              </div>
            </div>
          </div>
        </div>

        {/* Live IST Market Clock & Operational Mode */}
        <div className="hidden lg:flex items-center space-x-4 bg-cockpit-surface/60 border border-cockpit-border px-3 py-1.5 rounded-lg">
          <div className="flex items-center space-x-1.5 text-xs font-mono text-cockpit-text">
            <Clock className="h-3.5 w-3.5 text-cockpit-muted" />
            <span className="font-bold tracking-tight">{istTime || "--:--:--"}</span>
            <span className="text-[10px] text-cockpit-muted uppercase">IST</span>
          </div>

          <div className="h-3.5 w-px bg-cockpit-border" />

          {/* Mode Pill */}
          <div className="flex items-center space-x-1.5">
            <span className="text-[11px] text-cockpit-muted uppercase">Mode:</span>
            <span
              className={`text-xs font-mono font-semibold px-2 py-0.5 rounded border ${
                status?.mode === "LIVE"
                  ? "bg-cockpit-green/10 border-cockpit-green/40 text-cockpit-green"
                  : status?.mode === "REPLAY"
                  ? "bg-cockpit-purple/10 border-cockpit-purple/40 text-cockpit-purple"
                  : "bg-cockpit-blue/10 border-cockpit-blue/40 text-cockpit-blue"
              }`}
            >
              {status?.mode || "PAPER"}
            </span>
          </div>

          <div className="h-3.5 w-px bg-cockpit-border" />

          {/* Tier Pill */}
          <div className="flex items-center space-x-1.5">
            <Shield className="h-3.5 w-3.5 text-cockpit-blue" />
            <span className="text-xs font-mono text-cockpit-muted">
              {status?.capital_tier || "TIER_0_PAPER"}
            </span>
          </div>

          {/* Signal Funnel Counters */}
          {status?.funnel_metrics && (
            <>
              <div className="h-3.5 w-px bg-cockpit-border" />
              <div
                className="flex items-center space-x-1.5 text-[11px] font-mono text-cockpit-muted"
                title="Live Signal Evaluation Funnel (Candles Evaluated → Stock Filter → Sector Filter → Emitted Signals)"
              >
                <span className="text-cockpit-blue font-semibold">Funnel:</span>
                <span>{status.funnel_metrics.candles_evaluated} Eval</span>
                <span>→</span>
                <span className="text-cyan-400 font-semibold">{status.funnel_metrics.passed_stock_rule} Stock</span>
                <span>→</span>
                <span className="text-amber-400 font-semibold">{status.funnel_metrics.passed_sector_filter} Sector</span>
                <span>→</span>
                <span className="text-cockpit-green font-bold">{status.funnel_metrics.signals_emitted} Sig</span>
              </div>
            </>
          )}
        </div>

        {/* Daily MTM PnL Telemetry */}
        <div className="flex items-center space-x-4">
          <div className="text-right">
            <div className="text-[10px] uppercase font-mono tracking-wider text-cockpit-muted flex items-center justify-end space-x-1">
              <span>Total Daily MTM</span>
              {isProfit ? (
                <TrendingUp className="h-3 w-3 text-cockpit-green" />
              ) : (
                <TrendingDown className="h-3 w-3 text-cockpit-red" />
              )}
            </div>
            <div className="flex items-baseline justify-end space-x-2">
              <span
                className={`font-mono text-lg font-bold tracking-tight ${
                  isProfit ? "text-cockpit-green" : "text-cockpit-red"
                }`}
              >
                {isProfit ? "+" : ""}
                ₹{dailyMtm.toLocaleString("en-IN", { minimumFractionDigits: 2 })}
              </span>
              <span
                className={`text-xs font-mono font-medium ${
                  isProfit ? "text-cockpit-green/80" : "text-cockpit-red/80"
                }`}
              >
                ({isProfit ? "+" : ""}
                {mtmPct.toFixed(2)}%)
              </span>
            </div>
            <div className="text-[10px] font-mono text-cockpit-muted flex items-center justify-end space-x-2">
              <span>
                Realized:{" "}
                <span className={realized >= 0 ? "text-cockpit-green" : "text-cockpit-red"}>
                  ₹{realized.toLocaleString("en-IN", { minimumFractionDigits: 2 })}
                </span>
              </span>
              <span>•</span>
              <span>
                Unrealized:{" "}
                <span className={unrealized >= 0 ? "text-cockpit-green" : "text-cockpit-red"}>
                  ₹{unrealized.toLocaleString("en-IN", { minimumFractionDigits: 2 })}
                </span>
              </span>
            </div>
          </div>

          {/* Circuit Breakers Badge */}
          {status?.circuit_breaker_tripped ? (
            <div className="hidden sm:flex items-center space-x-1 px-2.5 py-1 rounded bg-cockpit-red/20 border border-cockpit-red text-cockpit-red text-xs font-mono animate-pulse">
              <AlertTriangle className="h-3.5 w-3.5" />
              <span>KILL SWITCH ACTIVE</span>
            </div>
          ) : status?.profit_floor_armed ? (
            <div className="hidden sm:flex items-center space-x-1 px-2.5 py-1 rounded bg-cockpit-green/20 border border-cockpit-green text-cockpit-green text-xs font-mono">
              <Shield className="h-3.5 w-3.5" />
              <span>PROFIT FLOOR +0.6%</span>
            </div>
          ) : null}

          {/* Action Buttons */}
          <div className="flex items-center space-x-2">
            {/* Start / Stop Engine Toggle */}
            {status?.is_running ? (
              <button
                onClick={onStopEngine}
                title="Stop Live Trading Engine"
                className="px-2.5 py-1.5 rounded-lg bg-amber-500/20 hover:bg-amber-500/30 border border-amber-500/50 text-amber-400 font-mono text-xs flex items-center space-x-1.5 transition"
              >
                <Square className="h-3.5 w-3.5 fill-current" />
                <span className="hidden md:inline font-semibold">Stop Engine</span>
              </button>
            ) : (
              <button
                onClick={onStartEngine}
                title="Start Trading Engine (Warmup & Stream)"
                className="px-2.5 py-1.5 rounded-lg bg-cockpit-green/20 hover:bg-cockpit-green/30 border border-cockpit-green/50 text-cockpit-green font-mono text-xs flex items-center space-x-1.5 transition"
              >
                <Play className="h-3.5 w-3.5 fill-current" />
                <span className="hidden md:inline font-semibold">Start Engine</span>
              </button>
            )}

            {/* Settings & Credentials Modal Trigger */}
            <button
              onClick={onOpenSettingsModal}
              title="Configure API Keys, Mode & Capital Tier"
              className="px-2.5 py-1.5 rounded-lg bg-cockpit-surface hover:bg-cockpit-hover border border-cockpit-border text-cockpit-text hover:text-white transition flex items-center space-x-1.5 text-xs font-medium font-mono"
            >
              <Sliders className="h-3.5 w-3.5 text-cockpit-blue" />
              <span className="hidden lg:inline">Settings & Keys</span>
            </button>

            <button
              onClick={onOpenReplayModal}
              title="Launch Historical Session Replay"
              className="p-2 rounded-lg bg-cockpit-surface hover:bg-cockpit-hover border border-cockpit-border text-cockpit-text transition flex items-center space-x-1.5 text-xs font-medium"
            >
              <Play className="h-3.5 w-3.5 text-cockpit-purple" />
              <span className="hidden md:inline">Replay</span>
            </button>

            <button
              onClick={onRefresh}
              title="Refresh Engine State"
              className="p-2 rounded-lg bg-cockpit-surface hover:bg-cockpit-hover border border-cockpit-border text-cockpit-muted hover:text-white transition"
            >
              <RefreshCw className="h-4 w-4" />
            </button>

            {/* Emergency Flatten Button */}
            <button
              onClick={() => setShowConfirmFlatten(true)}
              className="px-3 py-2 rounded-lg bg-cockpit-red/20 hover:bg-cockpit-red/30 border border-cockpit-red/60 text-cockpit-red hover:text-white transition flex items-center space-x-1.5 text-xs font-bold font-mono tracking-wider shadow-lg shadow-cockpit-red/10"
            >
              <Power className="h-4 w-4 text-cockpit-red" />
              <span>FLATTEN ALL</span>
            </button>
          </div>
        </div>
      </header>

      {/* Toast Notification */}
      {flattenToast && (
        <div className="fixed top-20 right-4 z-50 px-4 py-2.5 rounded-lg bg-cockpit-surface border border-cockpit-borderBright text-xs font-mono text-cockpit-text shadow-xl flex items-center space-x-2 animate-bounce">
          <Activity className="h-4 w-4 text-cockpit-blue" />
          <span>{flattenToast}</span>
        </div>
      )}

      {/* Emergency Flatten Confirmation Modal */}
      {showConfirmFlatten && (
        <div className="fixed inset-0 z-50 bg-black/80 backdrop-blur-sm flex items-center justify-center p-4">
          <div className="bg-cockpit-card border border-cockpit-red/60 rounded-xl max-w-md w-full p-6 shadow-2xl shadow-cockpit-red/20 space-y-4">
            <div className="flex items-center space-x-3 text-cockpit-red">
              <div className="h-10 w-10 rounded-full bg-cockpit-red/20 flex items-center justify-center">
                <AlertTriangle className="h-6 w-6" />
              </div>
              <div>
                <h3 className="font-bold text-lg text-white">EMERGENCY FLATTEN</h3>
                <p className="text-xs text-cockpit-muted">Confirmation Required</p>
              </div>
            </div>

            <p className="text-xs text-cockpit-text leading-relaxed">
              This action triggers an immediate market liquidation of <b>ALL open positions</b> across
              NSE equities via marketable limit orders and permanently locks the engine from generating new
              trades for the remainder of this session.
            </p>

            <div className="bg-cockpit-surface border border-cockpit-border p-3 rounded-lg text-xs font-mono space-y-1">
              <div className="flex justify-between text-cockpit-muted">
                <span>Active Positions:</span>
                <span className="text-white font-bold">{status?.open_positions_count ?? 0}</span>
              </div>
              <div className="flex justify-between text-cockpit-muted">
                <span>Current MTM:</span>
                <span className={isProfit ? "text-cockpit-green" : "text-cockpit-red"}>
                  ₹{dailyMtm.toFixed(2)}
                </span>
              </div>
            </div>

            <div className="flex items-center justify-end space-x-3 pt-2">
              <button
                disabled={isFlattening}
                onClick={() => setShowConfirmFlatten(false)}
                className="px-4 py-2 text-xs font-medium text-cockpit-muted hover:text-white transition"
              >
                Cancel
              </button>
              <button
                disabled={isFlattening}
                onClick={handleConfirmFlatten}
                className="px-4 py-2 rounded-lg bg-cockpit-red hover:bg-cockpit-red/80 text-white font-bold text-xs font-mono tracking-wider transition flex items-center space-x-2"
              >
                {isFlattening ? <RefreshCw className="h-4 w-4 animate-spin" /> : <Power className="h-4 w-4" />}
                <span>CONFIRM FLATTEN</span>
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
};
