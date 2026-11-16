"use client";

import React, { useEffect, useRef, useState, useMemo } from "react";
import {
  createChart,
  ColorType,
  CrosshairMode,
  IChartApi,
  ISeriesApi,
  SeriesMarker,
  Time,
} from "lightweight-charts";
import { Maximize2, Layers, TrendingUp } from "lucide-react";
import { Candle, Order, StockProfile, Tick } from "@/types/telemetry";

interface InteractiveChartPanelProps {
  symbol: string;
  profile?: StockProfile;
  candles: Candle[];
  tick?: Tick;
  orders: Order[];
}

export const InteractiveChartPanel: React.FC<InteractiveChartPanelProps> = ({
  symbol,
  profile,
  candles,
  tick,
  orders,
}) => {
  const chartContainerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleSeriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const vwapSeriesRef = useRef<ISeriesApi<"Line"> | null>(null);
  const ema200SeriesRef = useRef<ISeriesApi<"Line"> | null>(null);
  const volumeSeriesRef = useRef<ISeriesApi<"Histogram"> | null>(null);

  const [showVwap, setShowVwap] = useState(true);
  const [showEma200, setShowEma200] = useState(true);
  const [showVolume, setShowVolume] = useState(true);

  // Generate synthetic seed data if no historical candles loaded yet
  const chartData = useMemo(() => {
    if (candles && candles.length > 0) {
      return candles
        .slice()
        .sort(
          (a, b) =>
            new Date(a.timestamp_ist).getTime() -
            new Date(b.timestamp_ist).getTime()
        )
        .map((c) => {
          const timeSec = Math.floor(
            new Date(c.timestamp_ist).getTime() / 1000
          ) as Time;
          return {
            time: timeSec,
            open: c.open,
            high: c.high,
            low: c.low,
            close: c.close,
            volume: c.volume,
            vwap: c.vwap,
            ema_200: c.ema_200,
          };
        });
    }

    // Fallback seed bar based on current tick
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

  // Order fill markers on chart
  const markers = useMemo(() => {
    const symbolOrders = orders.filter(
      (o) => o.tradingsymbol === symbol && o.status === "FILLED"
    );
    const mList: SeriesMarker<Time>[] = [];

    for (const ord of symbolOrders) {
      const ordSec = Math.floor(
        new Date(ord.created_at_ist).getTime() / 1000
      ) as Time;
      const isBuy = ord.side === "BUY";
      mList.push({
        time: ordSec,
        position: isBuy ? "belowBar" : "aboveBar",
        color: isBuy ? "#10B981" : "#F43F5E",
        shape: isBuy ? "arrowUp" : "arrowDown",
        text: `${ord.side} ${ord.filled_quantity}@₹${ord.average_fill_price || ord.limit_price}`,
      });
    }
    return mList.sort((a, b) => (Number(a.time) - Number(b.time)));
  }, [orders, symbol]);

  // Initialize TradingView Lightweight Chart
  useEffect(() => {
    if (!chartContainerRef.current) return;

    const chart = createChart(chartContainerRef.current, {
      layout: {
        background: { type: ColorType.Solid, color: "#090D16" },
        textColor: "#94A3B8",
        fontSize: 11,
        fontFamily: "JetBrains Mono, monospace",
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
        scaleMargins: { top: 0.1, bottom: 0.25 },
      },
      timeScale: {
        borderColor: "#1F2B44",
        timeVisible: true,
        secondsVisible: false,
      },
    });

    // 1. Candlestick Series
    const candleSeries = chart.addCandlestickSeries({
      upColor: "#10B981",
      downColor: "#F43F5E",
      borderVisible: false,
      wickUpColor: "#10B981",
      wickDownColor: "#F43F5E",
    });

    // 2. Volume Histogram Series
    const volumeSeries = chart.addHistogramSeries({
      color: "#38BDF8",
      priceFormat: { type: "volume" },
      priceScaleId: "",
    });
    volumeSeries.priceScale().applyOptions({
      scaleMargins: { top: 0.8, bottom: 0 },
    });

    // 3. Intraday VWAP (Cyan)
    const vwapSeries = chart.addLineSeries({
      color: "#06B6D4",
      lineWidth: 2,
      title: "VWAP",
      priceLineVisible: false,
    });

    // 4. EMA(200) (Amber)
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

    // Handle container resize
    const handleResize = () => {
      if (chartContainerRef.current) {
        chart.applyOptions({
          width: chartContainerRef.current.clientWidth,
          height: chartContainerRef.current.clientHeight,
        });
      }
    };

    window.addEventListener("resize", handleResize);
    handleResize();

    return () => {
      window.removeEventListener("resize", handleResize);
      chart.remove();
    };
  }, []);

  // Update chart series data on new ticks/candles
  useEffect(() => {
    if (!candleSeriesRef.current || chartData.length === 0) return;

    // Set Candlesticks
    candleSeriesRef.current.setData(
      chartData.map((d) => ({
        time: d.time,
        open: d.open,
        high: d.high,
        low: d.low,
        close: d.close,
      }))
    );

    // Set Volume
    if (volumeSeriesRef.current && showVolume) {
      volumeSeriesRef.current.setData(
        chartData.map((d) => ({
          time: d.time,
          value: d.volume,
          color: d.close >= d.open ? "rgba(16, 185, 129, 0.4)" : "rgba(244, 63, 94, 0.4)",
        }))
      );
    }

    // Set VWAP
    if (vwapSeriesRef.current && showVwap) {
      vwapSeriesRef.current.setData(
        chartData
          .filter((d) => d.vwap != null)
          .map((d) => ({ time: d.time, value: d.vwap }))
      );
    }

    // Set EMA200
    if (ema200SeriesRef.current && showEma200) {
      ema200SeriesRef.current.setData(
        chartData
          .filter((d) => d.ema_200 != null)
          .map((d) => ({ time: d.time, value: d.ema_200 }))
      );
    }

    // Set markers on candle series
    if (markers.length > 0) {
      candleSeriesRef.current.setMarkers(markers);
    }
  }, [chartData, showVwap, showEma200, showVolume, markers]);

  // Handle live tick update into last candle
  useEffect(() => {
    if (!tick || !candleSeriesRef.current || chartData.length === 0) return;
    const lastBar = chartData[chartData.length - 1];
    const updatedHigh = Math.max(lastBar.high, tick.ltp);
    const updatedLow = Math.min(lastBar.low, tick.ltp);

    candleSeriesRef.current.update({
      time: lastBar.time,
      open: lastBar.open,
      high: updatedHigh,
      low: updatedLow,
      close: tick.ltp,
    });
  }, [tick, chartData]);

  return (
    <div className="h-full w-full flex flex-col bg-cockpit-card rounded-xl border border-cockpit-border overflow-hidden">
      {/* Chart Header Controls */}
      <div className="h-12 border-b border-cockpit-border px-4 flex items-center justify-between select-none">
        <div className="flex items-center space-x-3">
          <div className="flex items-baseline space-x-2">
            <span className="font-bold text-base text-white tracking-wide">
              {symbol}
            </span>
            <span className="text-xs font-mono text-cockpit-muted">
              {profile?.parent_index || "NSE"}
            </span>
          </div>

          <div className="h-3.5 w-px bg-cockpit-border" />

          {/* Live Price from Tick */}
          <div className="flex items-baseline space-x-2">
            <span className="font-mono text-base font-bold text-white">
              ₹{tick?.ltp?.toFixed(2) || (chartData[chartData.length - 1]?.close || 0).toFixed(2)}
            </span>
            <span className="text-[11px] font-mono text-cockpit-muted">
              Vol: {tick?.volume?.toLocaleString() || "0"}
            </span>
          </div>
        </div>

        {/* Indicator Toggles */}
        <div className="flex items-center space-x-2 text-xs font-mono">
          <button
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
            onClick={() => chartRef.current?.timeScale().fitContent()}
            title="Fit Chart to View"
            className="p-1 rounded bg-cockpit-surface hover:bg-cockpit-hover border border-cockpit-border text-cockpit-muted hover:text-white transition"
          >
            <Maximize2 className="h-3.5 w-3.5" />
          </button>
        </div>
      </div>

      {/* Chart Canvas Container */}
      <div className="relative flex-1 w-full min-h-[360px]">
        <div ref={chartContainerRef} className="absolute inset-0" />
      </div>
    </div>
  );
};
