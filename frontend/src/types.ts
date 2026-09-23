/**
 * Shared frontend types (SPEC §7.1 REST payloads + §7.2 WS events).
 */

export type Timeframe = "M1" | "M5" | "M15" | "M30" | "H1" | "H4" | "D1";

export const TIMEFRAMES: Timeframe[] = ["M1", "M5", "M15", "M30", "H1", "H4", "D1"];

export type SignalDirection = "BUY" | "SELL";

export type SignalStatus =
  | "pending"
  | "active"
  | "won"
  | "lost"
  | "expired"
  | "cancelled";

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

/** D-042 — one ICT/SMC confluence factor the engine verified. */
export interface ConfluenceFactor {
  name: string;
  ok: boolean;
  detail: string;
}

export interface SignalTrace {
  direction: SignalDirection | null;
  checks: TraceCheck[];
  params: Record<string, unknown>;
  /** D-041 — which pattern fired: "sfp" (liquidity sweep) | "pullback". */
  trigger?: "sfp" | "pullback" | string;
  /** D-042 — ICT/SMC confluence factors (structure, OB, FVG, liquidity…). */
  confluence_factors?: ConfluenceFactor[];
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
  rr?: number | null;
  target_note?: string | null;
  confidence: number;
  trace: SignalTrace | null;
  status: SignalStatus;
  result_r: number | null;
  closed_at: string | null;
  /** D-050 — POI pending (limit) order fields: the entry is a PENDING
   * limit anchored at a POI zone level beyond the market (BUY below the
   * demand zone / SELL above the supply zone) until the market retraces
   * to it and fills. */
  entry_type?: "market" | "limit";
  market_ref?: number | null;
  entry_note?: string | null;
  filled_at?: string | null;
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

/** D-037/D-044: the USER's own broker link (per-user, encrypted). */
export interface BrokerConnection {
  status: "connected" | "linked" | "disconnected" | "reconnecting";
  login?: string;
  login_masked?: string;
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
  /** D-042 — ICT/SMC block. */
  smc_enabled?: boolean;
  bias_tfs?: string[];
  min_confluence?: number;
  max_zone_atr?: number;
  vol_z_min?: number;
  max_sl_atr?: number;
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
  /* ---------------------------------------------------------- D-051 block */
  /** markets the SIGNAL engines run on (one engine per market). */
  signal_symbols?: string[];
  /** markets where AUTO-TRADE may EXECUTE (signals always generate). */
  auto_trade_symbols?: string[];
  /** pending-entry geometry: preferred / hard-cap USD distance. */
  pending_target_usd?: number;
  pending_max_usd?: number;
  entry_min_usd?: number;
  /** trusted-vote gate + real-time whale weighting. */
  trusted_min_votes?: number;
  whale_pulse_bars?: number;
}

export interface ConfigResponse {
  config: EngineConfig;
  auto_trade: boolean;
}

/* --------------------------------------------------- trading plane (Phase 4) */

export interface TradingStatus {
  connected: boolean;
  mode?: "demo" | "live" | "practice" | null;
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
  profit: number | null;
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

/** D-036/D-044 — live AI auto-execution events (arm/order/skip/close/log). */
export interface WsMt5AutoMsg {
  type: "mt5_auto";
  ts: string;
  level: string;
  message: string;
  event: "armed" | "disarmed" | "order" | "skip" | "close" | "log";
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

/** D-051 — the just-closed M1 candle's buyer/seller story. */
export interface PulseCandle {
  o: number;
  h: number;
  l: number;
  c: number;
  v: number;
  dir: "bull" | "bear" | "flat";
  change: number;
  range: number;
  delta: number;
  buy_pct: number;
  sell_pct: number;
  body_ratio: number;
  wick: "upper" | "lower" | "none";
  vol_ratio: number;
  reaction: string;
}

/** D-051 — one strategy-check state inside the radar frame. */
export interface PulseCheck {
  name: string;
  ok: boolean;
  value: string | number | null;
}

/** D-051 — nearest POI zone on the radar (USD distance from price). */
export interface PulseZone {
  side: "demand" | "supply";
  lo: number;
  hi: number;
  quality: number;
  source: string;
  dist_usd: number;
}

/**
 * D-051 — strategy_pulse: every M1 close the engine streams its live
 * per-strategy state (user directive: "অ্যাপ এর প্রত্যেকটি স্টাডিজির
 * ডাটা রিয়েল টাইমে সেকেন্ডের মধ্যে দেখাতে হবে") — which strategy is
 * doing WHAT, why no signal fired THIS bar, and the candle's
 * buyer/seller dominance.
 */
export interface WsStrategyPulseMsg {
  type: "strategy_pulse";
  symbol: string;
  tf: string;
  ts: string;
  price: number | null;
  bias: "BULL" | "BEAR" | "NEUTRAL" | null;
  rsi: number | null;
  atr: number | null;
  spread_points: number | null;
  session: string | null;
  triggers: { sfp: boolean; zone: boolean; pullback: boolean };
  whale: {
    bias: string | null;
    buy_events: number | null;
    sell_events: number | null;
    last: string | null;
  } | null;
  candle: PulseCandle | null;
  zones: PulseZone[];
  fired: {
    direction: "BUY" | "SELL";
    entry: number;
    sl: number;
    tp: number;
    entry_type: string;
    market_ref: number;
    confidence: number;
    trigger: string;
    whale: boolean;
  } | null;
  trigger?: string;
  near_miss: string | null;
  checks: PulseCheck[];
  factors?: { name: string; ok: boolean; detail: string }[];
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
  | WsStrategyPulseMsg
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

/* --------------------------------------------- D-036/D-044 AI auto-trade */

export interface Mt5AutoTradeStatus {
  armed: boolean;
  armed_at: string | null;
  armed_by: string | null;
  /** D-044: "account" (the user's own practice plane) or "institution". */
  scope?: "account" | "institution";
  /** D-044 — the USER's own account (regular users; replaces `terminal`). */
  account?: {
    mode: string;
    balance: number | null;
    equity: number | null;
    currency: string;
  } | null;
  /** Institution-terminal block — ADMIN payloads only. */
  terminal?: {
    available: boolean;
    trade_allowed: boolean;
    server: string | null;
    login: string | null;
    balance: number | null;
    equity: number | null;
    currency: string | null;
  } | null;
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
    rr?: number;
    min_sl_atr?: number;
    timeframe?: string;
  };
  last_skip_reason: string | null;
}

/* -------------------------------------------- D-044 per-user money settings */

export interface UserSettings {
  risk_mode: "percent" | "fixed";
  risk_percent: number;
  fixed_lot: number;
  max_positions: number;
  daily_max_loss_pct: number;
  max_trades_per_day: number;
  rr: number;
  min_sl_atr: number;
  max_spread_points: number;
  /** D-052 — daily stop loss in USD (0 = off) */
  daily_loss_usd?: number;
  /** D-052 — daily target profit in USD (0 = off) */
  daily_profit_usd?: number;
  /** D-052 — today's trading balance (the USD-window anchor) */
  day_start_balance?: number;
  balance?: number;
  currency?: string;
}

/* --------------------------------------- D-044 market intelligence blocks */

export interface FlowStats {
  usd_24h?: number;
  usd_1h?: number;
  volume_24h?: number;
  delta_1h?: number;
  buy_pct_1h?: number;
  velocity?: number | null;
  bias?: string;
  whale_zones?: {
    lo: number;
    hi: number;
    price: number;
    side: string;
    kind: string;
    vol_z: number;
    events: number;
    t: string;
    note: string;
  }[];
}

export interface NewsBlock {
  events: {
    time: string;
    impact: "high" | "medium";
    title: string;
    forecast?: string | null;
    previous?: string | null;
  }[];
  blackout_now?: boolean;
  available?: boolean;
}

/** D-047 — time-at-price profile: levels where the market SPENT TIME */
export interface TpoBlock {
  poc?: number | null;
  va_lo?: number | null;
  va_hi?: number | null;
  va_minutes?: number;
  total_minutes?: number;
  levels?: {
    price: number;
    minutes: number;
    side: "support" | "resistance";
    strength: number;
  }[];
}

export interface CotBlock {
  report_date?: string;
  open_interest?: number;
  large_speculators?: { long: number; short: number; net: number; net_change: number };
  commercial_hedgers?: { long: number; short: number; net: number };
  small_traders?: { long: number; short: number; net: number };
  net_percentile_52w?: number;
  bias?: string;
  note?: string;
}

/** D-039: app-level navigation tabs (mobile bottom bar / desktop ⋮ menu). */
export type AppTab = "home" | "charts" | "ai" | "settings";

/* ------------------------------------------------ D-042: ICT/SMC analysis */

/** One institutional zone on the chart (supply/demand, OB, FVG). */
export interface SmcZone {
  side: "bullish" | "bearish" | "demand" | "supply";
  t: string;
  hi: number;
  lo: number;
  /** extra context (OB impulse multiple, FVG gap, mitigated state…) */
  impulse?: number;
  gap?: number;
  filled?: boolean;
  mitigated?: boolean;
}

/** D-048 — one unified POI zone (the zone-retest trigger's source). */
export interface PoiZone {
  side: "demand" | "supply";
  /** sd = supply/demand base, ob = order block, fvg = fair value gap,
   *  tpo = time-at-price level, pd = prior-day high/low */
  source: "sd" | "ob" | "fvg" | "tpo" | "pd";
  t: string;
  hi: number;
  lo: number;
  /** 0..1 — hard-coded POI quality (impulse, freshness, age, TPO, HTF) */
  quality: number;
  /** true when the zone originates on M15 structure (institutional TF) */
  htf?: boolean;
}

export interface LiquidityLevel {
  kind: "BSL" | "SSL";
  price: number;
  t: string;
  hits: number;
  tag?: string;
}

export interface WhaleEvent {
  t: string;
  side: "buy" | "sell";
  kind: "momentum" | "sweep" | "absorption";
  vol_z: number;
  price: number;
  note: string;
}

export interface StructureInfo {
  trend: "bullish" | "bearish" | "balanced";
  swings: { t: string; price: number; kind: "high" | "low"; label: string }[];
  events: { t: string; level: number; kind: "BOS" | "CHoCH"; dir: "up" | "down" }[];
  last_event: { t: string; level: number; kind: "BOS" | "CHoCH"; dir: "up" | "down" } | null;
}

/** Per-timeframe analysis snapshot from GET /api/analysis. */
export interface AnalysisSnapshot {
  ok: boolean;
  bars: number;
  last: number;
  atr: number;
  structure: StructureInfo;
  order_blocks: SmcZone[];
  fvgs: SmcZone[];
  liquidity: { levels: LiquidityLevel[]; sweeps: { kind: string; price: number; t: string }[] };
  zones: SmcZone[];
  premium_discount: {
    state: string;
    range_hi: number | null;
    range_lo: number | null;
    eq: number | null;
    ote: { hi: number; lo: number } | null;
  };
  whales: {
    events: WhaleEvent[];
    buy_events: number;
    sell_events: number;
    bias: "buy" | "sell" | "neutral";
    last: WhaleEvent | null;
  };
  indicators: {
    rsi: number;
    macd: { macd: number; signal: number; hist: number; hist_prev: number };
    stoch: { k: number; d: number };
    adx: { adx: number; plus_di: number; minus_di: number };
    bollinger: { upper: number; mid: number; lower: number; width: number; pct_b: number };
    vwap: number;
    vwap_rel: string;
    cci: number;
    momentum: number;
    vol_z: number;
  };
  volume_profile: { poc: number | null; vah: number | null; val: number | null };
  delta: number;
}

export interface AnalysisResponse {
  symbol: string;
  updated_at: number;
  per_tf: Record<string, AnalysisSnapshot>;
  mtf: { bias: string; score: number; notes: string[] };
  errors: string[];
  /** D-043 — professional auto-drawings (M1 view; legacy field) */
  drawings?: ChartDrawing[];
  /** D-052 — per-timeframe drawing sets: switching TF re-draws its own
   *  marks (recent 80–150 candles of that TF) — never a blank overlay */
  drawings_by_tf?: Record<string, ChartDrawing[]>;
  /** D-044 — order-flow statistics (USD value, delta, whale zones) */
  flow?: FlowStats;
  /** D-047 — time-at-price profile: where the market SPENT TIME (S/R) */
  tpo?: TpoBlock;
  /** D-048 — unified POI zones (zone-retest trigger source) */
  poi?: { zones: PoiZone[] };
  /** D-044 — upcoming high-impact USD economic events */
  news?: NewsBlock;
  /** D-044 — weekly CFTC institutional positioning */
  cot?: CotBlock;
}

/* ------------------------------------------------- D-043: chart drawings */

export type DrawingTone = "bull" | "bear" | "gold" | "violet" | "neutral";

/** Horizontal level a trader would mark (support/resistance/liquidity…). */
export interface HLineDrawing {
  kind: "hline";
  price: number;
  label: string;
  tone: DrawingTone;
  style: "solid" | "dash";
}

/** D-052 — labeled zone box (supply/demand/order block/FVG), drawn from
 *  its origin time to the right edge exactly like the reference charts. */
export interface ZoneDrawing {
  kind: "zone";
  side: "supply" | "demand" | "ob_bull" | "ob_bear" | "fvg_bull" | "fvg_bear";
  lo: number;
  hi: number;
  t: string | null;
  label: string;
  tone: DrawingTone;
  source_tf: string;
}

/** D-052 — channel: upper + lower parallel lines + dashed median. */
export interface ChannelDrawing {
  kind: "channel";
  dir: "up" | "down";
  label: string;
  tone: DrawingTone;
  upper: { t1: string; p1: number; t2: string; p2: number };
  lower: { t1: string; p1: number; t2: string; p2: number };
  median: { t1: string; p1: number; t2: string; p2: number };
}

/** D-052 — liquidity sweep marker ("stop hunt" line at the swept pool). */
export interface SweepDrawing {
  kind: "sweep";
  t: string | null;
  price: number;
  side: "high" | "low";
  label: string;
  tone: DrawingTone;
}

/** D-052 — BOS / CHoCH event chip anchored on the break candle. */
export interface StructureDrawing {
  kind: "structure";
  t: string | null;
  price: number;
  dir: "up" | "down";
  label: string;
  tone: DrawingTone;
}

/** D-052 — direction projection arrow at a sweep / structure event. */
export interface ArrowDrawing {
  kind: "arrow";
  t: string | null;
  price: number;
  dir: "up" | "down";
  label: string;
  tone: DrawingTone;
}

/** Trendline through the last two swing points, projected forward. */
export interface TrendlineDrawing {
  kind: "trendline";
  t1: string;
  p1: number;
  t2: string;
  p2: number;
  label: string;
  tone: DrawingTone;
  broken: boolean;
}

/** Fibonacci retracement of the active leg + OTE (0.62–0.79) band. */
export interface FibDrawing {
  kind: "fib";
  t0: string;
  p0: number;
  t1: string;
  p1: number;
  dir: "up" | "down";
  levels: { ratio: number; price: number }[];
  ote: [number, number] | null;
  tone: DrawingTone;
}

/** Small text annotation anchored at (time, price). */
export interface NoteDrawing {
  kind: "note";
  t: string;
  price: number;
  text: string;
  tone: DrawingTone;
}

/** The entry-setup box — drawn while the setup FORMS, before entry. */
export interface SetupDrawing {
  kind: "setup";
  dir: "BUY" | "SELL";
  zone: [number, number];
  entry: number;
  sl: number;
  tp: number;
  rr: number;
  t0: string;
  status: "forming" | "triggered";
  factors: string[];
  note: string;
}

export type ChartDrawing =
  | HLineDrawing
  | ZoneDrawing
  | ChannelDrawing
  | SweepDrawing
  | StructureDrawing
  | ArrowDrawing
  | TrendlineDrawing
  | FibDrawing
  | NoteDrawing
  | SetupDrawing;
