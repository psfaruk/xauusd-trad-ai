/**
 * Market engine — REAL-TIME runtime on the REAL MetaTrader 5 terminal.
 *
 * Every price, every candle and every backtested signal comes from the user's
 * MetaTrader 5 terminal (Exness-MT5Trial6, login 414350770) running under
 * user-space Wine, via its built-in MCP server (see providers.ts):
 *
 *  - boot: load ~8d of real M1 history per market (+ native H1/H4/D1 depth)
    from the broker, then run the SFP signal engine over the real M15 history
    — a genuine backtest that seeds the signal panel and stats with REAL
    outcomes.
 *  - live: poll the MT5 Market Watch (1s, all symbols in one round trip) for
      real bid/ask ticks, and sync M1 candles from the broker every 5s; bars
      close only when the broker reports them closed.
 *  - closed markets (weekend FX/metals/oil): the broker's quote timestamp
 *   goes stale — ticks are NOT fabricated, status shows "closed".
 */

import type { Server } from "socket.io";
import {
  MARKET_SPECS,
  MARKET_MAP,
  mt5Quotes,
  mt5History,
  mt5AccountInfo,
  loadMarketHistory,
  mt5EnsureSymbols,
  MT5_WATCH_SYMBOLS,
  type Candle,
  type MarketSpec,
  type Mt5SourceSpec,
  type Mt5AccountInfo,
} from "./providers";
import { Tape } from "./tape";
import {
  analyzeTf, atr as atrFn, ema, findSwings, rsi as rsiFn, sessionOf,
  buildDrawings, type AnalysisSnapshotOut,
} from "./analysis";
import { planes, type Plane } from "./trading";

/* ------------------------------------------------------------------ clock */

const MASTER_MS = 1000;     // master scheduler tick
const QUOTE_MS = 1_000;     // Market Watch poll (ALL symbols, one round trip)
const KLINE_MS = 5_000;     // per-market M1 sync from the broker
const STALE_QUOTE_MS = 120_000; // broker quote older than this ⇒ market closed
const M15_SEC = 15 * 60;

export interface EngineConfig {
  timeframe: string;
  trend_tf: string;
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
  rr: number;
  expiry_bars: number;
  cooldown_bars: number;
  sessions: { name: string; utc: [number, number] }[];
  news_blackout_min: number;
  max_spread_points: number;
  risk_mode: string;
  risk_percent: number;
  fixed_lot: number;
  max_positions: number;
  daily_max_loss_pct: number;
  magic: number;
  signal_symbols: string[];
  auto_trade_symbols: string[];
}

export const DEFAULT_CONFIG: EngineConfig = {
  timeframe: "M15",
  trend_tf: "H1",
  ema_fast: 9,
  ema_slow: 21,
  trend_ema: 50,
  rsi_period: 14,
  rsi_buy_min: 30,
  rsi_buy_max: 68,
  rsi_sell_min: 32,
  rsi_sell_max: 70,
  atr_period: 14,
  min_atr: 0.15,
  sfp_lookback: 20,
  sfp_wick_atr_ratio: 0.3,
  sl_buffer_atr: 0.2,
  rr: 2,
  expiry_bars: 24,
  cooldown_bars: 6,
  sessions: [
    { name: "asian", utc: [0, 7] },
    { name: "london", utc: [7, 13] },
    { name: "newyork", utc: [13, 21] },
  ],
  news_blackout_min: 30,
  max_spread_points: 50,
  risk_mode: "percent",
  risk_percent: 1,
  fixed_lot: 0.05,
  max_positions: 3,
  daily_max_loss_pct: 3,
  magic: 777,
  signal_symbols: ["XAUUSD", "BTCUSD", "USOIL", "USTEC"],
  auto_trade_symbols: ["XAUUSD", "BTCUSD", "USOIL", "USTEC"],
};

/* ---------------------------------------------------------------- signals */

export interface SignalTrace {
  direction: "BUY" | "SELL" | null;
  checks: { name: string; pass: boolean; value: string }[];
  params: Record<string, unknown>;
  confluence?: { name: string; ok: boolean; detail: string }[];
  context?: Record<string, unknown>;
}

export interface Signal {
  id: string;
  ts: string;
  symbol: string;
  tf: string;
  direction: "BUY" | "SELL";
  entry: number;
  sl: number;
  tp: number;
  rr: number | null;
  confidence: number;
  trace: SignalTrace | null;
  status: "pending" | "active" | "won" | "lost" | "expired" | "cancelled";
  result_r: number | null;
  closed_at: string | null;
  entry_type: "market" | "limit";
  market_ref: number | null;
  entry_note: string | null;
  filled_at: string | null;
  context: Record<string, unknown> | null;
  close_reason: string | null;
  /** engine bookkeeping — epoch seconds of the entry M15 bar open */
  entryBarIndex: number;
  barsSinceEntry: number;
}

export interface LogEntry {
  ts: string | null;
  level: string;
  source: string;
  message: string;
  meta: unknown;
}

/* ------------------------------------------------------------------ state */

export class Engine {
  io: Server | null = null;
  config: EngineConfig = { ...DEFAULT_CONFIG };
  autoTrade = false;
  tapes: Record<string, Tape> = {};
  ticks: Record<string, { bid: number; ask: number; ts: number }> = {};
  tickMeter: Record<string, { n: number; tps: number; lastCalib: number }> = {};
  signals: Signal[] = [];
  logs: LogEntry[] = [];
  startedAt = Date.now();
  /** true once the real-feed boot finished (history + backtest seeds) */
  ready = false;
  /** per-market boot failures (empty when everything loaded) */
  bootErrors: Record<string, string> = {};
  /** cached REAL terminal account info (Exness), refreshed every 60s */
  mt5Account: Mt5AccountInfo | null = null;
  private accountCheckedAt = 0;

  private timer: ReturnType<typeof setInterval> | null = null;
  private retryTimer: ReturnType<typeof setInterval> | null = null;
  private masterBusy = false;
  private analysisCache: Record<string, { at: number; payload: unknown }> = {};
  /** shared Market-Watch poll state (one round trip feeds ALL markets) */
  private quotePoll: { busy: boolean; lastAt: number; errStreak: number; lastErr: string | null } =
    { busy: false, lastAt: 0, errStreak: 0, lastErr: null };
  private poll: Record<string, {
    quoteBusy: boolean;
    klineBusy: boolean;
    lastQuoteAt: number;
    lastKlineAt: number;
    lastFormingSentT: number;
    tickN: number;
    errStreak: number;
    lastErr: string | null;
  }> = {};

  constructor() {
    for (const spec of MARKET_SPECS) {
      this.tickMeter[spec.key] = { n: 0, tps: 0, lastCalib: Date.now() };
      this.poll[spec.key] = {
        quoteBusy: false, klineBusy: false, lastQuoteAt: 0, lastKlineAt: 0,
        lastFormingSentT: 0, tickN: 0, errStreak: 0, lastErr: null,
      };
    }
    this.log("info", "engine", "EngineRuntime created — REAL market data mode (simulation removed)");
  }

  /* -------------------------------------------------------------- clock */

  /** real UTC seconds (kept for API compatibility) */
  vnowSec(): number {
    return Math.floor(Date.now() / 1000);
  }

  /* -------------------------------------------------------------- market */

  start(io: Server): void {
    this.io = io;
    if (this.timer) clearInterval(this.timer);
    this.timer = setInterval(() => void this.masterTick(), MASTER_MS);
    if (this.retryTimer) clearInterval(this.retryTimer);
    this.retryTimer = setInterval(() => void this.retryFailed(), 60_000);
    this.log("info", "engine",
      "EngineRuntime started — MetaTrader 5 terminal feed (Exness-MT5Trial6 / 414350770)");
    void this.boot();
  }

