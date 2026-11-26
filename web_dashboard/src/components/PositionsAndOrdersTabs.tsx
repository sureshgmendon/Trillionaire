"use client";

import React, { useState, useMemo, useEffect } from "react";
import {
  Activity,
  Brain,
  Check,
  Download,
  ListOrdered,
  Maximize2,
  Minimize2,
  Pencil,
  PieChart,
  Search,
  Sparkles,
  X,
} from "lucide-react";
import { isIstCutoffWindow } from "@/lib/istTime";
import { computeOpenPositionMetrics } from "@/lib/pnl";
import {
  AnomalyAlert,
  ClosedTrade,
  Order,
  Position,
  RiskAlert,
  Signal,
  Tick,
  TradeAutopsyReport,
} from "@/types/telemetry";

export interface PositionsAndOrdersTabsProps {
  positions: Position[];
  orders: Order[];
  closedTrades?: ClosedTrade[];
  ticks?: Record<string, Tick>;
  selectedSymbol?: string;
  onSelectSymbol?: (symbol: string) => void;
  liveRealizedPnl?: number;
  liveUnrealizedPnl?: number;
  liveDailyMtm?: number;
  tradeAutopsy?: TradeAutopsyReport | null;
  onRunTradeAutopsy?: () => Promise<TradeAutopsyReport | null>;
  onExitPosition?: (symbol: string) => Promise<{ success: boolean; message?: string }>;
  onCancelOrder?: (orderId: string) => Promise<{ success: boolean; message?: string }>;
  onModifyOrder?: (
    orderId: string,
    params: { trigger_price?: number; limit_price?: number }
  ) => Promise<{ success: boolean; message?: string }>;
  onApplySettingToggle?: (
    settingKey: string,
    value: boolean | string
  ) => Promise<{ success: boolean }>;
  signals?: Signal[];
  anomalies?: AnomalyAlert[];
  riskAlerts?: RiskAlert[];
}

interface ReportRow {
  key: string;
  product: string;
  instrument: string;
  side: string;
  qty: number;
  avg: number;
  ltp: number;
  pnl: number;
  chgPct: number;
  isOpen: boolean;
  slPrice?: number;
  targetPrice?: number;
  exitReason?: string;
  sortTimeMs: number;
}

