"use client";

import React, { useEffect, useState } from "react";
import {
  Activity,
  AlertTriangle,
  Clock,
  Pause,
  Play,
  Power,
  RefreshCw,
  Shield,
  Sliders,
  Square,
} from "lucide-react";
import { EngineStatus } from "@/types/telemetry";
import { isIstCutoffWindow } from "@/lib/istTime";
import { DownloadReportsControl } from "@/components/DownloadReportsControl";

interface EngineControlsPanelProps {
  status: EngineStatus | null;
  liveDailyMtm: number;
  onStartEngine: () => Promise<void>;
  onStopEngine: () => Promise<void>;
  onPauseTrading?: () => Promise<void>;
  onResumeTrading?: () => Promise<void>;
  onOpenSettingsModal: (initialTab?: string) => void;
  onOpenReplayModal: () => void;
  onRefresh: () => void;
  onEmergencyFlatten: () => Promise<{ success: boolean; message: string }>;
}

export const EngineControlsPanel: React.FC<EngineControlsPanelProps> = ({
  status,
  liveDailyMtm,
  onStartEngine,
  onStopEngine,
  onPauseTrading,
  onResumeTrading,
  onOpenSettingsModal,
  onOpenReplayModal,
  onRefresh,
  onEmergencyFlatten,
}) => {
  const [cutoffActive, setCutoffActive] = useState(false);
  const [showConfirmFlatten, setShowConfirmFlatten] = useState(false);
  const [isFlattening, setIsFlattening] = useState(false);
  const [flattenToast, setFlattenToast] = useState<string | null>(null);

  useEffect(() => {
    const sync = () => setCutoffActive(isIstCutoffWindow());
    sync();
    const timer = setInterval(sync, 15000);
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

  const isProfit = liveDailyMtm >= 0;

  return (
    <>
      <div className="h-full flex flex-col bg-cockpit-card rounded-xl border border-cockpit-border overflow-hidden select-none">
        {/* Card Header */}
        <div className="h-10 shrink-0 border-b border-cockpit-border px-3 flex items-center justify-between">
          <div className="flex items-center space-x-2">
            <Shield className="h-3.5 w-3.5 text-cockpit-blue" />
            <span className="font-bold text-xs uppercase tracking-wider text-white">
              Engine Controls & Quick Actions
            </span>
          </div>
          <span
            className={`text-[10px] px-1.5 py-0.5 rounded font-mono font-semibold border ${
              status?.is_running
                ? status?.trading_paused
                  ? "bg-amber-500/15 border-amber-500/40 text-amber-300"
                  : "bg-emerald-500/15 border-emerald-500/40 text-emerald-400"
                : "bg-slate-800 border-slate-700 text-slate-400"
            }`}
          >
            {status?.is_running ? (status?.trading_paused ? "PAUSED" : "RUNNING") : "STOPPED"}
          </span>
        </div>

        {/* Controls Body */}
        <div className="flex-1 min-h-0 overflow-y-auto p-3 flex flex-col justify-between gap-2.5">
          {/* Session Cutoff Banner */}
          {cutoffActive ? (
            <div className="px-2.5 py-2 rounded-lg bg-amber-500/10 border border-amber-500/40 text-amber-300 text-[10px] font-mono font-semibold flex items-center space-x-2">
              <Clock className="h-3.5 w-3.5 text-amber-400 shrink-0" />
              <span>NO NEW ENTRIES AFTER 14:30 IST • EOD EXIT 15:10</span>
            </div>
          ) : (
            <div className="px-2.5 py-1.5 rounded-lg bg-cockpit-surface/80 border border-cockpit-border text-[10px] font-mono text-cockpit-muted flex items-center justify-between">
              <span>Mode: <b className="text-white">{status?.mode || "PAPER"}</b></span>
              <span>Entry 09:25–14:30 • Exit 15:10 IST</span>
            </div>
          )}

          {/* Action Grid */}
          <div className="grid grid-cols-2 gap-2 text-xs font-mono">
            {status?.is_running ? (
              status?.trading_paused ? (
                <button
                  type="button"
                  onClick={onResumeTrading}
                  className="px-2.5 py-2 rounded-lg bg-emerald-500/20 hover:bg-emerald-500/30 border border-emerald-500/50 text-emerald-300 font-semibold flex items-center justify-center space-x-1.5 transition"
                >
                  <Play className="h-3.5 w-3.5 fill-current" />
                  <span>Resume Trading</span>
                </button>
              ) : (
                <button
                  type="button"
                  onClick={onPauseTrading}
                  className="px-2.5 py-2 rounded-lg bg-amber-500/20 hover:bg-amber-500/30 border border-amber-500/50 text-amber-300 font-semibold flex items-center justify-center space-x-1.5 transition"
                >
                  <Pause className="h-3.5 w-3.5 fill-current" />
                  <span>Pause Trading</span>
                </button>
              )
            ) : (
              <button
                type="button"
                onClick={onStartEngine}
                className="px-2.5 py-2 rounded-lg bg-cockpit-green/20 hover:bg-cockpit-green/30 border border-cockpit-green/50 text-cockpit-green font-semibold flex items-center justify-center space-x-1.5 transition"
              >
                <Play className="h-3.5 w-3.5 fill-current" />
                <span>Start Engine</span>
              </button>
            )}

            <button
              type="button"
              onClick={() => onOpenSettingsModal()}
              className="px-2.5 py-2 rounded-lg bg-cockpit-surface hover:bg-cockpit-hover border border-cockpit-border text-cockpit-text hover:text-white font-medium flex items-center justify-center space-x-1.5 transition"
            >
              <Sliders className="h-3.5 w-3.5 text-cockpit-blue" />
              <span>Settings & Keys</span>
            </button>

            <button
              type="button"
              onClick={onOpenReplayModal}
              className="px-2.5 py-2 rounded-lg bg-cockpit-surface hover:bg-cockpit-hover border border-cockpit-border text-cockpit-text hover:text-white font-medium flex items-center justify-center space-x-1.5 transition"
            >
              <Play className="h-3.5 w-3.5 text-cockpit-purple" />
              <span>Replay</span>
            </button>

            <div className="flex items-center space-x-1.5">
              <button
                type="button"
                onClick={onRefresh}
                className="flex-1 px-2.5 py-2 rounded-lg bg-cockpit-surface hover:bg-cockpit-hover border border-cockpit-border text-cockpit-muted hover:text-white font-medium flex items-center justify-center space-x-1.5 transition"
              >
                <RefreshCw className="h-3.5 w-3.5" />
                <span>Refresh</span>
              </button>
              {status?.is_running && (
                <button
                  type="button"
                  onClick={onStopEngine}
                  title="Stop Live Trading Engine"
                  className="p-2 rounded-lg bg-cockpit-surface hover:bg-rose-500/20 border border-cockpit-border hover:border-rose-500/40 text-cockpit-muted hover:text-rose-400 transition"
                >
                  <Square className="h-3.5 w-3.5 fill-current" />
                </button>
              )}
            </div>
          </div>

          {/* Emergency Flatten Button */}
          <button
            type="button"
            onClick={() => setShowConfirmFlatten(true)}
            className="w-full py-2.5 px-3 rounded-lg bg-cockpit-red/20 hover:bg-cockpit-red/30 border border-cockpit-red/60 text-cockpit-red hover:text-white transition flex items-center justify-center space-x-2 text-xs font-bold font-mono tracking-wider shadow-lg shadow-cockpit-red/10"
          >
            <Power className="h-4 w-4 text-cockpit-red" />
            <span>FLATTEN ALL POSITIONS</span>
          </button>

          {/* Unified Date-Wise Multi-Tab XLSX Reports Control */}
          <div className="pt-2 border-t border-cockpit-border/70">
            <div className="text-[10px] font-mono uppercase text-cockpit-muted font-bold mb-1">
              Reports Export (.xlsx)
            </div>
            <DownloadReportsControl compact />
          </div>
        </div>
      </div>

      {flattenToast && (
        <div className="fixed top-20 right-4 z-50 px-4 py-2.5 rounded-lg bg-cockpit-surface border border-cockpit-borderBright text-xs font-mono text-cockpit-text shadow-xl flex items-center space-x-2 animate-bounce">
          <Activity className="h-4 w-4 text-cockpit-blue" />
          <span>{flattenToast}</span>
        </div>
      )}

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
                <span className={isProfit ? "text-emerald-400" : "text-rose-400"}>
                  ₹{liveDailyMtm.toFixed(2)}
                </span>
              </div>
            </div>

            <div className="flex items-center justify-end space-x-3 pt-2">
              <button
                type="button"
                disabled={isFlattening}
                onClick={() => setShowConfirmFlatten(false)}
                className="px-4 py-2 text-xs font-medium text-cockpit-muted hover:text-white transition"
              >
                Cancel
              </button>
              <button
                type="button"
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
