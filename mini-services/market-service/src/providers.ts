/**
 * META TRADER 5 — the ONE and ONLY data source of this platform.
 *
 * User directive (Bengali, verbatim): "এই অ্যাপ এর ডেটা শুধু মাত্র meta
 * trader 5 থেকে আসবে। সকল পেয়ার গুলোর ডেটা meta trader 5 থেকে নিতে হবে,
 * এটা বাধ্যতা মূলক, এমন কোনো দিন ট্রায় করাও যাবে না অন্য সোর্স।"
 *
 * → ALL prices, candles, ticks, history and account data flow through the
 *   REAL MetaTrader 5 terminal (build 6231) running under user-space Wine,
 *   logged into the user's Exness account (Exness-MT5Trial6, login
 *   414350770). The terminal exposes its built-in MCP server
 *   (http://127.0.0.1:22346/mcp, bearer-key auth) which this module talks
 *   to. There is NO Binance, NO Yahoo, NO simulation, NO fallback — if the
 *   terminal is unreachable the price simply goes stale and the status
 *   reports disconnected. Data is NEVER invented.
 *
 * Exness broker symbol names (Standard account, "m" suffix):
 *   XAUUSD → XAUUSDm   (Gold vs US Dollar, 3 digits, 100 oz contract)
 *   BTCUSD → BTCUSDm   (Bitcoin vs US Dollar, 24/7)
 *   USOIL  → USOILm    (Crude Oil WTI)
 *   USTEC  → USTECm    (US Tech 100 / Nasdaq-100)
 */

/* ------------------------------------------------------------------ types */

export interface Candle {
  t: number; // epoch seconds of bar open (UTC — terminal runs GMT+0)
  o: number;
  h: number;
  l: number;
  c: number;
  v: number;
}

export const TIMEFRAMES: Record<string, number> = {
  M1: 1, M5: 5, M15: 15, M30: 30, H1: 60, H4: 240, D1: 1440,
};

export interface MarketSpec {
  key: string;          // platform symbol (XAUUSD…)
  label: string;
  /** Exness MT5 symbol */
  mt5: string;
  digits: number;
  spread: number;       // indicative fallback spread (points handled by real bid/ask)
  contractSize: number;
  volumeMin: number;
  volumeStep: number;
  /** crypto pairs trade 24/7 incl. weekends */
  alwaysOpen: boolean;
}

/** The platform's markets — every one maps to an Exness MT5 symbol. */
export const MARKET_SPECS: MarketSpec[] = [
  {
    key: "XAUUSD", label: "XAU / Gold", mt5: "XAUUSDm",
    digits: 3, spread: 0.26, contractSize: 100, volumeMin: 0.01, volumeStep: 0.01,
    alwaysOpen: false,
  },
  {
    key: "BTCUSD", label: "BTC / Bitcoin", mt5: "BTCUSDm",
    digits: 2, spread: 10.0, contractSize: 1, volumeMin: 0.01, volumeStep: 0.01,
    alwaysOpen: true,
  },
  {
    key: "USOIL", label: "WTI / Crude Oil", mt5: "USOILm",
    digits: 3, spread: 0.02, contractSize: 1000, volumeMin: 0.01, volumeStep: 0.01,
    alwaysOpen: false,
  },
  {
    key: "USTEC", label: "US100 / Tech 100", mt5: "USTECm",
    digits: 2, spread: 1.2, contractSize: 1, volumeMin: 0.05, volumeStep: 0.01,
    alwaysOpen: false,
  },
];

export const MARKET_MAP: Record<string, MarketSpec> = Object.fromEntries(
  MARKET_SPECS.map((m) => [m.key, m]),
);

/** Identifies the single, authoritative data source of every tape. */
export interface Mt5SourceSpec {
  kind: "mt5";
  /** display id, e.g. "mt5:Exness/XAUUSDm" */
  id: string;
  /** broker symbol on the terminal */
  mt5Symbol: string;
}

export function mt5Source(spec: MarketSpec): Mt5SourceSpec {
  return { kind: "mt5", id: `mt5:Exness/${spec.mt5}`, mt5Symbol: spec.mt5 };
}

/* ------------------------------------------------------------- MCP client */

const MCP_URL = "http://127.0.0.1:22346/mcp";
const MCP_KEY_FILE = "/home/z/mt5-stack/mcp_key.txt";
const PROTOCOL = "2025-06-18";

import { readFileSync } from "node:fs";

