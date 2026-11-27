"use client";

import React, { useState } from "react";
import { Download, FileSpreadsheet, RefreshCw } from "lucide-react";
import { getApiBaseUrl } from "@/hooks/useTrillionaireStream";

export type ReportPreset =
  | "today"
  | "yesterday"
  | "this_week"
  | "this_month"
  | "quarterly"
  | "half_yearly"
  | "yearly"
  | "five_years"
  | "custom";

const PRESET_OPTIONS: { value: ReportPreset; label: string }[] = [
  { value: "today", label: "Today's Report" },
  { value: "yesterday", label: "Yesterday's Report" },
  { value: "this_week", label: "This Week" },
  { value: "this_month", label: "This Month" },
  { value: "quarterly", label: "Quarterly" },
  { value: "half_yearly", label: "Half-Yearly" },
  { value: "yearly", label: "Yearly" },
  { value: "five_years", label: "5 Years" },
  { value: "custom", label: "Custom Date Range" },
];

interface DownloadReportsControlProps {
  compact?: boolean;
}

function parseFilename(contentDisposition: string | null, fallback: string): string {
  if (!contentDisposition) return fallback;
  const match = contentDisposition.match(/filename="?([^"]+)"?/i);
  return match?.[1] || fallback;
}

export const DownloadReportsControl: React.FC<DownloadReportsControlProps> = ({
  compact = false,
}) => {
  const todayIso = new Date().toLocaleDateString("en-CA", { timeZone: "Asia/Kolkata" });
  const [preset, setPreset] = useState<ReportPreset>("today");
  const [fromDate, setFromDate] = useState<string>(todayIso);
  const [toDate, setToDate] = useState<string>(todayIso);
  const [downloading, setDownloading] = useState(false);
  const [liveNote, setLiveNote] = useState<string | null>(null);
  const [errorNote, setErrorNote] = useState<string | null>(null);

  const downloadBothWorkbooks = async (
    targetPreset: ReportPreset,
    customFrom?: string,
    customTo?: string
  ) => {
    setDownloading(true);
    setErrorNote(null);
    const apiBase = getApiBaseUrl();

    const triggerOne = async (reportType: "evaluation" | "pl") => {
      const qs = new URLSearchParams({
        preset: targetPreset,
        report_type: reportType,
      });
      if (targetPreset === "custom") {
        if (customFrom) qs.set("from_date", customFrom);
        if (customTo) qs.set("to_date", customTo);
      }

      const resp = await fetch(`${apiBase}/api/v1/reports/export?${qs.toString()}`);
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${resp.status}`);
      }
      const isLive = resp.headers.get("X-Report-Is-Live") === "true";
      const liveTs = resp.headers.get("X-Report-Timestamp") || "";
      const filename = parseFilename(
        resp.headers.get("Content-Disposition"),
        reportType === "evaluation"
          ? `Trillionaire_Evaluation_Report_${todayIso}.xlsx`
          : `Trillionaire_PL_Report_${todayIso}.xlsx`
      );
      const blob = await resp.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      URL.revokeObjectURL(url);
      return { isLive, liveTs };
    };

    try {
      const evalMeta = await triggerOne("evaluation");
      // Slight tick spacing so browsers do not block simultaneous multi-file download prompts
      await new Promise((res) => setTimeout(res, 150));
      await triggerOne("pl");
      if (evalMeta.isLive) {
        setLiveNote(`Live intraday snapshot as of ${evalMeta.liveTs} (EOD lock at 15:35 IST)`);
      } else {
        setLiveNote(null);
      }
    } catch (e: any) {
      setErrorNote(e?.message || "Failed to download .xlsx reports");
    } finally {
      setDownloading(false);
    }
  };

  const handlePresetChange = (next: ReportPreset) => {
    setPreset(next);
    if (next !== "custom") {
      downloadBothWorkbooks(next);
    }
  };

  return (
    <div className={compact ? "space-y-1" : "space-y-1.5"}>
      <div className="flex flex-wrap items-center gap-1.5 text-[11px] font-mono">
        <select
          value={preset}
          onChange={(e) => handlePresetChange(e.target.value as ReportPreset)}
          aria-label="Report Period"
          className="bg-cockpit-surface border border-cockpit-border rounded px-2 py-1 text-[11px] text-white focus:outline-none focus:border-cyan-500"
        >
          {PRESET_OPTIONS.map((opt) => (
            <option key={opt.value} value={opt.value}>
              {opt.label}
            </option>
          ))}
        </select>

        {preset === "custom" && (
          <div className="flex items-center gap-1">
            <input
              type="date"
              value={fromDate}
              onChange={(e) => setFromDate(e.target.value)}
              aria-label="From Date"
              className="bg-cockpit-surface border border-cockpit-border rounded px-1.5 py-0.5 text-[10px] text-white focus:outline-none focus:border-cyan-500"
            />
            <span className="text-cockpit-muted text-[10px]">to</span>
            <input
              type="date"
              value={toDate}
              onChange={(e) => setToDate(e.target.value)}
              aria-label="To Date"
              className="bg-cockpit-surface border border-cockpit-border rounded px-1.5 py-0.5 text-[10px] text-white focus:outline-none focus:border-cyan-500"
            />
          </div>
        )}

        <button
          type="button"
          disabled={downloading}
          onClick={() => downloadBothWorkbooks(preset, fromDate, toDate)}
          title="Simultaneously download 5-tab Evaluation Report (.xlsx) & 2-tab P&L Report (.xlsx)"
          className="px-2.5 py-1 rounded bg-emerald-500/15 hover:bg-emerald-500/30 border border-emerald-500/40 text-emerald-300 text-[11px] font-bold flex items-center space-x-1.5 transition"
        >
          {downloading ? (
            <RefreshCw className="h-3 w-3 animate-spin" />
          ) : (
            <FileSpreadsheet className="h-3 w-3 text-emerald-400" />
          )}
          <span>{downloading ? "Exporting..." : "Download Reports (.xlsx)"}</span>
          {!downloading && <Download className="h-3 w-3 opacity-80" />}
        </button>
      </div>

      {liveNote && (
        <div className="text-[9px] font-mono text-amber-300/90">
          ● {liveNote}
        </div>
      )}
      {errorNote && (
        <div className="text-[9px] font-mono text-rose-400">
          ! {errorNote}
        </div>
      )}
    </div>
  );
};
