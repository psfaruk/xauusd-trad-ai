/**
 * Shared frontend types (SPEC §7.1 REST payloads + §7.2 WS events).
 */

export type Timeframe = "M1" | "M5" | "M15" | "M30" | "H1" | "H4" | "D1";

export const TIMEFRAMES: Timeframe[] = ["M1", "M5", "M15", "M30", "H1", "H4", "D1"];

export type SignalDirection = "BUY" | "SELL";

export type SignalStatus = "active" | "won" | "lost" | "expired" | "cancelled";

export interface HealthInfo {
  status: string;
  version: string;
  data_source: "mock" | "mt5" | "live";
  /** D-032: what the env requested (differs from data_source only on degrade). */
  requested_data_source?: "mock" | "mt5" | "live";
  degraded?: boolean;
  db: boolean;
}

/** GET /api/me — profile + role (SPEC §7.1). */
export interface MeInfo {
  id: string;
  email: string | null;
  display_name: string | null;
  role: "admin" | "viewer" | string;
}

/** OHLCV bar — `t` is the UTC open time in epoch SECONDS. */
export interface Candle {
  t: number;
  o: number;
  h: number;
  l: number;
  c: number;
  v: number;
}

export interface CandlesResponse {
  symbol: string;
  tf: Timeframe;
  count: number;
  candles: Candle[];
}

export interface TraceCheck {
  name: string;
  pass: boolean;
  value: string;
}

export interface SignalTrace {
  direction: SignalDirection | null;
  checks: TraceCheck[];
  params: Record<string, unknown>;
  /** D-041 — which pattern fired: "sfp" (liquidity sweep) | "pullback". */
  trigger?: "sfp" | "pullback" | string;
}

export interface Signal {
  id: string;
  ts: string;
  symbol: string;
  tf: string;
  direction: SignalDirection;
  entry: number;
  sl: number;
  tp: number;
  confidence: number;
  trace: SignalTrace | null;
  status: SignalStatus;
  result_r: number | null;
  closed_at: string | null;
}

/** Live-feed transparency (D-030): active provider + price freshness. */
export interface FeedStatus {
  provider: string;
  detail: string;
  last_price: number | null;
  spread: number | null;
  last_tick_age_s: number | null;
  /** D-033: real events/sec + per-venue stream health. */
  tps?: number | null;
  venues?: Record<string, { ok: boolean; events: number; age_s: number | null; err: string | null }>;
  /** D-035: per-symbol view (XAUUSD + BTCUSD) — `mt5: true` = real broker
   * feed from the MetaTrader 5 terminal is the authority for that symbol. */
  symbols?: Record<string, SymbolFeedStatus>;
  mt5?: Record<string, Mt5FeedVenueStatus>;
  note?: string;
}

export interface SymbolFeedStatus {
  provider: string;
  detail: string;
  mt5: boolean;
  /** D-037: open | closed | unavailable (weekend gold = "closed"). */
  market?: "open" | "closed" | "unavailable" | "unknown";
  last_price: number | null;
  spread: number | null;
  last_tick_age_s: number | null;
  tps?: number | null;
  venues?: Record<string, { ok: boolean; events: number; age_s: number | null; err: string | null }>;
}

export interface Mt5FeedVenueStatus {
  ok: boolean;
  broker_symbol: string | null;
  events: number;
  age_s: number | null;
  tps: number | null;
  err: string | null;
}

/** D-037: the USER's own broker connection (per-user, real terminal). */
export interface BrokerConnection {
  status: "connected" | "disconnected" | "reconnecting";
  login?: string;
  server?: string;
  connected_at?: number;
  account?: {
    login: number | string | null;
    name?: string | null;
    server?: string | null;
    broker?: string | null;
    currency?: string | null;
    balance?: number | null;
    equity?: number | null;
    margin_free?: number | null;
    profit?: number | null;
    leverage?: number | null;
  } | null;
}

export interface Mt5Status {
  status: "connected" | "disconnected" | "reconnecting";
  symbol: string | null;
  /** D-035: every chartable symbol (XAUUSD + BTCUSD). */
  symbols?: string[];
  /** D-037: the requesting user's broker connection. */
  broker?: BrokerConnection;
  account: {
    balance: number | null;
    equity: number | null;
    currency: string | null;
    login: string | null;
    server: string | null;
    leverage: number | null;
  } | null;
  broker_time_utc_offset: number;
  engine_running: boolean;
  feed?: FeedStatus;
}

export interface Position {
  ticket: number;
  symbol: string;
  side: SignalDirection;
  volume: number;
  price_open: number;
  sl: number | null;
  tp: number | null;
  profit: number;
  time: string;
}

