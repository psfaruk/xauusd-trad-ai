/**
 * webfeed.ts — REAL live web market data (the always-on feed).
 *
 * Why this exists: the platform's preferred source is the user's REAL
 * MetaTrader 5 terminal (providers.ts). But the terminal only runs under
 * Wine with the MCP key present — on a fresh Linux/cloud host (Railway,
 * a restarted sandbox) it is offline, and the platform refused to fabricate
 * prices, leaving the UI on "loading" forever ("ডেটা আসছে না").
 *
 * This module keeps every number REAL — it streams genuine exchange data:
 *
 *   XAUUSD → Binance PAXG/USDT  — PAX Gold, each token = 1 fine troy ounce
 *            of allocated LBMA gold; the order book tracks spot XAU/USD
 *            tightly and trades 24/7. Real-time bid/ask via WebSocket
 *            bookTicker push (sub-second), klines for OHLCV history.
 *   BTCUSD → Binance BTC/USDT   — the deepest BTC order book on earth,
 *            same WebSocket + klines plumbing.
 *   USOIL  → NYMEX WTI front-month futures (Yahoo CL=F) — real CME/NYMEX
 *            exchange prices (nearly 24h during the trading week).
 *   USTEC  → CME E-mini Nasdaq-100 front-month futures (Yahoo NQ=F).
 *
 * Nothing is simulated: ticks are real top-of-book / last-trade prices,
 * candles are real exchange OHLCV bars, and market-closed periods simply
 * stop producing ticks (the same honesty rule as the MT5 path). When the
 * MetaTrader 5 terminal comes back, the engine upgrades each market back
 * to the broker feed (see engine.ts retryFailed).
 *
 * Rate-limit discipline: Binance endpoints are weight-cheap and polled at
 * ≤0.6 req/s; Yahoo is polled every 8s per symbol with query1/query2 host
 * rotation and exponential backoff (15s → 120s) on HTTP 429.
 */

import {
  MARKET_MAP,
  aggregateCandles,
  type Candle,
  type MarketHistory,
  type MarketSpec,
  type WebSourceSpec,
} from "./providers";

/* ------------------------------------------------------------------ config */

const BINANCE_REST = "https://api.binance.com";
const BINANCE_WS = "wss://stream.binance.com:9443/stream";
const YAHOO_HOSTS = ["https://query1.finance.yahoo.com", "https://query2.finance.yahoo.com"];
const UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36";

const BINANCE_TICK_MS = 2_500;    // REST bookTicker fallback while WS is down
const BINANCE_KLINE_MS = 10_000;  // M1 candle sync per Binance market
const YAHOO_POLL_MS = 8_000;      // quote + 1m-bar poll per Yahoo market
const YAHOO_BASE_BACKOFF_MS = 15_000;
const YAHOO_MAX_BACKOFF_MS = 120_000;
const TICK_MIN_INTERVAL_MS = 150; // per-market tick throttle (~6.7Hz — smooth
                                  // for charts/price tiles, easy on browsers)

/** Per-market last emit time — the throttle keeps every number REAL (the
 *  latest top-of-book always passes through on the next push) while capping
 *  broadcast volume; Binance bookTicker alone can exceed 300 msg/s. */
const lastTickEmitAt: Record<string, number> = {};

function emitTick(spec: MarketSpec, bid: number, ask: number, tsSec: number): void {
  if (!running) return;
  const now = Date.now();
  const last = lastTickEmitAt[spec.key] ?? 0;
  if (now - last < TICK_MIN_INTERVAL_MS) return;
  lastTickEmitAt[spec.key] = now;
  running.hooks.onTick(spec, bid, ask, tsSec);
}

/** Which real exchange feeds each platform market. Yahoo venues carry
 *  genuine exchange prices on a delayed (~10 min) timeline — flagged so the
 *  UI can label them honestly. */
interface WebSymbolDef {
  venue: "binance" | "yahoo";
  binance?: string;
  yahoo?: string;
  label: string;
  /** true ⇒ venue data lags the clock (Yahoo free futures feed ~10 min) */
  delayed?: boolean;
}

