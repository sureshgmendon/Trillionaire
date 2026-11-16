"use client";

import React, { useState } from "react";
import { Calendar, FastForward, Play, RefreshCw, X } from "lucide-react";

interface ReplayControlModalProps {
  isOpen: boolean;
  onClose: () => void;
  onTriggerReplay: (params: {
    trading_date?: string;
    speed: string;
    symbols?: string[];
  }) => Promise<{ success: boolean; message: string }>;
}

export const ReplayControlModal: React.FC<ReplayControlModalProps> = ({
  isOpen,
  onClose,
  onTriggerReplay,
}) => {
  const [tradingDate, setTradingDate] = useState<string>("");
  const [speed, setSpeed] = useState<string>("MAX_SPEED");
  const [selectedSymbols, setSelectedSymbols] = useState<string>("RELIANCE, HDFCBANK, INFY");
  const [isRunning, setIsRunning] = useState<boolean>(false);
  const [resultMsg, setResultMsg] = useState<string | null>(null);

  if (!isOpen) return null;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setIsRunning(true);
    setResultMsg(null);

    const symbolsArray = selectedSymbols
      ? selectedSymbols.split(",").map((s) => s.trim().toUpperCase()).filter(Boolean)
      : undefined;

    const res = await onTriggerReplay({
      trading_date: tradingDate || undefined,
      speed,
      symbols: symbolsArray,
    });

    setIsRunning(false);
    setResultMsg(res.message);
  };

  return (
    <div className="fixed inset-0 z-50 bg-black/80 backdrop-blur-sm flex items-center justify-center p-4 select-none">
      <div className="bg-cockpit-card border border-cockpit-borderBright rounded-xl max-w-md w-full p-6 shadow-2xl space-y-5">
        {/* Modal Header */}
        <div className="flex items-center justify-between border-b border-cockpit-border pb-3">
          <div className="flex items-center space-x-2">
            <div className="h-8 w-8 rounded-lg bg-cockpit-purple/20 border border-cockpit-purple/40 flex items-center justify-center">
              <FastForward className="h-4 w-4 text-cockpit-purple" />
            </div>
            <div>
              <h3 className="font-bold text-sm text-white uppercase tracking-wider">
                Historical Session Replay
              </h3>
              <p className="text-[11px] text-cockpit-muted">TickReplayer Event Stream</p>
            </div>
          </div>
          <button
            onClick={onClose}
            className="p-1 rounded text-cockpit-muted hover:text-white transition"
          >
            <X className="h-5 w-5" />
          </button>
        </div>

        {/* Modal Form */}
        <form onSubmit={handleSubmit} className="space-y-4 text-xs font-mono">
          {/* Trading Date */}
          <div className="space-y-1">
            <label className="text-cockpit-muted flex items-center space-x-1.5">
              <Calendar className="h-3.5 w-3.5" />
              <span>Target Trading Date (Optional):</span>
            </label>
            <input
              type="date"
              value={tradingDate}
              onChange={(e) => setTradingDate(e.target.value)}
              className="w-full bg-cockpit-surface border border-cockpit-border rounded px-3 py-2 text-white focus:outline-none focus:border-cockpit-purple"
            />
            <span className="text-[10px] text-cockpit-muted">
              Leave blank to replay first available date in dataset.
            </span>
          </div>

          {/* Replay Speed Selector */}
          <div className="space-y-1.5">
            <label className="text-cockpit-muted">Replay Speed:</label>
            <div className="grid grid-cols-4 gap-2">
              {["1x", "10x", "50x", "MAX_SPEED"].map((s) => (
                <button
                  type="button"
                  key={s}
                  onClick={() => setSpeed(s)}
                  className={`py-1.5 rounded text-center font-bold border transition ${
                    speed === s
                      ? "bg-cockpit-purple/20 border-cockpit-purple text-cockpit-purple"
                      : "bg-cockpit-surface border-cockpit-border text-cockpit-muted hover:text-white"
                  }`}
                >
                  {s}
                </button>
              ))}
            </div>
          </div>

          {/* Symbols */}
          <div className="space-y-1">
            <label className="text-cockpit-muted">Instruments (comma-separated):</label>
            <input
              type="text"
              value={selectedSymbols}
              onChange={(e) => setSelectedSymbols(e.target.value)}
              placeholder="e.g. RELIANCE, TCS, INFY"
              className="w-full bg-cockpit-surface border border-cockpit-border rounded px-3 py-2 text-white focus:outline-none focus:border-cockpit-purple"
            />
          </div>

          {/* Result Message */}
          {resultMsg && (
            <div className="p-2.5 rounded bg-cockpit-surface border border-cockpit-borderBright text-cockpit-text text-[11px]">
              {resultMsg}
            </div>
          )}

          {/* Footer Actions */}
          <div className="flex items-center justify-end space-x-2 pt-2 border-t border-cockpit-border">
            <button
              type="button"
              disabled={isRunning}
              onClick={onClose}
              className="px-4 py-2 text-cockpit-muted hover:text-white transition"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={isRunning}
              className="px-4 py-2 rounded-lg bg-cockpit-purple hover:bg-cockpit-purple/80 text-white font-bold transition flex items-center space-x-1.5"
            >
              {isRunning ? (
                <>
                  <RefreshCw className="h-4 w-4 animate-spin" />
                  <span>Replaying...</span>
                </>
              ) : (
                <>
                  <Play className="h-4 w-4" />
                  <span>Start Replay</span>
                </>
              )}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
};
