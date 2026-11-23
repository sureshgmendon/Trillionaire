"use client";

import React, { useState, useMemo } from "react";
import {
  Activity,
  ArrowDown,
  ArrowUp,
  Download,
  FileSpreadsheet,
  ListOrdered,
  PieChart,
  Search,
  X,
  XCircle,
} from "lucide-react";
import {
  AnomalyAlert,
  Order,
  Position,
  RiskAlert,
  Signal,
  Tick,
} from "@/types/telemetry";

export interface PositionsAndOrdersTabsProps {
  positions: Position[];
  orders: Order[];
  ticks?: Record<string, Tick>;
  onExitPosition?: (symbol: string) => Promise<{ success: boolean; message?: string }>;
  signals?: Signal[];
  anomalies?: AnomalyAlert[];
  riskAlerts?: RiskAlert[];
}

interface ReportItem {
  product: string;
  instrument: string;
  qty: number;
  avg: number;
  ltp: number;
  pnl: number;
  chg_pct: number;
  isOpen: boolean;
}

export const PositionsAndOrdersTabs: React.FC<PositionsAndOrdersTabsProps> = ({
  positions = [],
  orders = [],
  ticks = {},
  onExitPosition,
}) => {
  const [activeTab, setActiveTab] = useState<"OPEN" | "ORDERS" | "REPORT">("OPEN");
  const [ordersSearch, setOrdersSearch] = useState("");
  const [reportSearch, setReportSearch] = useState("");
  const [exitingSymbols, setExitingSymbols] = useState<Record<string, boolean>>({});

  // ---------------------------------------------------------------------------
  // Tab 1: OPEN DATA (Open Positions & Active Orders)
  // ---------------------------------------------------------------------------
  const openOrders = useMemo(() => {
    return orders.filter(
      (o) => o.status === "OPEN" || o.status === "PENDING_NEW"
    );
  }, [orders]);

  const totalOpenCount = positions.length + openOrders.length;

  const handleExit = async (symbol: string) => {
    if (!onExitPosition) return;
    setExitingSymbols((prev) => ({ ...prev, [symbol]: true }));
    try {
      await onExitPosition(symbol);
    } finally {
      setExitingSymbols((prev) => ({ ...prev, [symbol]: false }));
    }
  };

  // ---------------------------------------------------------------------------
  // Tab 2: EXECUTED ORDERS
  // ---------------------------------------------------------------------------
  const executedOrders = useMemo(() => {
    return orders.filter(
      (o) => o.status === "FILLED" || o.status === "CANCELLED" || o.status === "REJECTED"
    );
  }, [orders]);

  const filteredOrders = useMemo(() => {
    if (!ordersSearch.trim()) return executedOrders;
    const q = ordersSearch.toLowerCase().trim();
    return executedOrders.filter(
      (o) =>
        o.tradingsymbol.toLowerCase().includes(q) ||
        o.side.toLowerCase().includes(q) ||
        o.product.toLowerCase().includes(q) ||
        o.status.toLowerCase().includes(q) ||
        (o.exit_reason && o.exit_reason.toLowerCase().includes(q))
    );
  }, [executedOrders, ordersSearch]);

  const downloadOrdersCsv = () => {
    if (filteredOrders.length === 0) return;
    const headers = ["Time", "Type", "Instrument", "Product", "Qty", "Avg Price", "Status", "Exit Reason"];
    const rows = filteredOrders.map((o) => {
      const timeStr = o.created_at_ist
        ? new Date(o.created_at_ist).toLocaleTimeString("en-IN", {
            hour12: false,
            hour: "2-digit",
            minute: "2-digit",
            second: "2-digit",
          })
        : "";
      return [
        timeStr,
        o.side,
        o.tradingsymbol,
        o.product || "MIS",
        `${o.filled_quantity}/${o.quantity}`,
        (o.average_fill_price || o.limit_price || 0).toFixed(2),
        o.status,
        o.exit_reason || "",
      ].map((cell) => `"${String(cell).replace(/"/g, '""')}"`).join(",");
    });

    const csvContent = "data:text/csv;charset=utf-8," + [headers.join(","), ...rows].join("\n");
    const encodedUri = encodeURI(csvContent);
    const link = document.createElement("a");
    link.setAttribute("href", encodedUri);
    link.setAttribute("download", `trillionaire_orders_${new Date().toISOString().slice(0, 10)}.csv`);
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  };

  // ---------------------------------------------------------------------------
  // Tab 3: REPORT DATA (Round-trip + Open Positions Summary)
  // ---------------------------------------------------------------------------
  const reportItems: ReportItem[] = useMemo(() => {
    const symbolMap = new Map<string, ReportItem>();

    // 1. Process active open positions
    for (const pos of positions) {
      const sym = pos.tradingsymbol;
      const liveLtp = ticks[sym]?.ltp ?? pos.ltp ?? pos.average_entry_price;
      const avg = pos.average_entry_price;
      const isBuy = pos.side === "BUY";
      const netQty = isBuy ? pos.quantity : -pos.quantity;
      const unrl = isBuy ? (liveLtp - avg) * pos.quantity : (avg - liveLtp) * pos.quantity;
      const chgPct = avg > 0 ? ((liveLtp - avg) / avg) * 100 * (isBuy ? 1 : -1) : 0;

      symbolMap.set(sym, {
        product: "MIS",
        instrument: sym,
        qty: netQty,
        avg: Math.round(avg * 100) / 100,
        ltp: Math.round(liveLtp * 100) / 100,
        pnl: Math.round(unrl * 100) / 100,
        chg_pct: Math.round(chgPct * 100) / 100,
        isOpen: true,
      });
    }

    // 2. Process closed executed trades for instruments not currently open or closed portions
    // Group orders by symbol
    const ordersBySym = new Map<string, Order[]>();
    for (const ord of executedOrders) {
      if (ord.status !== "FILLED") continue;
      const list = ordersBySym.get(ord.tradingsymbol) || [];
      list.push(ord);
      ordersBySym.set(ord.tradingsymbol, list);
    }

    ordersBySym.forEach((symOrders, sym) => {
      if (symbolMap.has(sym)) {
        // If already in map from open positions, compute realized PnL addition
        let realizedPnl = 0;
        let totalBuyQty = 0;
        let totalBuyVal = 0;
        let totalSellQty = 0;
        let totalSellVal = 0;

        for (const ord of symOrders) {
          const fillPrice = ord.average_fill_price || ord.limit_price || 0;
          if (ord.side === "BUY") {
            totalBuyQty += ord.filled_quantity;
            totalBuyVal += ord.filled_quantity * fillPrice;
          } else {
            totalSellQty += ord.filled_quantity;
            totalSellVal += ord.filled_quantity * fillPrice;
          }
        }

        const closedPairs = Math.min(totalBuyQty, totalSellQty);
        if (closedPairs > 0 && totalBuyQty > 0 && totalSellQty > 0) {
          const buyAvg = totalBuyVal / totalBuyQty;
          const sellAvg = totalSellVal / totalSellQty;
          realizedPnl = (sellAvg - buyAvg) * closedPairs;
        }

        const item = symbolMap.get(sym)!;
        item.pnl = Math.round((item.pnl + realizedPnl) * 100) / 100;
      } else {
        // Fully closed position
        let totalBuyQty = 0;
        let totalBuyVal = 0;
        let totalSellQty = 0;
        let totalSellVal = 0;

        for (const ord of symOrders) {
          const fillPrice = ord.average_fill_price || ord.limit_price || 0;
          if (ord.side === "BUY") {
            totalBuyQty += ord.filled_quantity;
            totalBuyVal += ord.filled_quantity * fillPrice;
          } else {
            totalSellQty += ord.filled_quantity;
            totalSellVal += ord.filled_quantity * fillPrice;
          }
        }

        const buyAvg = totalBuyQty > 0 ? totalBuyVal / totalBuyQty : 0;
        const sellAvg = totalSellQty > 0 ? totalSellVal / totalSellQty : 0;
        const closedQty = Math.min(totalBuyQty, totalSellQty);
        const pnl = closedQty > 0 ? (sellAvg - buyAvg) * closedQty : 0;
        const chgPct = buyAvg > 0 ? ((sellAvg - buyAvg) / buyAvg) * 100 : 0;

        symbolMap.set(sym, {
          product: "MIS",
          instrument: sym,
          qty: 0,
          avg: Math.round(buyAvg * 100) / 100,
          ltp: Math.round(sellAvg * 100) / 100,
          pnl: Math.round(pnl * 100) / 100,
          chg_pct: Math.round(chgPct * 100) / 100,
          isOpen: false,
        });
      }
    });

    return Array.from(symbolMap.values());
  }, [positions, executedOrders, ticks]);

  const filteredReportItems = useMemo(() => {
    if (!reportSearch.trim()) return reportItems;
    const q = reportSearch.toLowerCase().trim();
    return reportItems.filter(
      (item) =>
        item.instrument.toLowerCase().includes(q) ||
        item.product.toLowerCase().includes(q)
    );
  }, [reportItems, reportSearch]);

  const totalReportPnl = useMemo(() => {
    return Math.round(filteredReportItems.reduce((acc, i) => acc + i.pnl, 0) * 100) / 100;
  }, [filteredReportItems]);

  const maxAbsPnl = useMemo(() => {
    const maxVal = Math.max(...reportItems.map((i) => Math.abs(i.pnl)), 1);
    return maxVal;
  }, [reportItems]);

  const downloadReportCsv = () => {
    if (filteredReportItems.length === 0) return;
    const headers = ["Product", "Instrument", "Qty", "Avg", "LTP", "P&L", "Chg Pct"];
    const rows = filteredReportItems.map((i) => {
      return [
        i.product,
        i.instrument,
        i.qty,
        i.avg.toFixed(2),
        i.ltp.toFixed(2),
        i.pnl.toFixed(2),
        `${i.chg_pct.toFixed(2)}%`,
      ].map((cell) => `"${String(cell).replace(/"/g, '""')}"`).join(",");
    });

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
          {/* Tab 1: Open */}
          <button
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
                totalOpenCount > 0
                  ? "bg-blue-500/20 text-blue-300 border border-blue-500/40"
                  : "bg-cockpit-card text-cockpit-muted"
              }`}
            >
              {totalOpenCount}
            </span>
          </button>

          {/* Tab 2: Orders */}
          <button
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
              {executedOrders.length}
            </span>
          </button>

          {/* Tab 3: Report */}
          <button
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
              {reportItems.length}
            </span>
          </button>
        </div>

        {/* Tab-Specific Top Right Actions */}
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
                  onClick={() => setOrdersSearch("")}
                  className="absolute right-1.5 top-1.5 text-cockpit-muted hover:text-white"
                >
                  <X className="h-3 w-3" />
                </button>
              )}
            </div>
            <button
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
                  onClick={() => setReportSearch("")}
                  className="absolute right-1.5 top-1.5 text-cockpit-muted hover:text-white"
                >
                  <X className="h-3 w-3" />
                </button>
              )}
            </div>
            <button
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

      {/* ------------------------------------------------------------------- */}
      {/* TAB 1: OPEN (Open Orders & Active Positions) */}
      {/* ------------------------------------------------------------------- */}
      {activeTab === "OPEN" && (
        <div className="flex-1 overflow-y-auto">
          {positions.length === 0 && openOrders.length === 0 ? (
            <div className="h-full flex flex-col items-center justify-center text-cockpit-muted text-xs font-mono space-y-1">
              <Activity className="h-6 w-6 text-cockpit-muted/40 mb-1" />
              <span>No active open orders or positions.</span>
              <span className="text-[10px]">Scanning active cluster regimes...</span>
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
                {/* 1. Active Open Positions */}
                {positions.map((pos) => {
                  const sym = pos.tradingsymbol;
                  const isBuy = pos.side === "BUY";
                  const liveLtp = ticks[sym]?.ltp ?? pos.ltp ?? pos.average_entry_price;
                  const isExiting = !!exitingSymbols[sym];

                  return (
                    <tr key={`pos-${sym}`} className="hover:bg-cockpit-surface/50 transition">
                      {/* Time */}
                      <td className="py-2.5 px-3 text-cockpit-muted whitespace-nowrap">
                        {new Date().toLocaleTimeString("en-IN", {
                          hour12: false,
                          hour: "2-digit",
                          minute: "2-digit",
                          second: "2-digit",
                        })}
                      </td>

                      {/* Type: BUY blue pill / SELL red pill */}
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

                      {/* Instrument: Symbol + NSE tag */}
                      <td className="py-2.5 px-3 font-bold text-white flex items-center space-x-1.5">
                        <span>{sym}</span>
                        <span className="text-[9px] px-1 py-0.2 rounded bg-cockpit-surface border border-cockpit-border text-cockpit-muted font-normal">
                          NSE
                        </span>
                      </td>

                      {/* Product: MIS purple badge */}
                      <td className="py-2.5 px-2">
                        <span className="px-1.5 py-0.5 rounded text-[10px] font-semibold bg-purple-500/20 text-purple-300 border border-purple-500/40">
                          MIS
                        </span>
                      </td>

                      {/* Qty: filled/total e.g. 197/197 */}
                      <td className="py-2.5 px-2 text-right font-medium text-white tabular-nums">
                        {pos.quantity}/{pos.quantity}
                      </td>

                      {/* LTP: live streaming tick price */}
                      <td className="py-2.5 px-2 text-right font-bold text-white tabular-nums">
                        ₹{liveLtp.toFixed(2)}
                      </td>

                      {/* Price: limit or avg entry price */}
                      <td className="py-2.5 px-2 text-right text-cockpit-muted tabular-nums">
                        ₹{pos.average_entry_price.toFixed(2)}
                      </td>

                      {/* Status: ACTIVE badge + quick Exit action button */}
                      <td className="py-2.5 px-3 text-right">
                        <div className="flex items-center justify-end space-x-2">
                          <span className="px-2 py-0.5 rounded text-[10px] font-semibold bg-emerald-500/20 text-emerald-400 border border-emerald-500/40">
                            ACTIVE
                          </span>
                          <button
                            onClick={() => handleExit(sym)}
                            disabled={isExiting}
                            className={`px-2 py-0.5 rounded text-[10px] font-bold border transition flex items-center space-x-1 ${
                              isExiting
                                ? "bg-rose-500/10 text-rose-500 border-rose-500/30 cursor-not-allowed"
                                : "bg-rose-500/20 hover:bg-rose-500/30 text-rose-300 border-rose-500/50"
                            }`}
                          >
                            <span>{isExiting ? "Exiting..." : "Exit"}</span>
                          </button>
                        </div>
                      </td>
                    </tr>
                  );
                })}

                {/* 2. Open / Pending Orders */}
                {openOrders.map((ord) => {
                  const sym = ord.tradingsymbol;
                  const isBuy = ord.side === "BUY";
                  const liveLtp = ticks[sym]?.ltp ?? ord.limit_price ?? 0;
                  const timeStr = ord.created_at_ist
                    ? new Date(ord.created_at_ist).toLocaleTimeString("en-IN", {
                        hour12: false,
                        hour: "2-digit",
                        minute: "2-digit",
                        second: "2-digit",
                      })
                    : "--:--:--";

                  return (
                    <tr key={`ord-${ord.order_id}`} className="hover:bg-cockpit-surface/50 transition">
                      {/* Time */}
                      <td className="py-2.5 px-3 text-cockpit-muted whitespace-nowrap">{timeStr}</td>

                      {/* Type: BUY blue pill / SELL red pill */}
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

                      {/* Instrument: Symbol + NSE tag */}
                      <td className="py-2.5 px-3 font-bold text-white flex items-center space-x-1.5">
                        <span>{sym}</span>
                        <span className="text-[9px] px-1 py-0.2 rounded bg-cockpit-surface border border-cockpit-border text-cockpit-muted font-normal">
                          NSE
                        </span>
                      </td>

                      {/* Product: MIS purple badge */}
                      <td className="py-2.5 px-2">
                        <span className="px-1.5 py-0.5 rounded text-[10px] font-semibold bg-purple-500/20 text-purple-300 border border-purple-500/40">
                          {ord.product || "MIS"}
                        </span>
                      </td>

                      {/* Qty: filled/total e.g. 0/100 */}
                      <td className="py-2.5 px-2 text-right font-medium text-white tabular-nums">
                        {ord.filled_quantity}/{ord.quantity}
                      </td>

                      {/* LTP: live streaming tick price */}
                      <td className="py-2.5 px-2 text-right font-bold text-white tabular-nums">
                        ₹{liveLtp.toFixed(2)}
                      </td>

                      {/* Price: limit price */}
                      <td className="py-2.5 px-2 text-right text-cockpit-muted tabular-nums">
                        ₹{ord.limit_price?.toFixed(2) || "MKT"}
                      </td>

                      {/* Status: OPEN badge */}
                      <td className="py-2.5 px-3 text-right">
                        <span className="px-2 py-0.5 rounded text-[10px] font-semibold bg-blue-500/20 text-blue-400 border border-blue-500/40">
                          OPEN
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

      {/* ------------------------------------------------------------------- */}
      {/* TAB 2: ORDERS (Executed Order Book) */}
      {/* ------------------------------------------------------------------- */}
      {activeTab === "ORDERS" && (
        <div className="flex-1 overflow-y-auto">
          {filteredOrders.length === 0 ? (
            <div className="h-full flex items-center justify-center text-cockpit-muted text-xs font-mono">
              <span>{ordersSearch ? "No orders matching query." : "No executed orders recorded in current session."}</span>
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
                  <th className="py-2 px-3 text-center">Status</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-cockpit-border/40">
                {filteredOrders.map((ord) => {
                  const isBuy = ord.side === "BUY";
                  const timeStr = ord.created_at_ist
                    ? new Date(ord.created_at_ist).toLocaleTimeString("en-IN", {
                        hour12: false,
                        hour: "2-digit",
                        minute: "2-digit",
                        second: "2-digit",
                      })
                    : "--:--:--";
                  const executedPrice = ord.average_fill_price || ord.limit_price || 0;
                  const isComplete = ord.status === "FILLED";
                  const isRejected = ord.status === "REJECTED";
                  const isCancelled = ord.status === "CANCELLED";
                  const tooltipText = ord.exit_reason
                    ? `Exit Trigger: ${ord.exit_reason}`
                    : `Order Status: ${ord.status}`;

                  return (
                    <tr key={ord.order_id} className="hover:bg-cockpit-surface/50 transition">
                      {/* 1. Time */}
                      <td className="py-2 px-3 text-cockpit-muted whitespace-nowrap">{timeStr}</td>

                      {/* 2. Type: BUY blue pill / SELL red pill */}
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

                      {/* 3. Instrument: Symbol + NSE tag */}
                      <td className="py-2 px-3 font-bold text-white flex items-center space-x-1.5">
                        <span>{ord.tradingsymbol}</span>
                        <span className="text-[9px] px-1 py-0.2 rounded bg-cockpit-surface border border-cockpit-border text-cockpit-muted font-normal">
                          NSE
                        </span>
                      </td>

                      {/* 4. Product: MIS purple badge */}
                      <td className="py-2 px-2">
                        <span className="px-1.5 py-0.5 rounded text-[10px] font-semibold bg-purple-500/20 text-purple-300 border border-purple-500/40">
                          {ord.product || "MIS"}
                        </span>
                      </td>

                      {/* 5. Qty: filled/total e.g. 197/197 */}
                      <td className="py-2 px-2 text-right font-medium text-white tabular-nums">
                        {ord.filled_quantity}/{ord.quantity}
                      </td>

                      {/* 6. Avg. price: exact executed fill price */}
                      <td className="py-2 px-2 text-right font-medium text-white tabular-nums">
                        ₹{executedPrice.toFixed(2)}
                      </td>

                      {/* 7. Status: COMPLETE green / REJECTED red / CANCELLED muted with tooltip */}
                      <td className="py-2 px-3 text-center">
                        <span
                          title={tooltipText}
                          className={`px-2 py-0.5 rounded text-[10px] font-semibold uppercase cursor-help ${
                            isComplete
                              ? "bg-emerald-500/20 text-emerald-400 border border-emerald-500/40"
                              : isRejected
                              ? "bg-rose-500/20 text-rose-400 border border-rose-500/40"
                              : isCancelled
                              ? "bg-slate-700/40 text-slate-400 border border-slate-600/40"
                              : "bg-blue-500/20 text-blue-400 border border-blue-500/40"
                          }`}
                        >
                          {isComplete ? "COMPLETE" : ord.status}
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

      {/* ------------------------------------------------------------------- */}
      {/* TAB 3: REPORT (Kite Positions Summary & Horizontal P&L Breakdown) */}
      {/* ------------------------------------------------------------------- */}
      {activeTab === "REPORT" && (
        <div className="flex-1 overflow-y-auto flex flex-col">
          {filteredReportItems.length === 0 ? (
            <div className="h-full flex items-center justify-center text-cockpit-muted text-xs font-mono">
              <span>{reportSearch ? "No report items matching query." : "No positions recorded today."}</span>
            </div>
          ) : (
            <div className="flex-1 flex flex-col">
              {/* Top Table */}
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

                      return (
                        <tr key={item.instrument} className="hover:bg-cockpit-surface/50 transition">
                          {/* 1. Product: MIS purple badge */}
                          <td className="py-2 px-3">
                            <span className="px-1.5 py-0.5 rounded text-[10px] font-semibold bg-purple-500/20 text-purple-300 border border-purple-500/40">
                              MIS
                            </span>
                          </td>

                          {/* 2. Instrument: Symbol + NSE tag */}
                          <td className="py-2 px-3 font-bold text-white flex items-center space-x-1.5">
                            <span>{item.instrument}</span>
                            <span className="text-[9px] px-1 py-0.2 rounded bg-cockpit-surface border border-cockpit-border text-cockpit-muted font-normal">
                              NSE
                            </span>
                          </td>

                          {/* 3. Qty.: blue for long, red for short, muted 0 for squared-off */}
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

                          {/* 4. Avg. */}
                          <td className="py-2 px-2 text-right text-cockpit-muted tabular-nums">
                            ₹{item.avg.toFixed(2)}
                          </td>

                          {/* 5. LTP */}
                          <td className="py-2 px-2 text-right font-bold text-white tabular-nums">
                            ₹{item.ltp.toFixed(2)}
                          </td>

                          {/* 6. P&L: green +₹... / red -₹... tabular-nums */}
                          <td
                            className={`py-2 px-3 text-right font-bold tabular-nums ${
                              isProfit ? "text-emerald-400" : "text-rose-400"
                            }`}
                          >
                            {isProfit ? "+" : ""}₹{item.pnl.toFixed(2)}
                          </td>

                          {/* 7. Chg.: +X.XX% / -X.XX% */}
                          <td
                            className={`py-2 px-3 text-right tabular-nums text-xs font-semibold ${
                              item.chg_pct >= 0 ? "text-emerald-400" : "text-rose-400"
                            }`}
                          >
                            {item.chg_pct >= 0 ? "+" : ""}
                            {item.chg_pct.toFixed(2)}%
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                  {/* Total Summary Row at bottom of table */}
                  <tfoot className="border-t-2 border-cockpit-border bg-cockpit-surface/80 font-bold">
                    <tr>
                      <td colSpan={2} className="py-2.5 px-3 text-white uppercase text-xs">
                        Total
                      </td>
                      <td colSpan={3} className="py-2.5 px-2 text-right text-cockpit-muted text-[11px]">
                        Net Realized + Unrealized
                      </td>
                      <td
                        className={`py-2.5 px-3 text-right text-sm tabular-nums font-bold ${
                          totalReportPnl >= 0 ? "text-emerald-400" : "text-rose-400"
                        }`}
                      >
                        {totalReportPnl >= 0 ? "+" : ""}₹{totalReportPnl.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                      </td>
                      <td className="py-2.5 px-3 text-right"></td>
                    </tr>
                  </tfoot>
                </table>
              </div>

              {/* Bottom Visual Horizontal P&L Breakdown (Zero-Centered) */}
              <div className="p-3 border-t border-cockpit-border bg-cockpit-surface/30">
                <div className="text-[10px] font-mono uppercase tracking-wider text-cockpit-muted mb-2 flex items-center justify-between">
                  <span>Horizontal P&L Breakdown</span>
                  <span className="text-[9px] text-cockpit-muted/60">Zero-Centered (Losing ← 0 → Profitable)</span>
                </div>

                <div className="space-y-1.5">
                  {reportItems.map((item) => {
                    const isProfit = item.pnl >= 0;
                    const barWidthPct = maxAbsPnl > 0 ? Math.min(100, (Math.abs(item.pnl) / maxAbsPnl) * 100) : 0;

                    return (
                      <div key={`bar-${item.instrument}`} className="flex items-center text-xs font-mono">
                        {/* Label: <SYMBOL> (MIS) */}
                        <div className="w-28 sm:w-36 shrink-0 truncate font-semibold text-white">
                          <span>{item.instrument}</span>
                          <span className="text-[10px] text-purple-400 ml-1">(MIS)</span>
                        </div>

                        {/* Split Track: Left = Losing (-), Right = Profitable (+) */}
                        <div className="flex-1 grid grid-cols-2 gap-0 h-4 bg-cockpit-surface rounded border border-cockpit-border/50 relative overflow-hidden">
                          {/* Left Half (Losing) */}
                          <div className="relative flex justify-end items-center border-r border-cockpit-border/80 pr-1">
                            {!isProfit && (
                              <div
                                style={{ width: `${barWidthPct}%` }}
                                className="h-full bg-gradient-to-l from-rose-500 to-rose-600/70 rounded-l"
                              />
                            )}
                          </div>

                          {/* Right Half (Profitable) */}
                          <div className="relative flex justify-start items-center pl-1">
                            {isProfit && (
                              <div
                                style={{ width: `${barWidthPct}%` }}
                                className="h-full bg-gradient-to-r from-blue-500 to-emerald-500 rounded-r"
                              />
                            )}
                          </div>
                        </div>

                        {/* PnL Value Badge */}
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