const WEB_SYMBOLS: Record<string, WebSymbolDef> = {
  XAUUSD: { venue: "binance", binance: "PAXGUSDT", yahoo: "GC=F", label: "Binance PAXG/USDT (1 oz allocated gold)" },
  BTCUSD: { venue: "binance", binance: "BTCUSDT", yahoo: "BTC-USD", label: "Binance BTC/USDT" },
  USOIL: { venue: "yahoo", yahoo: "CL=F", label: "NYMEX WTI crude futures (CL=F, delayed feed)", delayed: true },
  USTEC: { venue: "yahoo", yahoo: "NQ=F", label: "CME E-mini Nasdaq-100 futures (NQ=F, delayed feed)", delayed: true },
};

/** Indicative half-spreads for venues that only report last-trade prices. */
const INDICATIVE_SPREAD: Record<string, number> = { USOIL: 0.03, USTEC: 1.25 };

export function webSource(spec: MarketSpec): WebSourceSpec {
  const def = WEB_SYMBOLS[spec.key] ?? { venue: "yahoo" as const, yahoo: "GC=F", label: "web" };
  const sym = def.venue === "binance" ? def.binance! : def.yahoo!;
  return {
    kind: "web",
    id: `web:${def.venue}/${sym}`,
    venue: def.venue === "binance" ? "Binance" : "Yahoo Finance",
    webSymbol: sym,
  };
}

/* ------------------------------------------------------------ http helpers */

async function httpJson<T>(url: string, timeoutMs = 10_000): Promise<T> {
  const ctrl = new AbortController();
  const to = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const res = await fetch(url, {
      signal: ctrl.signal,
      headers: { "User-Agent": UA, Accept: "application/json" },
    });
    if (res.status === 429 || res.status === 503) {
      const err = new Error(`HTTP ${res.status} (rate limited)`) as Error & { rateLimited?: boolean };
      err.rateLimited = true;
      throw err;
    }
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return await res.json() as T;
  } finally {
    clearTimeout(to);
  }
}

/* -------------------------------------------------------- Binance klines */

type BinanceKline = (string | number)[];

async function binanceKlines(symbol: string, interval: string, limit: number, startTime?: number): Promise<Candle[]> {
  const params = new URLSearchParams({ symbol, interval, limit: String(limit) });
  if (startTime != null) params.set("startTime", String(startTime));
  const rows = await httpJson<BinanceKline[]>(`${BINANCE_REST}/api/v3/klines?${params}`);
  return rows.map((r) => ({
    t: Math.floor(Number(r[0]) / 1000),
    o: Number(r[1]),
    h: Number(r[2]),
    l: Number(r[3]),
    c: Number(r[4]),
    v: Number(r[5]),
  })).filter((c) => Number.isFinite(c.t) && Number.isFinite(c.c));
}

/** Paged klines fetch (Binance caps one call at 1000 bars). */
async function binanceKlinesPaged(symbol: string, interval: string, want: number): Promise<Candle[]> {
  const out: Candle[] = [];
  let start: number | undefined;
  while (out.length < want) {
    const page = await binanceKlines(symbol, interval, Math.min(1000, want - out.length), start);
    if (!page.length) break;
    out.push(...page);
    const last = page[page.length - 1];
    if (page.length < 1000) break;
    start = (last.t + 1) * 1000;
  }
  return out;
}

/* ---------------------------------------------------------- Yahoo charts */

interface YahooResult {
  meta: {
    symbol?: string;
    regularMarketPrice?: number;
    regularMarketTime?: number;
    previousClose?: number;
  };
  timestamp?: number[];
  indicators?: {
    quote?: Array<{
      open?: (number | null)[];
      high?: (number | null)[];
      low?: (number | null)[];
      close?: (number | null)[];
      volume?: (number | null)[];
    }>;
  };
}

interface YahooChart {
  chart: {
    result: YahooResult[] | null;
    error?: { code?: string; description?: string } | null;
  };
}