let mcpKey = "";
try { mcpKey = readFileSync(MCP_KEY_FILE, "utf8").trim(); } catch { /* reported via health */ }

let sessionId: string | null = null;
let rpcId = 1;

/** one JSON-RPC POST; keeps the MCP session warm. */
async function rpc(method: string, params?: unknown, timeoutMs = 25_000): Promise<unknown> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    Accept: "application/json, text/event-stream",
    Authorization: `Bearer ${mcpKey}`,
  };
  if (sessionId) headers["Mcp-Session-Id"] = sessionId;
  // JSON-RPC notifications carry NO id and NO params — the MT5 MCP server
  // enforces this strictly (a notification with an id leaves the session
  // in the "not initialized" state and every tools/call is rejected).
  const isNotification = method.startsWith("notifications/");
  const payload: Record<string, unknown> = { jsonrpc: "2.0", method };
  if (!isNotification) payload.id = rpcId++;
  if (params !== undefined) payload.params = params;
  const ctrl = new AbortController();
  const to = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const res = await fetch(MCP_URL, {
      method: "POST",
      headers,
      body: JSON.stringify(payload),
      signal: ctrl.signal,
    });
    const sid = res.headers.get("mcp-session-id");
    if (sid) sessionId = sid;
    if (!res.ok) throw new Error(`MCP HTTP ${res.status}`);
    let body = await res.text();
    if (body.startsWith("event:") || body.startsWith("data:")) {
      for (const line of body.split("\n")) {
        if (line.startsWith("data:")) { body = line.slice(5).trim(); break; }
      }
    }
    const parsed = JSON.parse(body) as { result?: unknown; error?: { message?: string } };
    if (parsed.error) throw new Error(String(parsed.error.message ?? parsed.error));
    return parsed.result;
  } finally {
    clearTimeout(to);
  }
}

/** (re)initialize the MCP session. */
async function ensureSession(): Promise<void> {
  await rpc("initialize", {
    protocolVersion: PROTOCOL,
    capabilities: {},
    clientInfo: { name: "xauusd-market-service", version: "1.0.0" },
  }, 12_000);
  try { await rpc("notifications/initialized", undefined, 8_000); } catch { /* notification */ }
}

/** call an MCP tool; transparently re-initializes a dead session once. */
export async function mcpTool<T = unknown>(name: string, args: Record<string, unknown> = {}): Promise<T> {
  if (!sessionId) await ensureSession();
  try {
    const result = await rpc("tools/call", { name, arguments: args }) as {
      content?: Array<{ type?: string; text?: string }>;
      isError?: boolean;
    };
    const text = result?.content?.[0]?.text ?? "";
    try { return JSON.parse(text) as T; } catch { return text as unknown as T; }
  } catch (err) {
    // session probably expired — one fresh handshake + retry
    sessionId = null;
    await ensureSession();
    const result = await rpc("tools/call", { name, arguments: args }) as {
      content?: Array<{ type?: string; text?: string }>;
    };
    const text = result?.content?.[0]?.text ?? "";
    try { return JSON.parse(text) as T; } catch { return text as unknown as T; }
  }
}

/* ------------------------------------------------------- source health */

export interface SourceHealth {
  id: string;
  ok: boolean;
  calls: number;
  fails: number;
  lastOk: number; // ms epoch
  lastErr: string | null;
  lastLatencyMs: number | null;
}

const health: Record<string, SourceHealth> = {};

export function sourceHealth(): Record<string, SourceHealth> {
  return { ...health };
}

function markOk(id: string, latency: number): void {
  const h = (health[id] ??= { id, ok: true, calls: 0, fails: 0, lastOk: 0, lastErr: null, lastLatencyMs: null });
  h.calls += 1; h.ok = true; h.lastOk = Date.now(); h.lastLatencyMs = latency;
}
function markFail(id: string, err: string): void {
  const h = (health[id] ??= { id, ok: false, calls: 0, fails: 0, lastOk: 0, lastErr: null, lastLatencyMs: null });
  h.calls += 1; h.fails += 1; h.ok = false; h.lastErr = err.slice(0, 200);
}

/* ------------------------------------------------------- account status */

export interface Mt5AccountInfo {
  account: {
    login?: string; server?: string; broker?: string; name?: string;
    type?: string; currency?: string; balance?: number; equity?: number;
    margin?: number; margin_free?: number; profit?: number;
    margin_mode?: string; read_only?: boolean;
  };
  terminal: {
    build?: number; server_connected?: boolean; server_last_ping?: number;
    trade_allowed?: boolean; mcp_trade_allowed?: boolean;
    experts_trade_allowed?: boolean;
  };
}

