"use client";

import React, { useState } from "react";
import { AlertCircle, CheckCircle2, Search, ShieldAlert, Zap } from "lucide-react";
import { StockProfile, Tick } from "@/types/telemetry";

interface WatchlistAndSentinelMatrixProps {
  profiles: StockProfile[];
  ticks: Record<string, Tick>;
  selectedSymbol: string;
  onSelectSymbol: (symbol: string) => void;
  onOverrideLock?: (symbol: string) => Promise<{ success: boolean }>;
}

export const WatchlistAndSentinelMatrix: React.FC<WatchlistAndSentinelMatrixProps> = ({
  profiles,
  ticks,
  selectedSymbol,
  onSelectSymbol,
  onOverrideLock,
}) => {
  const [searchQuery, setSearchQuery] = useState("");
  const [overridingSymbol, setOverridingSymbol] = useState<string | null>(null);

  const handleUnlock = async (e: React.MouseEvent, symbol: string) => {
    e.stopPropagation();
    if (!onOverrideLock) return;
    setOverridingSymbol(symbol);
    try {
      await onOverrideLock(symbol);
    } finally {
      setOverridingSymbol(null);
    }
  };

  const filteredProfiles = profiles.filter((p) => {
    const q = searchQuery.toLowerCase();
    return (
      p.tradingsymbol.toLowerCase().includes(q) ||
      p.parent_index.toLowerCase().includes(q) ||
      p.cluster_regime.toLowerCase().includes(q)
    );
  });

  return (
    <div className="h-full flex flex-col bg-cockpit-card rounded-xl border border-cockpit-border overflow-hidden select-none">
      {/* Header & Search Filter */}
      <div className="h-11 shrink-0 border-b border-cockpit-border px-3 flex items-center justify-between gap-2">
        <div className="flex items-center space-x-1.5 min-w-0">
          <Zap className="h-4 w-4 text-cockpit-blue shrink-0" />
          <span className="font-bold text-xs uppercase tracking-wider text-white truncate">
            Watchlist
          </span>
          <span className="text-[10px] font-mono px-1.5 py-0.5 rounded bg-cockpit-surface text-cockpit-muted shrink-0">
            {profiles.length}
          </span>
        </div>

        <div className="relative w-28 sm:w-32 shrink-0">
          <Search className="h-3.5 w-3.5 absolute left-2 top-2 text-cockpit-muted" />
          <input
            type="text"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder="Search..."
            className="w-full pl-7 pr-2 py-1 text-xs font-mono bg-cockpit-surface border border-cockpit-border rounded text-cockpit-text placeholder-cockpit-muted focus:outline-none focus:border-cockpit-blue"
          />
        </div>
      </div>

      {/* Table Content */}
      <div className="flex-1 min-h-0 overflow-y-auto">
        <table className="w-full text-left text-xs font-mono">
          <thead className="sticky top-0 z-10 bg-slate-950/95 backdrop-blur border-b border-cockpit-border text-[10px] text-cockpit-muted uppercase tracking-wider">
            <tr>
              <th className="py-2 px-3">Symbol</th>
              <th className="py-2 px-2">Regime</th>
              <th className="py-2 px-2 text-right">LTP</th>
              <th className="py-2 px-2 text-center">PF</th>
              <th className="py-2 px-3 text-center">Sentinel</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-cockpit-border/40">
            {filteredProfiles.map((p) => {
              const isSelected = p.tradingsymbol === selectedSymbol;
              const tick = ticks[p.tradingsymbol];
              const ltp = tick?.ltp || 0;
              const sentinel = p.sentinel;
              const status = sentinel?.status || "NORMAL";

              return (
                <tr
                  key={p.tradingsymbol}
                  onClick={() => onSelectSymbol(p.tradingsymbol)}
                  className={`cursor-pointer transition-colors ${
                    isSelected
                      ? "bg-cockpit-blue/15 border-l-2 border-cockpit-blue text-white"
                      : "hover:bg-cockpit-surface/70 text-cockpit-text"
                  }`}
                >
                  {/* Symbol & Index */}
                  <td className="py-2 px-3">
                    <div className="font-bold text-xs">{p.tradingsymbol}</div>
                    <div className="text-[10px] text-cockpit-muted">{p.parent_index}</div>
                  </td>

                  {/* Cluster Regime */}
                  <td className="py-2 px-2">
                    <span
                      className={`text-[9px] px-1.5 py-0.5 rounded uppercase font-semibold ${
                        p.cluster_regime === "TREND_MOMENTUM"
                          ? "bg-emerald-500/10 text-emerald-400 border border-emerald-500/20"
                          : p.cluster_regime === "MEAN_REVERSION"
                          ? "bg-cyan-500/10 text-cyan-400 border border-cyan-500/20"
                          : "bg-purple-500/10 text-purple-400 border border-purple-500/20"
                      }`}
                    >
                      {p.cluster_regime === "TREND_MOMENTUM"
                        ? "TREND"
                        : p.cluster_regime === "MEAN_REVERSION"
                        ? "MEAN-REV"
                        : "RVOL-BRK"}
                    </span>
                  </td>

                  {/* LTP */}
                  <td className="py-2 px-2 text-right font-mono font-bold">
                    {ltp > 0 ? `₹${ltp.toFixed(2)}` : "—"}
                  </td>

                  {/* OOS Profit Factor */}
                  <td className="py-2 px-2 text-center text-cockpit-muted">
                    {p.oos_profit_factor > 0 ? p.oos_profit_factor.toFixed(2) : "—"}
                  </td>

                  {/* Sentinel Status Badge with Interactive Tooltip & Override */}
                  <td className="py-2 px-3 text-center">
                    <div className="group relative inline-block">
                      {status === "NORMAL" && p.is_active ? (
                        <span className="inline-flex items-center space-x-1 px-1.5 py-0.5 rounded text-[10px] font-medium bg-cockpit-green/10 text-cockpit-green border border-cockpit-green/20">
                          <CheckCircle2 className="h-3 w-3" />
                          <span>1.0x</span>
                        </span>
                      ) : status === "CAUTION_HALF_SIZE" ? (
                        <span className="inline-flex items-center space-x-1 px-1.5 py-0.5 rounded text-[10px] font-medium bg-cockpit-amber/10 text-cockpit-amber border border-cockpit-amber/20">
                          <AlertCircle className="h-3 w-3" />
                          <span>0.5x</span>
                        </span>
                      ) : (
                        <span className="inline-flex items-center space-x-1 px-1.5 py-0.5 rounded text-[10px] font-bold bg-cockpit-red/10 text-cockpit-red border border-cockpit-red/30">
                          <ShieldAlert className="h-3 w-3" />
                          <span>LOCK</span>
                        </span>
                      )}

                      {/* Tooltip on Hover (supports 1-click Manual Sentinel Override) */}
                      {(sentinel?.reason || !p.is_active || status === "DO_NOT_TRADE") && (
                        <div className="absolute right-0 top-full pt-1 hidden group-hover:block z-50 w-60 text-left">
                          <div className="p-2.5 rounded bg-cockpit-surface border border-cockpit-borderBright text-[10px] text-cockpit-text shadow-xl space-y-1.5">
                            <div className="font-bold text-white">
                              {status === "DO_NOT_TRADE" ? "Sentinel Lockout" : !p.is_active ? "PF Threshold Lockout" : "Sentinel Triage"}
                            </div>
                            <div className="leading-snug text-cockpit-muted">
                              {sentinel?.reason ||
                                (!p.is_active
                                  ? `Disabled: OOS Profit Factor (${p.oos_profit_factor.toFixed(2)}) below 1.0 threshold.`
                                  : "Blocked by intraday risk triage.")}
                            </div>
                            {status === "DO_NOT_TRADE" && onOverrideLock && (
                              <button
                                type="button"
                                onClick={(e) => handleUnlock(e, p.tradingsymbol)}
                                disabled={overridingSymbol === p.tradingsymbol}
                                className="w-full mt-1 px-2 py-1 rounded bg-cyan-500/20 hover:bg-cyan-500/30 border border-cyan-500/50 text-cyan-300 font-bold text-[10px] transition"
                              >
                                {overridingSymbol === p.tradingsymbol ? "Unlocking..." : "Manual Unlock (1.0x)"}
                              </button>
                            )}
                          </div>
                        </div>
                      )}
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
};