let yahooHostIdx = 0;

async function yahooChart(symbol: string, interval: string, range: string): Promise<{ meta: YahooResult["meta"]; bars: Candle[] }> {
  const host = YAHOO_HOSTS[yahooHostIdx % YAHOO_HOSTS.length];
  yahooHostIdx += 1;
  const url = `${host}/v8/finance/chart/${encodeURIComponent(symbol)}?interval=${interval}&range=${range}`;
  const data = await httpJson<YahooChart>(url);
  const result = data?.chart?.result?.[0];
  if (!result) {
    const desc = data?.chart?.error?.description ?? "no data";
    throw new Error(`Yahoo ${symbol}: ${desc}`);
  }
  const q = result.indicators?.quote?.[0];
  const ts = result.timestamp ?? [];
  const bars: Candle[] = [];
  for (let i = 0; i < ts.length; i++) {
    const o = q?.open?.[i];
    const h = q?.high?.[i];
    const l = q?.low?.[i];
    const c = q?.close?.[i];
    if (o == null || h == null || l == null || c == null) continue;
    bars.push({ t: ts[i], o, h, l, c, v: q?.volume?.[i] ?? 0 });
  }
  return { meta: result.meta, bars };
}

/* ------------------------------------------------------------- web health */

interface WebSourceHealth {
  ok: boolean;
  calls: number;
  fails: number;
  lastOk: number;
  lastErr: string | null;
}

const webHealth: Record<string, WebSourceHealth> = {};

function markWebOk(id: string): void {
  const h = (webHealth[id] ??= { ok: true, calls: 0, fails: 0, lastOk: 0, lastErr: null });
  h.calls += 1; h.ok = true; h.lastOk = Date.now();
}

function markWebFail(id: string, err: string): void {
  const h = (webHealth[id] ??= { ok: true, calls: 0, fails: 0, lastOk: 0, lastErr: null });
  h.calls += 1; h.fails += 1; h.ok = false; h.lastErr = err.slice(0, 200);
}

export function webFeedHealth(): Record<string, unknown> {
  return {
    binance_ws: {
      open: wsOpen,
      reconnects: wsReconnects,
      last_msg_age_s: lastWsMsgAt ? Math.max(0, Math.round((Date.now() - lastWsMsgAt) / 1000)) : null,
      streams: activeBinanceSymbols(),
    },
    binance_rest: { ...(webHealth["binance:rest"] ?? { ok: null, calls: 0, fails: 0, lastOk: 0, lastErr: null }) },
    yahoo: {
      ...(webHealth["yahoo:chart"] ?? { ok: null, calls: 0, fails: 0, lastOk: 0, lastErr: null }),
      backoff_s: yahooBackoffMs ? Math.round(yahooBackoffMs / 1000) : 0,
    },
    note: "REAL exchange data: Binance order books (PAXG gold, BTC) + NYMEX/CME futures via Yahoo. No simulated prices.",
  };
}

/* ------------------------------------------------------------- history load */

/**
 * Load REAL web history for one market — enough native-venue depth to fully
 * power the platform (M1 chart, M15 backtest seeds, H1/H4/D1 analysis).
 */