let accountCache: { at: number; info: Mt5AccountInfo | null } = { at: 0, info: null };

/** Live Exness account + terminal state from the MT5 terminal (60s cache). */
export async function mt5AccountInfo(): Promise<Mt5AccountInfo | null> {
  if (Date.now() - accountCache.at < 60_000 && accountCache.info) return accountCache.info;
  const t0 = Date.now();
  try {
    const info = await mcpTool<Mt5AccountInfo>("get_trading_account_info");
    markOk("mt5:terminal", Date.now() - t0);
    accountCache = { at: Date.now(), info };
    return info;
  } catch (err) {
    markFail("mt5:terminal", err instanceof Error ? err.message : String(err));
    accountCache = { at: Date.now(), info: null };
    return null;
  }
}

/* ------------------------------------------------------------- quotes */

export interface Quote {
  symbol: string;    // MT5 symbol
  bid: number;
  ask: number;
  /** broker quote time (from Market Watch update_time, terminal GMT+0) */
  ts: number;
  /** wall-clock time of the poll that fetched it */
  pollTs: number;
}

interface WatchSymbol {
  symbol: string; bid?: number; ask?: number; last?: number;
  time?: string; update_time?: string;
}

/**
 * One Market Watch snapshot — ALL symbols in a single MCP round-trip.
 * This is the live-tick source for the whole platform.
 */
export async function mt5Quotes(): Promise<Record<string, Quote>> {
  const t0 = Date.now();
  try {
    const out = await mcpTool<{ symbols: WatchSymbol[] }>("get_marketwatch_symbols");
    const q: Record<string, Quote> = {};
    for (const s of out?.symbols ?? []) {
      if (typeof s.bid === "number" && typeof s.ask === "number") {
        // broker quote time — "2026-09-27T15:43:43" (terminal GMT+0)
        let ts = t0;
        const raw = s.update_time ?? s.time;
        if (raw) {
          const parsed = Date.parse(raw.length <= 19 ? raw + "Z" : raw);
          if (Number.isFinite(parsed)) ts = parsed;
        }
        q[s.symbol] = { symbol: s.symbol, bid: s.bid, ask: s.ask, ts, pollTs: t0 };
      }
    }
    markOk("mt5:watch", Date.now() - t0);
    return q;
  } catch (err) {
    markFail("mt5:watch", err instanceof Error ? err.message : String(err));
    return {};
  }
}

/* ------------------------------------------------------------ history */

interface McpBar {
  time: string; open: number; high: number; low: number; close: number;
  tick_volume?: number; volume?: number; spread?: number;
}

/** Parse "2026-09-25T17:00:00" (terminal GMT+0) → epoch seconds. */
function mt5TimeToEpoch(iso: string): number {
  // treat as UTC — terminal clock is GMT+0
  return Math.floor(Date.parse(iso.length <= 19 ? iso + "Z" : iso) / 1000);
}

const TF_TO_MT5: Record<string, string> = {
  M1: "M1", M5: "M5", M15: "M15", M30: "M30", H1: "H1", H4: "H4", D1: "D1",
};

/**
 * Closed MT5 bars for (symbol, TF) in [fromSec, toSec).
 * The terminal is the authority — bars only exist when the broker served them.
 */
export async function mt5History(
  mt5Symbol: string,
  tf: string,
  fromSec: number,
  toSec: number,
  limit = 100_000,
): Promise<Candle[]> {
  const period = TF_TO_MT5[tf];
  if (!period) return [];
  const t0 = Date.now();
  try {
    const out = await mcpTool<{ history: McpBar[] }>("get_chart_history", {
      symbol: mt5Symbol,
      period,
      datetime_from: new Date(fromSec * 1000).toISOString().replace(/\.\d{3}Z$/, ""),
      datetime_to: new Date(toSec * 1000).toISOString().replace(/\.\d{3}Z$/, ""),
      limit,
    });
    markOk(`mt5:${mt5Symbol}:${tf}`, Date.now() - t0);
    const tfSec = TIMEFRAMES[tf] * 60;
    return (out?.history ?? [])
      .map((b) => ({
        t: mt5TimeToEpoch(b.time),
        o: b.open, h: b.high, l: b.low, c: b.close,
        v: b.tick_volume ?? b.volume ?? 0,
      }))
      .filter((b) => Number.isFinite(b.t) && b.t + tfSec <= toSec + 1);
  } catch (err) {
    markFail(`mt5:${mt5Symbol}:${tf}`, err instanceof Error ? err.message : String(err));
    return [];
  }
}