export interface StatsResponse {
  days: number;
  total_signals: number;
  closed_signals: number;
  won: number;
  lost: number;
  expired: number;
  win_rate: number | null;
  avg_r: number | null;
  expectancy: number | null;
  profit_factor: number | null;
  max_drawdown_r: number;
  total_r: number | null;
  by_session: Record<string, { signals: number; won: number; lost: number; expired: number }>;
}

export interface SessionRule {
  name: string;
  utc: [number, number];
}

export interface EngineConfig {
  timeframe: string;
  trend_tf: string;
  /** D-041 MTF confirmation timeframes (e.g. ["M5", "M15"]). */
  confirm_tfs?: string[];
  /** D-041 how many confirm TFs must agree with the trend. */
  min_tf_agree?: number;
  ema_fast: number;
  ema_slow: number;
  trend_ema: number;
  rsi_period: number;
  rsi_buy_min: number;
  rsi_buy_max: number;
  rsi_sell_min: number;
  rsi_sell_max: number;
  atr_period: number;
  min_atr: number;
  sfp_lookback: number;
  sfp_wick_atr_ratio: number;
  sl_buffer_atr: number;
  /** D-041 — SL at least this many ATRs from entry (spread/noise floor). */
  min_sl_atr?: number;
  /** D-041 — skip when spread exceeds this fraction of the SL distance. */
  max_spread_to_risk?: number;
  rr: number;
  expiry_bars: number;
  cooldown_bars: number;
  /** D-041 pullback trigger. */
  pullback_enabled?: boolean;
  pullback_min_range_atr?: number;
  pullback_wick_ratio?: number;
  sessions: SessionRule[];
  news_blackout_min: number;
  max_spread_points: number;
  risk_mode: string;
  risk_percent: number;
  fixed_lot: number;
  max_positions: number;
  daily_max_loss_pct: number;
  magic: number;
}

export interface ConfigResponse {
  config: EngineConfig;
  auto_trade: boolean;
}

/* --------------------------------------------------- trading plane (Phase 4) */

export interface TradingStatus {
  connected: boolean;
  mode?: "demo" | "live" | null;
  status?: string;
  detail?: string;
  server?: string | null;
  login_masked?: string | null;
  auto_trade?: boolean;
  account?: {
    balance: number;
    equity: number;
    currency: string;
    login?: string;
    server?: string;
    leverage?: number;
  } | null;
  symbol?: string;
  point_size?: number;
  stored?: boolean;
}

export interface TradingPosition {
  ticket: number;
  symbol: string;
  side: SignalDirection;
  volume: number;
  price_open: number;
  sl: number | null;
  tp: number | null;
  profit: number;
  time: string;
}

export interface TradeRecord {
  signal_id: string | null;
  owner: string | null;
  ticket: number | null;
  side: string;
  volume: number;
  price_open: number;
  sl: number | null;
  tp: number | null;
  price_close: number | null;
  opened_at: string | null;
  closed_at: string | null;
  signal_direction?: string | null;
  signal_status?: string | null;
}

export interface OrderResult {
  ok: boolean;
  ticket: number | null;
  price: number | null;
  retcode: number | null;
  comment: string;
}

export interface ExternalSnapshot {
  ts: string;
  gold_reference: {
    ok: boolean;
    provider: string;
    symbol?: string;
    price?: number;
    change_24h_pct?: number;
    high_24h?: number;
    low_24h?: number;
    error?: string;
  };
  eur_usd: { ok: boolean; provider: string; rate?: number; date?: string; error?: string };
  usd_strength: {
    ok: boolean;
    provider: string;
    name?: string;
    value?: number;
    legs?: Record<string, number>;
    date?: string;
    error?: string;
  };
}

export interface LogEntry {
  ts: string | null;
  level: string;
  source: string;
  message: string;
  meta: unknown;
}

/* ------------------------------------------------------------------ WS events */

export interface WsTickMsg {
  type: "tick";
  symbol: string;
  bid: number;
  ask: number;
  ts: number;
  /** D-033: real market events batched into this frame + trailing rate. */
  n?: number;
  tps?: number;
}

export interface WsBarMsg {
  type: "bar_open" | "bar_update" | "bar_close";
  symbol: string;
  tf: Timeframe;
  candle: Candle;
}

export interface WsSignalMsg {
  type: "signal";
  id: string;
  ts: string;
  symbol: string;
  tf: string;
  direction: SignalDirection;
  entry: number;
  sl: number;
  tp: number;
  confidence: number;
  trace: SignalTrace;
}

