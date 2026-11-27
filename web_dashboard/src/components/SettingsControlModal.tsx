"use client";

import React, { useState, useEffect } from "react";
import {
  AlertTriangle,
  CheckCircle,
  Clock,
  ExternalLink,
  Key,
  Lock,
  Radio,
  RefreshCw,
  Save,
  Server,
  Shield,
  Sliders,
  Wifi,
  X,
  Zap,
} from "lucide-react";
import {
  DataFeederType,
  PipelineMilestones,
  RuntimeSettings,
  SentinelSummary,
  TradingMode,
  UpdateSettingsPayload,
} from "@/types/telemetry";
import { getApiBaseUrl } from "@/hooks/useTrillionaireStream";

interface SettingsControlModalProps {
  isOpen: boolean;
  onClose: () => void;
  onSettingsSaved?: () => void;
  initialTab?: "execution" | "broker" | "alerts";
  milestones?: PipelineMilestones | null;
  sentinelSummary?: SentinelSummary | null;
  tokenExpired?: boolean;
}

export const SettingsControlModal: React.FC<SettingsControlModalProps> = ({
  isOpen,
  onClose,
  onSettingsSaved,
  initialTab,
  milestones,
  sentinelSummary,
  tokenExpired = false,
}) => {
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [currentSettings, setCurrentSettings] = useState<RuntimeSettings | null>(null);
  const [toastMsg, setToastMsg] = useState<{ type: "success" | "error"; text: string } | null>(null);

  // Form State
  const [tradingMode, setTradingMode] = useState<TradingMode>("PAPER");
  const [dataFeeder, setDataFeeder] = useState<DataFeederType>("ZERODHA_KITE");
  const [capitalTier, setCapitalTier] = useState<string>("TIER_0_PAPER");
  const [allocatedCapital, setAllocatedCapital] = useState<string>("100000");
  const [riskPerTradePct, setRiskPerTradePct] = useState<string>("0.5");
  const [kiteApiKey, setKiteApiKey] = useState("");
  const [kiteApiSecret, setKiteApiSecret] = useState("");
  const [kiteUserId, setKiteUserId] = useState("");
  const [kitePassword, setKitePassword] = useState("");
  const [kiteTotpSecret, setKiteTotpSecret] = useState("");
  const [geminiApiKey, setGeminiApiKey] = useState("");
  const [telegramBotToken, setTelegramBotToken] = useState("");
  const [telegramChatId, setTelegramChatId] = useState("");
  const [enableTrendStrategy, setEnableTrendStrategy] = useState(true);
  const [enableMeanReversionStrategy, setEnableMeanReversionStrategy] = useState(true);
  const [enableRvolBreakoutStrategy, setEnableRvolBreakoutStrategy] = useState(true);
  const [enableProfitTradeManager, setEnableProfitTradeManager] = useState(true);
  const [enablePartialBooking, setEnablePartialBooking] = useState(true);
  const [enableLunchVolumeFilter, setEnableLunchVolumeFilter] = useState(true);
  const [enableVisionFilter, setEnableVisionFilter] = useState(true);
  const [entryStartTimeIst, setEntryStartTimeIst] = useState<"09:25" | "09:30">("09:25");

  const [activeTab, setActiveTab] = useState<"execution" | "broker" | "alerts">(initialTab || "execution");
  const [confirmLiveMode, setConfirmLiveMode] = useState(false);

  const apiUrl = getApiBaseUrl();
  const adminKey = process.env.NEXT_PUBLIC_ADMIN_KEY || "trillionaire-local-admin-123";

  useEffect(() => {
    if (initialTab) {
      setActiveTab(initialTab);
    }
  }, [initialTab, isOpen]);

  // Fetch current runtime settings on modal open
  useEffect(() => {
    if (!isOpen) return;

    const fetchSettings = async () => {
      setLoading(true);
      try {
        const res = await fetch(`${apiUrl}/api/v1/settings`, {
          headers: { "X-API-Key": adminKey },
        });
        if (res.ok) {
          const data: RuntimeSettings = await res.json();
          setCurrentSettings(data);
          setTradingMode(data.trading_mode);
          setDataFeeder("ZERODHA_KITE");
          setCapitalTier(data.active_capital_tier);
          setAllocatedCapital(String(data.allocated_capital ?? 100000));
          setRiskPerTradePct(String(data.risk_per_trade_pct ?? 0.5));
          setKiteUserId(data.kite_user_id || "");
          setEnableTrendStrategy(data.enable_trend_strategy ?? true);
          setEnableMeanReversionStrategy(data.enable_mean_reversion_strategy ?? true);
          setEnableRvolBreakoutStrategy(data.enable_rvol_breakout_strategy ?? true);
          setEnableProfitTradeManager(data.enable_profit_trade_manager ?? true);
          setEnablePartialBooking(data.enable_partial_booking ?? true);
          setEnableLunchVolumeFilter(data.enable_lunch_volume_filter ?? true);
          setEnableVisionFilter(data.enable_vision_filter ?? true);
          setEntryStartTimeIst(data.entry_start_time_ist === "09:30" ? "09:30" : "09:25");
        } else {
          setToastMsg({ type: "error", text: "Failed to load settings from engine API" });
        }
      } catch (e: any) {
        setToastMsg({ type: "error", text: `Connection error: ${e.message}` });
      } finally {
        setLoading(false);
      }
    };

    fetchSettings();
  }, [isOpen, apiUrl, adminKey]);

  if (!isOpen) return null;

  const handleSave = async () => {
    if (tradingMode === "LIVE" && currentSettings?.trading_mode !== "LIVE" && !confirmLiveMode) {
      setConfirmLiveMode(true);
      return;
    }

    setSaving(true);
    setToastMsg(null);

    const payload: UpdateSettingsPayload = {
      trading_mode: tradingMode,
      data_feeder: dataFeeder,
      active_capital_tier: capitalTier,
      enable_trend_strategy: enableTrendStrategy,
      enable_mean_reversion_strategy: enableMeanReversionStrategy,
      enable_rvol_breakout_strategy: enableRvolBreakoutStrategy,
      enable_profit_trade_manager: enableProfitTradeManager,
      enable_partial_booking: enablePartialBooking,
      enable_lunch_volume_filter: enableLunchVolumeFilter,
      enable_vision_filter: enableVisionFilter,
      entry_start_time_ist: entryStartTimeIst,
    };

    const parsedCapital = parseFloat(allocatedCapital);
    if (!Number.isNaN(parsedCapital) && parsedCapital > 0) {
      payload.allocated_capital = parsedCapital;
    }
    const parsedRiskPct = parseFloat(riskPerTradePct);
    if (!Number.isNaN(parsedRiskPct) && parsedRiskPct > 0) {
      payload.risk_per_trade_pct = parsedRiskPct;
    }

    if (kiteApiKey.trim()) payload.kite_api_key = kiteApiKey.trim();
    if (kiteApiSecret.trim()) payload.kite_api_secret = kiteApiSecret.trim();
    if (kiteUserId.trim()) payload.kite_user_id = kiteUserId.trim();
    if (kitePassword.trim()) payload.kite_password = kitePassword.trim();
    if (kiteTotpSecret.trim()) payload.kite_totp_secret = kiteTotpSecret.trim();
    if (geminiApiKey.trim()) payload.gemini_api_key = geminiApiKey.trim();
    if (telegramBotToken.trim()) payload.telegram_bot_token = telegramBotToken.trim();
    if (telegramChatId.trim()) payload.telegram_chat_id = telegramChatId.trim();

    try {
      const res = await fetch(`${apiUrl}/api/v1/settings`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-API-Key": adminKey,
        },
        body: JSON.stringify(payload),
      });

      if (res.ok) {
        const data = await res.json();
        setCurrentSettings(data.settings);
        setToastMsg({ type: "success", text: "Settings saved to .env & engine reloaded!" });
        // Clear sensitive input fields after save
        setKiteApiKey("");
        setKiteApiSecret("");
        setKitePassword("");
        setKiteTotpSecret("");
        setGeminiApiKey("");
        setTelegramBotToken("");
        setConfirmLiveMode(false);
        if (onSettingsSaved) onSettingsSaved();
        setTimeout(() => setToastMsg(null), 4000);
      } else {
        const err = await res.json();
        setToastMsg({ type: "error", text: err.detail || "Failed to persist settings" });
      }
    } catch (e: any) {
      setToastMsg({ type: "error", text: `Save error: ${e.message}` });
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 bg-black/80 backdrop-blur-md flex items-center justify-center p-4 select-none">
      <div className="bg-cockpit-card border border-cockpit-borderBright rounded-xl max-w-2xl w-full max-h-[90vh] flex flex-col shadow-2xl shadow-cockpit-blue/10 overflow-hidden animate-in fade-in zoom-in-95 duration-150">
        {/* Header */}
        <div className="h-14 px-6 border-b border-cockpit-border flex items-center justify-between bg-cockpit-surface/80">
          <div className="flex items-center space-x-2.5">
            <div className="h-8 w-8 rounded-lg bg-cockpit-blue/10 border border-cockpit-blue/30 flex items-center justify-center">
              <Sliders className="h-4 w-4 text-cockpit-blue" />
            </div>
            <div>
              <h2 className="text-sm font-bold text-white tracking-wide">SETTINGS & CREDENTIALS</h2>
              <p className="text-[11px] text-cockpit-muted">Runtime Mode, Sizing Tier & Broker Secrets</p>
            </div>
          </div>
          <button
            onClick={onClose}
            className="p-1.5 rounded-lg text-cockpit-muted hover:text-white hover:bg-cockpit-hover transition"
          >
            <X className="h-5 w-5" />
          </button>
        </div>

        {/* Tab Navigation */}
        <div className="flex border-b border-cockpit-border bg-cockpit-surface/40 px-6">
          <button
            onClick={() => setActiveTab("execution")}
            className={`py-2.5 px-4 text-xs font-medium border-b-2 transition flex items-center space-x-1.5 ${
              activeTab === "execution"
                ? "border-cockpit-blue text-cockpit-blue font-bold"
                : "border-transparent text-cockpit-muted hover:text-white"
            }`}
          >
            <Server className="h-3.5 w-3.5" />
            <span>Execution & Capital</span>
          </button>

          <button
            onClick={() => setActiveTab("broker")}
            className={`py-2.5 px-4 text-xs font-medium border-b-2 transition flex items-center space-x-1.5 ${
              activeTab === "broker"
                ? "border-cockpit-blue text-cockpit-blue font-bold"
                : "border-transparent text-cockpit-muted hover:text-white"
            }`}
          >
            <Radio className="h-3.5 w-3.5" />
            <span>Feeder & Broker</span>
            {currentSettings?.has_kite_key ? (
              <span className="h-1.5 w-1.5 rounded-full bg-cockpit-green" />
            ) : null}
          </button>

          <button
            onClick={() => setActiveTab("alerts")}
            className={`py-2.5 px-4 text-xs font-medium border-b-2 transition flex items-center space-x-1.5 ${
              activeTab === "alerts"
                ? "border-cockpit-blue text-cockpit-blue font-bold"
                : "border-transparent text-cockpit-muted hover:text-white"
            }`}
          >
            <Zap className="h-3.5 w-3.5" />
            <span>Sentinel & Telegram</span>
            {currentSettings?.has_gemini_key && (
              <span className="h-1.5 w-1.5 rounded-full bg-cockpit-green" />
            )}
          </button>
        </div>

        {/* Modal Body */}
        <div className="flex-1 overflow-y-auto p-6 space-y-5 text-xs font-mono">
          {loading ? (
            <div className="h-64 flex flex-col items-center justify-center space-y-3 text-cockpit-muted">
              <RefreshCw className="h-6 w-6 animate-spin text-cockpit-blue" />
              <span>Loading runtime settings...</span>
            </div>
          ) : (
            <>
              {/* Tab 1: Execution & Capital */}
              {activeTab === "execution" && (
                <div className="space-y-5">
                  {/* Trading Mode */}
                  <div>
                    <label className="text-[11px] font-bold text-cockpit-muted uppercase tracking-wider block mb-2">
                      Trading Mode
                    </label>
                    <div className="grid grid-cols-3 gap-3">
                      {(["REPLAY", "PAPER", "LIVE"] as TradingMode[]).map((mode) => (
                        <button
                          key={mode}
                          type="button"
                          onClick={() => setTradingMode(mode)}
                          className={`p-3 rounded-lg border text-left transition flex flex-col justify-between ${
                            tradingMode === mode
                              ? mode === "LIVE"
                                ? "bg-cockpit-green/10 border-cockpit-green text-cockpit-green shadow-md shadow-cockpit-green/10"
                                : mode === "REPLAY"
                                ? "bg-cockpit-purple/10 border-cockpit-purple text-cockpit-purple shadow-md shadow-cockpit-purple/10"
                                : "bg-cockpit-blue/10 border-cockpit-blue text-cockpit-blue shadow-md shadow-cockpit-blue/10"
                              : "bg-cockpit-surface border-cockpit-border text-cockpit-muted hover:border-cockpit-borderBright"
                          }`}
                        >
                          <div className="font-bold text-xs uppercase mb-1">{mode}</div>
                          <div className="text-[10px] opacity-80 font-sans leading-tight">
                            {mode === "REPLAY"
                              ? "Offline historical tick replayer"
                              : mode === "PAPER"
                              ? "Live NSE ticks, virtual money"
                              : "Real Zerodha broker orders"}
                          </div>
                        </button>
                      ))}
                    </div>

                    {tradingMode === "LIVE" && (
                      <div className="mt-3 p-3 rounded-lg bg-cockpit-amber/10 border border-cockpit-amber/40 text-cockpit-amber text-[11px] leading-relaxed flex items-start space-x-2">
                        <AlertTriangle className="h-4 w-4 shrink-0 mt-0.5" />
                        <div>
                          <b>CAUTION: LIVE BROKER EXECUTION SELECTED.</b> Orders will be submitted directly to
                          Zerodha Kite Connect as real MIS trades subject to statutory charges and market risks.
                        </div>
                      </div>
                    )}
                  </div>

                  {/* Capital Tier */}
                  <div>
                    <label className="text-[11px] font-bold text-cockpit-muted uppercase tracking-wider block mb-2">
                      Active Capital Tier (Risk Sizing)
                    </label>
                    <div className="grid grid-cols-2 gap-3">
                      {[
                        { id: "TIER_0_PAPER", name: "TIER 0 (PAPER)", cap: "₹1,00,000 Virtual", pos: "Max 3 Pos" },
                        { id: "TIER_1_MICRO", name: "TIER 1 (MICRO)", cap: "₹1,00,000 Live", pos: "Max 2 Pos" },
                        { id: "TIER_2_GROWTH", name: "TIER 2 (GROWTH)", cap: "₹5,00,000 Live", pos: "Max 3 Pos" },
                        { id: "TIER_3_SCALE", name: "TIER 3 (SCALE)", cap: "₹10,00,000 Live", pos: "Max 4 Pos" },
                      ].map((tier) => (
                        <button
                          key={tier.id}
                          type="button"
                          onClick={() => setCapitalTier(tier.id)}
                          className={`p-3 rounded-lg border text-left transition flex items-center justify-between ${
                            capitalTier === tier.id
                              ? "bg-cockpit-blue/10 border-cockpit-blue text-white shadow-md shadow-cockpit-blue/10"
                              : "bg-cockpit-surface border-cockpit-border text-cockpit-muted hover:border-cockpit-borderBright"
                          }`}
                        >
                          <div>
                            <div className="font-bold text-xs">{tier.name}</div>
                            <div className="text-[10px] text-cockpit-blue">{tier.cap}</div>
                          </div>
                          <span className="text-[10px] text-cockpit-muted font-mono">{tier.pos}</span>
                        </button>
                      ))}
                    </div>

                    {/* Allocated Capital & Risk-per-Trade Persistence */}
                    <div className="grid grid-cols-2 gap-3 mt-3">
                      <div className="bg-cockpit-surface border border-cockpit-border rounded-lg p-3">
                        <label className="block text-[10px] font-bold text-cockpit-muted uppercase mb-1">
                          Allocated Capital (₹)
                        </label>
                        <input
                          type="number"
                          min="1000"
                          step="1000"
                          value={allocatedCapital}
                          onChange={(e) => setAllocatedCapital(e.target.value)}
                          className="w-full px-2.5 py-1.5 rounded bg-cockpit-bg border border-cockpit-border text-white text-xs font-mono focus:outline-none focus:border-cockpit-blue"
                        />
                      </div>
                      <div className="bg-cockpit-surface border border-cockpit-border rounded-lg p-3">
                        <label className="block text-[10px] font-bold text-cockpit-muted uppercase mb-1">
                          Risk Per Trade (%)
                        </label>
                        <input
                          type="number"
                          min="0.05"
                          max="5.0"
                          step="0.05"
                          value={riskPerTradePct}
                          onChange={(e) => setRiskPerTradePct(e.target.value)}
                          className="w-full px-2.5 py-1.5 rounded bg-cockpit-bg border border-cockpit-border text-white text-xs font-mono focus:outline-none focus:border-cockpit-blue"
                        />
                      </div>
                    </div>
                  </div>

                  {/* Strategy & Execution Booster Toggles */}
                  <div className="bg-slate-950/70 border border-cockpit-border rounded-xl p-4 space-y-3">
                    <div className="flex items-center justify-between">
                      <label className="text-[11px] font-bold text-cockpit-muted uppercase tracking-wider">
                        Strategy Regimes & Profit Boosters (Persisted in .env)
                      </label>
                      <span className="text-[10px] text-cyan-400 font-mono">Live Hot-Reload</span>
                    </div>

                    {/* Opening Bell Entry Start Time */}
                    <div className="p-2.5 rounded-lg bg-cockpit-surface border border-cockpit-border flex items-center justify-between gap-3">
                      <div className="min-w-0">
                        <div className="text-xs font-bold text-white">Opening Bell Start Time (IST)</div>
                        <div className="text-[11px] text-slate-400 font-sans">
                          Sets earliest morning time (09:25 or 09:30 IST) when new intraday entries can fire.
                        </div>
                      </div>
                      <div className="flex items-center space-x-1.5 shrink-0">
                        {(["09:25", "09:30"] as const).map((tVal) => (
                          <button
                            key={tVal}
                            type="button"
                            onClick={() => setEntryStartTimeIst(tVal)}
                            className={`px-2.5 py-1 rounded text-[11px] font-bold border transition ${
                              entryStartTimeIst === tVal
                                ? "bg-cockpit-blue/20 text-cockpit-blue border-cockpit-blue"
                                : "bg-cockpit-bg text-cockpit-muted border-cockpit-border hover:text-white"
                            }`}
                          >
                            {tVal} IST
                          </button>
                        ))}
                      </div>
                    </div>

                    {/* 7 Interactive ON/OFF Toggles with 1-line plain-English descriptions */}
                    <div className="space-y-2">
                      {[
                        {
                          key: "enable_trend_strategy",
                          label: "Trend Momentum Strategy",
                          desc: "Trades Opening Range breakouts above/below VWAP and EMA(200) confirmed by parent index trend.",
                          val: enableTrendStrategy,
                          setter: setEnableTrendStrategy,
                        },
                        {
                          key: "enable_mean_reversion_strategy",
                          label: "Mean Reversion Strategy",
                          desc: "Fades extreme 1.5x ATR moves back toward VWAP inside CPR zones when the index is range-bound.",
                          val: enableMeanReversionStrategy,
                          setter: setEnableMeanReversionStrategy,
                        },
                        {
                          key: "enable_rvol_breakout_strategy",
                          label: "High-RVOL Breakout Strategy",
                          desc: "Enters strong volume-surge breakouts outperforming their parent sector index by at least +0.4%.",
                          val: enableRvolBreakoutStrategy,
                          setter: setEnableRvolBreakoutStrategy,
                        },
                        {
                          key: "enable_profit_trade_manager",
                          label: "Profit-Only Active Trade Manager",
                          desc: "On winning positions only: steps SL halfway at 50% to TP and extends TP +1R when near target with RVOL >= 1.5.",
                          val: enableProfitTradeManager,
                          setter: setEnableProfitTradeManager,
                        },
                        {
                          key: "enable_partial_booking",
                          label: "50% Partial Profit Booking (+1R)",
                          desc: "Books half of shares at +1R profit and moves Stop Loss on the remaining 50% runner to breakeven.",
                          val: enablePartialBooking,
                          setter: setEnablePartialBooking,
                        },
                        {
                          key: "enable_lunch_volume_filter",
                          label: "Choppy-Lunch Volume Filter (11:45–13:15 IST)",
                          desc: "Keeps scanning all 100 stocks, but blocks new entries during lunch unless relative volume is RVOL >= 2.0.",
                          val: enableLunchVolumeFilter,
                          setter: setEnableLunchVolumeFilter,
                        },
                        {
                          key: "enable_vision_filter",
                          label: "Gemini Vision Chart Trap Filter",
                          desc: "Inspects the M5 candlestick chart PNG before order entry to veto obvious double-top or exhaustion traps.",
                          val: enableVisionFilter,
                          setter: setEnableVisionFilter,
                        },
                      ].map((item) => (
                        <div
                          key={item.key}
                          className="p-2.5 rounded-lg bg-cockpit-surface border border-cockpit-border flex items-center justify-between gap-3"
                        >
                          <div className="min-w-0">
                            <div className="text-xs font-bold text-white">{item.label}</div>
                            <div className="text-[11px] text-slate-400 font-sans leading-snug">
                              {item.desc}
                            </div>
                          </div>
                          <button
                            type="button"
                            onClick={() => item.setter(!item.val)}
                            aria-pressed={item.val}
                            className={`px-3 py-1 rounded-full text-[11px] font-bold border shrink-0 transition ${
                              item.val
                                ? "bg-emerald-500/20 text-emerald-300 border-emerald-500/50"
                                : "bg-slate-800 text-slate-400 border-slate-700"
                            }`}
                          >
                            {item.val ? "ON" : "OFF"}
                          </button>
                        </div>
                      ))}
                    </div>
                  </div>
                </div>
              )}

              {/* Tab 2: Market Data Feeder & Zerodha Kite Connect */}
              {activeTab === "broker" && (
                <div className="space-y-4">
                  {/* Market Data Feeder Card: Strictly Zerodha Kite Connect */}
                  <div className="bg-cockpit-surface/80 border border-cockpit-borderBright p-4 rounded-xl space-y-3">
                    <div className="flex items-center justify-between">
                      <div className="flex items-center space-x-2">
                        <Radio className="h-4 w-4 text-amber-400" />
                        <span className="text-xs font-bold text-white tracking-wide">MARKET DATA FEEDER</span>
                      </div>
                      <span className="text-[10px] font-mono px-2 py-0.5 rounded bg-amber-500/10 border border-amber-500/40 text-amber-400 uppercase font-semibold">
                        Kite Connect WebSocket
                      </span>
                    </div>

                    <div className="p-3 rounded-lg border bg-amber-500/10 border-amber-500/60 shadow-lg shadow-amber-500/10">
                      <div className="flex items-center justify-between mb-1.5">
                        <div className="flex items-center space-x-1.5 font-bold text-xs text-white">
                          <Key className="h-3.5 w-3.5 text-amber-400" />
                          <span>Zerodha Kite Connect</span>
                        </div>
                        <span className="text-[10px] px-1.5 py-0.5 rounded bg-amber-500/20 text-amber-400 border border-amber-500/40 font-mono font-bold">
                          Sole Market Feeder
                        </span>
                      </div>
                      <p className="text-[11px] text-cockpit-muted leading-relaxed">
                        Official Zerodha KiteTicker WebSocket streaming real-time LTPs, quotes, and market depth for all 100 universe stocks and parent sector indices.
                      </p>
                    </div>
                  </div>

                  

        {/* 1-Click Browser OAuth Login */}
        <div
          className={`p-3.5 rounded-lg border flex flex-col sm:flex-row sm:items-center justify-between gap-3 mb-3 ${
            tokenExpired || currentSettings?.token_expired
              ? "bg-rose-950/50 border-rose-500/70 shadow-lg shadow-rose-500/15"
              : "bg-gradient-to-r from-cyan-950/40 via-blue-950/30 to-slate-900/60 border-cyan-500/30"
          }`}
        >
          <div>
            <div
              className={`flex items-center gap-1.5 text-xs font-semibold ${
                tokenExpired || currentSettings?.token_expired ? "text-rose-300" : "text-cyan-300"
              }`}
            >
              <Key
                className={`h-3.5 w-3.5 ${
                  tokenExpired || currentSettings?.token_expired ? "text-rose-400 animate-pulse" : "text-cyan-400"
                }`}
              />
              <span>
                {tokenExpired || currentSettings?.token_expired
                  ? "TOKEN EXPIRED — 1-Click Zerodha Re-Login Required"
                  : "1-Click Zerodha Kite Connect OAuth Login"}
              </span>
            </div>
            <div className="text-[11px] text-slate-300 mt-0.5">
              {tokenExpired || currentSettings?.token_expired
                ? "Your Kite access token expired or was invalidated (TokenException/403). Click Re-Login to resume live orders and WebSocket ticks."
                : "Instantly authenticate your live Kite session in your browser. Auto-exchanges token and arms Zerodha streaming."}
            </div>
          </div>
          <a
            href={`${apiUrl}/api/v1/kite/login`}
            target="_blank"
            rel="noopener noreferrer"
            className={`flex-shrink-0 inline-flex items-center justify-center space-x-1.5 px-3 py-1.5 rounded text-xs font-bold transition shadow-sm ${
              tokenExpired || currentSettings?.token_expired
                ? "bg-rose-500 hover:bg-rose-400 text-white animate-pulse"
                : "bg-cyan-600 hover:bg-cyan-500 text-slate-950"
            }`}
          >
            <span>{tokenExpired || currentSettings?.token_expired ? "Re-Login with Kite" : "Login with Kite"}</span>
            <ExternalLink className="h-3.5 w-3.5" />
          </a>
        </div>

        {/* Saved Kite Account Profile Selector */}

        <div className="p-3 rounded-lg bg-slate-900/80 border border-slate-700/80 flex flex-col sm:flex-row sm:items-center justify-between gap-2 mb-3">

          <div>

            <div className="text-xs font-semibold text-emerald-400">

              SAVED KITE ACCOUNT PROFILES (.env Vault)

            </div>

            <div className="text-[11px] text-slate-400">

              {currentSettings?.has_kite_key

                ? `Active in Vault: ${currentSettings.kite_user_id || "Not Set"} (${currentSettings.kite_api_key_masked || "****"}) — Leave secret boxes blank to keep saved credentials`

                : "No Kite profile saved yet — enter credentials below"}

            </div>

          </div>

          <select

            value={currentSettings?.has_kite_key ? "saved_default" : "new"}

            onChange={(e) => {

              if (e.target.value === "saved_default") {

                setDataFeeder("ZERODHA_KITE");

              }

            }}

            className="bg-slate-950 border border-slate-700 rounded px-2.5 py-1.5 text-xs text-slate-200 focus:outline-none focus:border-cyan-500"

          >

            {currentSettings?.has_kite_key && (

              <option value="saved_default">

                ✓ Kite ({currentSettings.kite_user_id || "Saved"}) • {currentSettings.kite_api_key_masked || "****"} [Active]

              </option>

            )}

            <option value="new">+ Enter / Overwrite Kite Credentials</option>

          </select>

        </div>


                  <div className="text-[11px] text-cockpit-muted leading-relaxed bg-cockpit-surface border border-cockpit-border p-3 rounded-lg flex items-center justify-between">
                    <span>
                      Zerodha Kite Connect credentials stored securely in <code className="text-cockpit-blue">.env</code>.
                    </span>
                    <span className="text-[10px] px-2 py-0.5 rounded bg-cockpit-surface border border-cockpit-border text-amber-400 uppercase font-mono">
                      Active Feeder
                    </span>
                  </div>

                  {/* KITE_API_KEY */}
                  <div>
                    <div className="flex items-center justify-between mb-1.5">
                      <label className="text-[11px] font-bold text-cockpit-text">KITE_API_KEY</label>
                      {currentSettings?.has_kite_key && (
                        <span className="text-[10px] text-cockpit-green flex items-center space-x-1">
                          <CheckCircle className="h-3 w-3" />
                          <span>Configured ({currentSettings.kite_api_key_masked})</span>
                        </span>
                      )}
                    </div>
                    <input
                      type="text"
                      value={kiteApiKey}
                      onChange={(e) => setKiteApiKey(e.target.value)}
                      placeholder={currentSettings?.has_kite_key ? "•••• Leave blank to keep current" : "Enter Zerodha API Key"}
                      className="w-full bg-cockpit-surface border border-cockpit-border rounded-lg px-3 py-2 text-xs text-white focus:outline-none focus:border-cockpit-blue transition"
                    />
                  </div>

                  {/* KITE_API_SECRET */}
                  <div>
                    <div className="flex items-center justify-between mb-1.5">
                      <label className="text-[11px] font-bold text-cockpit-text">KITE_API_SECRET</label>
                      {currentSettings?.has_kite_secret && (
                        <span className="text-[10px] text-cockpit-green flex items-center space-x-1">
                          <CheckCircle className="h-3 w-3" />
                          <span>Configured</span>
                        </span>
                      )}
                    </div>
                    <input
                      type="password"
                      value={kiteApiSecret}
                      onChange={(e) => setKiteApiSecret(e.target.value)}
                      placeholder={currentSettings?.has_kite_secret ? "•••• Leave blank to keep current" : "Enter API Secret"}
                      className="w-full bg-cockpit-surface border border-cockpit-border rounded-lg px-3 py-2 text-xs text-white focus:outline-none focus:border-cockpit-blue transition"
                    />
                  </div>

                  {/* KITE_USER_ID & PASSWORD */}
                  <div className="grid grid-cols-2 gap-3">
                    <div>
                      <label className="text-[11px] font-bold text-cockpit-text block mb-1.5">KITE_USER_ID</label>
                      <input
                        type="text"
                        value={kiteUserId}
                        onChange={(e) => setKiteUserId(e.target.value)}
                        placeholder="e.g. AB1234"
                        className="w-full bg-cockpit-surface border border-cockpit-border rounded-lg px-3 py-2 text-xs text-white focus:outline-none focus:border-cockpit-blue transition"
                      />
                    </div>
                    <div>
                      <div className="flex items-center justify-between mb-1.5">
                        <label className="text-[11px] font-bold text-cockpit-text">KITE_PASSWORD</label>
                        {currentSettings?.has_kite_password && (
                          <span className="text-[10px] text-cockpit-green flex items-center space-x-1">
                            <CheckCircle className="h-3 w-3" />
                            <span>Saved</span>
                          </span>
                        )}
                      </div>
                      <input
                        type="password"
                        value={kitePassword}
                        onChange={(e) => setKitePassword(e.target.value)}
                        placeholder={currentSettings?.has_kite_password ? "•••• Leave blank" : "Password"}
                        className="w-full bg-cockpit-surface border border-cockpit-border rounded-lg px-3 py-2 text-xs text-white focus:outline-none focus:border-cockpit-blue transition"
                      />
                    </div>
                  </div>

                  {/* KITE_TOTP_SECRET */}
                  <div>
                    <div className="flex items-center justify-between mb-1.5">
                      <label className="text-[11px] font-bold text-cockpit-text">
                        KITE_TOTP_SECRET (Headless 2FA Auto-Login)
                      </label>
                      {currentSettings?.has_totp_secret && (
                        <span className="text-[10px] text-cockpit-green flex items-center space-x-1">
                          <CheckCircle className="h-3 w-3" />
                          <span>Configured</span>
                        </span>
                      )}
                    </div>
                    <input
                      type="text"
                      value={kiteTotpSecret}
                      onChange={(e) => setKiteTotpSecret(e.target.value)}
                      placeholder={currentSettings?.has_totp_secret ? "•••• Leave blank to keep current" : "Base32 Secret String (e.g. JBSWY3DPEHPK3PXP)"}
                      className="w-full bg-cockpit-surface border border-cockpit-border rounded-lg px-3 py-2 text-xs text-white focus:outline-none focus:border-cockpit-blue transition"
                    />
                  </div>

                  {/* Engine Pipeline Readiness (5 Milestones) Card */}
                  <div className="bg-slate-950/80 border border-slate-800 rounded-xl p-4 space-y-3 mt-4">
                    <div className="flex items-center justify-between">
                      <div className="flex items-center space-x-2">
                        <Shield className="h-4 w-4 text-cyan-400" />
                        <span className="text-xs font-bold text-white uppercase tracking-wider">
                          Engine Pipeline Readiness ({milestones?.total_count ?? 6} Milestones)
                        </span>
                      </div>
                      {milestones && (
                        <span className="text-[10px] font-mono px-2 py-0.5 rounded bg-cyan-500/10 border border-cyan-500/40 text-cyan-400 font-bold">
                          {milestones.summary_label}
                        </span>
                      )}
                    </div>

                    <div className="space-y-2">
                      {milestones?.milestones?.map((m) => (
                        <div
                          key={m.id}
                          className="flex items-start justify-between p-2.5 rounded-lg bg-slate-900/60 border border-slate-800/80"
                        >
                          <div className="flex items-start space-x-2.5">
                            {m.status === "passed" ? (
                              <CheckCircle className="h-4 w-4 text-emerald-400 flex-shrink-0 mt-0.5" />
                            ) : m.status === "error" ? (
                              <AlertTriangle className="h-4 w-4 text-rose-400 flex-shrink-0 mt-0.5" />
                            ) : m.status === "warning" ? (
                              <AlertTriangle className="h-4 w-4 text-amber-400 flex-shrink-0 mt-0.5" />
                            ) : (
                              <Clock className="h-4 w-4 text-slate-400 flex-shrink-0 mt-0.5" />
                            )}
                            <div>
                              <div className="text-xs font-semibold text-slate-200">{m.label}</div>
                              <div className="text-[11px] text-slate-400 mt-0.5">{m.detail}</div>
                            </div>
                          </div>
                          <span
                            className={`text-[10px] font-mono font-bold px-1.5 py-0.5 rounded uppercase flex-shrink-0 ml-2 ${
                              m.status === "passed"
                                ? "bg-emerald-500/20 text-emerald-400 border border-emerald-500/40"
                                : m.status === "error"
                                ? "bg-rose-500/20 text-rose-400 border border-rose-500/40"
                                : m.status === "warning"
                                ? "bg-amber-500/20 text-amber-400 border border-amber-500/40"
                                : "bg-slate-700/40 text-slate-400 border border-slate-600/40"
                            }`}
                          >
                            {m.status}
                          </span>
                        </div>
                      ))}
                    </div>
                  </div>
                </div>
              )}

              {/* Tab 3: Alerts & Sentinel */}
              {activeTab === "alerts" && (
                <div className="space-y-4">
                  {/* GEMINI_API_KEY */}
                  <div>
                    <div className="flex items-center justify-between mb-1.5">
                      <label className="text-[11px] font-bold text-cockpit-text">
                        GEMINI_API_KEY (Pre-Market News NLP Triage)
                      </label>
                      {currentSettings?.has_gemini_key && (
                        <span className="text-[10px] text-cockpit-green flex items-center space-x-1">
                          <CheckCircle className="h-3 w-3" />
                          <span>Configured</span>
                        </span>
                      )}
                    </div>
                    <input
                      type="password"
                      value={geminiApiKey}
                      onChange={(e) => setGeminiApiKey(e.target.value)}
                      placeholder={currentSettings?.has_gemini_key ? "•••• Leave blank to keep current" : "AIzaSy..."}
                      className="w-full bg-cockpit-surface border border-cockpit-border rounded-lg px-3 py-2 text-xs text-white focus:outline-none focus:border-cockpit-blue transition"
                    />
                  </div>

                  {/* TELEGRAM_BOT_TOKEN & CHAT_ID */}
                  <div className="grid grid-cols-2 gap-3">
                    <div>
                      <div className="flex items-center justify-between mb-1.5">
                        <label className="text-[11px] font-bold text-cockpit-text">TELEGRAM_BOT_TOKEN</label>
                        {currentSettings?.has_telegram && (
                          <span className="text-[10px] text-cockpit-green flex items-center space-x-1">
                            <CheckCircle className="h-3 w-3" />
                            <span>Configured</span>
                          </span>
                        )}
                      </div>
                      <input
                        type="password"
                        value={telegramBotToken}
                        onChange={(e) => setTelegramBotToken(e.target.value)}
                        placeholder="123456789:ABCdef..."
                        className="w-full bg-cockpit-surface border border-cockpit-border rounded-lg px-3 py-2 text-xs text-white focus:outline-none focus:border-cockpit-blue transition"
                      />
                    </div>
                    <div>
                      <label className="text-[11px] font-bold text-cockpit-text block mb-1.5">TELEGRAM_CHAT_ID</label>
                      <input
                        type="text"
                        value={telegramChatId}
                        onChange={(e) => setTelegramChatId(e.target.value)}
                        placeholder="e.g. 987654321"
                        className="w-full bg-cockpit-surface border border-cockpit-border rounded-lg px-3 py-2 text-xs text-white focus:outline-none focus:border-cockpit-blue transition"
                      />
                    </div>
                  </div>

                  {/* Gemini Sentinel Brief & Overnight Market Cues Card */}
                  {sentinelSummary && (
                    <div className="bg-slate-950/80 border border-purple-500/30 rounded-xl p-4 space-y-3 mt-4">
                      <div className="flex items-center justify-between">
                        <div className="flex items-center space-x-2">
                          <Zap className="h-4 w-4 text-purple-400" />
                          <span className="text-xs font-bold text-white uppercase tracking-wider">
                            Gemini Sentinel Brief & Overnight Market Cues
                          </span>
                        </div>
                        <span className="text-[10px] font-mono text-purple-400">
                          {sentinelSummary.high_risk_count} Flagged Stocks
                        </span>
                      </div>
                      <div className="p-3 rounded-lg bg-purple-950/20 border border-purple-500/20 text-xs text-slate-200 leading-relaxed font-sans">
                        {sentinelSummary.gemini_brief}
                      </div>
                      {sentinelSummary.headlines && sentinelSummary.headlines.length > 0 && (
                        <div className="space-y-1.5 mt-2">
                          <div className="text-[11px] font-bold text-slate-400 uppercase tracking-wide">
                            Recent Headlines ({sentinelSummary.headlines.length})
                          </div>
                          <div className="max-h-36 overflow-y-auto space-y-1 pr-1 font-mono text-[11px]">
                            {sentinelSummary.headlines.map((h, i) => (
                              <div
                                key={i}
                                className="flex items-center justify-between p-1.5 rounded bg-slate-900 border border-slate-800 text-slate-300"
                              >
                                <span className="truncate mr-2">• {h.title}</span>
                                <span className="text-[10px] text-slate-500 flex-shrink-0">
                                  {h.source} | {h.time}
                                </span>
                              </div>
                            ))}
                          </div>
                        </div>
                      )}
                    </div>
                  )}
                </div>
              )}
            </>
          )}

          {/* Toast / Feedback */}
          {toastMsg && (
            <div
              className={`p-3 rounded-lg border text-xs flex items-center space-x-2 ${
                toastMsg.type === "success"
                  ? "bg-cockpit-green/10 border-cockpit-green text-cockpit-green"
                  : "bg-cockpit-red/10 border-cockpit-red text-cockpit-red"
              }`}
            >
              {toastMsg.type === "success" ? <CheckCircle className="h-4 w-4" /> : <AlertTriangle className="h-4 w-4" />}
              <span>{toastMsg.text}</span>
            </div>
          )}

          {/* Confirm LIVE Mode Confirmation Dialog */}
          {confirmLiveMode && (
            <div className="p-4 rounded-lg bg-cockpit-red/10 border border-cockpit-red text-cockpit-red space-y-2">
              <div className="font-bold flex items-center space-x-2">
                <AlertTriangle className="h-4 w-4" />
                <span>CONFIRM LIVE TRADING ACTIVATION</span>
              </div>
              <p className="text-[11px] text-cockpit-text leading-relaxed">
                You are about to switch the Trillionaire Engine to <b>LIVE REAL-MONEY TRADING</b>. Real orders
                will be executed on your Zerodha account. Are you sure you wish to apply this setting?
              </p>
              <div className="flex space-x-2 pt-1">
                <button
                  type="button"
                  onClick={() => setConfirmLiveMode(false)}
                  className="px-3 py-1.5 rounded bg-cockpit-surface border border-cockpit-border text-white text-[11px]"
                >
                  Cancel
                </button>
                <button
                  type="button"
                  onClick={handleSave}
                  className="px-3 py-1.5 rounded bg-cockpit-red text-white font-bold text-[11px]"
                >
                  Yes, Activate LIVE Mode
                </button>
              </div>
            </div>
          )}
        </div>

        {/* Footer Actions */}
        <div className="h-16 px-6 border-t border-cockpit-border bg-cockpit-surface/80 flex items-center justify-between">
          <div className="text-[11px] text-cockpit-muted flex items-center space-x-2">
            <Shield className="h-3.5 w-3.5 text-cockpit-blue" />
            <span>Admin Key Protected (X-API-Key)</span>
          </div>

          <div className="flex items-center space-x-3">
            <button
              type="button"
              onClick={onClose}
              className="px-4 py-2 text-xs font-medium text-cockpit-muted hover:text-white transition"
            >
              Cancel
            </button>
            <button
              type="button"
              disabled={saving || loading}
              onClick={handleSave}
              className="px-5 py-2 rounded-lg bg-cockpit-blue hover:bg-cockpit-blue/80 text-white font-bold text-xs tracking-wider transition flex items-center space-x-2 shadow-lg shadow-cockpit-blue/20"
            >
              {saving ? <RefreshCw className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
              <span>SAVE & APPLY SETTINGS</span>
            </button>
          </div>
        </div>
      </div>
    </div>
  );
};