function formatIstTime(iso?: string): string {
  if (!iso) return "--:--:--";
  const dt = new Date(iso);
  if (Number.isNaN(dt.getTime())) return "--:--:--";
  return dt.toLocaleTimeString("en-IN", {
    timeZone: "Asia/Kolkata",
    hour12: false,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function formatIstDateTime(iso?: string): string {
  if (!iso) return "--";
  const dt = new Date(iso);
  if (Number.isNaN(dt.getTime())) return "--";
  const datePart = dt.toLocaleDateString("en-CA", { timeZone: "Asia/Kolkata" });
  const timePart = dt.toLocaleTimeString("en-IN", {
    timeZone: "Asia/Kolkata",
    hour12: false,
    hour: "2-digit",
    minute: "2-digit",
  });
  return `${datePart} • ${timePart} IST`;
}

function getLegRole(ord: Order): { label: "EXIT" | "CANCELLED" | "ENTRY"; rank: number } {
  if (ord.status === "CANCELLED") {
    return { label: "CANCELLED", rank: 1 };
  }
  if (ord.is_exit) {
    return { label: "EXIT", rank: 0 };
  }
  return { label: "ENTRY", rank: 2 };
}

interface ExecutedGroup {
  groupId: string;
  symbol: string;
  latestMs: number;
  legs: Order[];
}

function groupExecutedOrders(orders: Order[]): ExecutedGroup[] {
  const byGroup = new Map<string, Order[]>();
  const chronological = orders
    .slice()
    .sort((a, b) => new Date(a.created_at_ist).getTime() - new Date(b.created_at_ist).getTime());

  // Track open/latest round-trip group per symbol for fallback grouping of legacy untagged orders
  const activeSymbolGroup = new Map<string, string>();
  let fallbackCounter = 0;

  for (const ord of chronological) {
    const explicitKey = ord.trade_group_id || ord.parent_order_id;
    let gKey: string;
    if (explicitKey) {
      gKey = explicitKey;
      activeSymbolGroup.set(ord.tradingsymbol, gKey);
    } else if (!ord.is_exit) {
      fallbackCounter += 1;
      gKey = `sym-${ord.tradingsymbol}-${fallbackCounter}`;
      activeSymbolGroup.set(ord.tradingsymbol, gKey);
    } else {
      gKey = activeSymbolGroup.get(ord.tradingsymbol) || `orphan-${ord.order_id}`;
    }

    const bucket = byGroup.get(gKey) || [];
    bucket.push(ord);
    byGroup.set(gKey, bucket);

    if (ord.is_exit && ord.status === "FILLED" && ord.exit_reason !== "PARTIAL_BOOK_1R") {
      // Final exit completes this fallback symbol cycle if next entry begins
      if (!explicitKey) {
        activeSymbolGroup.delete(ord.tradingsymbol);
      }
    }
  }

  const groups: ExecutedGroup[] = [];
  for (const [groupId, rawLegs] of Array.from(byGroup.entries())) {
    const legs = rawLegs.slice().sort((a, b) => {
      const ra = getLegRole(a).rank;
      const rb = getLegRole(b).rank;
      if (ra !== rb) return ra - rb;
      return new Date(b.created_at_ist).getTime() - new Date(a.created_at_ist).getTime();
    });
    const latestMs = Math.max(...legs.map((l) => new Date(l.created_at_ist).getTime() || 0));
    groups.push({
      groupId,
      symbol: legs[0]?.tradingsymbol || "",
      latestMs,
      legs,
    });
  }

  return groups.sort((a, b) => b.latestMs - a.latestMs);
}

function getExitReasonBadgeClass(reason: string): string {
  switch (reason) {
    case "TARGET_HIT":
      return "bg-emerald-500/20 text-emerald-300 border-emerald-500/40";
    case "PARTIAL_BOOK_1R":
      return "bg-teal-500/20 text-teal-300 border-teal-500/40";
    case "SL_HIT":
      return "bg-rose-500/20 text-rose-300 border-rose-500/40";
    case "FLATTEN_ALL":
      return "bg-amber-500/20 text-amber-300 border-amber-500/40";
    case "MANUAL_EXIT":
      return "bg-cyan-500/20 text-cyan-300 border-cyan-500/40";
    case "EOD_SQUARE_OFF":
      return "bg-indigo-500/20 text-indigo-300 border-indigo-500/40";
    default:
      return "bg-slate-700/40 text-slate-300 border-slate-600/40";
  }
}

export const PositionsAndOrdersTabs: React.FC<PositionsAndOrdersTabsProps> = ({
  positions = [],
  orders = [],
  closedTrades = [],
  ticks = {},
  selectedSymbol,
  onSelectSymbol,
  liveRealizedPnl: propRealizedPnl,
  liveUnrealizedPnl: propUnrealizedPnl,
  liveDailyMtm: propDailyMtm,
  tradeAutopsy = null,
  onRunTradeAutopsy,
  onExitPosition,
  onCancelOrder,
  onModifyOrder,
  onApplySettingToggle,
}) => {
  const [activeTab, setActiveTab] = useState<"OPEN" | "EXECUTED" | "REPORT">("OPEN");
  const [executedSearch, setExecutedSearch] = useState("");
  const [reportSearch, setReportSearch] = useState("");
  const [exitingSymbols, setExitingSymbols] = useState<Record<string, boolean>>({});
  const [cancellingOrders, setCancellingOrders] = useState<Record<string, boolean>>({});
  const [editingOrderId, setEditingOrderId] = useState<string | null>(null);
  const [editPriceInput, setEditPriceInput] = useState<string>("");
  const [modifyingOrders, setModifyingOrders] = useState<Record<string, boolean>>({});
  const [appliedToggles, setAppliedToggles] = useState<Record<string, boolean>>({});
  const [isExpanded, setIsExpanded] = useState(false);
  const [istCutoffActive, setIstCutoffActive] = useState(false);
  const [isRunningAutopsy, setIsRunningAutopsy] = useState(false);

  const handleRunAutopsy = async () => {
    if (!onRunTradeAutopsy) return;
    setIsRunningAutopsy(true);
    try {
      await onRunTradeAutopsy();
    } finally {
      setIsRunningAutopsy(false);
    }
  };

  useEffect(() => {
    const sync = () => setIstCutoffActive(isIstCutoffWindow());
    sync();
    const timer = setInterval(sync, 15000);
    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
    if (!isExpanded) return;
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setIsExpanded(false);
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [isExpanded]);

  // ---------------------------------------------------------------------------
  // Tab 1: OPEN (Pending Orders Only: OPEN, TRIGGER PENDING, PENDING_NEW)
  // ---------------------------------------------------------------------------
  const pendingOrders = useMemo(() => {
    return orders
      .filter(
        (o) =>
          o.status === "OPEN" ||
          o.status === "TRIGGER PENDING" ||
          o.status === "PENDING_NEW"
      )
      .slice()
      .sort(
        (a, b) =>
          new Date(b.created_at_ist).getTime() - new Date(a.created_at_ist).getTime()
      );
  }, [orders]);

  const handleCancelOrder = async (orderId: string) => {
    if (!onCancelOrder) return;
    setCancellingOrders((prev) => ({ ...prev, [orderId]: true }));
    try {
      await onCancelOrder(orderId);
    } finally {
      setCancellingOrders((prev) => ({ ...prev, [orderId]: false }));
    }
  };

  const handleSaveModifyOrder = async (ord: Order) => {
    if (!onModifyOrder) return;
    const parsed = parseFloat(editPriceInput);
    if (Number.isNaN(parsed) || parsed <= 0) return;
    setModifyingOrders((prev) => ({ ...prev, [ord.order_id]: true }));
    try {
      const isTrigger = ord.order_type === "SL_LIMIT" || ord.status === "TRIGGER PENDING";
      await onModifyOrder(
        ord.order_id,
        isTrigger ? { trigger_price: parsed } : { limit_price: parsed }
      );
      setEditingOrderId(null);
      setEditPriceInput("");
    } finally {
      setModifyingOrders((prev) => ({ ...prev, [ord.order_id]: false }));
    }
  };

  // ---------------------------------------------------------------------------
  // Tab 2: EXECUTED (Grouped 3-Leg Trade Pairs: Exit + Cancelled + Entry)
  // ---------------------------------------------------------------------------
  const executedOrders = useMemo(() => {
    return orders
      .filter(
        (o) =>
          o.status === "FILLED" ||
          o.status === "CANCELLED" ||
          o.status === "REJECTED"
      )
      .slice()
      .sort(
        (a, b) =>
          new Date(b.created_at_ist).getTime() - new Date(a.created_at_ist).getTime()
      );
  }, [orders]);

  const filteredExecutedOrders = useMemo(() => {
    if (!executedSearch.trim()) return executedOrders;
    const q = executedSearch.toLowerCase().trim();
    return executedOrders.filter(
      (o) =>
        o.tradingsymbol.toLowerCase().includes(q) ||
        o.side.toLowerCase().includes(q) ||
        o.product.toLowerCase().includes(q) ||
        o.status.toLowerCase().includes(q) ||
        (o.exit_reason && o.exit_reason.toLowerCase().includes(q)) ||
        (o.rejection_reason && o.rejection_reason.toLowerCase().includes(q))
    );
  }, [executedOrders, executedSearch]);

  const groupedExecutedTrades = useMemo(
    () => groupExecutedOrders(filteredExecutedOrders),
    [filteredExecutedOrders]
  );

  const downloadExecutedCsv = () => {
    if (filteredExecutedOrders.length === 0) return;
    const headers = [
      "Time (IST)",
      "Type",
      "Instrument",
      "Product",
      "Qty",
      "Avg Price",
      "Status",
      "Reason",
    ];
    const rows = filteredExecutedOrders.map((o) =>
      [
        formatIstTime(o.created_at_ist),
        o.side,
        o.tradingsymbol,
        o.product || "MIS",
        `${o.filled_quantity}/${o.quantity}`,
        (o.average_fill_price ?? o.limit_price ?? 0).toFixed(2),
        o.status === "FILLED" ? "COMPLETE" : o.status,
        o.rejection_reason || o.exit_reason || "",
      ]
        .map((cell) => `"${String(cell).replace(/"/g, '""')}"`)
        .join(",")
    );

    const csvContent = "data:text/csv;charset=utf-8," + [headers.join(","), ...rows].join("\n");
    const encodedUri = encodeURI(csvContent);
    const link = document.createElement("a");
    link.setAttribute("href", encodedUri);
    link.setAttribute("download", `trillionaire_executed_${new Date().toISOString().slice(0, 10)}.csv`);
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  };

  // ---------------------------------------------------------------------------
  // Tab 3: P&L REPORT (Live Running Positions + Immutable Closed Trades Ledger)
  // ---------------------------------------------------------------------------
  const handleExitPosition = async (symbol: string) => {
    if (!onExitPosition) return;
    setExitingSymbols((prev) => ({ ...prev, [symbol]: true }));
    try {
      await onExitPosition(symbol);
    } finally {
      setExitingSymbols((prev) => ({ ...prev, [symbol]: false }));
    }
  };

  const reportItems: ReportRow[] = useMemo(() => {
    const rows: ReportRow[] = [];

    // 1. Live running open positions (Qty != 0) updated dynamically on every tick
    for (const pos of positions) {
      if (Math.abs(pos.quantity ?? 0) === 0) continue;
      const metrics = computeOpenPositionMetrics(pos, ticks);

      rows.push({
        key: `open-${metrics.sym}`,
        product: "MIS",
        instrument: metrics.sym,
        side: pos.side,
        qty: metrics.netQty,
        avg: metrics.avgPrice,
        ltp: metrics.liveLtp,
        pnl: metrics.unrealizedPnl,
        chgPct: metrics.pctChange,
        isOpen: true,
        slPrice: pos.stop_loss,
        targetPrice: pos.target_price,
        sortTimeMs: pos.entry_time ? new Date(pos.entry_time).getTime() : Date.now(),
      });
    }

    // 2. Closed trades ledger (Qty = 0) sorted newest exit first
    const sortedClosed = closedTrades
      .slice()
      .sort((a, b) => new Date(b.exit_time).getTime() - new Date(a.exit_time).getTime());

    for (const ct of sortedClosed) {
      const closedPnl = (ct as any).net_pnl ?? (ct as any).pnl ?? ct.realized_pnl ?? 0;
      rows.push({
        key: `closed-${ct.order_id}-${ct.exit_time}`,
        product: ct.product || "MIS",
        instrument: ct.symbol,
        side: ct.side,
        qty: 0,
        avg: Math.round(ct.entry_price * 100) / 100,
        ltp: Math.round(ct.actual_exit_price * 100) / 100,
        pnl: Math.round(closedPnl * 100) / 100,
        chgPct: Math.round(ct.pnl_pct * 100) / 100,
        isOpen: false,
        slPrice: ct.sl_price,
        targetPrice: ct.target_price,
        exitReason: ct.exit_reason,
        sortTimeMs: new Date(ct.exit_time).getTime(),
      });
    }

    return rows;
  }, [positions, closedTrades, ticks]);

  const filteredReportItems = useMemo(() => {
    if (!reportSearch.trim()) return reportItems;
    const q = reportSearch.toLowerCase().trim();
    return reportItems.filter(
      (item) =>
        item.instrument.toLowerCase().includes(q) ||
        item.product.toLowerCase().includes(q) ||
        (item.exitReason && item.exitReason.toLowerCase().includes(q))
    );
  }, [reportItems, reportSearch]);

  const totalReportRealized = useMemo(() => {
    if (!reportSearch.trim() && propRealizedPnl !== undefined) {
      return propRealizedPnl;
    }
    return (
      Math.round(
        filteredReportItems
          .filter((i) => !i.isOpen)
          .reduce((acc, item) => acc + item.pnl, 0) * 100
      ) / 100
    );
  }, [filteredReportItems, reportSearch, propRealizedPnl]);

  const totalReportUnrealized = useMemo(() => {
    if (!reportSearch.trim() && propUnrealizedPnl !== undefined) {
      return propUnrealizedPnl;
    }
    return (
      Math.round(
        filteredReportItems
          .filter((i) => i.isOpen)
          .reduce((acc, item) => acc + item.pnl, 0) * 100
      ) / 100
    );
  }, [filteredReportItems, reportSearch, propUnrealizedPnl]);

  const totalReportPnl = useMemo(() => {
    if (!reportSearch.trim() && propDailyMtm !== undefined) {
      return propDailyMtm;
    }
    return (
      Math.round(filteredReportItems.reduce((acc, item) => acc + item.pnl, 0) * 100) /
      100
    );
  }, [filteredReportItems, reportSearch, propDailyMtm]);

  const maxAbsPnl = useMemo(() => {
    if (reportItems.length === 0) return 1;
    return Math.max(...reportItems.map((i) => Math.abs(i.pnl)), 1);
  }, [reportItems]);

  const escapeCsvRow = (cells: (string | number)[]) =>
    cells.map((cell) => `"${String(cell).replace(/"/g, '""')}"`).join(",");

  const triggerCsvDownload = (lines: string[], filename: string) => {
    const blob = new Blob([lines.join("\n")], { type: "text/csv;charset=utf-8;" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.setAttribute("href", url);
    link.setAttribute("download", filename);
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
  };

  const downloadReportCsv = () => {
    if (filteredReportItems.length === 0 && executedOrders.length === 0) return;

    const lines: string[] = [];

    // Section 1: P&L Report + TOTAL Summary Row
    lines.push("=== P&L REPORT ===");
    lines.push(
      escapeCsvRow([
        "Product",
        "Instrument",
        "Qty",
        "Avg",
        "LTP",
        "P&L",
        "Chg %",
        "Status / Exit Reason",
      ])
    );
    for (const item of filteredReportItems) {
      lines.push(
        escapeCsvRow([
          item.product,
          item.instrument,
          item.qty,
          item.avg.toFixed(2),
          item.ltp.toFixed(2),
          item.pnl.toFixed(2),
          `${item.chgPct.toFixed(2)}%`,
          item.exitReason || (item.isOpen ? "OPEN" : ""),
        ])
      );
    }
    lines.push(
      escapeCsvRow([
        "TOTAL",
        `Realized: ${totalReportRealized >= 0 ? "+" : ""}${totalReportRealized.toFixed(2)} | Unrealized: ${
          totalReportUnrealized >= 0 ? "+" : ""
        }${totalReportUnrealized.toFixed(2)}`,
        "",
        "",
        "",
        totalReportPnl.toFixed(2),
        "",
        "COMBINED TOTAL P&L",
      ])
    );

    // Two blank separator rows
    lines.push("");
    lines.push("");

    // Section 2: Executed Orders
    lines.push("=== EXECUTED ORDERS ===");
    lines.push(
      escapeCsvRow([
        "Time",
        "Type",
        "Instrument",
        "Product",
        "Qty",
        "Avg Price",
        "Status",
        "Exit / Rejection Reason",
      ])
    );
    for (const o of executedOrders) {
      lines.push(
        escapeCsvRow([
          formatIstTime(o.created_at_ist),
          o.side,
          o.tradingsymbol,
          o.product || "MIS",
          `${o.filled_quantity}/${o.quantity}`,
          (o.average_fill_price ?? o.limit_price ?? 0).toFixed(2),
          o.status === "FILLED" ? "COMPLETE" : o.status,
          o.rejection_reason || o.exit_reason || "",
        ])
      );
    }

    triggerCsvDownload(
      lines,
      `trillionaire_pnl_and_orders_${new Date().toISOString().slice(0, 10)}.csv`
    );
  };

  return (
    <div
      className={
        isExpanded
          ? "fixed inset-0 z-50 bg-slate-950 flex flex-col p-4 select-none"
          : "h-full flex flex-col bg-cockpit-card rounded-xl border border-cockpit-border overflow-hidden select-none"
      }
    >
      {/* Zerodha Kite-Style 3-Tab Header Navigation */}
      <div className="h-11 shrink-0 border-b border-cockpit-border px-3 flex items-center justify-between gap-2">
        <div className="flex items-center space-x-1">
          {/* Tab 1: Open ({pendingCount}) */}
          <button
            type="button"
            onClick={() => setActiveTab("OPEN")}
            className={`px-3 py-1.5 rounded-lg text-xs font-mono font-medium flex items-center space-x-1.5 transition ${
              activeTab === "OPEN"
                ? "bg-cockpit-surface text-white border border-cockpit-borderBright shadow-sm"
                : "text-cockpit-muted hover:text-white"
            }`}
          >
            <Activity className="h-3.5 w-3.5 text-blue-400" />
            <span>Open ({pendingOrders.length})</span>
          </button>

          {/* Tab 2: Executed ({executedCount}) */}
          <button
            type="button"
            onClick={() => setActiveTab("EXECUTED")}
            className={`px-3 py-1.5 rounded-lg text-xs font-mono font-medium flex items-center space-x-1.5 transition ${
              activeTab === "EXECUTED"
                ? "bg-cockpit-surface text-white border border-cockpit-borderBright shadow-sm"
                : "text-cockpit-muted hover:text-white"
            }`}
          >
            <ListOrdered className="h-3.5 w-3.5 text-purple-400" />
            <span>Executed ({executedOrders.length})</span>
          </button>

          {/* Tab 3: P&L Report ({totalPositionsCount}) */}
          <button
            type="button"
            onClick={() => setActiveTab("REPORT")}
            className={`px-3 py-1.5 rounded-lg text-xs font-mono font-medium flex items-center space-x-1.5 transition ${
              activeTab === "REPORT"
                ? "bg-cockpit-surface text-white border border-cockpit-borderBright shadow-sm"
                : "text-cockpit-muted hover:text-white"
            }`}
          >
            <PieChart className="h-3.5 w-3.5 text-emerald-400" />
            <span>P&L Report ({reportItems.length})</span>
          </button>
        </div>

        {/* Search, CSV & Full-Screen Expand Controls */}
        <div className="flex items-center space-x-2">
          {activeTab === "EXECUTED" && (
            <>
              <div className="relative">
                <Search className="h-3 w-3 absolute left-2 top-2 text-cockpit-muted" />
                <input
                  type="text"
                  placeholder="Search executed..."
                  value={executedSearch}
                  onChange={(e) => setExecutedSearch(e.target.value)}
                  className="pl-7 pr-6 py-0.5 text-xs bg-cockpit-surface border border-cockpit-border rounded text-white placeholder-cockpit-muted/60 focus:outline-none focus:border-cockpit-borderBright w-36 sm:w-44"
                />
                {executedSearch && (
                  <button
                    type="button"
                    onClick={() => setExecutedSearch("")}
                    className="absolute right-1.5 top-1.5 text-cockpit-muted hover:text-white"
                  >
                    <X className="h-3 w-3" />
                  </button>
                )}
              </div>
              <button
                type="button"
                onClick={downloadExecutedCsv}
                title="Download Executed Orders CSV"
                className="px-2 py-1 rounded bg-cockpit-surface hover:bg-cockpit-hover border border-cockpit-border text-cockpit-muted hover:text-white text-xs font-mono flex items-center space-x-1 transition"
              >
                <Download className="h-3 w-3 text-purple-400" />
                <span className="hidden sm:inline">CSV</span>
              </button>
            </>
          )}

          {activeTab === "REPORT" && (
            <>
              <div className="relative">
                <Search className="h-3 w-3 absolute left-2 top-2 text-cockpit-muted" />
                <input
                  type="text"
                  placeholder="Search P&L..."
                  value={reportSearch}
                  onChange={(e) => setReportSearch(e.target.value)}
                  className="pl-7 pr-6 py-0.5 text-xs bg-cockpit-surface border border-cockpit-border rounded text-white placeholder-cockpit-muted/60 focus:outline-none focus:border-cockpit-borderBright w-36 sm:w-44"
                />
                {reportSearch && (
                  <button
                    type="button"
                    onClick={() => setReportSearch("")}
                    className="absolute right-1.5 top-1.5 text-cockpit-muted hover:text-white"
                  >
                    <X className="h-3 w-3" />
                  </button>
                )}
              </div>
              <button
                type="button"
                onClick={downloadReportCsv}
                title="Download Combined P&L Report & Executed Orders CSV"
                className="px-2 py-1 rounded bg-cockpit-surface hover:bg-cockpit-hover border border-cockpit-border text-cockpit-muted hover:text-white text-xs font-mono flex items-center space-x-1 transition"
              >
                <Download className="h-3 w-3 text-emerald-400" />
                <span className="hidden sm:inline">CSV</span>
              </button>
            </>
          )}

          <button
            type="button"
            onClick={() => setIsExpanded((prev) => !prev)}
            title={isExpanded ? "Exit Full-Screen View (Esc)" : "Expand Full-Screen View"}
            className="px-2 py-1 rounded bg-cockpit-surface hover:bg-cockpit-hover border border-cockpit-border text-cockpit-muted hover:text-white text-xs font-mono flex items-center space-x-1 transition"
          >
            {isExpanded ? (
              <>
                <Minimize2 className="h-3.5 w-3.5 text-cyan-400" />
                <span>Close (Esc)</span>
              </>
            ) : (
              <>
                <Maximize2 className="h-3.5 w-3.5 text-cyan-400" />
                <span className="hidden sm:inline">Expand</span>
              </>
            )}
          </button>
        </div>
      </div>

      {/* TAB 1: OPEN / PENDING ORDERS ONLY */}
      {activeTab === "OPEN" && (
        <div className="flex-1 min-h-0 overflow-y-auto">
          {pendingOrders.length === 0 ? (
            <div className="h-full flex flex-col items-center justify-center text-cockpit-muted text-xs font-mono space-y-1 px-4 text-center">
              <Activity className="h-6 w-6 text-cockpit-muted/40 mb-1" />
              <span>
                {istCutoffActive
                  ? "No new intraday entries after 14:30 IST (auto square-off at 15:10 IST)"
                  : "No open or trigger-pending orders."}
              </span>
              <span className="text-[10px]">
                Active filled positions are tracked live under P&L Report.
              </span>
            </div>
          ) : (
            <table className="w-full text-left text-xs font-mono">
              <thead className="sticky top-0 z-10 bg-slate-950/95 backdrop-blur border-b border-cockpit-border text-[10px] text-cockpit-muted uppercase">
                <tr>
                  <th className="py-2 px-3">Time</th>
                  <th className="py-2 px-2">Type</th>
                  <th className="py-2 px-3">Instrument</th>
                  <th className="py-2 px-2">Product</th>
                  <th className="py-2 px-2 text-right">Qty.</th>
                  <th className="py-2 px-2 text-right">LTP</th>
                  <th className="py-2 px-2 text-right">Price</th>
                  <th className="py-2 px-3 text-right">Status</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-cockpit-border/40">
                {pendingOrders.map((ord) => {
                  const sym = ord.tradingsymbol;
                  const isBuy = ord.side === "BUY";
                  const liveLtp = ticks[sym]?.ltp ?? ord.limit_price ?? ord.trigger_price ?? 0;
                  const displayPrice = ord.trigger_price ?? ord.limit_price ?? 0;
                  const isTriggerPending = ord.status === "TRIGGER PENDING";
                  const isTpPending = !!ord.is_exit && ord.order_type === "LIMIT" && ord.status === "OPEN";
                  const isCancelling = !!cancellingOrders[ord.order_id];
                  const isModifying = !!modifyingOrders[ord.order_id];
                  const isEditingThis = editingOrderId === ord.order_id;
                  const statusLabel = isTpPending ? "LIMIT PENDING" : ord.status;

                  const isSelected = selectedSymbol === sym;

                  return (
                    <tr
                      key={`pending-${ord.order_id}`}
                      onClick={() => onSelectSymbol?.(sym)}
                      className={`cursor-pointer transition ${
                        isSelected
                          ? "bg-cockpit-blue/15 border-l-2 border-cockpit-blue"
                          : "hover:bg-cockpit-surface/60"
                      }`}
                    >
                      <td className="py-2.5 px-3 text-cockpit-muted whitespace-nowrap">
                        {formatIstTime(ord.created_at_ist)}
                      </td>
                      <td className="py-2.5 px-2">
                        <span
                          className={`px-2 py-0.5 rounded text-[10px] font-bold ${
                            isBuy
                              ? "bg-blue-500/20 text-blue-400 border border-blue-500/40"
                              : "bg-rose-500/20 text-rose-400 border border-rose-500/40"
                          }`}
                        >
                          {ord.side}
                        </span>
                      </td>
                      <td className="py-2.5 px-3 font-bold text-white whitespace-nowrap">
                        <span>{sym}</span>
                        <span className="ml-1.5 text-[9px] px-1 py-0.2 rounded bg-cockpit-surface border border-cockpit-border text-cockpit-muted font-normal">
                          {ord.order_type === "SL_LIMIT" ? "SL-L" : isTpPending ? "TP-LIM" : ord.order_type}
                        </span>
                      </td>
                      <td className="py-2.5 px-2">
                        <span className="px-1.5 py-0.5 rounded text-[10px] font-semibold bg-purple-500/20 text-purple-300 border border-purple-500/40">
                          {ord.product || "MIS"}
                        </span>
                      </td>
                      <td className="py-2.5 px-2 text-right font-medium text-white tabular-nums">
                        {ord.filled_quantity}/{ord.quantity}
                      </td>
                      <td className="py-2.5 px-2 text-right font-bold text-white tabular-nums">
                        ₹{liveLtp.toFixed(2)}
                      </td>
                      <td className="py-2.5 px-2 text-right text-slate-300 tabular-nums">
                        {isEditingThis ? (
                          <div
                            className="inline-flex items-center space-x-1"
                            onClick={(e) => e.stopPropagation()}
                          >
                            <input
                              type="number"
                              step="0.05"
                              value={editPriceInput}
                              onChange={(e) => setEditPriceInput(e.target.value)}
                              className="w-20 px-1.5 py-0.5 rounded bg-slate-900 border border-cyan-500 text-white text-[11px] font-mono text-right focus:outline-none"
                            />
                            <button
                              type="button"
                              disabled={isModifying}
                              onClick={() => handleSaveModifyOrder(ord)}
                              title="Save Price"
                              className="p-1 rounded bg-emerald-500/20 hover:bg-emerald-500/35 text-emerald-300 border border-emerald-500/40"
                            >
                              <Check className="h-3 w-3" />
                            </button>
                            <button
                              type="button"
                              onClick={() => {
                                setEditingOrderId(null);
                                setEditPriceInput("");
                              }}
                              title="Discard"
                              className="p-1 rounded bg-slate-700/40 hover:bg-slate-700 text-slate-300 border border-slate-600/40"
                            >
                              <X className="h-3 w-3" />
                            </button>
                          </div>
                        ) : (
                          <span>₹{displayPrice.toFixed(2)}</span>
                        )}
                      </td>
                      <td className="py-2.5 px-3 text-right">
                        <div className="flex items-center justify-end space-x-1.5">
                          <span
                            className={`px-2 py-0.5 rounded text-[10px] font-semibold uppercase ${
                              isTriggerPending
                                ? "bg-amber-500/20 text-amber-300 border border-amber-500/40"
                                : isTpPending
                                ? "bg-emerald-500/15 text-emerald-300 border border-emerald-500/40"
                                : "bg-blue-500/20 text-blue-400 border border-blue-500/40"
                            }`}
                          >
                            {statusLabel}
                          </span>
                          {onModifyOrder && !isEditingThis && (
                            <button
                              type="button"
                              onClick={(e) => {
                                e.stopPropagation();
                                setEditingOrderId(ord.order_id);
                                setEditPriceInput(displayPrice.toFixed(2));
                              }}
                              title="Modify Trigger / Limit Price"
                              className="px-1.5 py-0.5 rounded text-[10px] font-bold border bg-cyan-500/15 hover:bg-cyan-500/30 text-cyan-300 border-cyan-500/40 transition inline-flex items-center space-x-0.5"
                            >
                              <Pencil className="h-2.5 w-2.5" />
                              <span>Modify</span>
                            </button>
                          )}
                          {onCancelOrder && (
                            <button
                              type="button"
                              onClick={(e) => {
                                e.stopPropagation();
                                handleCancelOrder(ord.order_id);
                              }}
                              disabled={isCancelling}
                              className="px-2 py-0.5 rounded text-[10px] font-bold border bg-rose-500/15 hover:bg-rose-500/30 text-rose-300 border-rose-500/40 transition"
                            >
                              {isCancelling ? "..." : "Cancel"}
                            </button>
                          )}
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>
      )}

      {/* TAB 2: EXECUTED ORDERS (COMPLETE / CANCELLED / REJECTED) */}
      {activeTab === "EXECUTED" && (
        <div className="flex-1 min-h-0 overflow-y-auto">
          {filteredExecutedOrders.length === 0 ? (
            <div className="h-full flex items-center justify-center text-cockpit-muted text-xs font-mono">
              <span>
                {executedSearch
                  ? "No executed orders matching query."
                  : "No completed, cancelled, or rejected orders recorded yet."}
              </span>
            </div>
          ) : (
            <table className="w-full text-left text-xs font-mono">
              <thead className="sticky top-0 z-10 bg-slate-950/95 backdrop-blur border-b border-cockpit-border text-[10px] text-cockpit-muted uppercase">
                <tr>
                  <th className="py-2 px-3">Time</th>
                  <th className="py-2 px-2">Type</th>
                  <th className="py-2 px-3">Instrument</th>
                  <th className="py-2 px-2">Product</th>
                  <th className="py-2 px-2 text-right">Qty.</th>
                  <th className="py-2 px-2 text-right">Avg. price</th>
                  <th className="py-2 px-3 text-right">Status</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-cockpit-border/30">
                {groupedExecutedTrades.map((grp, groupIdx) => {
                  const groupBg = groupIdx % 2 === 0 ? "bg-slate-900/40" : "bg-slate-800/35";
                  return grp.legs.map((ord, legIdx) => {
                    const isBuy = ord.side === "BUY";
                    const execPrice = ord.average_fill_price ?? ord.limit_price ?? ord.trigger_price ?? 0;
                    const isComplete = ord.status === "FILLED";
                    const isRejected = ord.status === "REJECTED";
                    const reasonBadge = ord.rejection_reason || ord.exit_reason;
                    const role = getLegRole(ord);
                    const isSelected = selectedSymbol === ord.tradingsymbol;

                    return (
                      <tr
                        key={ord.order_id}
                        onClick={() => onSelectSymbol?.(ord.tradingsymbol)}
                        className={`cursor-pointer transition ${
                          isSelected
                            ? "bg-cockpit-blue/15 border-l-2 border-cockpit-blue"
                            : `${groupBg} hover:bg-cockpit-surface/70`
                        } ${legIdx === 0 && groupIdx > 0 ? "border-t-2 border-slate-700/70" : ""}`}
                      >
                        <td className="py-2 px-3 text-cockpit-muted whitespace-nowrap">
                          {formatIstTime(ord.created_at_ist)}
                        </td>
                        <td className="py-2 px-2">
                          <div className="inline-flex items-center space-x-1">
                            <span
                              className={`px-2 py-0.5 rounded text-[10px] font-bold ${
                                isBuy
                                  ? "bg-blue-500/20 text-blue-400 border border-blue-500/40"
                                  : "bg-rose-500/20 text-rose-400 border border-rose-500/40"
                              }`}
                            >
                              {ord.side}
                            </span>
                            <span
                              className={`px-1 py-0.2 rounded text-[8px] font-bold uppercase ${
                                role.label === "EXIT"
                                  ? "bg-emerald-500/15 text-emerald-300"
                                  : role.label === "CANCELLED"
                                  ? "bg-slate-700/60 text-slate-400"
                                  : "bg-cyan-500/15 text-cyan-300"
                              }`}
                            >
                              {role.label}
                            </span>
                          </div>
                        </td>
                        <td className="py-2 px-3 font-bold text-white whitespace-nowrap">
                          <span>{ord.tradingsymbol}</span>
                          <span className="ml-1.5 text-[9px] px-1 py-0.2 rounded bg-cockpit-surface border border-cockpit-border text-cockpit-muted font-normal">
                            {ord.order_type === "SL_LIMIT" ? "SL-L" : "NSE"}
                          </span>
                        </td>
                        <td className="py-2 px-2">
                          <span className="px-1.5 py-0.5 rounded text-[10px] font-semibold bg-purple-500/20 text-purple-300 border border-purple-500/40">
                            {ord.product || "MIS"}
                          </span>
                        </td>
                        <td className="py-2 px-2 text-right font-medium text-white tabular-nums">
                          {ord.filled_quantity}/{ord.quantity}
                        </td>
                        <td className="py-2 px-2 text-right font-medium text-white tabular-nums">
                          ₹{execPrice.toFixed(2)}
                        </td>
                        <td className="py-2 px-3 text-right">
                          <div className="inline-flex items-center space-x-1.5">
                            {reasonBadge && (
                              <span
                                title={reasonBadge}
                                className={`px-1.5 py-0.5 rounded text-[9px] font-semibold border max-w-[160px] truncate ${
                                  isRejected
                                    ? "bg-rose-500/10 text-rose-300 border-rose-500/30"
                                    : getExitReasonBadgeClass(reasonBadge)
                                }`}
                              >
                                {reasonBadge}
                              </span>
                            )}
                            <span
                              title={ord.rejection_reason || ord.exit_reason || ord.status}
                              className={`px-2 py-0.5 rounded text-[10px] font-semibold uppercase ${
                                isComplete
                                  ? "bg-emerald-500/20 text-emerald-400 border border-emerald-500/40"
                                  : isRejected
                                  ? "bg-rose-500/20 text-rose-400 border border-rose-500/40"
                                  : "bg-slate-700/40 text-slate-400 border border-slate-600/40"
                              }`}
                            >
                              {isComplete ? "COMPLETE" : ord.status}
                            </span>
                          </div>
                        </td>
                      </tr>
                    );
                  });
                })}
              </tbody>
            </table>
          )}
        </div>
      )}

      {/* TAB 3: P&L REPORT (Live Running Positions + Closed Trades Ledger & Bar Chart) */}
      {activeTab === "REPORT" && (
        <div className="flex-1 min-h-0 overflow-y-auto flex flex-col">
          {filteredReportItems.length === 0 ? (
            <div className="h-full flex items-center justify-center text-cockpit-muted text-xs font-mono">
              <span>
                {reportSearch
                  ? "No positions matching query."
                  : "No live or closed positions recorded today."}
              </span>
            </div>
          ) : (
            <div className="flex-1 flex flex-col">
              <div className="overflow-x-auto">
                <table className="w-full text-left text-xs font-mono">
                  <thead className="sticky top-0 z-10 bg-slate-950/95 backdrop-blur border-b border-cockpit-border text-[10px] text-cockpit-muted uppercase">
                    <tr>
                      <th className="py-2 px-2">Product</th>
                      <th className="py-2 px-2">Instrument</th>
                      <th className="py-2 px-1.5 text-right">Qty.</th>
                      <th className="py-2 px-1.5 text-right">Avg.</th>
                      <th className="py-2 px-1.5 text-right">LTP</th>
                      <th className="py-2 px-2 text-center w-28 sm:w-36">P&amp;L Bar</th>
                      <th className="py-2 px-2 text-right">P&amp;L</th>
                      <th className="py-2 px-2 text-right">Chg.</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-cockpit-border/40">
                    {filteredReportItems.map((item) => {
                      const isProfit = item.pnl >= 0;
                      const isLong = item.qty > 0;
                      const isShort = item.qty < 0;
                      const isExiting = !!exitingSymbols[item.instrument];
                      const barWidthPct =
                        maxAbsPnl > 0
                          ? Math.min(100, (Math.abs(item.pnl) / maxAbsPnl) * 100)
                          : 0;

                      const isSelected = selectedSymbol === item.instrument;

                      return (
                        <tr
                          key={item.key}
                          onClick={() => onSelectSymbol?.(item.instrument)}
                          className={`cursor-pointer transition ${
                            isSelected
                              ? "bg-cockpit-blue/15 border-l-2 border-cockpit-blue"
                              : "hover:bg-cockpit-surface/60"
                          }`}
                        >
                          <td className="py-1.5 px-2">
                            <span className="px-1.5 py-0.5 rounded text-[10px] font-semibold bg-purple-500/20 text-purple-300 border border-purple-500/40">
                              {item.product}
                            </span>
                          </td>
                          <td className="py-1.5 px-2 whitespace-nowrap">
                            <div className="flex items-center space-x-1.5">
                              <span className="font-bold text-white">{item.instrument}</span>
                              {item.isOpen && item.slPrice !== undefined && item.targetPrice !== undefined && (
                                <span
                                  title={`SL ₹${item.slPrice.toFixed(2)} | Target ₹${item.targetPrice.toFixed(2)}`}
                                  className="text-[9px] px-1.5 py-0.2 rounded bg-cockpit-surface border border-cockpit-border text-cockpit-muted font-normal"
                                >
                                  SL {item.slPrice.toFixed(1)} / TP {item.targetPrice.toFixed(1)}
                                </span>
                              )}
                              {!item.isOpen && item.exitReason && (
                                <span
                                  title={
                                    item.slPrice !== undefined && item.targetPrice !== undefined
                                      ? `SL ₹${item.slPrice.toFixed(2)} | Target ₹${item.targetPrice.toFixed(2)}`
                                      : item.exitReason
                                  }
                                  className={`px-1.5 py-0.2 rounded text-[9px] font-semibold border ${getExitReasonBadgeClass(
                                    item.exitReason
                                  )}`}
                                >
                                  {item.exitReason}
                                </span>
                              )}
                            </div>
                          </td>
                          <td
                            className={`py-1.5 px-1.5 text-right font-medium tabular-nums ${
                              isLong
                                ? "text-blue-400 font-bold"
                                : isShort
                                ? "text-rose-400 font-bold"
                                : "text-cockpit-muted"
                            }`}
                          >
                            {item.qty}
                          </td>
                          <td className="py-1.5 px-1.5 text-right text-cockpit-muted tabular-nums">
                            ₹{item.avg.toFixed(2)}
                          </td>
                          <td className="py-1.5 px-1.5 text-right font-bold text-white tabular-nums">
                            ₹{item.ltp.toFixed(2)}
                          </td>
                          {/* Inline Zero-Centered Horizontal P&L Slider Bar */}
                          <td className="py-1.5 px-2">
                            <div
                              title={`Zero-Centered P&L Slider (${isProfit ? "+" : ""}₹${item.pnl.toFixed(2)})`}
                              className="w-24 sm:w-32 mx-auto grid grid-cols-2 gap-0 h-3 bg-cockpit-surface rounded border border-cockpit-border/50 overflow-hidden"
                            >
                              <div className="relative flex justify-end items-center border-r border-cockpit-border/80 pr-0.5">
                                {!isProfit && (
                                  <div
                                    style={{ width: `${barWidthPct}%` }}
                                    className="h-full bg-gradient-to-l from-rose-500 to-rose-600/80 rounded-l"
                                  />
                                )}
                              </div>
                              <div className="relative flex justify-start items-center pl-0.5">
                                {isProfit && (
                                  <div
                                    style={{ width: `${barWidthPct}%` }}
                                    className="h-full bg-gradient-to-r from-blue-500 to-emerald-500 rounded-r"
                                  />
                                )}
                              </div>
                            </div>
                          </td>
                          <td
                            className={`py-1.5 px-2 text-right font-bold tabular-nums ${
                              isProfit ? "text-emerald-400" : "text-rose-400"
                            }`}
                          >
                            {isProfit ? "+" : ""}₹{item.pnl.toFixed(2)}
                          </td>
                          <td className="py-1.5 px-2 text-right tabular-nums">
                            <div className="inline-flex items-center space-x-2">
                              <span
                                className={`text-xs font-semibold ${
                                  item.chgPct >= 0 ? "text-emerald-400" : "text-rose-400"
                                }`}
                              >
                                {item.chgPct >= 0 ? "+" : ""}
                                {item.chgPct.toFixed(2)}%
                              </span>
                              {item.isOpen && onExitPosition && (
                                <button
                                  type="button"
                                  onClick={(e) => {
                                    e.stopPropagation();
                                    handleExitPosition(item.instrument);
                                  }}
                                  disabled={isExiting}
                                  className={`px-2 py-0.5 rounded text-[10px] font-bold border transition ${
                                    isExiting
                                      ? "bg-rose-500/10 text-rose-500 border-rose-500/30 cursor-not-allowed"
                                      : "bg-rose-500/20 hover:bg-rose-500/30 text-rose-300 border-rose-500/50"
                                  }`}
                                >
                                  {isExiting ? "..." : "Exit"}
                                </button>
                              )}
                            </div>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                  <tfoot className="sticky bottom-0 border-t-2 border-cockpit-border bg-cockpit-surface/95 backdrop-blur-sm font-bold">
                    <tr>
                      <td colSpan={5} className="py-2 px-2">
                        <div className="inline-flex items-center space-x-2 px-2 py-0.5 rounded bg-cockpit-card border border-cockpit-border text-[10px] font-mono font-medium text-cockpit-muted">
                          <span>
                            Realized:{" "}
                            <span
                              className={`tabular-nums font-semibold ${
                                totalReportRealized >= 0 ? "text-emerald-400" : "text-rose-400"
                              }`}
                            >
                              {totalReportRealized >= 0 ? "+" : "-"}₹
                              {Math.abs(totalReportRealized).toLocaleString("en-IN", {
                                minimumFractionDigits: 2,
                                maximumFractionDigits: 2,
                              })}
                            </span>
                          </span>
                          <span className="opacity-40">|</span>
                          <span>
                            Unrealized:{" "}
                            <span
                              className={`tabular-nums font-semibold ${
                                totalReportUnrealized >= 0 ? "text-emerald-400" : "text-rose-400"
                              }`}
                            >
                              {totalReportUnrealized >= 0 ? "+" : "-"}₹
                              {Math.abs(totalReportUnrealized).toLocaleString("en-IN", {
                                minimumFractionDigits: 2,
                                maximumFractionDigits: 2,
                              })}
                            </span>
                          </span>
                        </div>
                      </td>
                      <td className="py-2 px-2 text-right text-white uppercase text-xs font-bold tracking-wider">
                        Total
                      </td>
                      <td
                        className={`py-2 px-2 text-right text-sm font-mono tabular-nums font-bold ${
                          totalReportPnl >= 0 ? "text-emerald-400" : "text-rose-400"
                        }`}
                      >
                        {totalReportPnl >= 0 ? "+" : "-"}₹
                        {Math.abs(totalReportPnl).toLocaleString("en-IN", {
                          minimumFractionDigits: 2,
                          maximumFractionDigits: 2,
                        })}
                      </td>
                      <td />
                    </tr>
                  </tfoot>
                </table>
              </div>

              {/* Hourly & Post-Market Gemini Trade Autopsy Coach Cards (Stacked Newest-First) */}
              <div className="p-3 border-t border-cockpit-border bg-slate-950/60 space-y-2.5">
                <div className="flex items-center justify-between">
                  <div className="flex items-center space-x-1.5 text-[11px] font-mono font-bold text-cyan-300">
                    <Brain className="h-3.5 w-3.5 text-cyan-400" />
                    <span>AI TRADE AUTOPSY COACH</span>
                    <span className="text-[9px] px-1.5 py-0.2 rounded bg-cockpit-surface border border-cockpit-border text-cockpit-muted font-normal">
                      Hourly (10:30–15:35 IST)
                    </span>
                  </div>
                  {onRunTradeAutopsy && (
                    <button
                      type="button"
                      onClick={handleRunAutopsy}
                      disabled={isRunningAutopsy}
                      className="px-2 py-1 rounded bg-cyan-500/15 hover:bg-cyan-500/25 border border-cyan-500/40 text-cyan-300 text-[10px] font-mono font-semibold flex items-center space-x-1 transition"
                    >
                      <Sparkles className="h-3 w-3" />
                      <span>{isRunningAutopsy ? "Analyzing..." : "Generate Autopsy"}</span>
                    </button>
                  )}
                </div>

                {(() => {
                  const autopsyList: TradeAutopsyReport[] =
                    tradeAutopsy?.history && tradeAutopsy.history.length > 0
                      ? tradeAutopsy.history
                      : tradeAutopsy
                      ? [tradeAutopsy]
                      : [];

                  if (autopsyList.length === 0) {
                    return (
                      <div className="text-[10px] font-mono text-cockpit-muted">
                        Scheduled automatically at 10:30, 11:30, 12:30, 13:30, 14:30, and 15:35 IST — or click Generate Autopsy for instant review.
                      </div>
                    );
                  }

                  return (
                    <div className="space-y-2.5">
                      {autopsyList.map((entry, reviewIdx) => {
                        const timeLabel =
                          entry.generated_time_label || formatIstDateTime(entry.generated_at_ist);
                        return (
                          <div
                            key={`autopsy-${entry.generated_at_ist}-${reviewIdx}`}
                            className="w-full p-2.5 rounded-lg bg-cockpit-surface/75 border border-cockpit-border space-y-2 text-[11px] font-mono"
                          >
                            <div className="flex flex-wrap items-center justify-between gap-2 border-b border-cockpit-border/60 pb-1.5">
                              <span className="px-2 py-0.5 rounded bg-cyan-500/15 border border-cyan-500/40 text-cyan-300 font-bold text-[10px]">
                                {timeLabel}
                              </span>
                              <span className="text-[10px] text-cockpit-muted">
                                Trades {entry.session_scorecard.total_trades} • WR{" "}
                                {entry.session_scorecard.win_rate_pct}% • PF{" "}
                                {entry.session_scorecard.profit_factor} • {entry.source}
                              </span>
                            </div>

                            <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
                              <div className="space-y-1">
                                <div className="text-[10px] uppercase text-cockpit-muted font-bold">
                                  Diagnosis
                                </div>
                                {entry.loss_and_trap_diagnosis.map((d, idx) => (
                                  <div key={`diag-${idx}`} className="text-slate-300 leading-snug">
                                    • {d}
                                  </div>
                                ))}
                              </div>
                              <div className="space-y-1">
                                <div className="text-[10px] uppercase text-cockpit-muted font-bold">
                                  Parameter Recommendations
                                </div>
                                {entry.parameter_recommendations.map((r, idx) => (
                                  <div key={`rec-${idx}`} className="text-emerald-300 leading-snug">
                                    • {r}
                                  </div>
                                ))}
                              </div>
                            </div>

                            {entry.suggested_toggle_actions && entry.suggested_toggle_actions.length > 0 && (
                              <div className="pt-1.5 border-t border-cockpit-border/50 space-y-1.5">
                                <div className="text-[10px] uppercase font-bold text-amber-300 tracking-wide">
                                  SUGGESTED TOGGLE ACTIONS (Settings &amp; Keys)
                                </div>
                                <div className="flex flex-wrap gap-1.5">
                                  {entry.suggested_toggle_actions.map((act, actIdx) => {
                                    const toggleStateKey = `${entry.generated_at_ist}-${act.setting_key}-${actIdx}`;
                                    const isApplied = !!appliedToggles[toggleStateKey];
                                    return (
                                      <div
                                        key={toggleStateKey}
                                        title={act.reason}
                                        className="inline-flex items-center space-x-1.5 px-2 py-1 rounded bg-slate-900 border border-amber-500/40 text-[10px]"
                                      >
                                        <span className="font-bold text-amber-300">{act.action}</span>
                                        {onApplySettingToggle && (
                                          <button
                                            type="button"
                                            onClick={async () => {
                                              const res = await onApplySettingToggle(
                                                act.setting_key,
                                                act.recommended_value
                                              );
                                              if (res.success) {
                                                setAppliedToggles((prev) => ({
                                                  ...prev,
                                                  [toggleStateKey]: true,
                                                }));
                                              }
                                            }}
                                            disabled={isApplied}
                                            className={`px-1.5 py-0.2 rounded font-bold border transition ${
                                              isApplied
                                                ? "bg-emerald-500/20 text-emerald-300 border-emerald-500/40"
                                                : "bg-cockpit-blue/20 hover:bg-cockpit-blue/35 text-cyan-300 border-cyan-500/40"
                                            }`}
                                          >
                                            {isApplied ? "Applied ✓" : "Apply"}
                                          </button>
                                        )}
                                      </div>
                                    );
                                  })}
                                </div>
                              </div>
                            )}
                          </div>
                        );
                      })}
                    </div>
                  );
                })()}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
};
