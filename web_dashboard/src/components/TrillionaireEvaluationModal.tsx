"use client";

import React, { useEffect, useMemo, useState } from "react";
import { Filter, RefreshCw, Search, X } from "lucide-react";
import { FunnelLog, FunnelStage } from "@/types/telemetry";
import { getApiBaseUrl } from "@/hooks/useTrillionaireStream";
import { DownloadReportsControl } from "@/components/DownloadReportsControl";

interface TrillionaireEvaluationModalProps {
  isOpen: boolean;
  initialStage: FunnelStage;
  liveLogs: FunnelLog[];
  selectedSymbol?: string;
  onSelectSymbol?: (symbol: string) => void;
  onClose: () => void;
}

const STAGE_TABS: {
  key: FunnelStage;
  label: string;
  activeClass: string;
  badgeClass: string;
}[] = [
  {
    key: "ALL",
    label: "All",
    activeClass: "bg-slate-700/60 text-white border-slate-500",
    badgeClass: "bg-slate-800 text-slate-300",
  },
  {
    key: "EVAL",
    label: "Eval",
    activeClass: "bg-blue-500/20 text-blue-300 border-blue-500/50",
    badgeClass: "bg-blue-500/20 text-blue-300",
  },
  {
    key: "STOCK",
    label: "Stock",
    activeClass: "bg-cyan-500/20 text-cyan-300 border-cyan-500/50",
    badgeClass: "bg-cyan-500/20 text-cyan-300",
  },
  {
    key: "SECTOR",
    label: "Sector",
    activeClass: "bg-amber-500/20 text-amber-300 border-amber-500/50",
    badgeClass: "bg-amber-500/20 text-amber-300",
  },
  {
    key: "SIGNAL",
    label: "Signal",
    activeClass: "bg-emerald-500/20 text-emerald-300 border-emerald-500/50",
    badgeClass: "bg-emerald-500/20 text-emerald-300",
  },
];

function getStageBadgeClass(stage: string): string {
  switch (stage.toUpperCase()) {
    case "EVAL":
      return "bg-blue-500/20 text-blue-300 border-blue-500/40";
    case "STOCK":
      return "bg-cyan-500/20 text-cyan-300 border-cyan-500/40";
    case "SECTOR":
      return "bg-amber-500/20 text-amber-300 border-amber-500/40";
    case "SIGNAL":
      return "bg-emerald-500/20 text-emerald-300 border-emerald-500/40";
    default:
      return "bg-slate-700/40 text-slate-300 border-slate-600/40";
  }
}