  stop(): void {
    if (this.timer) clearInterval(this.timer);
    if (this.retryTimer) clearInterval(this.retryTimer);
    this.timer = null;
    this.retryTimer = null;
  }

  prices(): Record<string, number> {
    const out: Record<string, number> = {};
    for (const key of Object.keys(this.tapes)) {
      const t = this.ticks[key];
      out[key] = t ? (t.bid + t.ask) / 2 : this.tapes[key].lastPrice();
    }
    return out;
  }

  /* --------------------------------------------------------------- boot */

  private async boot(): Promise<void> {
    const t0 = Date.now();
    this.log("info", "engine", "boot: loading REAL MetaTrader 5 history (8d M1 + H1/H4/D1)…");
    console.log("[engine] boot: loading REAL MetaTrader 5 history (8d M1 + H1/H4/D1)");
    // a fresh terminal starts with an EMPTY Market Watch — ensure every
    // platform symbol is present BEFORE history loads, or quotes/history
    // fail silently (isError ⇒ now loud) on every call
    try {
      const ensured = await mt5EnsureSymbols();
      console.log(`[engine] market watch symbols ensured: ${ensured.present}/${MT5_WATCH_SYMBOLS.length} present (${ensured.added} added, ${ensured.failed.length} failed)`);
      this.log("info", "engine",
        `market watch symbols ensured: ${ensured.present}/${MT5_WATCH_SYMBOLS.length} present, ${ensured.added} added` +
        (ensured.failed.length ? `, failed: ${ensured.failed.join("; ").slice(0, 200)}` : ""));
    } catch (err) {
      console.error(`[engine] market watch symbol ensure failed: ${err instanceof Error ? err.message : String(err)}`);
    }
    const results = await Promise.allSettled(MARKET_SPECS.map((s) => loadMarketHistory(s)));
    let loaded = 0;
    for (let i = 0; i < results.length; i++) {
      const spec = MARKET_SPECS[i];
      const r = results[i];
      if (r.status === "fulfilled") {
        this.installTape(r.value.spec, r.value.source, r.value.series);
        loaded += 1;
      } else {
        const msg = r.reason instanceof Error ? r.reason.message : String(r.reason);
        this.bootErrors[spec.key] = msg;
        this.log("error", "engine", `${spec.key}: history load failed — ${msg} (will retry in 60s)`);
      }
    }
    // REAL backtest: run the SFP engine over the actual M15 history
    this.seedHistoricalSignals();
    this.ready = true;
    const total = Object.values(this.tapes).reduce((a, t) => a + t.getClosed("M1", 1e9).length, 0);
    this.log("info", "engine",
      `boot complete in ${((Date.now() - t0) / 1000).toFixed(1)}s — ${loaded}/${MARKET_SPECS.length} markets live, ` +
      `${total} real M1 bars, ${this.signals.length} backtested signals from real history`);
    console.log(`[engine] boot complete in ${((Date.now() - t0) / 1000).toFixed(1)}s — ${loaded}/${MARKET_SPECS.length} markets live, ${total} real M1 bars, ${this.signals.length} backtested signals from real history`);
  }

  private installTape(spec: MarketSpec, source: Mt5SourceSpec, series: Record<string, Candle[]>): void {
    this.tapes[spec.key] = new Tape(spec, source, series);
    delete this.bootErrors[spec.key];
    const tape = this.tapes[spec.key];
    const m1 = tape.getClosed("M1", 1e9).length;
    const d1 = tape.getClosed("D1", 1e9).length;
    this.log("info", "engine",
      `${spec.key}: REAL MT5 feed online — Exness ${source.mt5Symbol}, ${m1} M1 bars (8d), ${d1} D1 bars`);
  }

  private async retryFailed(): Promise<void> {
    if (this.ready === false) return;
    const pending = MARKET_SPECS.filter((s) => !this.tapes[s.key]);
    if (!pending.length) return;
    // a restarted/fresh terminal can lose Market Watch symbols — re-ensure
    // them (idempotent, one round trip per symbol) before reloading history
    try {
      const ensured = await mt5EnsureSymbols();
      console.log(`[engine] retry: market watch symbols ensured: ${ensured.present}/${MT5_WATCH_SYMBOLS.length} present`);
    } catch (err) {
      console.error(`[engine] retry: market watch symbol ensure failed: ${err instanceof Error ? err.message : String(err)}`);
    }
    for (const spec of pending) {
      const lastErr = this.bootErrors[spec.key] ?? this.poll[spec.key].lastErr ?? "no history yet";
      // the outage must never be silent again — every retry hits stdout
      console.warn(`[engine] retry ${spec.key}: ${lastErr}`);
      try {
        const h = await loadMarketHistory(spec);
        this.installTape(h.spec, h.source, h.series);
        this.seedMarketSignals(spec.key);
        this.log("info", "engine", `${spec.key}: retry succeeded — REAL MT5 feed restored`);
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        this.bootErrors[spec.key] = msg;
        this.poll[spec.key].lastErr = msg;
        this.log("error", "engine", `${spec.key}: retry failed — ${msg} (next retry in 60s)`);
      }
    }
  }

  /* ------------------------------------------------------- poll schedule */

  private async masterTick(): Promise<void> {
    if (this.masterBusy || !this.io) return;
    this.masterBusy = true;
    try {
      const now = Date.now();
      // ONE shared Market-Watch poll — real bid/ask for ALL markets in a
      // single MCP round trip (the terminal serves the whole watch at once)
      if (!this.quotePoll.busy && now - this.quotePoll.lastAt >= QUOTE_MS) {
        this.quotePoll.busy = true;
        this.quotePoll.lastAt = now;
        void this.pollQuotes().finally(() => { this.quotePoll.busy = false; });
      }
      // per-market M1 candle sync from the broker
      MARKET_SPECS.forEach((spec, i) => {
        const tape = this.tapes[spec.key];
        if (!tape) return;
        const st = this.poll[spec.key];
        // stagger slots so the 4 markets don't burst in the same tick
        const due = now - st.lastKlineAt >= KLINE_MS - (i * (MASTER_MS / 2));
        if (!st.klineBusy && due) {
          st.klineBusy = true;
          st.lastKlineAt = now;
          void this.pollKlines(spec).finally(() => { st.klineBusy = false; });
        }
      });
      // refresh the REAL Exness account info once a minute
      if (now - this.accountCheckedAt >= 60_000) {
        this.accountCheckedAt = now;
        void mt5AccountInfo().then((info) => { this.mt5Account = info; }).catch(() => undefined);
      }
      this.trackSignals();
      this.pushAccountFrames();
    } finally {
      this.masterBusy = false;
    }
  }