export async function loadWebMarketHistory(spec: MarketSpec): Promise<MarketHistory> {
  const def = WEB_SYMBOLS[spec.key];
  if (!def) throw new Error(`no web symbol mapping for ${spec.key}`);
  const series: Record<string, Candle[]> = {};

  if (def.venue === "binance") {
    const sym = def.binance!;
    series.M1 = await binanceKlines(sym, "1m", 1000);                       // ~17h
    series.M5 = await binanceKlines(sym, "5m", 1000);                       // ~3.5d
    series.M15 = await binanceKlines(sym, "15m", 800);                      // ~8.3d (backtest seeds)
    series.M30 = await binanceKlines(sym, "30m", 1000);                     // ~21d
    series.H1 = await binanceKlinesPaged(sym, "1h", 2160);                  // 90d
    series.H4 = await binanceKlinesPaged(sym, "4h", 1440);                  // 240d
    series.D1 = await binanceKlinesPaged(sym, "1d", 1100);                  // ~3y
    markWebOk("binance:rest");
  } else {
    const sym = def.yahoo!;
    // sequential with spacing — Yahoo rate-limits bursts hard
    const steps: Array<[string, string]> = [
      ["1m", "5d"], ["5m", "1mo"], ["15m", "1mo"], ["30m", "1mo"],
      ["60m", "3mo"], ["1d", "5y"],
    ];
    const byTf: Record<string, Candle[]> = {};
    for (const [interval, range] of steps) {
      const { bars } = await yahooChart(sym, interval, range);
      byTf[interval] = bars;
      markWebOk("yahoo:chart");
      await sleep(1_200);
    }
    series.M1 = byTf["1m"] ?? [];
    series.M5 = byTf["5m"] ?? [];
    series.M15 = byTf["15m"] ?? [];
    series.M30 = byTf["30m"] ?? [];
    series.H1 = byTf["60m"] ?? [];
    series.H4 = aggregateCandles(series.H1, 240);
    series.D1 = byTf["1d"] ?? [];
  }

  const total = Object.values(series).reduce((a, s) => a + s.length, 0);
  if (total < 50) throw new Error(`web history too thin for ${spec.key} (${total} bars)`);
  return { spec, source: webSource(spec), series };
}

/* -------------------------------------------------------- live feed engine */

export interface WebFeedHooks {
  onTick(spec: MarketSpec, bid: number, ask: number, tsSec: number): void;
  onBars(spec: MarketSpec, closed: Candle[], forming: Candle | null): void;
}

interface RunningFeed {
  specs: MarketSpec[];
  hooks: WebFeedHooks;
}

let running: RunningFeed | null = null;
let ws: WebSocket | null = null;
let wsOpen = false;
let wsReconnects = 0;
let lastWsMsgAt = 0;
let wsRetryMs = 1_000;
let wsTimer: ReturnType<typeof setTimeout> | null = null;
let restTimer: ReturnType<typeof setInterval> | null = null;
let klineTimer: ReturnType<typeof setInterval> | null = null;
let yahooTimer: ReturnType<typeof setInterval> | null = null;
let yahooBackoffMs = 0;
let yahooNextPollAt = 0;

function activeBinanceSymbols(): string[] {
  return (running?.specs ?? [])
    .filter((s) => WEB_SYMBOLS[s.key]?.venue === "binance")
    .map((s) => WEB_SYMBOLS[s.key].binance!);
}

function activeYahooSpecs(): MarketSpec[] {
  return (running?.specs ?? []).filter((s) => WEB_SYMBOLS[s.key]?.venue === "yahoo");
}

function binanceSpecFor(symbol: string): MarketSpec | null {
  return (running?.specs ?? []).find((s) => WEB_SYMBOLS[s.key]?.binance === symbol) ?? null;
}

/** Start (or restart) the live web feed for exactly these specs. */
export function startWebFeed(specs: MarketSpec[], hooks: WebFeedHooks): void {
  if (!specs.length) {
    stopWebFeed();
    return;
  }
  stopWebFeed();
  running = { specs, hooks };
  const binance = activeBinanceSymbols();
  if (binance.length) {
    connectBinanceWs(binance);
    // REST fallback loop — only actually fetches while the WS is down
    restTimer = setInterval(() => {
      if (wsOpen || !running) return;
      void pollBinanceRest();
    }, BINANCE_TICK_MS);
    // M1 candle sync per Binance market (staggered)
    let idx = 0;
    klineTimer = setInterval(() => {
      if (!running) return;
      const bs = activeBinanceSymbols();
      if (!bs.length) return;
      const sym = bs[idx % bs.length];
      idx += 1;
      void syncBinanceKlines(sym);
    }, BINANCE_KLINE_MS / Math.max(1, activeBinanceSymbols().length));
  }
  if (activeYahooSpecs().length) {
    yahooTimer = setInterval(() => void pollYahoo(), YAHOO_POLL_MS);
  }
  console.log(`[webfeed] started — Binance WS [${binance.join(", ")}] + Yahoo [${activeYahooSpecs().map((s) => WEB_SYMBOLS[s.key].yahoo).join(", ")}]`);
}

