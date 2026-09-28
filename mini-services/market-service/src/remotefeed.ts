/**
 * remotefeed.ts — the AURUM Terminal remote MetaTrader 5 bridge.
 *
 * User directive (Bengali, verbatim): "অ্যাপ এর মধ্যে ডেটা আসছে না, এই সোর্স
 * থেকে ডেটা আনতে হবে। https://u1m7j8csutd1-d.space-z.ai এবং লাইভ রিয়েল
 * টাইম ডেটা আপডেট হতে হবে।" — the app must stream from THIS source.
 *
 * That host runs the AURUM Terminal: the user's REAL MetaTrader 5 terminal
 * (Exness) under Wine with a live MCP bridge, exposed as clean JSON:
 *
 *   GET /api/symbols?XTransformPort=3031
 *     → { source:"mt5", list:[{ name, digits, bid, ask, mid, spread,
 *                               live, ts, change, changePct }] }
 *     (ALL symbols, real broker bid/ask, ts = quote epoch seconds, ~1s fresh)
 *   GET /api/candles?symbol=XAUUSDm&tf=M1|M5|M15|M30|H1|H4|D1&limit=N
 *     → { symbol, tf, digits, source:"mt5", bars:[{ t,o,h,l,c,v, f? }] }
 *     (f:1 marks the venue's own forming bar; v = real tick volume)
 *
 * Source ladder in the engine: local MT5 terminal → THIS remote MT5 bridge
 * → live web exchanges (webfeed.ts). If the bridge goes down, the engine
 * fails affected markets over to the web feed within ~15s and probes this
 * bridge for recovery every 60s (with a 2-minute cooldown after each
 * failover so a flapping host can't oscillate the feed).
 *
 * Configure via env:
 *   REMOTE_MT5_URL   (default https://u1m7j8csutd1-d.space-z.ai)
 *   REMOTE_MT5_PORT  (default 3031 — the gateway port hint for /api/*)
 */

import type { Candle, MarketHistory, MarketSpec, RemoteMt5SourceSpec } from "./providers";

/* ------------------------------------------------------------------ config */

const REMOTE_URL = (process.env.REMOTE_MT5_URL ?? "https://u1m7j8csutd1-d.space-z.ai").replace(/\/+$/, "");
const REMOTE_PORT = process.env.REMOTE_MT5_PORT ?? "3031";
const BASE = `${REMOTE_URL}/api`;
const PORT_PARAM = `XTransformPort=${REMOTE_PORT}`;

const SYMBOLS_MS = 1_000;     // one /api/symbols poll feeds ALL markets
const CANDLE_MS = 10_000;     // per-market M1 sync (staggered)
const OUTAGE_FAILS = 15;      // consecutive request failures ⇒ outage (~15s)
const STALE_QUOTE_MS = 120_000; // broker quote older than this ⇒ market closed
const PROBE_TIMEOUT_MS = 4_000;

export function remoteSource(spec: MarketSpec): RemoteMt5SourceSpec {
  return {
    kind: "remote-mt5",
    id: `remote-mt5:AURUM/${spec.mt5}`,
    venue: "AURUM Terminal",
    mt5Symbol: spec.mt5,
  };
}

/* ------------------------------------------------------------- http helpers */

async function httpJson<T>(path: string, timeoutMs = 10_000): Promise<T> {
  const sep = path.includes("?") ? "&" : "?";
  const url = `${BASE}${path}${sep}${PORT_PARAM}`;
  const ctrl = new AbortController();
  const to = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const res = await fetch(url, { signal: ctrl.signal });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return await res.json() as T;
  } finally {
    clearTimeout(to);
  }
}

/* ------------------------------------------------------------- web health */

interface RemoteHealth {
  ok: boolean;
  calls: number;
  fails: number;
  lastOk: number;
  lastErr: string | null;
  consecutiveFails: number;
  liveSymbols: number;
}

const health: RemoteHealth = { ok: false, calls: 0, fails: 0, lastOk: 0, lastErr: null, consecutiveFails: 0, liveSymbols: 0 };