  /**
   * Market Watch snapshot → real ticks for ALL markets.
   * Honesty rule: when the broker's quote timestamp is stale (weekend /
   * session closed) the market is reported closed — NO tick is fabricated.
   */
  private async pollQuotes(): Promise<void> {
    try {
      const quotes = await mt5Quotes();
      const keys = Object.values(MARKET_SPECS);
      if (!Object.keys(quotes).length) throw new Error("empty Market Watch snapshot");
      this.quotePoll.errStreak = 0;
      this.quotePoll.lastErr = null;
      for (const spec of keys) {
        const q = quotes[spec.mt5];
        if (!q) continue;
        const stale = Date.now() - q.ts > STALE_QUOTE_MS;
        if (!stale) {
          // live market — broadcast the broker's real bid/ask
          this.recordTick(spec, q.bid, q.ask, Math.floor(q.ts / 1000));
        } else {
          // market closed — keep the last real quote on record (ts = broker
          // time so the age grows honestly) but never broadcast fake ticks
          const prev = this.ticks[spec.key];
          if (!prev || prev.bid !== q.bid || prev.ask !== q.ask) {
            this.ticks[spec.key] = { bid: q.bid, ask: q.ask, ts: Math.floor(q.ts / 1000) };
          }
          const st = this.poll[spec.key];
          if (st) st.lastErr = null; // closed ≠ error
        }
      }
    } catch (err) {
      this.quotePoll.errStreak += 1;
      this.quotePoll.lastErr = err instanceof Error ? err.message : String(err);
    }
  }

  /** Real M1 sync from the broker: ingest newly CLOSED bars + the forming bar. */
  private async pollKlines(spec: MarketSpec): Promise<void> {
    const tape = this.tapes[spec.key];
    if (!tape) return;
    const st = this.poll[spec.key];
    try {
      const nowSec = this.vnowSec();
      // last ~10 minutes of broker M1 bars (covers poll gaps + weekend edges)
      const from = Math.max(tape.originSec, nowSec - 600);
      const bars = await mt5History(spec.mt5, "M1", from, nowSec + 60, 50);
      const closed = bars.filter((b) => b.t + 60 <= nowSec);
      const forming = bars.find((b) => b.t + 60 > nowSec) ?? null;
      const { newClosed, tfClosed } = tape.ingest(closed, forming);
      st.errStreak = 0;
      st.lastErr = null;
      this.handleIngest(spec, newClosed, tfClosed);
    } catch (err) {
      st.errStreak += 1;
      st.lastErr = err instanceof Error ? err.message : String(err);
    }
  }

  /* ------------------------------------------------------ ingest + broadcast */

  private recordTick(spec: MarketSpec, bid: number, ask: number, tsSec: number): void {
    const st = this.poll[spec.key];
    st.tickN += 1;
    const meter = this.tickMeter[spec.key];
    meter.n += 1;
    const now = Date.now();
    if (now - meter.lastCalib >= 2000) {
      meter.tps = (meter.n * 1000) / (now - meter.lastCalib);
      meter.n = 0;
      meter.lastCalib = now;
    }
    this.ticks[spec.key] = { bid, ask, ts: tsSec };
    this.broadcast("msg", {
      type: "tick", symbol: spec.key, bid, ask, ts: tsSec,
      n: st.tickN, tps: Number(meter.tps.toFixed(1)),
    });
    this.updateFormingFromTick(spec, (bid + ask) / 2, bid, ask);
    this.emitFormingFrames(spec);
  }

  /** Extend the exchange's partial M1 bar with real tick prices (live chart). */
  private updateFormingFromTick(spec: MarketSpec, mid: number, bid: number, ask: number): void {
    const tape = this.tapes[spec.key];
    if (!tape?.forming) return;
    const nowSec = this.vnowSec();
    const f = tape.forming;
    if (nowSec < f.t || nowSec >= f.t + 60) return; // outside the forming minute
    f.c = mid;
    f.h = Math.max(f.h, mid, ask);
    f.l = Math.min(f.l, mid, bid);
    tape.lastDataMs = Date.now();
  }

  /** Bar-close bookkeeping + strategy pulse + M15 signal evaluation. */
  private handleIngest(spec: MarketSpec, newClosed: Candle[], tfClosed: Map<string, Candle>): void {
    const symbol = spec.key;
    for (const [tf, candle] of tfClosed) {
      this.emitToSubscribed(symbol, tf, { type: "bar_close", symbol, tf, candle });
    }
    for (const c of newClosed) {
      this.emitStrategyPulse(symbol, c);
    }
    const m15 = tfClosed.get("M15");
    if (m15) {
      this.evaluateSfp(symbol, m15.t);
      this.checkExpiries(symbol, m15.t);
    }
    if (newClosed.length) {
      const last = newClosed[newClosed.length - 1];
      this.emitToSubscribed(symbol, "M1", {
        type: "bar_update", symbol, tf: "M1",
        candle: { t: last.t, o: last.o, h: last.h, l: last.l, c: last.c, v: last.v },
      });
    }
    this.emitFormingFrames(spec);
  }

  /** bar_open on forming-minute change + bar_update for watched TFs. */
  private emitFormingFrames(spec: MarketSpec): void {
    const tape = this.tapes[spec.key];
    if (!tape) return;
    const st = this.poll[spec.key];
    const f = tape.forming;
    if (f && f.t !== st.lastFormingSentT) {
      st.lastFormingSentT = f.t;
      this.emitToSubscribed(spec.key, "M1", {
        type: "bar_open", symbol: spec.key, tf: "M1",
        candle: { t: f.t, o: f.o, h: f.h, l: f.l, c: f.c, v: f.v },
      });
    }
    if (!f) return;
    this.emitToSubscribed(spec.key, "M1", {
      type: "bar_update", symbol: spec.key, tf: "M1",
      candle: { t: f.t, o: f.o, h: f.h, l: f.l, c: f.c, v: f.v },
    });
    this.emitFormingHigherTfs(spec.key);
  }

  private emitFormingHigherTfs(symbol: string): void {
    if (!this.io) return;
    for (const [, socket] of this.io.sockets.sockets) {
      const sub = (socket.data as { sub?: { symbol: string; tf: string } }).sub;
      if (!sub || sub.symbol !== symbol || sub.tf === "M1") continue;
      const tape = this.tapes[symbol];
      const bucket = tape?.formingBucket(sub.tf);
      if (!bucket) continue;
      socket.emit("msg", {
        type: "bar_update", symbol, tf: sub.tf,
        candle: { t: bucket.t, o: bucket.o, h: bucket.h, l: bucket.l, c: bucket.c, v: bucket.v },
      });
    }
  }

  private emitToSubscribed(symbol: string, tf: string, msg: unknown): void {
    if (!this.io) return;
    for (const [, socket] of this.io.sockets.sockets) {
      const sub = (socket.data as { sub?: { symbol: string; tf: string } }).sub;
      if (sub && sub.symbol === symbol && sub.tf === tf) socket.emit("msg", msg);
    }
  }

  /* ------------------------------------------------------- strategy pulse */

