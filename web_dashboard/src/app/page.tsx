"use client";

import React, { useMemo, useState } from "react";
import { TopTelemetryBar } from "@/components/TopTelemetryBar";
import { InteractiveChartPanel } from "@/components/InteractiveChartPanel";
import { WatchlistAndSentinelMatrix } from "@/components/WatchlistAndSentinelMatrix";
import { PositionsAndOrdersTabs } from "@/components/PositionsAndOrdersTabs";
import { ReplayControlModal } from "@/components/ReplayControlModal";
import { SettingsControlModal } from "@/components/SettingsControlModal";
import { NewsMarquee } from "@/components/NewsMarquee";
import { useTrillionaireStream } from "@/hooks/useTrillionaireStream";
import { SentinelHeadline } from "@/types/telemetry";

export default function CockpitPage() {
  const {
    connectionStatus,
    status,
    ticks,
    candles,
    signals,
    orders,
    positions,
    closedTrades,
    anomalies,
    riskAlerts,
    profiles,
    sentinelSummary,
    selectedSymbol,
    setSelectedSymbol,
    selectedTimeframe,
    setSelectedTimeframe,
    liveUnrealizedPnl,
    liveRealizedPnl,
    liveDailyMtm,
    exitPosition,
    cancelOrder,
    triggerEmergencyFlatten,
    overrideSentinelLock,
    triggerReplay,
    startEngine,
    stopEngine,
    pauseTrading,
    resumeTrading,
    refreshState,
  } = useTrillionaireStream();

  const [isReplayModalOpen, setIsReplayModalOpen] = useState(false);
  const [isSettingsModalOpen, setIsSettingsModalOpen] = useState(false);
  const [settingsTab, setSettingsTab] = useState<"execution" | "broker" | "alerts">("execution");

  const selectedProfile = profiles.find((p) => p.tradingsymbol === selectedSymbol);
  const selectedCandles = candles[`${selectedSymbol}_${selectedTimeframe}`] || [];
  const selectedTick = ticks[selectedSymbol];

  // Surface live operational alerts (token expiry, order rejections, circuit breakers) into Sentinel Newswire
  const mergedHeadlines: SentinelHeadline[] = useMemo(() => {
    const alertItems: SentinelHeadline[] = riskAlerts.slice(0, 5).map((alert) => ({
      title: `[${alert.level}] ${alert.symbol}: ${alert.message}`,
      summary: alert.message,
      source: "Risk Desk",
      time: new Date(alert.timestamp_ist).toLocaleTimeString("en-IN", {
        timeZone: "Asia/Kolkata",
        hour12: false,
        hour: "2-digit",
        minute: "2-digit",
      }) + " IST",
      category: "SYSTEM_ALERT",
    }));
    const newsItems = sentinelSummary?.headlines || [];
    return [...alertItems, ...newsItems];
  }, [riskAlerts, sentinelSummary?.headlines]);

  return (
    <div className="min-h-screen flex flex-col bg-cockpit-bg text-cockpit-text font-mono">
      {/* 1. Header Telemetry Bar */}
      <TopTelemetryBar
        connectionStatus={connectionStatus}
        status={status}
        ticks={ticks}
        positions={positions}
        closedTrades={closedTrades}
        liveUnrealizedPnl={liveUnrealizedPnl}
        liveRealizedPnl={liveRealizedPnl}
        liveDailyMtm={liveDailyMtm}
        onEmergencyFlatten={triggerEmergencyFlatten}
        onOpenReplayModal={() => setIsReplayModalOpen(true)}
        onOpenSettingsModal={(tab) => {
          if (tab && (tab === "execution" || tab === "broker" || tab === "alerts")) {
            setSettingsTab(tab);
          }
          setIsSettingsModalOpen(true);
        }}
        onStartEngine={startEngine}
        onStopEngine={stopEngine}
        onPauseTrading={pauseTrading}
        onResumeTrading={resumeTrading}
        onRefresh={refreshState}
      />

      {/* 2. Scrolling Token-Free News & Alert Marquee */}
      <NewsMarquee headlines={mergedHeadlines} />

      {/* 3. Main High-Density Cockpit Grid */}
      <main className="flex-1 p-3 grid grid-cols-1 xl:grid-cols-12 gap-3 overflow-hidden">
        {/* Left Column (4 cols): Watchlist & Sentinel Matrix */}
        <div className="xl:col-span-4 h-[calc(100vh-5.5rem)] min-h-[500px]">
          <WatchlistAndSentinelMatrix
            profiles={profiles}
            ticks={ticks}
            selectedSymbol={selectedSymbol}
            onSelectSymbol={setSelectedSymbol}
            onOverrideLock={overrideSentinelLock}
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
              positions={positions}
              closedTrades={closedTrades}
              timeframe={selectedTimeframe}
              onTimeframeChange={setSelectedTimeframe}
            />
          </div>

          {/* Bottom Half: Positions, Order Book & Immutable Report Tabs */}
          <div className="h-[280px]">
            <PositionsAndOrdersTabs
              positions={positions}
              orders={orders}
              closedTrades={closedTrades}
              ticks={ticks}
              liveRealizedPnl={liveRealizedPnl}
              liveUnrealizedPnl={liveUnrealizedPnl}
              liveDailyMtm={liveDailyMtm}
              onExitPosition={exitPosition}
              onCancelOrder={cancelOrder}
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

      {/* Settings & Credentials Control Modal */}
      <SettingsControlModal
        isOpen={isSettingsModalOpen}
        onClose={() => setIsSettingsModalOpen(false)}
        onSettingsSaved={refreshState}
        initialTab={settingsTab}
        milestones={status?.milestones}
        sentinelSummary={sentinelSummary}
        tokenExpired={Boolean(status?.token_expired)}
      />
    </div>
  );
}
