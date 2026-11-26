"use client";

import React, { useMemo, useState } from "react";
import { TopTelemetryBar } from "@/components/TopTelemetryBar";
import { InteractiveChartPanel } from "@/components/InteractiveChartPanel";
import { WatchlistAndSentinelMatrix } from "@/components/WatchlistAndSentinelMatrix";
import { EngineControlsPanel } from "@/components/EngineControlsPanel";
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
    tradeAutopsy,
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
    runTradeAutopsy,
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
    <div className="min-h-screen lg:h-screen lg:overflow-hidden flex flex-col bg-cockpit-bg text-cockpit-text font-mono">
      {/* 1. Pinned Top Header Telemetry Bar */}
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
      <div className="shrink-0">
        <NewsMarquee headlines={mergedHeadlines} />
      </div>

      {/* 3. Main Zero-Outer-Scroll 4-Quadrant Cockpit Grid */}
      <main className="flex-1 min-h-0 flex flex-col lg:flex-row gap-2 p-2 overflow-y-auto lg:overflow-hidden">
        {/* Left Column: Watchlist (Top-Left 58%) + Engine Controls (Bottom-Left 42%) */}
        <div className="lg:w-[340px] shrink-0 flex flex-col gap-2 min-h-0">
          <div className="flex-[58] min-h-0">
            <WatchlistAndSentinelMatrix
              profiles={profiles}
              ticks={ticks}
              selectedSymbol={selectedSymbol}
              onSelectSymbol={setSelectedSymbol}
              onOverrideLock={overrideSentinelLock}
            />
          </div>
          <div className="hidden lg:block flex-[42] min-h-0">
            <EngineControlsPanel
              status={status}
              liveDailyMtm={liveDailyMtm}
              onStartEngine={startEngine}
              onStopEngine={stopEngine}
              onPauseTrading={pauseTrading}
              onResumeTrading={resumeTrading}
              onOpenSettingsModal={(tab) => {
                if (tab && (tab === "execution" || tab === "broker" || tab === "alerts")) {
                  setSettingsTab(tab);
                }
                setIsSettingsModalOpen(true);
              }}
              onOpenReplayModal={() => setIsReplayModalOpen(true)}
              onRefresh={refreshState}
              onEmergencyFlatten={triggerEmergencyFlatten}
            />
          </div>
        </div>

        {/* Right Column: Chart (Top-Right 58%) + Orders/Report Tabs (Bottom-Right 42%) */}
        <div className="flex-1 flex flex-col gap-2 min-h-0 min-w-0">
          <div className="flex-[58] min-h-0">
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
          <div className="flex-[42] min-h-0">
            <PositionsAndOrdersTabs
              positions={positions}
              orders={orders}
              closedTrades={closedTrades}
              ticks={ticks}
              selectedSymbol={selectedSymbol}
              onSelectSymbol={setSelectedSymbol}
              liveRealizedPnl={liveRealizedPnl}
              liveUnrealizedPnl={liveUnrealizedPnl}
              liveDailyMtm={liveDailyMtm}
              tradeAutopsy={tradeAutopsy}
              onRunTradeAutopsy={runTradeAutopsy}
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