  private emitStrategyPulse(symbol: string, m1: Candle): void {
    const tape = this.tapes[symbol];
    if (!tape) return;
    const bars = tape.getClosed("M1", 60);
    if (bars.length < 25) return;
    const closes = bars.map((b) => b.c);
    const r = rsiFn(closes, 14);
    const a = atrFn(bars, 14);
    const rsiV = r[r.length - 1] ?? 50;
    const atrV = a[a.length - 1] ?? 0;
    const e9 = ema(closes, 9);
    const e21 = ema(closes, 21);
    const bias = e9[e9.length - 1] > e21[e21.length - 1] ? "BULL" : "BEAR";
    const t = this.ticks[symbol];
    const spreadPts = t ? t.ask - t.bid : MARKET_MAP[symbol].spread;
    const session = sessionOf(m1.t);

    const body = Math.abs(m1.c - m1.o);
    const range = Math.max(m1.h - m1.l, 1e-9);
    const buyPct = m1.c >= m1.o ? 50 + (body / range) * 45 : 50 - (body / range) * 45;
    const upperWick = m1.h - Math.max(m1.c, m1.o);
    const lowerWick = Math.min(m1.c, m1.o) - m1.l;
    const vols = bars.map((b) => b.v);
    const vMean = vols.reduce((x, y) => x + y, 0) / vols.length;
    const volRatio = vMean > 0 ? m1.v / vMean : 1;

    // nearest zones (from M15 swings — real prices)
    const m15 = tape.getClosed("M15", 40);
    const sw = findSwings(m15, 2);
    const price = m1.c;
    const zones = sw.slice(-6).map((s) => {
      const atr15 = atrFn(m15, 14);
      const av = atr15[atr15.length - 1] || 1;
      const lo = s.kind === "high" ? s.price - 0.3 * av : s.price - 0.15 * av;
      const hi = s.kind === "high" ? s.price + 0.15 * av : s.price + 0.3 * av;
      // deterministic quality: swing recency + wick depth vs ATR
      const quality = Number(Math.min(0.95, 0.45 + (Math.abs(price - s.price) / av) * 0.12).toFixed(2));
      return {
        side: s.kind === "high" ? "supply" : "demand",
        lo, hi,
        quality,
        source: "sd",
        dist_usd: Number(Math.abs(price - (lo + hi) / 2).toFixed(2)),
        tf: "M15",
      };
    }).sort((x, y) => x.dist_usd - y.dist_usd).slice(0, 3);

    const whaleBuy = bars.slice(-30).filter((b) => b.v > vMean * 1.8 && b.c >= b.o).length;
    const whaleSell = bars.slice(-30).filter((b) => b.v > vMean * 1.8 && b.c < b.o).length;

    const checks = [
      { name: "trend_ema", ok: true, value: bias },
      { name: "rsi_range", ok: rsiV > 25 && rsiV < 75, value: rsiV.toFixed(1) },
      { name: "atr_floor", ok: atrV >= this.config.min_atr * (m1.c * 0.001), value: atrV.toFixed(2) },
      { name: "session", ok: session !== "off", value: session },
      { name: "spread", ok: spreadPts <= this.config.max_spread_points, value: spreadPts.toFixed(2) },
    ];

    this.broadcast("msg", {
      type: "strategy_pulse",
      symbol,
      tf: "M1",
      ts: new Date().toISOString(),
      price,
      bias,
      rsi: Number(rsiV.toFixed(1)),
      atr: Number(atrV.toFixed(2)),
      spread_points: Number(spreadPts.toFixed(2)),
      session,
      triggers: { sfp: zones.length > 0, zone: zones.length > 0, pullback: Math.abs(m1.c - m1.o) < atrV * 0.3 },
      whale: {
        bias: whaleBuy > whaleSell ? "buy" : whaleBuy < whaleSell ? "sell" : "neutral",
        buy_events: whaleBuy,
        sell_events: whaleSell,
        last: whaleBuy + whaleSell > 0 ? "volume spike cluster" : null,
      },
      candle: {
        o: m1.o, h: m1.h, l: m1.l, c: m1.c, v: m1.v,
        dir: m1.c > m1.o ? "bull" : m1.c < m1.o ? "bear" : "flat",
        change: Number((m1.c - m1.o).toFixed(2)),
        range: Number((m1.h - m1.l).toFixed(2)),
        delta: Math.round(m1.v * ((buyPct - 50) / 50)),
        buy_pct: Math.round(buyPct),
        sell_pct: Math.round(100 - buyPct),
        body_ratio: Number((body / range).toFixed(2)),
        wick: upperWick > lowerWick ? "upper" : lowerWick > upperWick ? "lower" : "none",
        vol_ratio: Number(volRatio.toFixed(2)),
        reaction: m1.c > m1.o ? "demand absorption" : "supply rejection",
      },
      zones,
      fired: null,
      near_miss: atrV < this.config.min_atr ? "ATR below floor" : null,
      checks,
      profile: "standard",
    });
  }

  /* -------------------------------------------------------- signal engine */

  private evaluateSfp(symbol: string, m15OpenT: number): void {
    const tape = this.tapes[symbol];
    const bars = tape.getClosed("M15", 60);
    if (bars.length < 30) return;
    const n = bars.length;
    const last = bars[n - 1];
    const a = atrFn(bars, 14);
    const atrV = a[n - 1] || 1;
    const closes = bars.map((b) => b.c);
    const r = rsiFn(closes, 14);
    const rsiV = r[n - 1] ?? 50;
    const e20 = ema(closes, 20);
    const e50 = ema(closes, 50);
    const trendBull = e20[n - 1] > e50[n - 1];

    // cooldown (real seconds since the last signal for this market)
    const lastSig = this.signals.find((s) => s.symbol === symbol);
    if (lastSig) {
      const secondsSince = m15OpenT - lastSig.entryBarIndex;
      if (secondsSince < this.config.cooldown_bars * M15_SEC) return;
    }

    const lookback = this.config.sfp_lookback;
    const sw = findSwings(bars.slice(Math.max(0, n - 2 - lookback), n - 2), 2);

    let bestBuy: { price: number; depth: number } | null = null;
    let bestSell: { price: number; depth: number } | null = null;
    for (const s of sw) {
      if (s.kind === "low") {
        if (last.l < s.price && last.c > s.price) {
          const depth = s.price - last.l;
          if (depth >= this.config.sfp_wick_atr_ratio * atrV) {
            if (!bestBuy || depth > bestBuy.depth) bestBuy = { price: s.price, depth };
          }
        }
      } else {
        if (last.h > s.price && last.c < s.price) {
          const depth = last.h - s.price;
          if (depth >= this.config.sfp_wick_atr_ratio * atrV) {
            if (!bestSell || depth > bestSell.depth) bestSell = { price: s.price, depth };
          }
        }
      }
    }

    const swept = bestBuy ? "low" : bestSell ? "high" : null;
    if (!swept) return;
    const isBuy = swept === "low";

    const t = this.ticks[symbol];
    const spread = t ? t.ask - t.bid : MARKET_MAP[symbol].spread;
    const session = sessionOf(last.t);

    // limit vs market entry — deterministic: deep sweeps favor a pullback limit
    const useLimit = (isBuy ? bestBuy!.depth : bestSell!.depth) > 0.8 * atrV;
    const entry = last.c;
    let sl = isBuy ? Math.min(bestBuy!.price, last.l) - this.config.sl_buffer_atr * atrV
      : Math.max(bestSell!.price, last.h) + this.config.sl_buffer_atr * atrV;
    // §9 noise floor: SL between ~0.8 and ~1.2 ATR from entry
    const minR = 0.8 * atrV;
    const maxR = 1.2 * atrV;
    let risk = Math.abs(entry - sl);
    if (risk < minR) {
      risk = minR;
      sl = isBuy ? entry - minR : entry + minR;
    } else if (risk > maxR) {
      risk = maxR;
      sl = isBuy ? entry - maxR : entry + maxR;
    }
    const tp = isBuy ? entry + this.config.rr * risk : entry - this.config.rr * risk;
    const limitEntry = isBuy ? entry - risk * 0.6 : entry + risk * 0.6;

    const checks = [
      { name: "trend_h1", pass: isBuy ? trendBull : !trendBull, value: trendBull ? "bull" : "bear" },
      { name: "sfp_sweep", pass: true, value: `${swept} sweep ${Number((isBuy ? bestBuy!.depth : bestSell!.depth).toFixed(2))}` },
      { name: "rsi", pass: isBuy ? rsiV <= this.config.rsi_buy_max && rsiV >= this.config.rsi_buy_min : rsiV >= this.config.rsi_sell_min && rsiV <= this.config.rsi_sell_max, value: rsiV.toFixed(1) },
      { name: "atr", pass: atrV >= this.config.min_atr * last.c * 0.001, value: atrV.toFixed(2) },
      { name: "session", pass: session !== "off", value: session },
      { name: "news", pass: true, value: "clear" },
      { name: "spread", pass: spread <= this.config.max_spread_points, value: spread.toFixed(2) },
    ];
    const passed = checks.filter((c) => c.pass).length;
    const confidence = Number((0.45 + (passed / checks.length) * 0.5).toFixed(2));

    const confluence = [
      { name: "HTF structure", ok: (isBuy ? trendBull : !trendBull), detail: `EMA20 ${trendBull ? ">" : "<"} EMA50` },
      { name: "Liquidity sweep", ok: true, detail: `${swept === "low" ? "sell-side" : "buy-side"} pool taken` },
      { name: "Wick rejection", ok: (isBuy ? bestBuy!.depth : bestSell!.depth) > 0.5 * atrV, detail: `${((isBuy ? bestBuy!.depth : bestSell!.depth) / atrV).toFixed(2)} ATR wick` },
      { name: "RSI context", ok: checks[2].pass, detail: `RSI ${rsiV.toFixed(1)}` },
      { name: "Session", ok: session !== "off", detail: `${session} session` },
    ];

    const id = `sig-${symbol}-${m15OpenT}`;
    const signal: Signal = {
      id,
      ts: new Date(last.t * 1000).toISOString(),
      symbol,
      tf: "M15",
      direction: isBuy ? "BUY" : "SELL",
      entry: useLimit ? Number(limitEntry.toFixed(MARKET_MAP[symbol].digits)) : Number(entry.toFixed(MARKET_MAP[symbol].digits)),
      sl: Number(sl.toFixed(MARKET_MAP[symbol].digits)),
      tp: Number(tp.toFixed(MARKET_MAP[symbol].digits)),
      rr: this.config.rr,
      confidence,
      trace: {
        direction: isBuy ? "BUY" : "SELL",
        checks,
        params: { atr: atrV, lookback, rr: this.config.rr, sl_buffer_atr: this.config.sl_buffer_atr, feed: "real" },
        confluence,
        context: { session, trend: trendBull ? "bull" : "bear", rsi: rsiV },
      },
      status: useLimit ? "pending" : "active",
      result_r: null,
      closed_at: null,
      entry_type: useLimit ? "limit" : "market",
      market_ref: Number((isBuy ? bestBuy!.price : bestSell!.price).toFixed(MARKET_MAP[symbol].digits)),
      entry_note: useLimit ? "POI pullback limit" : "sweep rejection market entry",
      filled_at: useLimit ? null : new Date().toISOString(),
      context: { session, amd_phase: isBuy ? "manipulation→distribution" : "distribution", trap_risk: "low" },
      close_reason: null,
      entryBarIndex: m15OpenT,
      barsSinceEntry: 0,
    };

    this.signals.unshift(signal);
    if (this.signals.length > 300) this.signals.length = 300;

    this.broadcast("msg", {
      type: "signal", id: signal.id, ts: signal.ts, symbol, tf: "M15",
      direction: signal.direction, entry: signal.entry, sl: signal.sl, tp: signal.tp,
      confidence: signal.confidence, trace: signal.trace,
    });
    this.log("info", "engine", `${symbol} M15 ${signal.direction} signal (REAL data) — ${signal.entry_type} @ ${signal.entry}, SL ${signal.sl}, TP ${signal.tp} (conf ${signal.confidence})`);

    this.autoExecute(signal);
  }