export function remoteFeedHealth(): Record<string, unknown> {
  return {
    url: REMOTE_URL,
    ok: health.ok,
    calls: health.calls,
    fails: health.fails,
    lastOk: health.lastOk ? Math.round((Date.now() - health.lastOk) / 1000) : null, // age_s
    lastErr: health.lastErr,
    consecutiveFails: health.consecutiveFails,
    liveSymbols: health.liveSymbols,
    note: "User-directed source — the AURUM Terminal hosts the REAL MetaTrader 5 (Exness) terminal; every bid/ask/candle comes from the broker.",
  };
}

/* --------------------------------------------------------------- payloads */

interface RemoteSymbol {
  name: string;
  digits?: number;
  bid: number;
  ask: number;
  live?: boolean;
  ts: number;
}

interface RemoteCandlesResponse {
  symbol: string;
  tf: string;
  digits?: number;
  source?: string;
  bars: Array<{ t: number; o: number; h: number; l: number; c: number; v: number; f?: number }>;
}

async function fetchSymbols(): Promise<RemoteSymbol[]> {
  const out = await httpJson<{ source?: string; list?: RemoteSymbol[] }>("/symbols", 6_000);
  const list = out?.list;
  if (!Array.isArray(list)) throw new Error("malformed /api/symbols payload");
  return list;
}

async function fetchCandles(mt5Symbol: string, tf: string, limit: number): Promise<Candle[]> {
  const out = await httpJson<RemoteCandlesResponse>(`/candles?symbol=${encodeURIComponent(mt5Symbol)}&tf=${tf}&limit=${limit}`);
  const bars = out?.bars;
  if (!Array.isArray(bars)) throw new Error(`malformed /api/candles payload for ${mt5Symbol} ${tf}`);
  return bars
    .map((b) => ({ t: b.t, o: b.o, h: b.h, l: b.l, c: b.c, v: b.v }))
    .filter((b) => Number.isFinite(b.t) && Number.isFinite(b.c));
}

/* ------------------------------------------------------------- history load */

/** Raw candle fetch from the AURUM bridge (public helper — also used by
 * backtest.ts so the deep backtest runs on the same REAL broker data). */
export async function remoteCandles(mt5Symbol: string, tf: string, limit: number): Promise<Candle[]> {
  return fetchCandles(mt5Symbol, tf, limit);
}

/**
 * Load REAL broker history for one market from the AURUM bridge — native
 * venue depth for every timeframe the platform analyzes.
 */
export async function loadRemoteMarketHistory(spec: MarketSpec): Promise<MarketHistory> {
  const series: Record<string, Candle[]> = {};
  const steps: Array<[string, number]> = [
    ["M1", 2000],   // ~3 days
    ["M5", 2000],   // ~7 days
    ["M15", 1000],  // ~17 days (backtest seeds need 800)
    ["M30", 1000],  // ~3 weeks
    ["H1", 2426],   // ~5 months (their cap)
    ["H4", 2000],   // ~8 months+
    ["D1", 769],    // ~2.5 years (their cap)
  ];
  for (const [tf, limit] of steps) {
    series[tf] = await fetchCandles(spec.mt5, tf, limit);
    await sleep(250); // be polite to the bridge
  }
  const total = Object.values(series).reduce((a, s) => a + s.length, 0);
  if (total < 50) throw new Error(`remote MT5 history too thin for ${spec.key} (${total} bars)`);
  return { spec, source: remoteSource(spec), series };
}

/** Quick reachability probe — used by the engine's 60s recovery ladder. */
export async function remoteAvailable(): Promise<boolean> {
  try {
    const list = await fetchSymbols();
    return list.length > 0;
  } catch {
    return false;
  }
}

/* -------------------------------------------------------- live feed engine */

export interface RemoteFeedHooks {
  onTick(spec: MarketSpec, bid: number, ask: number, tsSec: number): void;
  onBars(spec: MarketSpec, closed: Candle[], forming: Candle | null): void;
  /** the bridge went dark (network errors) — engine fails over to web */
  onOutage(err: string): void;
}