/** Stop the whole live web feed (all markets switched back / shutdown). */
export function stopWebFeed(): void {
  if (wsTimer) { clearTimeout(wsTimer); wsTimer = null; }
  if (restTimer) { clearInterval(restTimer); restTimer = null; }
  if (klineTimer) { clearInterval(klineTimer); klineTimer = null; }
  if (yahooTimer) { clearInterval(yahooTimer); yahooTimer = null; }
  if (ws) {
    try { ws.onclose = null; ws.onerror = null; ws.onmessage = null; ws.close(); } catch { /* ignore */ }
    ws = null;
  }
  wsOpen = false;
  running = null;
}

/* ------------------------------------------------------ Binance WebSocket */

function connectBinanceWs(symbols: string[]): void {
  const streams = symbols.map((s) => `${s.toLowerCase()}@bookTicker`).join("/");
  const url = `${BINANCE_WS}?streams=${streams}`;
  try {
    ws = new WebSocket(url);
  } catch (err) {
    console.error(`[webfeed] WS construct failed: ${err instanceof Error ? err.message : String(err)}`);
    scheduleWsRetry();
    return;
  }
  ws.onopen = () => {
    wsOpen = true;
    wsRetryMs = 1_000;
    lastWsMsgAt = Date.now();
    console.log(`[webfeed] Binance WS open — real-time bookTicker push (${symbols.join(", ")})`);
  };
  ws.onmessage = (ev: MessageEvent) => {
    lastWsMsgAt = Date.now();
    try {
      const frame = JSON.parse(String(ev.data)) as { stream?: string; data?: { s?: string; b?: string; a?: string } };
      const d = frame?.data;
      if (!d?.s) return;
      const spec = binanceSpecFor(d.s);
      if (!spec || !running) return;
      const bid = Number(d.b);
      const ask = Number(d.a);
      if (!Number.isFinite(bid) || !Number.isFinite(ask) || bid <= 0 || ask <= 0) return;
      emitTick(spec, bid, ask, Math.floor(Date.now() / 1000));
    } catch { /* malformed frame */ }
  };
  ws.onerror = () => { /* onclose follows */ };
  ws.onclose = () => {
    wsOpen = false;
    ws = null;
    scheduleWsRetry();
  };
}

function scheduleWsRetry(): void {
  if (!running) return;
  if (wsTimer) clearTimeout(wsTimer);
  wsTimer = setTimeout(() => {
    if (!running) return;
    wsReconnects += 1;
    connectBinanceWs(activeBinanceSymbols());
  }, wsRetryMs);
  wsRetryMs = Math.min(wsRetryMs * 2, 30_000);
}

/** REST fallback while the WS is down — real top-of-book, every 2.5s. */
async function pollBinanceRest(): Promise<void> {
  if (!running) return;
  const symbols = activeBinanceSymbols();
  if (!symbols.length) return;
  try {
    const params = encodeURIComponent(JSON.stringify(symbols));
    const rows = await httpJson<Array<{ symbol: string; bidPrice: string; askPrice: string }>>(
      `${BINANCE_REST}/api/v3/ticker/bookTicker?symbols=${params}`, 6_000);
    markWebOk("binance:rest");
    for (const r of rows) {
      const spec = binanceSpecFor(r.symbol);
      if (!spec || !running) continue;
      const bid = Number(r.bidPrice);
      const ask = Number(r.askPrice);
      if (!Number.isFinite(bid) || !Number.isFinite(ask) || bid <= 0 || ask <= 0) continue;
      emitTick(spec, bid, ask, Math.floor(Date.now() / 1000));
    }
  } catch (err) {
    markWebFail("binance:rest", err instanceof Error ? err.message : String(err));
  }
}