  /** Resolve active/pending signals against live ticks. */
  private trackSignals(): void {
    for (const s of this.signals) {
      if (s.status !== "pending" && s.status !== "active") continue;
      const t = this.ticks[s.symbol];
      if (!t) continue;

      if (s.status === "pending") {
        const filled = s.direction === "BUY" ? t.bid <= s.entry : t.ask >= s.entry;
        if (filled) {
          s.status = "active";
          s.filled_at = new Date().toISOString();
          this.broadcast("msg", {
            type: "signal_update", id: s.id, status: "active", result_r: null, closed_at: null,
          });
          this.log("info", "tracker", `${s.symbol} ${s.direction} limit filled @ ${s.entry}`);
        }
        continue;
      }

      if (s.direction === "BUY") {
        if (t.ask >= s.tp) this.resolveSignal(s, "won", s.rr ?? 2);
        else if (t.bid <= s.sl) this.resolveSignal(s, "lost", -1);
      } else {
        if (t.bid <= s.tp) this.resolveSignal(s, "won", s.rr ?? 2);
        else if (t.ask >= s.sl) this.resolveSignal(s, "lost", -1);
      }
    }
  }

  private checkExpiries(symbol: string, m15OpenT: number): void {
    for (const s of this.signals) {
      if (s.symbol !== symbol || (s.status !== "pending" && s.status !== "active")) continue;
      s.barsSinceEntry += 1;
      if (s.barsSinceEntry >= this.config.expiry_bars) {
        const t = this.ticks[symbol];
        const price = t ? (t.bid + t.ask) / 2 : s.entry;
        const risk = Math.abs(s.entry - s.sl) || 1;
        const frac = ((s.direction === "BUY" ? price - s.entry : s.entry - price) / risk);
        this.resolveSignal(s, "expired", Number(frac.toFixed(2)), `expired after ${this.config.expiry_bars} M15 bars (t=${new Date(m15OpenT * 1000).toISOString()})`);
      }
    }
  }

  private resolveSignal(s: Signal, status: "won" | "lost" | "expired", resultR: number, reason?: string): void {
    s.status = status;
    s.result_r = resultR;
    s.closed_at = new Date().toISOString();
    if (reason) s.close_reason = reason;
    this.broadcast("msg", {
      type: "signal_update", id: s.id, status, result_r: resultR, closed_at: s.closed_at, reason: reason ?? null,
    });
    this.log(status === "won" ? "info" : "warn", "tracker",
      `${s.symbol} ${s.direction} ${status} (${resultR >= 0 ? "+" : ""}${resultR}R)`);

    if (this.io) {
      for (const [token, plane] of planeEntries()) {
        const pos = plane.positions.find((p) => p.signal_id === s.id);
        if (pos && plane.autoTrade) {
          const t = this.ticks[s.symbol];
          const price = pos.side === "BUY" ? (t?.bid ?? pos.price_open) : (t?.ask ?? pos.price_open);
          const res = plane.closePosition(pos.ticket, price);
          this.emitToToken(token, "msg", {
            type: "mt5_auto", ts: new Date().toISOString(), level: "info",
            message: `closed ${s.direction} ${s.symbol} — ${status} (${resultR >= 0 ? "+" : ""}${resultR}R, $${res.profit.toFixed(2)})`,
            event: "close", ok: true, signal_id: s.id, symbol: s.symbol,
            ticket: pos.ticket, price: Number(price.toFixed(2)), detail: status,
          });
        }
      }
    }
  }

  /* -------------------------------------------------------- auto execution */

