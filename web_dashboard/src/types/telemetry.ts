/**
 * Trillionaire Cockpit - Telemetry & Domain Types
 * Matches backend Pydantic v2 schemas and EventBus telemetry envelopes.
 */

export type TradingMode = "PAPER" | "LIVE" | "REPLAY";

export type DataFeederType = "FREE_LIVE" | "ZERODHA_KITE";

export type ClusterRegime =
  | "TREND_MOMENTUM"
  | "MEAN_REVERSION"
  | "HIGH_RVOL_BREAKOUT";

export type SentinelStatus = "NORMAL" | "CAUTION_HALF_SIZE" | "DO_NOT_TRADE";

export type Side = "BUY" | "SELL";

export type OrderType = "LIMIT" | "SL_LIMIT";

export type OrderStatus =
  | "PENDING_NEW"
  | "OPEN"
  | "PARTIALLY_FILLED"
  | "FILLED"
  | "CANCELLED"
  | "REJECTED";

export interface Tick {
  instrument_token: number;
  tradingsymbol: string;
  timestamp_ist: string;
  ltp: number;
  volume: number;
  bid: number;
  ask: number;
  oi: number;
}

export interface Candle {
  tradingsymbol: string;
  timeframe: string;
  timestamp_ist: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  vwap: number;
  ema_200: number;
  atr_14: number;
  rvol: number;
}

export interface StockProfile {
  tradingsymbol: string;
  instrument_token: number;
  parent_index: string;
  index_token: number;
  cluster_regime: ClusterRegime;
  beta_5y: number;
  adr_pct: number;
  atr_multiplier_sl: number;
  atr_multiplier_tp: number;
  min_rvol_threshold: number;
  oos_sharpe: number;
  oos_profit_factor: number;
  is_active: boolean;
  sentinel?: SentinelTag;
}

export interface SentinelTag {
  tradingsymbol: string;
  status: SentinelStatus;
  reason: string;
  risk_multiplier: number;
  updated_at_ist: string;
}

export interface Signal {
  signal_id: string;
  tradingsymbol: string;
  parent_index: string;
  side: Side;
  entry_price: number;
  stop_loss: number;
  target_price: number;
  confidence: number;
  strategy_name: string;
  generated_at_ist: string;
}

export interface Order {
  order_id: string;
  broker_order_id?: string;
  signal_id?: string;
  tradingsymbol: string;
  side: Side;
  product: string;
  order_type: OrderType;
  quantity: number;
  filled_quantity: number;
  limit_price?: number;
  average_fill_price?: number;
  status: OrderStatus;
  created_at_ist: string;
}

export interface Position {
  tradingsymbol: string;
  side: Side;
  quantity: number;
  average_entry_price: number;
  ltp: number;
  unrealized_pnl: number;
  realized_pnl: number;
  stop_loss: number;
  target_price: number;
}

export interface AnomalyAlert {
  symbol: string;
  kind: "INSTITUTIONAL_SURGE" | "FLASH_VOLATILITY_FREEZE" | string;
  message: string;
  timestamp_ist: string;
  metrics?: Record<string, any>;
}

export interface RiskAlert {
  symbol: string;
  level: "WARNING" | "CRITICAL" | "CIRCUIT_BREAKER" | "INFO";
  message: string;
  timestamp_ist: string;
}

export interface FunnelMetrics {
  candles_evaluated: number;
  passed_stock_rule: number;
  passed_sector_filter: number;
  signals_emitted: number;
}

export interface EngineStatus {
  mode: TradingMode;
  data_feeder?: DataFeederType;
  is_running: boolean;
  is_booted: boolean;
  capital_tier: string;
  allocated_capital: number;
  daily_mtm: number;
  realized_pnl: number;
  unrealized_pnl: number;
  circuit_breaker_tripped: boolean;
  profit_floor_armed: boolean;
  open_positions_count: number;
  active_stocks_count: number;
  funnel_metrics?: FunnelMetrics;
  timestamp_ist: string;
}

export interface RuntimeSettings {
  has_kite_key: boolean;
  kite_api_key_masked: string;
  kite_user_id: string;
  has_kite_secret: boolean;
  has_kite_password: boolean;
  has_totp_secret: boolean;
  has_gemini_key: boolean;
  has_telegram: boolean;
  trading_mode: TradingMode;
  data_feeder: DataFeederType;
  active_capital_tier: string;
  allocated_capital: number;
  available_capital_tiers: string[];
  engine_running: boolean;
  engine_booted: boolean;
  timestamp_ist: string;
}

export interface UpdateSettingsPayload {
  kite_api_key?: string;
  kite_api_secret?: string;
  kite_user_id?: string;
  kite_password?: string;
  kite_totp_secret?: string;
  gemini_api_key?: string;
  telegram_bot_token?: string;
  telegram_chat_id?: string;
  trading_mode?: TradingMode;
  data_feeder?: DataFeederType;
  active_capital_tier?: string;
}

export interface SnapshotPayload {
  status: Partial<EngineStatus>;
  latest_ticks: Record<string, Tick>;
  latest_candles: Record<string, Candle>;
  recent_signals: Signal[];
  recent_orders: Order[];
  recent_risk_alerts: RiskAlert[];
  recent_anomalies: AnomalyAlert[];
}

export type TelemetryEnvelope =
  | { type: "TICK"; timestamp_ist: string; payload: Tick }
  | { type: "CANDLE"; timestamp_ist: string; payload: Candle }
  | { type: "SIGNAL"; timestamp_ist: string; payload: Signal }
  | { type: "ORDER"; timestamp_ist: string; payload: Order }
  | { type: "ANOMALY"; timestamp_ist: string; payload: AnomalyAlert }
  | { type: "RISK"; timestamp_ist: string; payload: RiskAlert }
  | { type: "SNAPSHOT"; timestamp_ist: string; payload: SnapshotPayload };