export interface WsSignalUpdateMsg {
  type: "signal_update";
  id: string;
  status: SignalStatus;
  result_r: number | null;
  closed_at: string | null;
}

export interface WsAccountMsg {
  type: "account";
  balance: number;
  equity: number;
  currency: string;
  positions: { ticket: number; symbol: string; side: string; volume: number; profit: number }[];
}

export interface WsMt5StatusMsg {
  type: "mt5_status";
  status: Mt5Status["status"];
  symbol: string | null;
  /** D-035: every chartable symbol (XAUUSD + BTCUSD). */
  symbols?: string[];
  feed?: FeedStatus;
}

export interface WsEngineLogMsg {
  type: "engine_log";
  level: string;
  message: string;
}

export interface WsTradingAccountMsg {
  type: "trading_account";
  mode: string;
  balance: number;
  equity: number;
  currency: string;
  auto_trade: boolean;
  positions: TradingPosition[];
}

export interface WsTradingLogMsg {
  type: "trading_log";
  level: string;
  message: string;
}

/** D-036 — live AI auto-execution events (arm/order/skip/close). */
export interface WsMt5AutoMsg {
  type: "mt5_auto";
  ts: string;
  level: string;
  message: string;
  event: "armed" | "disarmed" | "order" | "skip" | "close";
  ok?: boolean;
  signal_id?: string;
  symbol?: string;
  side?: string;
  volume?: number | null;
  price?: number | null;
  ticket?: number | null;
  retcode?: number | null;
  detail?: string;
  reason?: string;
}

export interface WsHeartbeatMsg {
  type: "heartbeat" | "subscribed" | "unsubscribed" | "error";
  ts?: number;
  symbol?: string;
  tf?: string;
  detail?: string;
}

export type WsMessage =
  | WsTickMsg
  | WsBarMsg
  | WsSignalMsg
  | WsSignalUpdateMsg
  | WsAccountMsg
  | WsMt5StatusMsg
  | WsEngineLogMsg
  | WsTradingAccountMsg
  | WsTradingLogMsg
  | WsMt5AutoMsg
  | WsHeartbeatMsg;

/* ------------------------------------------------- D-034 real MT5 account */

export interface Mt5Account {
  connected: boolean;
  bridge: string;
  login: number | null;
  name: string | null;
  server: string | null;
  broker: string | null;
  type: string | null;
  currency: string | null;
  leverage: number | null;
  margin_mode: string | null;
  balance: number | null;
  equity: number | null;
  margin: number | null;
  margin_free: number | null;
  profit: number | null;
  mcp_trade_allowed: boolean | null;
  build: number | null;
}

export interface Mt5OpenPosition {
  position_id: number;
  action: string;
  reason: string;
  symbol: string;
  create_time: string;
  update_time: string;
  volume: number;
  price_open: number;
  price_last?: number;
  profit?: number;
  comment?: string;
}

export interface Mt5HistoryPosition {
  position_id: number;
  type: string;
  symbol: string;
  open_reason: string;
  open_time: string;
  open_volume: number;
  open_price: number;
  close_reason: string;
  close_time: string;
  close_volume: number;
  close_price: number;
  profit: number;
  comment?: string;
}

export interface Mt5Symbol {
  symbol: string;
  description: string;
  bid?: number;
  ask?: number;
  digits?: number;
  trade_mode?: number;
}

export interface Mt5OrderResult {
  ok: boolean;
  retcode: number;
  detail: string | null;
  deal: number | null;
  order: number | null;
  price: number | null;
  volume: number | null;
  symbol: string | null;
}

/* --------------------------------------------- D-036 AI signal -> MT5 order */

export interface Mt5AutoTradeStatus {
  armed: boolean;
  armed_at: string | null;
  armed_by: string | null;
  terminal: {
    available: boolean;
    trade_allowed: boolean;
    server: string | null;
    login: string | null;
    balance: number | null;
    equity: number | null;
    currency: string | null;
  };
  /** D-039: per-symbol broker market state (weekend/holiday logic). */
  markets?: Record<string, { open: boolean; detail: string }>;
  /** D-039: honest one-line diagnosis — why the AI is (not) trading. */
  why?: { code: string; text: string } | null;
  risk: {
    risk_mode: string;
    risk_percent: number;
    fixed_lot: number;
    max_positions: number;
    daily_max_loss_pct: number;
    max_spread_points: number;
    timeframe: string;
  };
  last_skip_reason: string | null;
}

/** D-039: app-level navigation tabs (mobile bottom bar / desktop ⋮ menu). */
export type AppTab = "home" | "charts" | "ai" | "settings";
