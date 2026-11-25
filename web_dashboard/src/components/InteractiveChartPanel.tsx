"use client";

import React, { useEffect, useRef, useState, useMemo, useCallback } from "react";
import {
  createChart,
  ColorType,
  CrosshairMode,
  IChartApi,
  IPriceLine,
  ISeriesApi,
  LineStyle,
  SeriesMarker,
  Time,
} from "lightweight-charts";
import { Maximize2 } from "lucide-react";
import {
  Candle,
  ClosedTrade,
  Order,
  Position,
  StockProfile,
  Tick,
  TimeframeCode,
} from "@/types/telemetry";

interface InteractiveChartPanelProps {
  symbol: string;
  profile?: StockProfile;
  candles: Candle[];
  tick?: Tick;
  orders: Order[];
  positions?: Position[];
  closedTrades?: ClosedTrade[];
  timeframe?: TimeframeCode;
  onTimeframeChange?: (tf: TimeframeCode) => void;
}

const TIMEFRAME_BUTTONS: { label: string; value: TimeframeCode }[] = [
  { label: "1m", value: "M1" },
  { label: "3m", value: "M3" },
  { label: "5m", value: "M5" },
  { label: "15m", value: "M15" },
];

const istDateTimeFormatter = new Intl.DateTimeFormat("en-IN", {
  timeZone: "Asia/Kolkata",
  month: "short",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});

const istTimeOnlyFormatter = new Intl.DateTimeFormat("en-IN", {
  timeZone: "Asia/Kolkata",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});

function formatTimeSecToIST(time: Time, includeDate: boolean): string {
  if (typeof time === "number") {
    const dt = new Date(time * 1000);
    return includeDate ? istDateTimeFormatter.format(dt) : istTimeOnlyFormatter.format(dt);
  }
  return String(time);
}

