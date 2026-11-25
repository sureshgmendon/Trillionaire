"use client";

import React, { useState, useMemo } from "react";
import {
  Activity,
  Download,
  ListOrdered,
  PieChart,
  Search,
  X,
} from "lucide-react";
import {
  AnomalyAlert,
  ClosedTrade,
  Order,
  Position,
  RiskAlert,
  Signal,
  Tick,
} from "@/types/telemetry";

export interface PositionsAndOrdersTabsProps {
  positions: Position[];
  orders: Order[];
  closedTrades?: ClosedTrade[];
  ticks?: Record<string, Tick>;
  onExitPosition?: (symbol: string) => Promise<{ success: boolean; message?: string }>;
  onCancelOrder?: (orderId: string) => Promise<{ success: boolean; message?: string }>;
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

function getExitReasonBadgeClass(reason: string): string {
  switch (reason) {
    case "TARGET_HIT":
      return "bg-emerald-500/20 text-emerald-300 border-emerald-500/40";
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
  onExitPosition,
  onCancelOrder,
}) => {
  const [activeTab, setActiveTab] = useState<"OPEN" | "EXECUTED" | "REPORT">("OPEN");
  const [executedSearch, setExecutedSearch] = useState("");
  const [reportSearch, setReportSearch] = useState("");
  const [exitingSymbols, setExitingSymbols] = useState<Record<string, boolean>>({});
  const [cancellingOrders, setCancellingOrders] = useState<Record<string, boolean>>({});

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

  // ---------------------------------------------------------------------------
  // Tab 2: EXECUTED (Completed / Cancelled / Rejected Orders)
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
      if (!pos.quantity) continue;
      const sym = pos.tradingsymbol;
      const isBuy = pos.side === "BUY";
      const netQty = isBuy ? pos.quantity : -pos.quantity;
      const avg = pos.average_entry_price || 0;
      const liveLtp = ticks[sym]?.ltp ?? pos.ltp ?? avg;
      const unrealized = isBuy
        ? (liveLtp - avg) * Math.abs(pos.quantity)
        : (avg - liveLtp) * Math.abs(pos.quantity);
      const chgPct =
        avg > 0 ? ((liveLtp - avg) / avg) * 100 * (isBuy ? 1 : -1) : 0;

      rows.push({
        key: `open-${sym}`,
        product: "MIS",
        instrument: sym,
        side: pos.side,
        qty: netQty,
        avg: Math.round(avg * 100) / 100,
        ltp: Math.round(liveLtp * 100) / 100,
        pnl: Math.round(unrealized * 100) / 100,
        chgPct: Math.round(chgPct * 100) / 100,
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
      rows.push({
        key: `closed-${ct.order_id}-${ct.exit_time}`,
        product: ct.product || "MIS",
        instrument: ct.symbol,
        side: ct.side,
        qty: 0,
        avg: Math.round(ct.entry_price * 100) / 100,
        ltp: Math.round(ct.actual_exit_price * 100) / 100,
        pnl: Math.round(ct.realized_pnl * 100) / 100,
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

  const totalReportPnl = useMemo(() => {
    return (
      Math.round(filteredReportItems.reduce((acc, item) => acc + item.pnl, 0) * 100) /
      100
    );
  }, [filteredReportItems]);

  const maxAbsPnl = useMemo(() => {
    if (reportItems.length === 0) return 1;
    return Math.max(...reportItems.map((i) => Math.abs(i.pnl)), 1);
  }, [reportItems]);

  const downloadReportCsv = () => {
    if (filteredReportItems.length === 0) return;
    const headers = [
      "Product",
      "Instrument",
      "Side",
      "Qty",
      "Avg",
      "SL",
      "Target",
      "LTP",
      "P&L",
      "Chg %",
      "Exit Reason",
    ];
    const rows = filteredReportItems.map((item) =>
      [
        item.product,
        item.instrument,
        item.side,
        item.qty,
        item.avg.toFixed(2),
        item.slPrice !== undefined ? item.slPrice.toFixed(2) : "",
        item.targetPrice !== undefined ? item.targetPrice.toFixed(2) : "",
        item.ltp.toFixed(2),
        item.pnl.toFixed(2),
        `${item.chgPct.toFixed(2)}%`,
        item.exitReason || (item.isOpen ? "OPEN" : ""),
      ]
        .map((cell) => `"${String(cell).replace(/"/g, '""')}"`)
        .join(",")
    );

    const csvContent = "data:text/csv;charset=utf-8," + [headers.join(","), ...rows].join("\n");
    const encodedUri = encodeURI(csvContent);
    const link = document.createElement("a");
    link.setAttribute("href", encodedUri);
    link.setAttribute("download", `trillionaire_pnl_report_${new Date().toISOString().slice(0, 10)}.csv`);
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  };

  return (
    <div className="h-full flex flex-col bg-cockpit-card rounded-xl border border-cockpit-border overflow-hidden select-none">
      {/* Zerodha Kite-Style 3-Tab Header Navigation */}
      <div className="h-11 border-b border-cockpit-border px-3 flex items-center justify-between shrink-0">
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

        {/* Search & CSV Controls */}
        {activeTab === "EXECUTED" && (
          <div className="flex items-center space-x-2">
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
          </div>
        )}

        {activeTab === "REPORT" && (
          <div className="flex items-center space-x-2">
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
              title="Download P&L Report CSV"
              className="px-2 py-1 rounded bg-cockpit-surface hover:bg-cockpit-hover border border-cockpit-border text-cockpit-muted hover:text-white text-xs font-mono flex items-center space-x-1 transition"
            >
              <Download className="h-3 w-3 text-emerald-400" />
              <span className="hidden sm:inline">CSV</span>
            </button>
          </div>
        )}
      </div>

      {/* TAB 1: OPEN / PENDING ORDERS ONLY */}
      {activeTab === "OPEN" && (
        <div className="flex-1 overflow-y-auto">
          {pendingOrders.length === 0 ? (
            <div className="h-full flex flex-col items-center justify-center text-cockpit-muted text-xs font-mono space-y-1">
              <Activity className="h-6 w-6 text-cockpit-muted/40 mb-1" />
              <span>No open or trigger-pending orders.</span>
              <span className="text-[10px]">
                Active filled positions are tracked live under P&L Report.
              </span>
            </div>
          ) : (
            <table className="w-full text-left text-xs font-mono">
              <thead className="sticky top-0 bg-cockpit-surface/90 backdrop-blur-sm border-b border-cockpit-border text-[10px] text-cockpit-muted uppercase">
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
                  const isCancelling = !!cancellingOrders[ord.order_id];

                  return (
                    <tr key={`pending-${ord.order_id}`} className="hover:bg-cockpit-surface/50 transition">
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
                          {ord.order_type === "SL_LIMIT" ? "SL-L" : ord.order_type}
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
                        ₹{displayPrice.toFixed(2)}
                      </td>
                      <td className="py-2.5 px-3 text-right">
                        <div className="flex items-center justify-end space-x-2">
                          <span
                            className={`px-2 py-0.5 rounded text-[10px] font-semibold uppercase ${
                              isTriggerPending
                                ? "bg-amber-500/20 text-amber-300 border border-amber-500/40"
                                : "bg-blue-500/20 text-blue-400 border border-blue-500/40"
                            }`}
                          >
                            {ord.status}
                          </span>
                          {onCancelOrder && (
                            <button
                              type="button"
                              onClick={() => handleCancelOrder(ord.order_id)}
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
        <div className="flex-1 overflow-y-auto">
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
              <thead className="sticky top-0 bg-cockpit-surface/90 backdrop-blur-sm border-b border-cockpit-border text-[10px] text-cockpit-muted uppercase">
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
              <tbody className="divide-y divide-cockpit-border/40">
                {filteredExecutedOrders.map((ord) => {
                  const isBuy = ord.side === "BUY";
                  const execPrice = ord.average_fill_price ?? ord.limit_price ?? 0;
                  const isComplete = ord.status === "FILLED";
                  const isRejected = ord.status === "REJECTED";
                  const reasonBadge = ord.rejection_reason || ord.exit_reason;

                  return (
                    <tr key={ord.order_id} className="hover:bg-cockpit-surface/50 transition">
                      <td className="py-2 px-3 text-cockpit-muted whitespace-nowrap">
                        {formatIstTime(ord.created_at_ist)}
                      </td>
                      <td className="py-2 px-2">
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
                      <td className="py-2 px-3 font-bold text-white whitespace-nowrap">
                        <span>{ord.tradingsymbol}</span>
                        <span className="ml-1.5 text-[9px] px-1 py-0.2 rounded bg-cockpit-surface border border-cockpit-border text-cockpit-muted font-normal">
                          NSE
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
                })}
              </tbody>
            </table>
          )}
        </div>
      )}

      {/* TAB 3: P&L REPORT (Live Running Positions + Closed Trades Ledger & Bar Chart) */}
      {activeTab === "REPORT" && (
        <div className="flex-1 overflow-y-auto flex flex-col">
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
                  <thead className="sticky top-0 bg-cockpit-surface/90 backdrop-blur-sm border-b border-cockpit-border text-[10px] text-cockpit-muted uppercase">
                    <tr>
                      <th className="py-2 px-3">Product</th>
                      <th className="py-2 px-3">Instrument</th>
                      <th className="py-2 px-2 text-right">Qty.</th>
                      <th className="py-2 px-2 text-right">Avg.</th>
                      <th className="py-2 px-2 text-right">LTP</th>
                      <th className="py-2 px-3 text-right">P&L</th>
                      <th className="py-2 px-3 text-right">Chg.</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-cockpit-border/40">
                    {filteredReportItems.map((item) => {
                      const isProfit = item.pnl >= 0;
                      const isLong = item.qty > 0;
                      const isShort = item.qty < 0;
                      const isExiting = !!exitingSymbols[item.instrument];

                      return (
                        <tr key={item.key} className="hover:bg-cockpit-surface/50 transition">
                          <td className="py-2 px-3">
                            <span className="px-1.5 py-0.5 rounded text-[10px] font-semibold bg-purple-500/20 text-purple-300 border border-purple-500/40">
                              {item.product}
                            </span>
                          </td>
                          <td className="py-2 px-3 whitespace-nowrap">
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
                            className={`py-2 px-2 text-right font-medium tabular-nums ${
                              isLong
                                ? "text-blue-400 font-bold"
                                : isShort
                                ? "text-rose-400 font-bold"
                                : "text-cockpit-muted"
                            }`}
                          >
                            {item.qty}
                          </td>
                          <td className="py-2 px-2 text-right text-cockpit-muted tabular-nums">
                            ₹{item.avg.toFixed(2)}
                          </td>
                          <td className="py-2 px-2 text-right font-bold text-white tabular-nums">
                            ₹{item.ltp.toFixed(2)}
                          </td>
                          <td
                            className={`py-2 px-3 text-right font-bold tabular-nums ${
                              isProfit ? "text-emerald-400" : "text-rose-400"
                            }`}
                          >
                            {isProfit ? "+" : ""}₹{item.pnl.toFixed(2)}
                          </td>
                          <td className="py-2 px-3 text-right tabular-nums">
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
                                  onClick={() => handleExitPosition(item.instrument)}
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
                  <tfoot className="border-t-2 border-cockpit-border bg-cockpit-surface/80 font-bold">
                    <tr>
                      <td colSpan={2} className="py-2.5 px-3 text-white uppercase text-xs">
                        Total P&L ({filteredReportItems.length})
                      </td>
                      <td colSpan={3} className="py-2.5 px-2 text-right text-cockpit-muted text-[11px]">
                        Net Realized + Unrealized
                      </td>
                      <td
                        className={`py-2.5 px-3 text-right text-sm tabular-nums font-bold ${
                          totalReportPnl >= 0 ? "text-emerald-400" : "text-rose-400"
                        }`}
                      >
                        {totalReportPnl >= 0 ? "+" : ""}₹
                        {totalReportPnl.toLocaleString("en-IN", {
                          minimumFractionDigits: 2,
                          maximumFractionDigits: 2,
                        })}
                      </td>
                      <td />
                    </tr>
                  </tfoot>
                </table>
              </div>

              {/* Bottom Zero-Centered Horizontal P&L Breakdown Chart */}
              <div className="p-3 border-t border-cockpit-border bg-cockpit-surface/30">
                <div className="text-[10px] font-mono uppercase tracking-wider text-cockpit-muted mb-2 flex items-center justify-between">
                  <span>Horizontal P&L Breakdown</span>
                  <span className="text-[9px] text-cockpit-muted/60">
                    Zero-Centered (Losing ← 0 → Profitable)
                  </span>
                </div>
                <div className="space-y-1.5">
                  {reportItems.map((item) => {
                    const isProfit = item.pnl >= 0;
                    const barWidthPct =
                      maxAbsPnl > 0
                        ? Math.min(100, (Math.abs(item.pnl) / maxAbsPnl) * 100)
                        : 0;
                    return (
                      <div
                        key={`bar-${item.key}`}
                        className="flex items-center text-xs font-mono"
                      >
                        <div className="w-32 sm:w-40 shrink-0 truncate font-semibold text-white">
                          <span>{item.instrument}</span>
                          <span className="text-[10px] text-purple-400 ml-1">
                            ({item.isOpen ? "LIVE" : item.exitReason || "MIS"})
                          </span>
                        </div>
                        <div className="flex-1 grid grid-cols-2 gap-0 h-4 bg-cockpit-surface rounded border border-cockpit-border/50 relative overflow-hidden">
                          <div className="relative flex justify-end items-center border-r border-cockpit-border/80 pr-1">
                            {!isProfit && (
                              <div
                                style={{ width: `${barWidthPct}%` }}
                                className="h-full bg-gradient-to-l from-rose-500 to-rose-600/70 rounded-l"
                              />
                            )}
                          </div>
                          <div className="relative flex justify-start items-center pl-1">
                            {isProfit && (
                              <div
                                style={{ width: `${barWidthPct}%` }}
                                className="h-full bg-gradient-to-r from-blue-500 to-emerald-500 rounded-r"
                              />
                            )}
                          </div>
                        </div>
                        <div
                          className={`w-24 text-right shrink-0 tabular-nums font-bold text-xs ${
                            isProfit ? "text-emerald-400" : "text-rose-400"
                          }`}
                        >
                          {isProfit ? "+" : ""}₹{item.pnl.toFixed(2)}
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
};