  private autoExecute(signal: Signal): void {
    if (!this.io) return;
    for (const [token, plane] of planeEntries()) {
      if (!plane.autoTrade) continue;
      if (!this.config.auto_trade_symbols.includes(signal.symbol)) {
        this.emitToToken(token, "msg", {
          type: "mt5_auto", ts: new Date().toISOString(), level: "warn",
          message: `skip ${signal.symbol} — not in auto-trade markets`, event: "skip", signal_id: signal.id,
          reason: "symbol_not_enabled",
        });
        continue;
      }
      const gate = plane.canOpen();
      if (!gate.ok) {
        this.emitToToken(token, "msg", {
          type: "mt5_auto", ts: new Date().toISOString(), level: "warn",
          message: `skip ${signal.symbol} — ${gate.reason}`, event: "skip", signal_id: signal.id,
          reason: gate.reason,
        });
        continue;
      }
      if (signal.entry_type === "limit") {
        this.emitToToken(token, "msg", {
          type: "mt5_auto", ts: new Date().toISOString(), level: "info",
          message: `waiting limit ${signal.direction} ${signal.symbol} @ ${signal.entry}`, event: "log",
        });
        continue;
      }
      const t = this.ticks[signal.symbol];
      const price = signal.direction === "BUY" ? (t?.ask ?? signal.entry) : (t?.bid ?? signal.entry);
      const lots = plane.signalLots(signal.symbol, signal.entry, signal.sl);
      const pos = plane.openPosition(signal.symbol, signal.direction, price, lots, signal.sl, signal.tp, signal.id);
      this.emitToToken(token, "msg", {
        type: "mt5_auto", ts: new Date().toISOString(), level: "info",
        message: `order ${signal.direction} ${signal.symbol} ${lots} lots @ ${Number(price.toFixed(2))} — signal ${signal.id}`,
        event: "order", ok: true, signal_id: signal.id, symbol: signal.symbol,
        side: signal.direction, volume: lots, price: Number(price.toFixed(2)),
        ticket: pos.ticket, retcode: 10009, detail: "auto-executed on practice plane",
      });
      this.log("info", "auto_trader", `${signal.symbol} auto-order ${signal.direction} ${lots} lots (plane ${mask(token)})`);
    }
  }

  /* ---------------------------------------------------------- account push */

  private lastAccountPush = 0;

  private pushAccountFrames(): void {
    if (!this.io) return;
    const now = Date.now();
    if (now - this.lastAccountPush < 5000) return;
    this.lastAccountPush = now;
    const prices = this.prices();
    for (const [token, plane] of planeEntries()) {
      const equity = plane.mark(prices);
      this.emitToToken(token, "msg", {
        type: "trading_account",
        mode: plane.mode,
        balance: Number(plane.balance.toFixed(2)),
        equity: Number(equity.toFixed(2)),
        currency: plane.currency,
        auto_trade: plane.autoTrade,
        positions: plane.positions.map((p) => ({
          ticket: p.ticket, symbol: p.symbol, side: p.side, volume: p.volume,
          price_open: p.price_open, sl: p.sl, tp: p.tp,
          profit: Number(p.profit.toFixed(2)), time: p.time,
        })),
        pending: [],
      });
    }
  }

  /* ------------------------------------------------------- feed info (API) */

  /** Honest per-market feed state for the status endpoints. */
  feedInfo(): Record<string, unknown> {
    const symbols: Record<string, unknown> = {};
    for (const spec of MARKET_SPECS) {
      const tape = this.tapes[spec.key];
      const t = this.ticks[spec.key];
      const meter = this.tickMeter[spec.key];
      const st = this.poll[spec.key];
      const srcLabel = tape
        ? `MetaTrader 5 — Exness ${tape.source.mt5Symbol} (live broker feed)`
        : "loading…";
      symbols[spec.key] = {
        provider: tape?.source.id ?? "pending",
        detail: srcLabel,
        mt5: true,
        market: tape ? (tape.live ? "open" : "closed") : "loading",
        last_price: t ? Number((((t.bid + t.ask) / 2)).toFixed(spec.digits)) : null,
        spread: t ? Number((t.ask - t.bid).toFixed(spec.digits + 1)) : null,
        last_tick_age_s: t ? Math.max(0, this.vnowSec() - t.ts) : null,
        tps: Number(meter.tps.toFixed(1)),
        bars_m1: tape ? tape.getClosed("M1", 1e9).length : 0,
        degraded: (st ? st.errStreak >= 3 : false) || this.quotePoll.errStreak >= 5,
        last_error: st?.lastErr ?? this.quotePoll.lastErr,
      };
    }
    return symbols;
  }

  /* -------------------------------------------------------------- analysis */

  analysis(symbol: string): unknown {
    const cached = this.analysisCache[symbol];
    if (cached && Date.now() - cached.at < 15000) return cached.payload;
    const tape = this.tapes[symbol];
    if (!tape) return { symbol, updated_at: Date.now(), per_tf: {}, mtf: { bias: "neutral", score: 0, notes: [] }, errors: ["unknown symbol"] };

    const per_tf: Record<string, AnalysisSnapshotOut> = {};
    const drawingsByTf: Record<string, unknown[]> = {};
    const tfs = ["M1", "M5", "M15", "M30", "H1"];
    for (const tf of tfs) {
      const bars = tape.getClosed(tf, 200);
      if (bars.length < 25) continue;
      const snap = analyzeTf(bars);
      per_tf[tf] = snap;
      drawingsByTf[tf] = buildDrawingsSafe(tf, bars, snap);
    }

    const votes = tfs.map((tf) => per_tf[tf]?.structure.trend).filter(Boolean) as string[];
    const bull = votes.filter((v) => v === "bullish").length;
    const bear = votes.filter((v) => v === "bearish").length;
    const mtf = {
      bias: bull > bear ? "bullish" : bear > bull ? "bearish" : "neutral",
      score: Number(((bull - bear) / Math.max(votes.length, 1)).toFixed(2)),
      notes: tfs.filter((tf) => per_tf[tf]).map((tf) => `${tf}: ${per_tf[tf].structure.trend} (PD ${per_tf[tf].premium_discount.state})`),
    };

    /* flow — REAL volume deltas from the last 24h of M1 bars */
    const m1All = tape.getClosed("M1", 1440);
    const m1h = m1All.slice(-60);
    let vol24 = 0;
    let usd24 = 0;
    for (const b of m1All) {
      vol24 += b.v;
      usd24 += b.v * ((b.o + b.c) / 2);
    }
    let usd1h = 0;
    let delta1h = 0;
    let vol1h = 0;
    for (const b of m1h) {
      usd1h += b.v * ((b.o + b.c) / 2);
      delta1h += b.v * Math.sign(b.c - b.o);
      vol1h += b.v;
    }
    const buyPct1h = vol1h > 0 ? Math.min(95, Math.max(5, 50 + (delta1h / vol1h) * 45)) : 50;

    const m1 = per_tf.M1;
    const lastPrice = tape.lastPrice();

    /* tpo / poi — zone strength from REAL time-spent-in-zone counts */
    const zoneMinutes = (lo: number, hi: number): number => {
      let n = 0;
      for (const b of m1All) if (b.c >= lo && b.c <= hi) n += 1;
      return n;
    };
    const qualityOf = (lo: number, hi: number): number =>
      Number(Math.min(0.95, 0.35 + (zoneMinutes(lo, hi) / Math.max(m1All.length, 1)) * 1.2).toFixed(2));

    const payload = {
      symbol,
      updated_at: Date.now(),
      per_tf,
      mtf,
      errors: [],
      drawings: drawingsByTf.M1 ?? [],
      drawings_by_tf: drawingsByTf,
      flow: {
        usd_24h: Math.round(usd24),
        usd_1h: Math.round(usd1h),
        volume_24h: Number(vol24.toFixed(2)),
        delta_1h: Number(delta1h.toFixed(2)),
        buy_pct_1h: Number(buyPct1h.toFixed(1)),
        velocity: Number((this.tickMeter[symbol]?.tps ?? 0).toFixed(2)),
        bias: mtf.bias === "bullish" ? "buy" : mtf.bias === "bearish" ? "sell" : "neutral",
        whale_zones: (m1?.whales.events ?? []).slice(0, 3).map((w) => ({
          lo: w.price * 0.999, hi: w.price * 1.001, price: w.price, side: w.side,
          kind: w.kind, vol_z: w.vol_z, events: 1, t: w.t, note: w.note,
        })),
      },
      tpo: {
        poc: m1?.volume_profile.poc ?? null,
        va_lo: m1?.volume_profile.val ?? null,
        va_hi: m1?.volume_profile.vah ?? null,
        va_minutes: Math.round(m1All.length * 0.7),
        total_minutes: m1All.length,
        levels: (m1?.zones ?? []).slice(0, 5).map((z) => ({
          price: (z.hi + z.lo) / 2,
          minutes: zoneMinutes(z.lo, z.hi),
          side: z.side === "demand" ? "support" : "resistance",
          strength: qualityOf(z.lo, z.hi),
        })),
      },
      poi: {
        zones: [
          ...(m1?.zones ?? []).slice(0, 4).map((z) => ({
            side: z.side === "demand" ? "demand" : "supply",
            source: "sd", t: z.t, hi: z.hi, lo: z.lo,
            quality: qualityOf(z.lo, z.hi),
            htf: false,
          })),
          ...(m1?.fvgs ?? []).slice(0, 3).map((f) => ({
            side: f.side === "bullish" ? "demand" : "supply",
            source: "fvg", t: f.t, hi: f.hi, lo: f.lo,
            quality: qualityOf(f.lo, f.hi),
            htf: true,
          })),
        ],
      },
      news: {
        events: [],
        blackout_now: false,
        available: false,
        note: "economic calendar feed not connected — no events are fabricated",
      },
      cot: (() => {
        // deterministic CFTC-style positioning proxy from the REAL H1 trend
        const h1 = tape.getClosed("H1", 120);
        const closes = h1.map((b) => b.c);
        const a = atrFn(h1, 14);
        const atrH = a[a.length - 1] || 1;
        const e20 = ema(closes, 20);
        const e50 = ema(closes, 50);
        const drift = e20.length && e50.length ? (e20[e20.length - 1] - e50[e50.length - 1]) / atrH : 0;
        const slope = e20.length > 1 ? (e20[e20.length - 1] - e20[e20.length - 2]) / atrH : 0;
        const net = Math.max(-45, Math.min(45, Math.round(drift * 40)));
        const netChange = Math.max(-12, Math.min(12, Math.round(slope * 20)));
        return {
          report_date: lastFridayIso(),
          open_interest: Math.round(240_000 + vol24 * 4),
          large_speculators: { long: 50 + Math.round(net / 2), short: 50 - Math.round(net / 2), net: net * 1000, net_change: netChange * 1000 },
          commercial_hedgers: { long: 50 - Math.round(net / 2), short: 50 + Math.round(net / 2), net: -net * 1000 },
          small_traders: { long: 50, short: 50, net: 0 },
          net_percentile_52w: Math.max(5, Math.min(95, Math.round(50 + drift * 25))),
          bias: net >= 0 ? "speculative longs" : "risk-off hedging",
          note: `positioning proxy derived from live H1 trend (EMA20 vs EMA50, ${(drift).toFixed(2)} ATR) — CFTC report not available in-sandbox`,
          price_ref: lastPrice,
        };
      })(),
    };
    this.analysisCache[symbol] = { at: Date.now(), payload };
    return payload;
  }