export const InteractiveChartPanel: React.FC<InteractiveChartPanelProps> = ({
  symbol,
  profile,
  candles,
  tick,
  orders,
  positions = [],
  closedTrades = [],
  timeframe = "M5",
  onTimeframeChange,
}) => {
  const chartContainerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleSeriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const vwapSeriesRef = useRef<ISeriesApi<"Line"> | null>(null);
  const ema200SeriesRef = useRef<ISeriesApi<"Line"> | null>(null);
  const volumeSeriesRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const priceLinesRef = useRef<IPriceLine[]>([]);

  // User manual pan/zoom interaction flag
  const userManuallyZoomedRef = useRef<boolean>(false);

  const [showVwap, setShowVwap] = useState(true);
  const [showEma200, setShowEma200] = useState(true);
  const [showVolume, setShowVolume] = useState(true);

  // Sort and deduplicate candles by epoch timestamp for Lightweight Charts
  const chartData = useMemo(() => {
    if (candles && candles.length > 0) {
      const sorted = candles
        .slice()
        .sort(
          (a, b) =>
            new Date(a.timestamp_ist).getTime() -
            new Date(b.timestamp_ist).getTime()
        );
      const dedupMap = new Map<number, (typeof sorted)[0]>();
      for (const c of sorted) {
        const timeSec = Math.floor(new Date(c.timestamp_ist).getTime() / 1000);
        if (!Number.isNaN(timeSec) && timeSec > 0) {
          dedupMap.set(timeSec, c);
        }
      }
      return Array.from(dedupMap.entries()).map(([timeSec, c]) => ({
        time: timeSec as Time,
        open: c.open,
        high: c.high,
        low: c.low,
        close: c.close,
        volume: c.volume,
        vwap: c.vwap,
        ema_200: c.ema_200,
      }));
    }

    // Fallback seed bar based on current tick when warm-up data is still loading
    const basePrice = tick?.ltp || 2500;
    const nowSec = Math.floor(Date.now() / 1000);
    return [
      {
        time: (nowSec - 300) as Time,
        open: basePrice * 0.998,
        high: basePrice * 1.002,
        low: basePrice * 0.997,
        close: basePrice * 0.999,
        volume: 25000,
        vwap: basePrice * 0.999,
        ema_200: basePrice * 0.995,
      },
      {
        time: nowSec as Time,
        open: basePrice * 0.999,
        high: basePrice * 1.004,
        low: basePrice * 0.998,
        close: basePrice,
        volume: 45000,
        vwap: basePrice * 1.001,
        ema_200: basePrice * 0.995,
      },
    ];
  }, [candles, tick]);

  // Snap marker epochs to the closest available candle timestamp so markers always render cleanly
  const snapToCandleTime = useCallback(
    (isoTs: string): Time | null => {
      if (chartData.length === 0) return null;
      const targetSec = Math.floor(new Date(isoTs).getTime() / 1000);
      if (Number.isNaN(targetSec)) return null;

      let closest = Number(chartData[0].time);
      let minDiff = Math.abs(targetSec - closest);
      for (const bar of chartData) {
        const bSec = Number(bar.time);
        const diff = Math.abs(targetSec - bSec);
        if (diff < minDiff) {
          minDiff = diff;
          closest = bSec;
        }
      }
      return closest as Time;
    },
    [chartData]
  );

  // Order fill & closed-trade exit markers on chart
  const markers = useMemo(() => {
    const mList: SeriesMarker<Time>[] = [];
    const symbolClosed = closedTrades.filter((t) => t.symbol === symbol);
    const closedOrderIds = new Set(symbolClosed.map((t) => t.order_id));

    for (const ct of symbolClosed) {
      const entrySnapped = snapToCandleTime(ct.entry_time);
      if (entrySnapped !== null) {
        mList.push({
          time: entrySnapped,
          position: ct.side === "BUY" ? "belowBar" : "aboveBar",
          color: ct.side === "BUY" ? "#10B981" : "#F43F5E",
          shape: ct.side === "BUY" ? "arrowUp" : "arrowDown",
          text: `${ct.side === "BUY" ? "▲" : "▼"} ${ct.side} @₹${ct.entry_price.toFixed(1)}`,
        });
      }
      const exitSnapped = snapToCandleTime(ct.exit_time);
      if (exitSnapped !== null) {
        const isSl = ct.exit_reason === "SL_HIT";
        const isTarget = ct.exit_reason === "TARGET_HIT";
        mList.push({
          time: exitSnapped,
          position: ct.side === "BUY" ? "aboveBar" : "belowBar",
          color: isSl ? "#F43F5E" : isTarget ? "#10B981" : "#F59E0B",
          shape: isSl ? "circle" : "arrowDown",
          text: isSl
            ? `✖ SL HIT @₹${ct.actual_exit_price.toFixed(1)}`
            : isTarget
            ? `▼ TARGET HIT @₹${ct.actual_exit_price.toFixed(1)}`
            : `■ ${ct.exit_reason} @₹${ct.actual_exit_price.toFixed(1)}`,
        });
      }
    }

    const symbolOrders = orders.filter(
      (o) => o.tradingsymbol === symbol && o.status === "FILLED" && !closedOrderIds.has(o.order_id)
    );
    for (const ord of symbolOrders) {
      const ordSnapped = snapToCandleTime(ord.created_at_ist);
      if (ordSnapped === null) continue;
      const isBuy = ord.side === "BUY";
      const fillPx = ord.average_fill_price || ord.limit_price || 0;
      mList.push({
        time: ordSnapped,
        position: isBuy ? "belowBar" : "aboveBar",
        color: isBuy ? "#10B981" : "#F43F5E",
        shape: isBuy ? "arrowUp" : "arrowDown",
        text: `${isBuy ? "▲" : "▼"} ${ord.exit_reason || ord.side} @₹${fillPx.toFixed(1)}`,
      });
    }

    return mList.sort((a, b) => Number(a.time) - Number(b.time));
  }, [orders, closedTrades, symbol, snapToCandleTime]);

  // Active position or most recent closed trade levels for ENTRY / TARGET / SL dashed price lines
  const activeLevels = useMemo(() => {
    const activePos = positions.find((p) => p.tradingsymbol === symbol && p.quantity > 0);
    if (activePos) {
      return {
        entry: activePos.average_entry_price,
        sl: activePos.stop_loss,
        target: activePos.target_price,
      };
    }
    const symbolClosed = closedTrades.filter((t) => t.symbol === symbol);
    if (symbolClosed.length > 0) {
      const latest = symbolClosed[symbolClosed.length - 1];
      return {
        entry: latest.entry_price,
        sl: latest.sl_price,
        target: latest.target_price,
      };
    }
    return null;
  }, [positions, closedTrades, symbol]);

  // Auto-Frame Recent Candles
  const autoFrameRecentCandles = useCallback(() => {
    if (!chartRef.current || chartData.length === 0) return;
    const chart = chartRef.current;

    chart.priceScale("right").applyOptions({
      autoScale: true,
      scaleMargins: { top: 0.12, bottom: 0.18 },
    });

    const totalBars = chartData.length;
    if (totalBars <= 90) {
      chart.timeScale().fitContent();
    } else {
      chart.timeScale().setVisibleLogicalRange({
        from: Math.max(0, totalBars - 75),
        to: totalBars + 3,
      });
    }
  }, [chartData.length]);

  // Initialize TradingView Lightweight Chart with IST Localization
  useEffect(() => {
    if (!chartContainerRef.current) return;

    const chart = createChart(chartContainerRef.current, {
      layout: {
        background: { type: ColorType.Solid, color: "#090D16" },
        textColor: "#94A3B8",
        fontSize: 11,
        fontFamily: "JetBrains Mono, monospace",
      },
      localization: {
        locale: "en-IN",
        timeFormatter: (time: Time) => formatTimeSecToIST(time, true),
      },
      grid: {
        vertLines: { color: "#1F2B44" },
        horzLines: { color: "#1F2B44" },
      },
      crosshair: {
        mode: CrosshairMode.Normal,
        vertLine: { color: "#38BDF8", width: 1, style: 2 },
        horzLine: { color: "#38BDF8", width: 1, style: 2 },
      },
      rightPriceScale: {
        borderColor: "#1F2B44",
        autoScale: true,
        scaleMargins: { top: 0.12, bottom: 0.18 },
      },
      timeScale: {
        borderColor: "#1F2B44",
        timeVisible: true,
        secondsVisible: false,
        rightOffset: 5,
        tickMarkFormatter: (time: Time) => formatTimeSecToIST(time, false),
      },
    });

    const candleSeries = chart.addCandlestickSeries({
      upColor: "#10B981",
      downColor: "#F43F5E",
      borderVisible: false,
      wickUpColor: "#10B981",
      wickDownColor: "#F43F5E",
    });

    const volumeSeries = chart.addHistogramSeries({
      color: "#38BDF8",
      priceFormat: { type: "volume" },
      priceScaleId: "",
    });
    volumeSeries.priceScale().applyOptions({
      scaleMargins: { top: 0.8, bottom: 0 },
    });

    const vwapSeries = chart.addLineSeries({
      color: "#06B6D4",
      lineWidth: 2,
      title: "VWAP",
      priceLineVisible: false,
    });

    const ema200Series = chart.addLineSeries({
      color: "#F59E0B",
      lineWidth: 1,
      title: "EMA(200)",
      priceLineVisible: false,
    });

    chartRef.current = chart;
    candleSeriesRef.current = candleSeries;
    volumeSeriesRef.current = volumeSeries;
    vwapSeriesRef.current = vwapSeries;
    ema200SeriesRef.current = ema200Series;

    const container = chartContainerRef.current;
    const handleUserInteraction = () => {
      userManuallyZoomedRef.current = true;
    };
    container.addEventListener("wheel", handleUserInteraction, { passive: true });
    container.addEventListener("pointerdown", handleUserInteraction, { passive: true });
    container.addEventListener("touchstart", handleUserInteraction, { passive: true });

    const handleResize = () => {
      if (chartContainerRef.current) {
        chart.applyOptions({
          width: chartContainerRef.current.clientWidth,
          height: chartContainerRef.current.clientHeight,
        });
      }
    };

    const resizeObserver = new ResizeObserver(() => handleResize());
    resizeObserver.observe(container);
    window.addEventListener("resize", handleResize);
    handleResize();

    return () => {
      resizeObserver.disconnect();
      container.removeEventListener("wheel", handleUserInteraction);
      container.removeEventListener("pointerdown", handleUserInteraction);
      container.removeEventListener("touchstart", handleUserInteraction);
      window.removeEventListener("resize", handleResize);
      priceLinesRef.current = [];
      chart.remove();
    };
  }, []);

  // Update chart series data & markers
  useEffect(() => {
    if (!candleSeriesRef.current || chartData.length === 0) return;

    candleSeriesRef.current.setData(
      chartData.map((d) => ({
        time: d.time,
        open: d.open,
        high: d.high,
        low: d.low,
        close: d.close,
      }))
    );

    if (volumeSeriesRef.current) {
      volumeSeriesRef.current.setData(
        showVolume
          ? chartData.map((d) => ({
              time: d.time,
              value: d.volume,
              color: d.close >= d.open ? "rgba(16, 185, 129, 0.4)" : "rgba(244, 63, 94, 0.4)",
            }))
          : []
      );
    }

    if (vwapSeriesRef.current) {
      vwapSeriesRef.current.setData(
        showVwap
          ? chartData
              .filter((d) => d.vwap != null && d.vwap > 0)
              .map((d) => ({ time: d.time, value: d.vwap }))
          : []
      );
    }

    if (ema200SeriesRef.current) {
      ema200SeriesRef.current.setData(
        showEma200
          ? chartData
              .filter((d) => d.ema_200 != null && d.ema_200 > 0)
              .map((d) => ({ time: d.time, value: d.ema_200 }))
          : []
      );
    }

    candleSeriesRef.current.setMarkers(markers);
  }, [chartData, showVwap, showEma200, showVolume, markers]);

  // Draw / refresh ENTRY, TARGET, and SL dashed price lines cleanly
  useEffect(() => {
    const series = candleSeriesRef.current;
    if (!series) return;

    for (const oldLine of priceLinesRef.current) {
      try {
        series.removePriceLine(oldLine);
      } catch {
        // Ignore stale handle on unmount
      }
    }
    priceLinesRef.current = [];

    if (!activeLevels) return;

    if (activeLevels.entry > 0) {
      priceLinesRef.current.push(
        series.createPriceLine({
          price: activeLevels.entry,
          color: "#38BDF8",
          lineWidth: 1,
          lineStyle: LineStyle.Dashed,
          axisLabelVisible: true,
          title: "ENTRY",
        })
      );
    }
    if (activeLevels.target > 0) {
      priceLinesRef.current.push(
        series.createPriceLine({
          price: activeLevels.target,
          color: "#10B981",
          lineWidth: 1,
          lineStyle: LineStyle.Dashed,
          axisLabelVisible: true,
          title: "TARGET",
        })
      );
    }
    if (activeLevels.sl > 0) {
      priceLinesRef.current.push(
        series.createPriceLine({
          price: activeLevels.sl,
          color: "#F43F5E",
          lineWidth: 1,
          lineStyle: LineStyle.Dashed,
          axisLabelVisible: true,
          title: "SL",
        })
      );
    }
  }, [activeLevels, symbol]);

  // Auto-fit chart viewport whenever selectedSymbol or timeframe changes
  useEffect(() => {
    userManuallyZoomedRef.current = false;
    if (chartRef.current && chartData.length > 0) {
      const timer = setTimeout(() => {
        autoFrameRecentCandles();
      }, 50);
      return () => clearTimeout(timer);
    }
  }, [symbol, timeframe, chartData.length, autoFrameRecentCandles]);

  // Live tick update on rightmost bar
  useEffect(() => {
    if (!tick || !candleSeriesRef.current || chartData.length === 0) return;
    const lastBar = chartData[chartData.length - 1];
    candleSeriesRef.current.update({
      time: lastBar.time,
      open: lastBar.open,
      high: Math.max(lastBar.high, tick.ltp),
      low: Math.min(lastBar.low, tick.ltp),
      close: tick.ltp,
    });

    if (!userManuallyZoomedRef.current && chartRef.current) {
      chartRef.current.timeScale().scrollToRealTime();
    }
  }, [tick, chartData]);

  return (
    <div className="h-full w-full flex flex-col bg-cockpit-card rounded-xl border border-cockpit-border overflow-hidden">
      {/* Chart Header Controls */}
      <div className="h-11 shrink-0 border-b border-cockpit-border px-4 flex items-center justify-between select-none">
        <div className="flex items-center space-x-3">
          <div className="flex items-baseline space-x-2">
            <span className="font-bold text-base text-white tracking-wide">{symbol}</span>
            <span className="text-xs font-mono text-cockpit-muted">
              {profile?.parent_index || "NSE"} • IST
            </span>
          </div>

          <div className="h-3.5 w-px bg-cockpit-border" />

          {/* Live Price from Tick */}
          <div className="flex items-baseline space-x-2">
            <span className="font-mono text-base font-bold text-white tabular-nums">
              ₹{tick?.ltp?.toFixed(2) || (chartData[chartData.length - 1]?.close || 0).toFixed(2)}
            </span>
            <span className="text-[11px] font-mono text-cockpit-muted">
              Vol: {tick?.volume?.toLocaleString() || "0"}
            </span>
          </div>
        </div>

        {/* Timeframe Switcher + Indicator Toggles */}
        <div className="flex items-center space-x-2 text-xs font-mono">
          {onTimeframeChange && (
            <div className="flex items-center bg-cockpit-surface border border-cockpit-border rounded p-0.5 mr-1">
              {TIMEFRAME_BUTTONS.map((tf) => (
                <button
                  key={tf.value}
                  type="button"
                  onClick={() => onTimeframeChange(tf.value)}
                  className={`px-2 py-0.5 rounded text-[11px] font-bold transition ${
                    timeframe === tf.value
                      ? "bg-cockpit-blue/20 text-cockpit-blue border border-cockpit-blue/40"
                      : "text-cockpit-muted hover:text-white border border-transparent"
                  }`}
                >
                  {tf.label}
                </button>
              ))}
            </div>
          )}

          <button
            type="button"
            onClick={() => setShowVwap(!showVwap)}
            className={`px-2 py-1 rounded border transition ${
              showVwap
                ? "bg-cyan-500/20 border-cyan-500/50 text-cyan-400 font-bold"
                : "bg-cockpit-surface border-cockpit-border text-cockpit-muted"
            }`}
          >
            VWAP
          </button>

          <button
            type="button"
            onClick={() => setShowEma200(!showEma200)}
            className={`px-2 py-1 rounded border transition ${
              showEma200
                ? "bg-amber-500/20 border-amber-500/50 text-amber-400 font-bold"
                : "bg-cockpit-surface border-cockpit-border text-cockpit-muted"
            }`}
          >
            EMA(200)
          </button>

          <button
            type="button"
            onClick={() => setShowVolume(!showVolume)}
            className={`px-2 py-1 rounded border transition ${
              showVolume
                ? "bg-sky-500/20 border-sky-500/50 text-sky-400 font-bold"
                : "bg-cockpit-surface border-cockpit-border text-cockpit-muted"
            }`}
          >
            VOL
          </button>

          <div className="h-3.5 w-px bg-cockpit-border" />

          <button
            type="button"
            onClick={() => {
              userManuallyZoomedRef.current = false;
              autoFrameRecentCandles();
            }}
            title="Reset / Fit View (Auto-frame recent ~75 candles & center LTP)"
            className="px-2 py-1 rounded bg-cockpit-surface hover:bg-cockpit-hover border border-cockpit-border text-cockpit-muted hover:text-white transition flex items-center space-x-1"
          >
            <Maximize2 className="h-3.5 w-3.5 text-blue-400" />
            <span className="hidden sm:inline">Fit View</span>
          </button>
        </div>
      </div>

      {/* Chart Canvas Container */}
      <div className="relative flex-1 w-full min-h-[200px]">
        <div ref={chartContainerRef} className="absolute inset-0" />
      </div>
    </div>
  );
};
