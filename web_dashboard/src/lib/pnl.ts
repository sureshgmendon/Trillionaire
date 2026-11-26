/**
 * Shared unrealized P&L and directional percentage helpers respecting BUY/SELL position side.
 */

import { Position, Tick } from "@/types/telemetry";

export function isShortPosition(side?: string, rawQty: number = 0): boolean {
  const s = String(side || "").toUpperCase();
  if (s === "SELL" || s === "SHORT") return true;
  if (s === "BUY" || s === "LONG") return false;
  return rawQty < 0;
}

export function computeOpenPositionMetrics(pos: Position, ticks: Record<string, Tick>) {
  const rawQty = pos.quantity ?? 0;
  const absQty = Math.abs(rawQty);
  const sym = (pos as any).symbol || pos.tradingsymbol;
  const avgPrice = (pos as any).avg_price ?? pos.average_entry_price ?? 0;
  const liveLtp = (sym && ticks[sym]?.ltp) ?? pos.ltp ?? avgPrice;
  const isShort = isShortPosition(pos.side, rawQty);

  const unrealizedPnl = isShort
    ? (avgPrice - liveLtp) * absQty
    : (liveLtp - avgPrice) * absQty;

  const pctChange =
    avgPrice > 0
      ? isShort
        ? ((avgPrice - liveLtp) / avgPrice) * 100
        : ((liveLtp - avgPrice) / avgPrice) * 100
      : 0;

  return {
    sym,
    absQty,
    netQty: isShort ? -absQty : absQty,
    isShort,
    avgPrice: Math.round(avgPrice * 100) / 100,
    liveLtp: Math.round(liveLtp * 100) / 100,
    unrealizedPnl: Math.round(unrealizedPnl * 100) / 100,
    pctChange: Math.round(pctChange * 100) / 100,
  };
}
