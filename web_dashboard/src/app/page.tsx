"use client";

import React, { useState } from "react";
import { TopTelemetryBar } from "@/components/TopTelemetryBar";
import { InteractiveChartPanel } from "@/components/InteractiveChartPanel";
import { WatchlistAndSentinelMatrix } from "@/components/WatchlistAndSentinelMatrix";
import { PositionsAndOrdersTabs } from "@/components/PositionsAndOrdersTabs";
import { ReplayControlModal } from "@/components/ReplayControlModal";
import { useTrillionaireStream } from "@/hooks/useTrillionaireStream";

export default function CockpitPage() {
  const {
    connectionStatus,
    status,
    ticks,
    candles,
    signals,
    orders,
    positions,
    anomalies,
    riskAlerts,
    profiles,
    selectedSymbol,
    setSelectedSymbol,
    triggerEmergencyFlatten,
    triggerReplay,
    refreshState,
  } = useTrillionaireStream();

  const [isReplayModalOpen, setIsReplayModalOpen] = useState(false);

  const selectedProfile = profiles.find((p) => p.tradingsymbol === selectedSymbol);
  const selectedCandles = candles[selectedSymbol] || [];
  const selectedTick = ticks[selectedSymbol];

  return (
    <div className="min-h-screen flex flex-col bg-cockpit-bg text-cockpit-text font-mono">
      {/* 1. Header Telemetry Bar */}
      <TopTelemetryBar
        connectionStatus={connectionStatus}
        status={status}
        onEmergencyFlatten={triggerEmergencyFlatten}
        onOpenReplayModal={() => setIsReplayModalOpen(true)}
        onRefresh={refreshState}
      />

      {/* 2. Main High-Density Cockpit Grid */}
      <main className="flex-1 p-3 grid grid-cols-1 xl:grid-cols-12 gap-3 overflow-hidden">
        {/* Left Column (4 cols): Watchlist & Sentinel Matrix */}
        <div className="xl:col-span-4 h-[calc(100vh-5.5rem)] min-h-[500px]">
          <WatchlistAndSentinelMatrix
            profiles={profiles}
            ticks={ticks}
            selectedSymbol={selectedSymbol}
            onSelectSymbol={setSelectedSymbol}
          />
        </div>

        {/* Right Column (8 cols): Interactive Chart + Positions & Orders */}
        <div className="xl:col-span-8 flex flex-col space-y-3 h-[calc(100vh-5.5rem)] min-h-[500px]">
          {/* Top Half: TradingView Lightweight Candlestick Chart */}
          <div className="flex-1 min-h-[380px]">
            <InteractiveChartPanel
              symbol={selectedSymbol}
              profile={selectedProfile}
              candles={selectedCandles}
              tick={selectedTick}
              orders={orders}
            />
          </div>

          {/* Bottom Half: Positions, Order Book & Telemetry Stream Tabs */}
          <div className="h-[280px]">
            <PositionsAndOrdersTabs
              positions={positions}
              orders={orders}
              signals={signals}
              anomalies={anomalies}
              riskAlerts={riskAlerts}
            />
          </div>
        </div>
      </main>

      {/* Replay Control Modal */}
      <ReplayControlModal
        isOpen={isReplayModalOpen}
        onClose={() => setIsReplayModalOpen(false)}
        onTriggerReplay={triggerReplay}
      />
    </div>
  );
}