interface RunningFeed {
  specs: MarketSpec[];
  hooks: RemoteFeedHooks;
}

let running: RunningFeed | null = null;
let symbolsTimer: ReturnType<typeof setInterval> | null = null;
let candleTimer: ReturnType<typeof setInterval> | null = null;
let candleIdx = 0;

/** Start (or restart) the remote bridge feed for exactly these specs. */
export function startRemoteFeed(specs: MarketSpec[], hooks: RemoteFeedHooks): void {
  if (!specs.length) {
    stopRemoteFeed();
    return;
  }
  stopRemoteFeed();
  running = { specs, hooks };
  health.consecutiveFails = 0;

  // ONE poll for ALL markets — real broker bid/ask, every second
  symbolsTimer = setInterval(() => void pollSymbols(), SYMBOLS_MS);

  // staggered per-market M1 candle sync
  candleTimer = setInterval(() => {
    if (!running || !running.specs.length) return;
    const spec = running.specs[candleIdx % running.specs.length];
    candleIdx += 1;
    void syncCandles(spec);
  }, Math.max(2_000, CANDLE_MS / Math.max(1, specs.length)));

  console.log(`[remotefeed] started — AURUM Terminal bridge ${REMOTE_URL} (${specs.map((s) => s.mt5).join(", ")})`);
}

/** Stop the remote bridge feed (all markets switched / shutdown). */
export function stopRemoteFeed(): void {
  if (symbolsTimer) { clearInterval(symbolsTimer); symbolsTimer = null; }
  if (candleTimer) { clearInterval(candleTimer); candleTimer = null; }
  running = null;
}

export function remoteFeedRunning(): boolean {
  return running != null;
}

/* ------------------------------------------------------------- poll loops */

async function pollSymbols(): Promise<void> {
  if (!running) return;
  try {
    const list = await fetchSymbols();
    health.calls += 1;
    health.ok = true;
    health.lastOk = Date.now();
    health.lastErr = null;
    health.consecutiveFails = 0;
    const byName = new Map(list.map((s) => [s.name, s]));
    let liveCount = 0;
    for (const s of list) if (s.live) liveCount += 1;
    health.liveSymbols = liveCount;

    for (const spec of running.specs) {
      const s = byName.get(spec.mt5);
      if (!s) continue;
      const bid = Number(s.bid);
      const ask = Number(s.ask);
      if (!Number.isFinite(bid) || !Number.isFinite(ask) || bid <= 0 || ask <= 0) continue;
      // honesty rule: a stale broker quote (weekend / session break) is NOT
      // a tick — the market is simply closed
      const stale = Date.now() - s.ts * 1000 > STALE_QUOTE_MS;
      if (!stale && s.live !== false) {
        running.hooks.onTick(spec, bid, ask, s.ts);
      }
    }
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    health.calls += 1;
    health.fails += 1;
    health.ok = false;
    health.lastErr = msg.slice(0, 200);
    health.consecutiveFails += 1;
    if (health.consecutiveFails >= OUTAGE_FAILS) {
      console.error(`[remotefeed] AURUM bridge outage (${health.consecutiveFails} consecutive failures: ${msg}) — failing over to the live web feed`);
      const hooks = running.hooks;
      stopRemoteFeed();
      hooks.onOutage(msg.slice(0, 200));
    }
  }
}

/** Real broker M1 bars — closed + the venue's own forming bar. */
async function syncCandles(spec: MarketSpec): Promise<void> {
  if (!running) return;
  try {
    const bars = await fetchCandles(spec.mt5, "M1", 6);
    const nowSec = Math.floor(Date.now() / 1000);
    const closed = bars.filter((b) => (b as { f?: number }).f !== 1 && b.t + 60 <= nowSec);
    const forming = bars.find((b) => (b as { f?: number }).f === 1 || b.t + 60 > nowSec) ?? null;
    running.hooks.onBars(spec, closed, forming);
  } catch {
    /* counted by the symbols poll — a single candle-sync miss is noise */
  }
}

/* ----------------------------------------------------------------- utils */

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}
