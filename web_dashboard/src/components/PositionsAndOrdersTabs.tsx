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
  signals?: Signal[];
  anomalies?: AnomalyAlert[];
  riskAlerts?: RiskAlert[];
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
}) => {
  const [activeTab, setActiveTab] = useState<"OPEN" | "ORDERS" | "REPORT">("OPEN");
  const [ordersSearch, setOrdersSearch] = useState("");
  const [reportSearch, setReportSearch] = useState("");
  const [exitingSymbols, setExitingSymbols] = useState<Record<string, boolean>>({});

  // Strictly active net open positions (quantity > 0)
  const activePositions = useMemo(
    () => positions.filter((p) => (p.quantity || 0) > 0),
    [positions]
  );

  const handleExit = async (symbol: string) => {
    if (!onExitPosition) return;
    setExitingSymbols((prev) => ({ ...prev, [symbol]: true }));
    try {
      await onExitPosition(symbol);
    } finally {
      setExitingSymbols((prev) => ({ ...prev, [symbol]: false }));
    }
  };

  // All session orders (OPEN, TRIGGER PENDING, PARTIALLY_FILLED, FILLED, CANCELLED, REJECTED)
  const sortedOrders = useMemo(() => {
    return orders
      .slice()
      .sort(
        (a, b) =>
          new Date(b.created_at_ist).getTime() - new Date(a.created_at_ist).getTime()
      );
  }, [orders]);

  const filteredOrders = useMemo(() => {
    if (!ordersSearch.trim()) return sortedOrders;
    const q = ordersSearch.toLowerCase().trim();
    return sortedOrders.filter(
      (o) =>
        o.tradingsymbol.toLowerCase().includes(q) ||
        o.side.toLowerCase().includes(q) ||
        o.product.toLowerCase().includes(q) ||
        o.status.toLowerCase().includes(q) ||
        (o.order_type && o.order_type.toLowerCase().includes(q)) ||
        (o.exit_reason && o.exit_reason.toLowerCase().includes(q)) ||
        (o.rejection_reason && o.rejection_reason.toLowerCase().includes(q))
    );
  }, [sortedOrders, ordersSearch]);

  const downloadOrdersCsv = () => {
    if (filteredOrders.length === 0) return;
    const headers = [
      "Order ID",
      "Time (IST)",
      "Side",
      "Symbol",
      "Product",
      "Order Type",
      "Qty",
      "Limit/Trigger Price",
      "Fill Price",
      "Status",
      "Exit Reason",
      "Rejection Reason",
    ];
    const rows = filteredOrders.map((o) =>
      [
        o.order_id,
        formatIstTime(o.created_at_ist),
        o.side,
        o.tradingsymbol,
        o.product || "MIS",
        o.order_type,
        `${o.filled_quantity}/${o.quantity}`,
        (o.trigger_price ?? o.limit_price ?? 0).toFixed(2),
        (o.average_fill_price ?? o.limit_price ?? 0).toFixed(2),
        o.status,
        o.exit_reason || "",
        o.rejection_reason || "",
      ]
        .map((cell) => `"${String(cell).replace(/"/g, '""')}"`)
        .join(",")
    );

    const csvContent = "data:text/csv;charset=utf-8," + [headers.join(","), ...rows].join("\n");
    const encodedUri = encodeURI(csvContent);
    const link = document.createElement("a");
    link.setAttribute("href", encodedUri);
    link.setAttribute("download", `trillionaire_orders_${new Date().toISOString().slice(0, 10)}.csv`);
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  };

  // Report reads strictly from immutable ClosedTrades ledger
  const sortedClosedTrades = useMemo(() => {
    return closedTrades
      .slice()
      .sort((a, b) => new Date(b.exit_time).getTime() - new Date(a.exit_time).getTime());
  }, [closedTrades]);

  const filteredReportItems = useMemo(() => {
    if (!reportSearch.trim()) return sortedClosedTrades;
    const q = reportSearch.toLowerCase().trim();
    return sortedClosedTrades.filter(
      (t) =>
        t.symbol.toLowerCase().includes(q) ||
        t.product.toLowerCase().includes(q) ||
        t.side.toLowerCase().includes(q) ||
        t.exit_reason.toLowerCase().includes(q)
    );
  }, [sortedClosedTrades, reportSearch]);

  const totalReportPnl = useMemo(() => {
    return Math.round(
      filteredReportItems.reduce((acc, item) => acc + (item.realized_pnl || 0), 0) * 100
    ) / 100;
  }, [filteredReportItems]);

  const maxAbsPnl = useMemo(() => {
    if (sortedClosedTrades.length === 0) return 1;
    return Math.max(...sortedClosedTrades.map((t) => Math.abs(t.realized_pnl || 0)), 1);
  }, [sortedClosedTrades]);

  const downloadReportCsv = () => {
    if (filteredReportItems.length === 0) return;
    const headers = [
      "Order ID",
      "Entry Time (IST)",
      "Exit Time (IST)",
      "Product",
      "Side",
      "Symbol",
      "Entry Price",
      "SL Price",
      "Target Price",
      "Exit Price",
      "Qty",
      "Realized PnL",
      "Chg Pct",
      "Exit Reason",
    ];
    const rows = filteredReportItems.map((t) =>
      [
        t.order_id,
        formatIstTime(t.entry_time),
        formatIstTime(t.exit_time),
        t.product,
        t.side,
        t.symbol,
        t.entry_price.toFixed(2),
        t.sl_price.toFixed(2),
        t.target_price.toFixed(2),
        t.actual_exit_price.toFixed(2),
        t.filled_qty,
        t.realized_pnl.toFixed(2),
        `${t.pnl_pct.toFixed(2)}%`,
        t.exit_reason,
      ]
        .map((cell) => `"${String(cell).replace(/"/g, '""')}"`)
        .join(",")
    );

    const csvContent = "data:text/csv;charset=utf-8," + [headers.join(","), ...rows].join("\n");
    const encodedUri = encodeURI(csvContent);
    const link = document.createElement("a");
    link.setAttribute("href", encodedUri);
    link.setAttribute("download", `trillionaire_report_${new Date().toISOString().slice(0, 10)}.csv`);
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  };

  return (
    <div className="h-full flex flex-col bg-cockpit-card rounded-xl border border-cockpit-border overflow-hidden select-none">
      {/* 3-Tab Header Navigation */}
      <div className="h-11 border-b border-cockpit-border px-3 flex items-center justify-between shrink-0">
        <div className="flex items-center space-x-1">
          {/* Tab 1: Open Positions */}
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
            <span>Open</span>
            <span
              className={`text-[10px] px-1.5 py-0.2 rounded font-bold ${
                activePositions.length > 0
                  ? "bg-blue-500/20 text-blue-300 border border-blue-500/40"
                  : "bg-cockpit-card text-cockpit-muted"
              }`}
            >
              {activePositions.length}
            </span>
          </button>

          {/* Tab 2: Orders */}
          <button
            type="button"
            onClick={() => setActiveTab("ORDERS")}
            className={`px-3 py-1.5 rounded-lg text-xs font-mono font-medium flex items-center space-x-1.5 transition ${
              activeTab === "ORDERS"
                ? "bg-cockpit-surface text-white border border-cockpit-borderBright shadow-sm"
                : "text-cockpit-muted hover:text-white"
            }`}
          >
            <ListOrdered className="h-3.5 w-3.5 text-purple-400" />
            <span>Orders</span>
            <span className="text-[10px] px-1.5 py-0.2 rounded bg-cockpit-card text-cockpit-muted">
              {sortedOrders.length}
            </span>
          </button>

          {/* Tab 3: Report (Closed Trades Ledger) */}
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
            <span>Report</span>
            <span className="text-[10px] px-1.5 py-0.2 rounded bg-cockpit-card text-cockpit-muted">
              {sortedClosedTrades.length}
            </span>
          </button>
        </div>

        {/* Search & CSV Controls */}
        {activeTab === "ORDERS" && (
          <div className="flex items-center space-x-2">
            <div className="relative">
              <Search className="h-3 w-3 absolute left-2 top-2 text-cockpit-muted" />
              <input
                type="text"
                placeholder="Search orders..."
                value={ordersSearch}
                onChange={(e) => setOrdersSearch(e.target.value)}
                className="pl-7 pr-6 py-0.5 text-xs bg-cockpit-surface border border-cockpit-border rounded text-white placeholder-cockpit-muted/60 focus:outline-none focus:border-cockpit-borderBright w-36 sm:w-44"
              />
              {ordersSearch && (
                <button
                  type="button"
                  onClick={() => setOrdersSearch("")}
                  className="absolute right-1.5 top-1.5 text-cockpit-muted hover:text-white"
                >
                  <X className="h-3 w-3" />
                </button>
              )}
            </div>
            <button
              type="button"
              onClick={downloadOrdersCsv}
              title="Download Orders CSV"
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
                placeholder="Search report..."
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
              title="Download Report CSV"
              className="px-2 py-1 rounded bg-cockpit-surface hover:bg-cockpit-hover border border-cockpit-border text-cockpit-muted hover:text-white text-xs font-mono flex items-center space-x-1 transition"
            >
              <Download className="h-3 w-3 text-emerald-400" />
              <span className="hidden sm:inline">CSV</span>
            </button>
          </div>
        )}
      </div>

      {/* TAB 1: OPEN POSITIONS ONLY */}
      {activeTab === "OPEN" && (
        <div className="flex-1 overflow-y-auto">
          {activePositions.length === 0 ? (
            <div className="h-full flex flex-col items-center justify-center text-cockpit-muted text-xs font-mono space-y-1">
              <Activity className="h-6 w-6 text-cockpit-muted/40 mb-1" />
              <span>No active open positions.</span>
              <span className="text-[10px]">
                Pending SL/Target legs are displayed cleanly in the Orders tab.
              </span>
            </div>
          ) : (
            <table className="w-full text-left text-xs font-mono">
              <thead className="sticky top-0 bg-cockpit-surface/90 backdrop-blur-sm border-b border-cockpit-border text-[10px] text-cockpit-muted uppercase">
                <tr>
                  <th className="py-2 px-3">Time</th>
                  <th className="py-2 px-2">Type</th>
                  <th className="py-2 px-3">Instrument</th>
                  <th className="py-2 px-2 text-right">Entry (Avg.)</th>
                  <th className="py-2 px-2 text-right">SL</th>
                  <th className="py-2 px-2 text-right">Target</th>
                  <th className="py-2 px-2 text-right">LTP</th>
                  <th className="py-2 px-2 text-right">Qty.</th>
                  <th className="py-2 px-2 text-right">Unrealized P&L</th>
                  <th className="py-2 px-3 text-right">Action</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-cockpit-border/40">
                {activePositions.map((pos) => {
                  const sym = pos.tradingsymbol;
                  const isBuy = pos.side === "BUY";
                  const liveLtp = ticks[sym]?.ltp ?? pos.ltp ?? pos.average_entry_price;
                  const unrealized = isBuy
                    ? (liveLtp - pos.average_entry_price) * pos.quantity
                    : (pos.average_entry_price - liveLtp) * pos.quantity;
                  const isExiting = !!exitingSymbols[sym];
                  const isProfit = unrealized >= 0;

                  return (
                    <tr key={`pos-${sym}`} className="hover:bg-cockpit-surface/50 transition">
                      <td className="py-2.5 px-3 text-cockpit-muted whitespace-nowrap">
                        {formatIstTime(pos.entry_time)}
                      </td>
                      <td className="py-2.5 px-2">
                        <span
                          className={`px-2 py-0.5 rounded text-[10px] font-bold ${
                            isBuy
                              ? "bg-blue-500/20 text-blue-400 border border-blue-500/40"
                              : "bg-rose-500/20 text-rose-400 border border-rose-500/40"
                          }`}
                        >
                          {pos.side}
                        </span>
                      </td>
                      <td className="py-2.5 px-3 font-bold text-white whitespace-nowrap">
                        <span>{sym}</span>
                        <span className="ml-1.5 text-[9px] px-1 py-0.2 rounded bg-cockpit-surface border border-cockpit-border text-cockpit-muted font-normal">
                          MIS
                        </span>
                      </td>
                      <td className="py-2.5 px-2 text-right text-slate-300 tabular-nums">
                        ₹{pos.average_entry_price.toFixed(2)}
                      </td>
                      <td className="py-2.5 px-2 text-right text-rose-400 tabular-nums">
                        ₹{pos.stop_loss.toFixed(2)}
                      </td>
                      <td className="py-2.5 px-2 text-right text-emerald-400 tabular-nums">
                        ₹{pos.target_price.toFixed(2)}
                      </td>
                      <td className="py-2.5 px-2 text-right font-bold text-white tabular-nums">
                        ₹{liveLtp.toFixed(2)}
                      </td>
                      <td className="py-2.5 px-2 text-right font-medium text-white tabular-nums">
                        {pos.quantity}
                      </td>
                      <td
                        className={`py-2.5 px-2 text-right font-bold tabular-nums ${
                          isProfit ? "text-emerald-400" : "text-rose-400"
                        }`}
                      >
                        {isProfit ? "+" : ""}₹{unrealized.toFixed(2)}
                      </td>
                      <td className="py-2.5 px-3 text-right">
                        <button
                          type="button"
                          onClick={() => handleExit(sym)}
                          disabled={isExiting}
                          className={`px-2.5 py-0.5 rounded text-[10px] font-bold border transition ${
                            isExiting
                              ? "bg-rose-500/10 text-rose-500 border-rose-500/30 cursor-not-allowed"
                              : "bg-rose-500/20 hover:bg-rose-500/30 text-rose-300 border-rose-500/50"
                          }`}
                        >
                          {isExiting ? "Exiting..." : "Exit"}
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>
      )}

      {/* TAB 2: ORDERS (Full Order Book: OPEN, TRIGGER PENDING, FILLED, CANCELLED, REJECTED) */}
      {activeTab === "ORDERS" && (
        <div className="flex-1 overflow-y-auto">
          {filteredOrders.length === 0 ? (
            <div className="h-full flex items-center justify-center text-cockpit-muted text-xs font-mono">
              <span>
                {ordersSearch
                  ? "No orders matching query."
                  : "No orders recorded in current session."}
              </span>
            </div>
          ) : (
            <table className="w-full text-left text-xs font-mono">
              <thead className="sticky top-0 bg-cockpit-surface/90 backdrop-blur-sm border-b border-cockpit-border text-[10px] text-cockpit-muted uppercase">
                <tr>
                  <th className="py-2 px-3">Time</th>
                  <th className="py-2 px-2">Side</th>
                  <th className="py-2 px-3">Instrument</th>
                  <th className="py-2 px-2">Order Type</th>
                  <th className="py-2 px-2 text-right">Qty.</th>
                  <th className="py-2 px-2 text-right">Price</th>
                  <th className="py-2 px-2 text-center">Status</th>
                  <th className="py-2 px-3">Note / Reason</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-cockpit-border/40">
                {filteredOrders.map((ord) => {
                  const isBuy = ord.side === "BUY";
                  const displayPrice =
                    ord.average_fill_price || ord.trigger_price || ord.limit_price || 0;
                  const isComplete = ord.status === "FILLED";
                  const isRejected = ord.status === "REJECTED";
                  const isCancelled = ord.status === "CANCELLED";
                  const isTriggerPending = ord.status === "TRIGGER PENDING";
                  const note =
                    ord.rejection_reason ||
                    ord.exit_reason ||
                    (ord.is_exit ? "OCO Exit Leg" : "Entry");

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
                        <span className="ml-1.5 text-[9px] px-1 py-0.2 rounded bg-purple-500/20 text-purple-300 border border-purple-500/40 font-normal">
                          {ord.product || "MIS"}
                        </span>
                      </td>
                      <td className="py-2 px-2 text-slate-300 text-[11px]">
                        {ord.order_type === "SL_LIMIT" ? "SL-L" : ord.order_type}
                      </td>
                      <td className="py-2 px-2 text-right font-medium text-white tabular-nums">
                        {ord.filled_quantity}/{ord.quantity}
                      </td>
                      <td className="py-2 px-2 text-right font-medium text-white tabular-nums">
                        ₹{displayPrice.toFixed(2)}
                      </td>
                      <td className="py-2 px-2 text-center">
                        <span
                          className={`px-2 py-0.5 rounded text-[10px] font-semibold uppercase ${
                            isComplete
                              ? "bg-emerald-500/20 text-emerald-400 border border-emerald-500/40"
                              : isRejected
                              ? "bg-rose-500/20 text-rose-400 border border-rose-500/40"
                              : isCancelled
                              ? "bg-slate-700/40 text-slate-400 border border-slate-600/40"
                              : isTriggerPending
                              ? "bg-amber-500/20 text-amber-300 border border-amber-500/40"
                              : "bg-blue-500/20 text-blue-400 border border-blue-500/40"
                          }`}
                        >
                          {isComplete ? "COMPLETE" : ord.status}
                        </span>
                      </td>
                      <td
                        title={note}
                        className={`py-2 px-3 text-[11px] max-w-[220px] truncate ${
                          isRejected ? "text-rose-400" : "text-cockpit-muted"
                        }`}
                      >
                        {note}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>
      )}

      {/* TAB 3: REPORT (Immutable Closed-Trades Ledger) */}
      {activeTab === "REPORT" && (
        <div className="flex-1 overflow-y-auto flex flex-col">
          {filteredReportItems.length === 0 ? (
            <div className="h-full flex items-center justify-center text-cockpit-muted text-xs font-mono">
              <span>
                {reportSearch
                  ? "No closed trades matching query."
                  : "No closed trades recorded today."}
              </span>
            </div>
          ) : (
            <div className="flex-1 flex flex-col">
              <div className="overflow-x-auto">
                <table className="w-full text-left text-xs font-mono">
                  <thead className="sticky top-0 bg-cockpit-surface/90 backdrop-blur-sm border-b border-cockpit-border text-[10px] text-cockpit-muted uppercase">
                    <tr>
                      <th className="py-2 px-2">Time</th>
                      <th className="py-2 px-2">Side</th>
                      <th className="py-2 px-2">Symbol</th>
                      <th className="py-2 px-2 text-right">Entry</th>
                      <th className="py-2 px-2 text-right">SL</th>
                      <th className="py-2 px-2 text-right">Target</th>
                      <th className="py-2 px-2 text-right">Exit Price</th>
                      <th className="py-2 px-2 text-right">Qty</th>
                      <th className="py-2 px-2 text-right">Realized P&L</th>
                      <th className="py-2 px-2 text-right">Chg %</th>
                      <th className="py-2 px-3 text-center">Exit Reason</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-cockpit-border/40">
                    {filteredReportItems.map((trade) => {
                      const isProfit = trade.realized_pnl >= 0;
                      const isBuy = trade.side === "BUY";

                      return (
                        <tr
                          key={`${trade.order_id}-${trade.exit_time}`}
                          className="hover:bg-cockpit-surface/50 transition"
                        >
                          <td className="py-2 px-2 text-cockpit-muted whitespace-nowrap">
                            {formatIstTime(trade.exit_time)}
                          </td>
                          <td className="py-2 px-2">
                            <span
                              className={`px-1.5 py-0.5 rounded text-[10px] font-bold ${
                                isBuy
                                  ? "bg-blue-500/20 text-blue-400 border border-blue-500/40"
                                  : "bg-rose-500/20 text-rose-400 border border-rose-500/40"
                              }`}
                            >
                              {trade.side}
                            </span>
                          </td>
                          <td className="py-2 px-2 font-bold text-white whitespace-nowrap">
                            <span>{trade.symbol}</span>
                            <span className="ml-1 text-[9px] px-1 py-0.2 rounded bg-purple-500/20 text-purple-300 border border-purple-500/40 font-normal">
                              {trade.product}
                            </span>
                          </td>
                          <td className="py-2 px-2 text-right text-slate-300 tabular-nums">
                            ₹{trade.entry_price.toFixed(2)}
                          </td>
                          <td className="py-2 px-2 text-right text-rose-400 tabular-nums">
                            ₹{trade.sl_price.toFixed(2)}
                          </td>
                          <td className="py-2 px-2 text-right text-emerald-400 tabular-nums">
                            ₹{trade.target_price.toFixed(2)}
                          </td>
                          <td className="py-2 px-2 text-right font-bold text-white tabular-nums">
                            ₹{trade.actual_exit_price.toFixed(2)}
                          </td>
                          <td className="py-2 px-2 text-right text-white tabular-nums">
                            {trade.filled_qty}
                          </td>
                          <td
                            className={`py-2 px-2 text-right font-bold tabular-nums ${
                              isProfit ? "text-emerald-400" : "text-rose-400"
                            }`}
                          >
                            {isProfit ? "+" : ""}₹{trade.realized_pnl.toFixed(2)}
                          </td>
                          <td
                            className={`py-2 px-2 text-right tabular-nums font-semibold ${
                              trade.pnl_pct >= 0 ? "text-emerald-400" : "text-rose-400"
                            }`}
                          >
                            {trade.pnl_pct >= 0 ? "+" : ""}
                            {trade.pnl_pct.toFixed(2)}%
                          </td>
                          <td className="py-2 px-3 text-center">
                            <span
                              className={`px-2 py-0.5 rounded text-[10px] font-semibold border whitespace-nowrap ${getExitReasonBadgeClass(
                                trade.exit_reason
                              )}`}
                            >
                              {trade.exit_reason}
                            </span>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                  <tfoot className="border-t-2 border-cockpit-border bg-cockpit-surface/80 font-bold">
                    <tr>
                      <td colSpan={3} className="py-2.5 px-3 text-white uppercase text-xs">
                        Total ({filteredReportItems.length} Closed Trades)
                      </td>
                      <td colSpan={5} className="py-2.5 px-2 text-right text-cockpit-muted text-[11px]">
                        Realized Ledger P&L (incl. charges)
                      </td>
                      <td
                        className={`py-2.5 px-2 text-right text-sm tabular-nums font-bold ${
                          totalReportPnl >= 0 ? "text-emerald-400" : "text-rose-400"
                        }`}
                      >
                        {totalReportPnl >= 0 ? "+" : ""}₹
                        {totalReportPnl.toLocaleString("en-IN", {
                          minimumFractionDigits: 2,
                          maximumFractionDigits: 2,
                        })}
                      </td>
                      <td colSpan={2} />
                    </tr>
                  </tfoot>
                </table>
              </div>

              {/* Bottom Horizontal P&L Breakdown */}
              <div className="p-3 border-t border-cockpit-border bg-cockpit-surface/30">
                <div className="text-[10px] font-mono uppercase tracking-wider text-cockpit-muted mb-2 flex items-center justify-between">
                  <span>Horizontal Closed-Trade P&L Breakdown</span>
                  <span className="text-[9px] text-cockpit-muted/60">
                    Zero-Centered (Losing ← 0 → Profitable)
                  </span>
                </div>
                <div className="space-y-1.5">
                  {sortedClosedTrades.map((trade) => {
                    const isProfit = trade.realized_pnl >= 0;
                    const barWidthPct =
                      maxAbsPnl > 0
                        ? Math.min(100, (Math.abs(trade.realized_pnl) / maxAbsPnl) * 100)
                        : 0;
                    return (
                      <div
                        key={`bar-${trade.order_id}`}
                        className="flex items-center text-xs font-mono"
                      >
                        <div className="w-32 sm:w-40 shrink-0 truncate font-semibold text-white">
                          <span>{trade.symbol}</span>
                          <span className="text-[10px] text-purple-400 ml-1">
                            ({trade.exit_reason})
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
                          {isProfit ? "+" : ""}₹{trade.realized_pnl.toFixed(2)}
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