/** Generic aggregation of a closed candle series into `tfMin` buckets. */
export function aggregateCandles(bars: Candle[], tfMin: number): Candle[] {
  const tfSec = tfMin * 60;
  const out: Candle[] = [];
  let cur: Candle | null = null;
  for (const b of bars) {
    const bucket = Math.floor(b.t / tfSec) * tfSec;
    if (!cur || cur.t !== bucket) {
      if (cur) out.push(cur);
      cur = { t: bucket, o: b.o, h: b.h, l: b.l, c: b.c, v: b.v };
    } else {
      cur.h = Math.max(cur.h, b.h);
      cur.l = Math.min(cur.l, b.l);
      cur.c = b.c;
      cur.v += b.v;
    }
  }
  if (cur) out.push(cur);
  return out;
}

/* ------------------------------------------------------ history loading */

export interface MarketHistory {
  spec: MarketSpec;
  source: Mt5SourceSpec;
  series: Record<string, Candle[]>; // closed candles per TF
}

/**
 * Load REAL MT5 history for one market: 8d of M1 (aggregated to M5/M15/M30)
 * plus native H1/H4/D1 depth. Falls back to nothing — MT5 or silence.
 */
export async function loadMarketHistory(spec: MarketSpec): Promise<MarketHistory> {
  const nowSec = Math.floor(Date.now() / 1000);
  const series: Record<string, Candle[]> = {};

  // M1: last 8 days (native broker M1, includes closed bars only)
  const m1 = await mt5History(spec.mt5, "M1", nowSec - 8 * 86_400, nowSec + 60, 100_000);
  series.M1 = m1;
  series.M5 = aggregateCandles(m1, 5);
  series.M15 = aggregateCandles(m1, 15);
  series.M30 = aggregateCandles(m1, 30);

  // native higher TFs from the broker — CLOSED bars only (drop the forming
  // tail bar; the tape rebuilds forming buckets from live M1s instead)
  const closedOnly = (bars: Candle[], tfMin: number): Candle[] => {
    const tfSec = tfMin * 60;
    const out = bars.filter((b) => b.t + tfSec <= nowSec);
    return out.length ? out : bars.slice(0, Math.max(0, bars.length - 1));
  };
  series.H1 = closedOnly(await mt5History(spec.mt5, "H1", nowSec - 90 * 86_400, nowSec + 3_600, 5_000), 60);
  series.H4 = closedOnly(await mt5History(spec.mt5, "H4", nowSec - 240 * 86_400, nowSec + 14_400, 5_000), 240);
  series.D1 = closedOnly(await mt5History(spec.mt5, "D1", nowSec - 3 * 365 * 86_400, nowSec + 86_400, 3_000), 1440);

  const total = Object.values(series).reduce((a, s) => a + s.length, 0);
  if (total < 50) throw new Error(`MT5 history too thin for ${spec.mt5} (${total} bars)`);
  return { spec, source: mt5Source(spec), series };
}

/* ------------------------------------------------------- open positions */

export interface Mt5OpenPosition {
  ticket: number;
  symbol: string;
  side: "BUY" | "SELL";
  volume: number;
  price_open: number;
  sl: number | null;
  tp: number | null;
  profit: number;
  time: string;
}

/** REAL open positions on the user's Exness account (via the terminal). */
export async function mt5OpenPositions(): Promise<Mt5OpenPosition[]> {
  try {
    const out = await mcpTool<{ positions?: Array<Record<string, unknown>> }>(
      "get_trading_open_positions", {});
    return (out?.positions ?? []).map((p) => ({
      ticket: Number(p.ticket ?? p.position ?? 0),
      symbol: String(p.symbol ?? ""),
      side: String(p.type ?? "").toLowerCase() === "sell" ? "SELL" : "BUY",
      volume: Number(p.volume ?? 0),
      price_open: Number(p.price_open ?? p.price_open_current ?? 0),
      sl: p.sl != null ? Number(p.sl) : null,
      tp: p.tp != null ? Number(p.tp) : null,
      profit: Number(p.profit ?? 0),
      time: String(p.time ?? ""),
    }));
  } catch {
    return [];
  }
}