export const TrillionaireEvaluationModal: React.FC<TrillionaireEvaluationModalProps> = ({
  isOpen,
  initialStage,
  liveLogs,
  selectedSymbol,
  onSelectSymbol,
  onClose,
}) => {
  const [stage, setStage] = useState<FunnelStage>(initialStage);
  const [searchQuery, setSearchQuery] = useState("");
  const [passedOnly, setPassedOnly] = useState(false);
  const [restLogs, setRestLogs] = useState<FunnelLog[]>([]);
  const [restCounts, setRestCounts] = useState<Record<string, number> | null>(null);
  const [loading, setLoading] = useState(false);

  const todayIso = new Date().toLocaleDateString("en-CA", { timeZone: "Asia/Kolkata" });

  useEffect(() => {
    setStage(initialStage || "ALL");
  }, [initialStage, isOpen]);

  const fetchFunnelLogs = async () => {
    setLoading(true);
    try {
      const apiBase = getApiBaseUrl();
      const resp = await fetch(`${apiBase}/api/v1/funnel/logs?stage=ALL&limit=1000`);
      if (resp.ok) {
        const data = await resp.json();
        if (Array.isArray(data.logs)) setRestLogs(data.logs);
        if (data.counts) setRestCounts(data.counts);
      }
    } catch {
      // keep live stream logs
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (!isOpen) return;
    fetchFunnelLogs();
  }, [isOpen]);

  useEffect(() => {
    if (!isOpen) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [isOpen, onClose]);

  const mergedLogs = useMemo(() => {
    const map = new Map<string, FunnelLog>();
    for (const r of restLogs) map.set(r.id, r);
    for (const r of liveLogs) map.set(r.id, r);
    return Array.from(map.values()).sort((a, b) =>
      String(b.full_timestamp_ist || `${b.date_ist}T${b.timestamp_ist}`).localeCompare(
        String(a.full_timestamp_ist || `${a.date_ist}T${a.timestamp_ist}`)
      )
    );
  }, [restLogs, liveLogs]);

  const counts = useMemo(() => {
    if (mergedLogs.length > 0) {
      return {
        ALL: mergedLogs.length,
        EVAL: mergedLogs.filter((l) => l.stage === "EVAL").length,
        STOCK: mergedLogs.filter((l) => l.stage === "STOCK").length,
        SECTOR: mergedLogs.filter((l) => l.stage === "SECTOR").length,
        SIGNAL: mergedLogs.filter((l) => l.stage === "SIGNAL").length,
      };
    }
    return restCounts || { ALL: 0, EVAL: 0, STOCK: 0, SECTOR: 0, SIGNAL: 0 };
  }, [mergedLogs, restCounts]);

  const visibleLogs = useMemo(() => {
    const q = searchQuery.trim().toLowerCase();
    return mergedLogs.filter((row) => {
      if (stage !== "ALL" && row.stage !== stage) return false;
      if (passedOnly && !row.passed) return false;
      if (
        q &&
        !row.symbol.toLowerCase().includes(q) &&
        !row.parent_index.toLowerCase().includes(q) &&
        !row.reason.toLowerCase().includes(q)
      ) {
        return false;
      }
      return true;
    });
  }, [mergedLogs, stage, passedOnly, searchQuery]);

  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 z-50 bg-black/80 backdrop-blur-md flex items-center justify-center p-3 sm:p-5 select-none">
      <div className="bg-cockpit-card border border-cockpit-borderBright rounded-xl w-full max-w-6xl h-[88vh] flex flex-col shadow-2xl overflow-hidden">
        {/* Header */}
        <div className="h-14 px-5 border-b border-cockpit-border bg-cockpit-surface/80 flex items-center justify-between gap-3 shrink-0">
          <div className="flex items-center space-x-2.5 min-w-0">
            <Filter className="h-4 w-4 text-cyan-400 shrink-0" />
            <h2 className="text-sm font-bold text-white tracking-wide font-mono truncate">
              TRILLIONAIRE EVALUATION REPORT • {todayIso}
            </h2>
          </div>

          <div className="flex items-center space-x-2">
            <button
              type="button"
              onClick={fetchFunnelLogs}
              title="Refresh Funnel Evaluation Logs"
              className="p-1.5 rounded-lg bg-cockpit-surface hover:bg-cockpit-hover border border-cockpit-border text-cockpit-muted hover:text-white transition"
            >
              <RefreshCw className={`h-4 w-4 ${loading ? "animate-spin text-cyan-400" : ""}`} />
            </button>
            <button
              type="button"
              onClick={onClose}
              className="p-1.5 rounded-lg text-cockpit-muted hover:text-white hover:bg-cockpit-hover transition"
            >
              <X className="h-5 w-5" />
            </button>
          </div>
        </div>

        {/* Filter Tabs, Search & Download Bar */}
        <div className="p-3 border-b border-cockpit-border bg-slate-950/60 flex flex-wrap items-center justify-between gap-2.5 shrink-0">
          <div className="flex flex-wrap items-center gap-1.5">
            {STAGE_TABS.map((t) => {
              const isActive = stage === t.key;
              return (
                <button
                  key={t.key}
                  type="button"
                  onClick={() => setStage(t.key)}
                  className={`px-2.5 py-1 rounded-lg text-xs font-mono font-semibold border transition flex items-center space-x-1.5 ${
                    isActive
                      ? t.activeClass
                      : "bg-cockpit-surface border-cockpit-border text-cockpit-muted hover:text-white"
                  }`}
                >
                  <span>{t.label}</span>
                  <span className={`px-1.5 py-0.2 rounded text-[10px] ${t.badgeClass}`}>
                    {counts[t.key] ?? 0}
                  </span>
                </button>
              );
            })}
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <div className="relative">
              <Search className="h-3 w-3 absolute left-2 top-2 text-cockpit-muted" />
              <input
                type="text"
                placeholder="Symbol / reason..."
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                className="pl-7 pr-6 py-1 text-xs font-mono bg-cockpit-surface border border-cockpit-border rounded text-white placeholder-cockpit-muted/60 focus:outline-none focus:border-cyan-500 w-40 sm:w-48"
              />
              {searchQuery && (
                <button
                  type="button"
                  onClick={() => setSearchQuery("")}
                  className="absolute right-1.5 top-1.5 text-cockpit-muted hover:text-white"
                >
                  <X className="h-3 w-3" />
                </button>
              )}
            </div>

            <label className="inline-flex items-center space-x-1.5 text-xs font-mono text-slate-300 cursor-pointer px-2 py-1 rounded bg-cockpit-surface border border-cockpit-border">
              <input
                type="checkbox"
                checked={passedOnly}
                onChange={(e) => setPassedOnly(e.target.checked)}
                className="rounded border-slate-600 bg-slate-900 text-emerald-500 focus:ring-0"
              />
              <span>Passed Only</span>
            </label>

            <DownloadReportsControl compact />
          </div>
        </div>

        {/* Sticky Evaluation Table */}
        <div className="flex-1 min-h-0 overflow-auto">
          {visibleLogs.length === 0 ? (
            <div className="h-full flex flex-col items-center justify-center text-cockpit-muted text-xs font-mono p-6">
              <span>No M5 funnel evaluation records match the active filter yet.</span>
            </div>
          ) : (
            <table className="w-full text-left text-xs font-mono">
              <thead className="sticky top-0 z-10 bg-slate-950/95 backdrop-blur border-b border-cockpit-border text-[10px] text-cockpit-muted uppercase">
                <tr>
                  <th className="py-2 px-2.5">Date</th>
                  <th className="py-2 px-2.5">Time IST</th>
                  <th className="py-2 px-2">Stage</th>
                  <th className="py-2 px-2.5">Symbol</th>
                  <th className="py-2 px-2">Sector</th>
                  <th className="py-2 px-2">Regime</th>
                  <th className="py-2 px-2 text-right">Close</th>
                  <th className="py-2 px-2 text-right">VWAP</th>
                  <th className="py-2 px-2 text-right">RVOL</th>
                  <th className="py-2 px-3">Diagnostic Reason</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-cockpit-border/35">
                {visibleLogs.map((row) => {
                  const isSel = selectedSymbol === row.symbol;
                  return (
                    <tr
                      key={row.id}
                      onClick={() => onSelectSymbol?.(row.symbol)}
                      className={`cursor-pointer transition ${
                        isSel
                          ? "bg-cockpit-blue/15 border-l-2 border-cockpit-blue"
                          : "hover:bg-cockpit-surface/60"
                      }`}
                    >
                      <td className="py-1.5 px-2.5 text-cockpit-muted whitespace-nowrap">
                        {row.date_ist}
                      </td>
                      <td className="py-1.5 px-2.5 text-slate-300 whitespace-nowrap">
                        {row.timestamp_ist}
                      </td>
                      <td className="py-1.5 px-2">
                        <span
                          className={`px-1.5 py-0.5 rounded text-[10px] font-bold border ${getStageBadgeClass(
                            row.stage
                          )}`}
                        >
                          {row.stage}
                        </span>
                      </td>
                      <td className="py-1.5 px-2.5 font-bold text-white whitespace-nowrap">
                        {row.symbol}
                      </td>
                      <td className="py-1.5 px-2 text-slate-400 whitespace-nowrap text-[11px]">
                        {row.parent_index}
                      </td>
                      <td className="py-1.5 px-2 text-slate-400 whitespace-nowrap text-[10px]">
                        {row.regime}
                      </td>
                      <td className="py-1.5 px-2 text-right font-semibold text-white tabular-nums">
                        ₹{Number(row.close || 0).toFixed(2)}
                      </td>
                      <td className="py-1.5 px-2 text-right text-slate-300 tabular-nums">
                        {row.vwap !== null && row.vwap !== undefined
                          ? `₹${Number(row.vwap).toFixed(2)}`
                          : "—"}
                      </td>
                      <td className="py-1.5 px-2 text-right text-cyan-300 tabular-nums">
                        {Number(row.rvol || 0).toFixed(2)}x
                      </td>
                      <td className="py-1.5 px-3 text-[11px]">
                        <span
                          className={
                            row.passed ? "text-emerald-300" : "text-slate-300"
                          }
                        >
                          {row.passed ? "✓ " : "• "}
                          {row.reason}
                        </span>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </div>
  );
};