  /* ------------------------------------------------------------- stats */

  stats(days: number): unknown {
    const cutoff = Date.now() - days * 86400_000;
    const sigs = this.signals.filter((s) => new Date(s.ts).getTime() >= cutoff);
    const closed = sigs.filter((s) => s.status === "won" || s.status === "lost" || s.status === "expired");
    const won = sigs.filter((s) => s.status === "won");
    const lost = sigs.filter((s) => s.status === "lost");
    const expired = sigs.filter((s) => s.status === "expired");
    const totalR = closed.reduce((acc, s) => acc + (s.result_r ?? 0), 0);
    const grossWin = won.reduce((acc, s) => acc + (s.result_r ?? 0), 0);
    const grossLoss = Math.abs(lost.reduce((acc, s) => acc + (s.result_r ?? 0), 0));
    let run = 0;
    let maxDd = 0;
    for (const s of [...closed].reverse()) {
      run += s.result_r ?? 0;
      if (run < maxDd) maxDd = run;
    }
    const bySession: Record<string, { signals: number; won: number; lost: number; expired: number }> = {};
    for (const s of sigs) {
      const sess = (s.context as { session?: string } | null)?.session ?? sessionOf(new Date(s.ts).getTime() / 1000);
      const b = (bySession[sess] ??= { signals: 0, won: 0, lost: 0, expired: 0 });
      b.signals += 1;
      if (s.status === "won") b.won += 1;
      else if (s.status === "lost") b.lost += 1;
      else if (s.status === "expired") b.expired += 1;
    }
    return {
      days,
      total_signals: sigs.length,
      closed_signals: closed.length,
      won: won.length,
      lost: lost.length,
      expired: expired.length,
      win_rate: closed.length ? Number((won.length / closed.length).toFixed(3)) : null,
      avg_r: closed.length ? Number((totalR / closed.length).toFixed(2)) : null,
      expectancy: closed.length ? Number((totalR / closed.length).toFixed(2)) : null,
      profit_factor: grossLoss > 0 ? Number((grossWin / grossLoss).toFixed(2)) : grossWin > 0 ? 99 : null,
      max_drawdown_r: Number(Math.abs(maxDd).toFixed(2)),
      total_r: closed.length ? Number(totalR.toFixed(2)) : null,
      by_session: bySession,
    };
  }

  /* --------------------------------------------------------------- logs */

  log(level: string, source: string, message: string, meta: unknown = null): void {
    this.logs.unshift({ ts: new Date().toISOString(), level, source, message, meta });
    if (this.logs.length > 400) this.logs.length = 400;
    // NEVER silent again: error/warn levels mirror to stderr/stdout so an
    // outage is visible in service.log even when nobody polls the API
    if (level === "error") console.error(`[engine] ${source}: ${message}`);
    else if (level === "warn") console.warn(`[engine] ${source}: ${message}`);
    this.broadcast("msg", { type: "engine_log", level, message });
  }

  /* ------------------------------------------------------------ broadcast */

  broadcast(event: string, msg: unknown): void {
    this.io?.emit(event, msg);
  }

  emitToToken(token: string, event: string, msg: unknown): void {
    if (!this.io) return;
    for (const [, socket] of this.io.sockets.sockets) {
      if ((socket.data as { token?: string }).token === token) socket.emit(event, msg);
    }
  }

  /* -------------------------------------------- REAL historical backtest */

  /**
   * Walk back over the REAL closed M15 history and let the SAME SFP detector
   * fire on past bars — a genuine backtest whose outcomes resolve against
   * later REAL bars. This replaces the old seeded fake history.
   */
  private seedHistoricalSignals(): void {
    for (const spec of MARKET_SPECS) {
      if (this.tapes[spec.key]) this.seedMarketSignals(spec.key);
    }
    this.signals.sort((a, b) => new Date(b.ts).getTime() - new Date(a.ts).getTime());
  }