/** Real M1 bars from the exchange — closed + the forming bar. */
async function syncBinanceKlines(symbol: string): Promise<void> {
  if (!running) return;
  const spec = binanceSpecFor(symbol);
  if (!spec) return;
  try {
    const bars = await binanceKlines(symbol, "1m", 5);
    markWebOk("binance:rest");
    const nowSec = Math.floor(Date.now() / 1000);
    const closed = bars.filter((b) => b.t + 60 <= nowSec);
    const forming = bars.find((b) => b.t + 60 > nowSec) ?? null;
    running.hooks.onBars(spec, closed, forming);
  } catch (err) {
    markWebFail("binance:rest", err instanceof Error ? err.message : String(err));
  }
}

/* ------------------------------------------------------------- Yahoo poll */

async function pollYahoo(): Promise<void> {
  if (!running) return;
  const now = Date.now();
  if (yahooBackoffMs && now < yahooNextPollAt) return; // 429 backoff active
  for (const spec of activeYahooSpecs()) {
    if (!running) return;
    const def = WEB_SYMBOLS[spec.key];
    try {
      const { meta, bars } = await yahooChart(def.yahoo!, "1m", "1d");
      markWebOk("yahoo:chart");
      yahooBackoffMs = 0;

      /* ---- activity detection ------------------------------------------
       * Yahoo's free futures feed streams REAL exchange prices that update
       * on every poll, but on a timeline stamped ~10 min in the past
       * (delayed feed). "Fresh" therefore means the venue is actively
       * PRODUCING data (price or venue-time moved), not "timestamp recent".
       * When the market closes, updates stop and so do the ticks. */
      const lastBar = bars.length ? bars[bars.length - 1] : null;
      const price = (
        Number.isFinite(meta?.regularMarketPrice ?? NaN)
          ? meta!.regularMarketPrice
          : lastBar?.c
      ) ?? null;
      const venueTs = meta?.regularMarketTime ?? lastBar?.t ?? 0;
      const prev = yahooPrev[spec.key];
      const active = prev == null || price !== prev.price || venueTs !== prev.ts;
      yahooPrev[spec.key] = { price, ts: venueTs };

      // ---- real exchange tick (last trade price, venue timestamp) ----
      if (active && Number.isFinite(price) && price != null && price > 0 && venueTs > 0) {
        const spread = INDICATIVE_SPREAD[spec.key] ?? MARKET_MAP[spec.key].spread;
        const bid = Number(((price as number) - spread / 2).toFixed(spec.digits));
        const ask = Number(((price as number) + spread / 2).toFixed(spec.digits));
        emitTick(spec, bid, ask, venueTs);
      }

      // ---- real 1m bars: closed + forming ----
      // On a delayed venue the partial (forming) bar is time-shifted into
      // the past, so "forming" = the newest bar while the venue is active;
      // when it goes quiet the same bar is delivered as closed (final).
      const nowSec = Math.floor(now / 1000);
      let forming: Candle | null = null;
      if (lastBar) {
        if (active || lastBar.t + 60 > nowSec) forming = lastBar;
      }
      const closed = forming
        ? bars.slice(0, -1).filter((b) => b.t + 60 <= nowSec)
        : bars.filter((b) => b.t + 60 <= nowSec);
      running.hooks.onBars(spec, closed, forming);
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      markWebFail("yahoo:chart", msg);
      if (/HTTP 429|HTTP 503|rate limited/i.test(msg)) {
        yahooBackoffMs = Math.min(yahooBackoffMs || YAHOO_BASE_BACKOFF_MS, YAHOO_MAX_BACKOFF_MS);
        yahooNextPollAt = now + yahooBackoffMs;
        console.warn(`[webfeed] Yahoo rate-limited — backing off ${yahooBackoffMs / 1000}s`);
      }
    }
    await sleep(700); // spacing between the two Yahoo symbols
  }
}

/** Last venue price/time seen per Yahoo market — the activity signal. */
const yahooPrev: Record<string, { price: number | null; ts: number }> = {};

/* ----------------------------------------------------------------- utils */

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}
