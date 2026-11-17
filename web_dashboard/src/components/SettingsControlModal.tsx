"use client";

import React, { useState, useEffect } from "react";
import {
  AlertTriangle,
  CheckCircle,
  Key,
  Lock,
  RefreshCw,
  Save,
  Server,
  Shield,
  Sliders,
  X,
  Zap,
} from "lucide-react";
import { RuntimeSettings, TradingMode, UpdateSettingsPayload } from "@/types/telemetry";

interface SettingsControlModalProps {
  isOpen: boolean;
  onClose: () => void;
  onSettingsSaved?: () => void;
}

export const SettingsControlModal: React.FC<SettingsControlModalProps> = ({
  isOpen,
  onClose,
  onSettingsSaved,
}) => {
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [currentSettings, setCurrentSettings] = useState<RuntimeSettings | null>(null);
  const [toastMsg, setToastMsg] = useState<{ type: "success" | "error"; text: string } | null>(null);

  // Form State
  const [tradingMode, setTradingMode] = useState<TradingMode>("PAPER");
  const [capitalTier, setCapitalTier] = useState<string>("TIER_0_PAPER");
  const [kiteApiKey, setKiteApiKey] = useState("");
  const [kiteApiSecret, setKiteApiSecret] = useState("");
  const [kiteUserId, setKiteUserId] = useState("");
  const [kitePassword, setKitePassword] = useState("");
  const [kiteTotpSecret, setKiteTotpSecret] = useState("");
  const [geminiApiKey, setGeminiApiKey] = useState("");
  const [telegramBotToken, setTelegramBotToken] = useState("");
  const [telegramChatId, setTelegramChatId] = useState("");

  const [activeTab, setActiveTab] = useState<"execution" | "broker" | "alerts">("execution");
  const [confirmLiveMode, setConfirmLiveMode] = useState(false);

  const apiUrl = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
  const adminKey = process.env.NEXT_PUBLIC_ADMIN_KEY || "trillionaire-dev-secret-key-change-in-prod";

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
          setCapitalTier(data.active_capital_tier);
          setKiteUserId(data.kite_user_id || "");
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
      active_capital_tier: capitalTier,
    };

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
            <Key className="h-3.5 w-3.5" />
            <span>Zerodha Kite Connect</span>
            {currentSettings?.has_kite_key && (
              <span className="h-1.5 w-1.5 rounded-full bg-cockpit-green" />
            )}
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
                  </div>
                </div>
              )}

              {/* Tab 2: Zerodha Kite Connect Credentials */}
              {activeTab === "broker" && (
                <div className="space-y-4">
                  <div className="text-[11px] text-cockpit-muted leading-relaxed bg-cockpit-surface border border-cockpit-border p-3 rounded-lg">
                    Credentials are stored securely in root <code className="text-cockpit-blue">.env</code>.
                    Leave fields blank to preserve existing configured secrets.
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