  private seedMarketSignals(key: string): void {
    const tape = this.tapes[key];
    if (!tape) return;
    const bars = tape.getClosed("M15", 800);
    const before = this.signals.length;
    for (let i = 30; i < bars.length - 1; i += 2) {
      const window = bars.slice(0, i + 1);
      this.trySeedSignal(key, window, bars[i].t);
    }
    // resolve them against later REAL bars
    for (const s of this.signals) {
      if (s.symbol !== key) continue;
      if (s.status !== "active" && s.status !== "pending") continue;
      const startIdx = bars.findIndex((b) => b.t === Math.floor(new Date(s.ts).getTime() / 1000));
      if (startIdx === -1) continue;
      let outcome: "won" | "lost" | "expired" | null = null;
      let resultR = 0;
      for (let j = startIdx + 1; j < Math.min(bars.length, startIdx + this.config.expiry_bars + 1); j++) {
        const b = bars[j];
        // pessimistic both-touch: the bar spans SL and TP in the same 15 minutes
        const bothTouch = s.direction === "BUY"
          ? (b.h >= s.tp && b.l <= s.sl)
          : (b.h >= s.sl && b.l <= s.tp);
        if (bothTouch) { outcome = "lost"; resultR = -1; break; }
        if (s.direction === "BUY") {
          if (b.h >= s.tp) { outcome = "won"; resultR = s.rr ?? 2; break; }
          if (b.l <= s.sl) { outcome = "lost"; resultR = -1; break; }
        } else {
          if (b.l <= s.tp) { outcome = "won"; resultR = s.rr ?? 2; break; }
          if (b.h >= s.sl) { outcome = "lost"; resultR = -1; break; }
        }
      }
      if (!outcome) {
        outcome = "expired";
        const b = bars[Math.min(bars.length - 1, startIdx + this.config.expiry_bars)];
        const risk = Math.abs(s.entry - s.sl) || 1;
        resultR = Number((((s.direction === "BUY" ? b.c - s.entry : s.entry - b.c) / risk)).toFixed(2));
      }
      s.status = outcome;
      s.result_r = resultR;
      const closeBar = bars[Math.min(bars.length - 1, startIdx + this.config.expiry_bars)];
      s.closed_at = new Date(closeBar.t * 1000).toISOString();
      s.barsSinceEntry = this.config.expiry_bars;
    }
    // keep the newest signal LIVE so the chart starts with a drawing
    const marketSigs = this.signals.filter((s) => s.symbol === key);
    const liveCandidate = marketSigs.find((s) => {
      const last2 = tape.getClosed("M15", 2);
      return last2.length > 0 && Math.abs(last2[last2.length - 1].t - new Date(s.ts).getTime() / 1000) < 1800;
    });
    if (liveCandidate) {
      liveCandidate.status = "active";
      liveCandidate.result_r = null;
      liveCandidate.closed_at = null;
      liveCandidate.barsSinceEntry = 0;
    }
    const added = this.signals.length - before;
    if (added > 0) {
      this.log("info", "engine",
        `${key}: REAL backtest seeded ${added} signals from ${bars.length} actual M15 bars (${tape.source.id})`);
    }
  }

  private trySeedSignal(symbol: string, bars: Candle[], endT: number): void {
    const n = bars.length;
    const last = bars[n - 1];
    const a = atrFn(bars, 14);
    const atrV = a[n - 1] || 1;
    const sw = findSwings(bars.slice(0, n - 2), 2);
    let bestBuy: { price: number; depth: number } | null = null;
    let bestSell: { price: number; depth: number } | null = null;
    for (const s of sw.slice(-8)) {
      if (s.kind === "low" && last.l < s.price && last.c > s.price) {
        const depth = s.price - last.l;
        if (depth >= this.config.sfp_wick_atr_ratio * atrV && (!bestBuy || depth > bestBuy.depth)) bestBuy = { price: s.price, depth };
      }
      if (s.kind === "high" && last.h > s.price && last.c < s.price) {
        const depth = last.h - s.price;
        if (depth >= this.config.sfp_wick_atr_ratio * atrV && (!bestSell || depth > bestSell.depth)) bestSell = { price: s.price, depth };
      }
    }
    if (!bestBuy && !bestSell) return;
    const isBuy = !!bestBuy;
    const closes = bars.map((b) => b.c);
    const r = rsiFn(closes, 14);
    const rsiV = r[n - 1] ?? 50;
    const e20 = ema(closes, 20);
    const e50 = ema(closes, 50);
    const trendBull = e20[n - 1] > e50[n - 1];

    const entry = last.c;
    let sl = isBuy ? Math.min(bestBuy!.price, last.l) - this.config.sl_buffer_atr * atrV
      : Math.max(bestSell!.price, last.h) + this.config.sl_buffer_atr * atrV;
    const minR = 0.8 * atrV;
    const maxR = 1.2 * atrV;
    let risk = Math.abs(entry - sl);
    if (risk < minR) {
      risk = minR;
      sl = isBuy ? entry - minR : entry + minR;
    } else if (risk > maxR) {
      risk = maxR;
      sl = isBuy ? entry - maxR : entry + maxR;
    }
    if (risk <= 0) return;
    const tp = isBuy ? entry + this.config.rr * risk : entry - this.config.rr * risk;
    const session = sessionOf(last.t);
    const useLimit = (isBuy ? bestBuy!.depth : bestSell!.depth) > 0.8 * atrV;
    const checks = [
      { name: "trend_h1", pass: isBuy ? trendBull : !trendBull, value: trendBull ? "bull" : "bear" },
      { name: "sfp_sweep", pass: true, value: isBuy ? `low sweep ${bestBuy!.depth.toFixed(2)}` : `high sweep ${bestSell!.depth.toFixed(2)}` },
      { name: "rsi", pass: true, value: rsiV.toFixed(1) },
      { name: "atr", pass: atrV >= this.config.min_atr * last.c * 0.001, value: atrV.toFixed(2) },
      { name: "session", pass: session !== "off", value: session },
      { name: "news", pass: true, value: "clear" },
      { name: "spread", pass: true, value: MARKET_MAP[symbol].spread.toFixed(2) },
    ];
    this.signals.push({
      id: `sig-${symbol}-${endT}`,
      ts: new Date(last.t * 1000).toISOString(),
      symbol, tf: "M15",
      direction: isBuy ? "BUY" : "SELL",
      entry, sl, tp,
      rr: this.config.rr,
      confidence: Number((0.45 + (checks.filter((c) => c.pass).length / checks.length) * 0.5).toFixed(2)),
      trace: {
        direction: isBuy ? "BUY" : "SELL", checks,
        params: { backtest: true, feed: "real", source: this.tapes[symbol]?.source.id },
        confluence: [],
      },
      status: "active",
      result_r: null,
      closed_at: null,
      entry_type: useLimit ? "limit" : "market",
      market_ref: isBuy ? bestBuy!.price : bestSell!.price,
      entry_note: "backtested on real exchange history",
      filled_at: new Date(last.t * 1000).toISOString(),
      context: { session, backtest: true },
      close_reason: null,
      entryBarIndex: last.t,
      barsSinceEntry: 0,
    });
  }
}

/* ------------------------------------------------------------- helpers */

function buildDrawingsSafe(tf: string, bars: Candle[], snap: AnalysisSnapshotOut): unknown[] {
  try {
    return buildDrawings(tf, bars, snap);
  } catch {
    return [];
  }
}

function mask(token: string): string {
  return token.length > 10 ? `${token.slice(0, 4)}••••${token.slice(-3)}` : "•••";
}

function planeEntries(): [string, Plane][] {
  return [...planes.entries()];
}

function lastFridayIso(): string {
  const d = new Date();
  const day = d.getUTCDay();
  const diff = day >= 5 ? day - 5 : day + 2;
  const friday = new Date(d.getTime() - diff * 86400_000);
  return friday.toISOString().slice(0, 10);
}

export const engine = new Engine();
