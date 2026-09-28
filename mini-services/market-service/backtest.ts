/**
 * DEEP BACKTEST — 100% REAL market history (source ladder).
 *
 * Loads ~90 days of native M15 bars per pair via the platform's source
 * ladder — local MT5 terminal first, then the user-directed AURUM Terminal
 * remote MT5 bridge (https://u1m7j8csutd1-d.space-z.ai), then the live web
 * exchanges (Binance PAXG/BTC, NYMEX/CME futures) — and runs the SAME SFP
 * signal engine the live app uses (swing-failure-pivot detection with ATR
 * guards, deterministic limit-vs-market entry, pessimistic both-touch
 * resolution), then prints honest per-pair performance stats.
 *
 * Run: bun run backtest.ts
 */

import {
  MARKET_SPECS,
  mt5History,
  type Candle,
  type MarketSpec,
} from "./src/providers";
import { remoteCandles } from "./src/remotefeed";
import {
  atr as atrFn,
  ema,
  findSwings,
  rsi as rsiFn,
  sessionOf,
} from "./src/analysis";

/* -------------------------------------------------- engine config (mirrors DEFAULT_CONFIG) */

const CFG = {
  sfp_wick_atr_ratio: 0.3,
  sl_buffer_atr: 0.2,
  min_sl_atr: 0.8,
  max_sl_atr: 1.2,
  rr: 2,
  expiry_bars: 24,
  stride: 2, // evaluate every other bar (same as the live engine seed)
};

const DAYS = 90;

interface BtSignal {
  symbol: string;
  dir: "BUY" | "SELL";
  ts: number;
  entry: number;
  sl: number;
  tp: number;
  entryType: "market" | "limit";
  status: "won" | "lost" | "expired";
  resultR: number;
}

/** The SFP detector — identical math to engine.trySeedSignal(). */
function detectSfp(symbol: string, bars: Candle[], endIdx: number): Omit<BtSignal, "status" | "resultR"> | null {
  const window = bars.slice(0, endIdx + 1);
  const n = window.length;
  const last = window[n - 1];
  const a = atrFn(window, 14);
  const atrV = a[n - 1] || 1;
  const sw = findSwings(window.slice(0, n - 2), 2);
  let bestBuy: { price: number; depth: number } | null = null;
  let bestSell: { price: number; depth: number } | null = null;
  for (const s of sw.slice(-8)) {
    if (s.kind === "low" && last.l < s.price && last.c > s.price) {
      const depth = s.price - last.l;
      if (depth >= CFG.sfp_wick_atr_ratio * atrV && (!bestBuy || depth > bestBuy.depth)) bestBuy = { price: s.price, depth };
    }
    if (s.kind === "high" && last.h > s.price && last.c < s.price) {
      const depth = last.h - s.price;
      if (depth >= CFG.sfp_wick_atr_ratio * atrV && (!bestSell || depth > bestSell.depth)) bestSell = { price: s.price, depth };
    }
  }
  if (!bestBuy && !bestSell) return null;
  const isBuy = !!bestBuy;
  const closes = window.map((b) => b.c);
  const r = rsiFn(closes, 14);
  const rsiV = r[n - 1] ?? 50;
  const e20 = ema(closes, 20);
  const e50 = ema(closes, 50);
  const trendBull = e20[n - 1] > e50[n - 1];

  const entry = last.c;
  let sl = isBuy ? Math.min(bestBuy!.price, last.l) - CFG.sl_buffer_atr * atrV
    : Math.max(bestSell!.price, last.h) + CFG.sl_buffer_atr * atrV;
  const minR = CFG.min_sl_atr * atrV;
  const maxR = CFG.max_sl_atr * atrV;
  let risk = Math.abs(entry - sl);
  if (risk < minR) { risk = minR; sl = isBuy ? entry - minR : entry + minR; }
  else if (risk > maxR) { risk = maxR; sl = isBuy ? entry - maxR : entry + maxR; }
  if (risk <= 0) return null;
  const tp = isBuy ? entry + CFG.rr * risk : entry - CFG.rr * risk;

  // confluence gates (trend + session) — same as the live engine
  const session = sessionOf(last.t);
  const trendOk = isBuy ? trendBull : !trendBull;
  if (!trendOk) return null;
  if (session === "off") return null;
  if (atrV < 0.15 * last.c * 0.001) return null;
  if (rsiV < 30 || rsiV > 70) return null;
  void rsiV;

  const useLimit = (isBuy ? bestBuy!.depth : bestSell!.depth) > 0.8 * atrV;
  return {
    symbol,
    dir: isBuy ? "BUY" : "SELL",
    ts: last.t,
    entry,
    sl,
    tp,
    entryType: useLimit ? "limit" : "market",
  };
}

/** Deterministic outcome resolution — identical to engine.seedMarketSignals. */
function resolve(sig: Omit<BtSignal, "status" | "resultR">, bars: Candle[], startIdx: number): BtSignal {
  let outcome: "won" | "lost" | "expired" | null = null;
  let resultR = 0;
  for (let j = startIdx + 1; j < Math.min(bars.length, startIdx + CFG.expiry_bars + 1); j++) {
    const b = bars[j];
    const bothTouch = sig.dir === "BUY"
      ? (b.h >= sig.tp && b.l <= sig.sl)
      : (b.h >= sig.sl && b.l <= sig.tp);
    if (bothTouch) { outcome = "lost"; resultR = -1; break; }
    if (sig.dir === "BUY") {
      if (b.h >= sig.tp) { outcome = "won"; resultR = CFG.rr; break; }
      if (b.l <= sig.sl) { outcome = "lost"; resultR = -1; break; }
    } else {
      if (b.l <= sig.tp) { outcome = "won"; resultR = CFG.rr; break; }
      if (b.h >= sig.sl) { outcome = "lost"; resultR = -1; break; }
    }
  }
  if (!outcome) {
    outcome = "expired";
    const b = bars[Math.min(bars.length - 1, startIdx + CFG.expiry_bars)];
    const risk = Math.abs(sig.entry - sig.sl) || 1;
    resultR = Number((((sig.dir === "BUY" ? b.c - sig.entry : sig.entry - b.c) / risk)).toFixed(2));
  }
  return { ...sig, status: outcome, resultR };
}

/* ------------------------------------------------------------------ runner */

const UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36";

interface YahooChartLite {
  chart?: {
    result?: Array<{
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
    }>;
  };
}

/** Web-exchange M15 history for the last resort of the ladder. */
async function webM15(spec: MarketSpec): Promise<Candle[]> {
  const map: Record<string, { venue: "binance"; symbol: string } | { venue: "yahoo"; symbol: string }> = {
    XAUUSD: { venue: "binance", symbol: "PAXGUSDT" },
    BTCUSD: { venue: "binance", symbol: "BTCUSDT" },
    USOIL: { venue: "yahoo", symbol: "CL=F" },
    USTEC: { venue: "yahoo", symbol: "NQ=F" },
  };
  const def = map[spec.key];
  if (!def) return [];
  if (def.venue === "binance") {
    // paged klines — up to 90 days of 15m bars
    const out: Candle[] = [];
    let start: number | undefined = Math.floor(Date.now() / 1000 - DAYS * 86_400) * 1000;
    while (out.length < DAYS * 96) {
      const url = `https://api.binance.com/api/v3/klines?symbol=${def.symbol}&interval=15m&limit=1000${start != null ? `&startTime=${start}` : ""}`;
      const ctrl = new AbortController();
      const to = setTimeout(() => ctrl.abort(), 15_000);
      let rows: unknown[];
      try {
        const res = await fetch(url, { signal: ctrl.signal, headers: { "User-Agent": UA } });
        rows = await res.json() as unknown[];
      } finally { clearTimeout(to); }
      if (!Array.isArray(rows) || !rows.length) break;
      for (const r of rows as unknown[][]) {
        out.push({ t: Math.floor(Number(r[0]) / 1000), o: Number(r[1]), h: Number(r[2]), l: Number(r[3]), c: Number(r[4]), v: Number(r[5]) });
      }
      if (rows.length < 1000) break;
      start = Number((rows as unknown[][])[rows.length - 1][0]) + 1;
      await new Promise((r) => setTimeout(r, 300));
    }
    return out.filter((b) => Number.isFinite(b.c));
  }
  // Yahoo futures: 15m bars for the last month
  const host = "https://query1.finance.yahoo.com";
  const url = `${host}/v8/finance/chart/${encodeURIComponent(def.symbol)}?interval=15m&range=1mo`;
  const ctrl = new AbortController();
  const to = setTimeout(() => ctrl.abort(), 15_000);
  try {
    const res = await fetch(url, { headers: { "User-Agent": UA } , signal: ctrl.signal });
    const d = await res.json() as YahooChartLite;
    const r = d.chart?.result?.[0];
    const q = r?.indicators?.quote?.[0];
    const ts = r?.timestamp ?? [];
    const out: Candle[] = [];
    for (let i = 0; i < ts.length; i++) {
      const o = q?.open?.[i]; const h = q?.high?.[i]; const l = q?.low?.[i]; const c = q?.close?.[i];
      if (o == null || h == null || l == null || c == null) continue;
      out.push({ t: ts[i], o, h, l, c, v: q?.volume?.[i] ?? 0 });
    }
    return out;
  } finally { clearTimeout(to); }
}

/** Source ladder for backtest history: local MT5 → AURUM remote MT5 → web. */
async function loadM15(spec: MarketSpec): Promise<{ bars: Candle[]; source: string }> {
  const nowSec = Math.floor(Date.now() / 1000);
  const from = nowSec - DAYS * 86_400;
  // 1 — local MetaTrader 5 terminal (MCP bridge)
  try {
    const bars = await mt5History(spec.mt5, "M15", from, nowSec + 900, 20_000);
    if (bars.length >= 100) return { bars, source: "local MT5 terminal (Exness MCP)" };
  } catch { /* terminal offline on this host */ }
  // 2 — the user-directed AURUM Terminal remote MT5 bridge (real broker data)
  try {
    const bars = await remoteCandles(spec.mt5, "M15", 1000);
    if (bars.length >= 100) return { bars, source: "AURUM Terminal remote MT5 bridge (17d window)" };
  } catch { /* bridge unreachable */ }
  // 3 — live web exchanges
  const bars = await webM15(spec);
  return { bars, source: bars.length >= 100 ? "live web exchange feed (Binance/NYMEX-CME)" : "unavailable" };
}

async function backtestPair(spec: MarketSpec): Promise<void> {
  const { bars, source } = await loadM15(spec);
  if (bars.length < 100) {
    console.log(`${spec.key}: insufficient history on every source (${bars.length} M15 bars) — skipped`);
    return;
  }
  const sigs: BtSignal[] = [];
  for (let i = 60; i < bars.length - 1; i += CFG.stride) {
    const raw = detectSfp(spec.key, bars, i);
    if (!raw) continue;
    // deterministic limit fill: entry = the signal bar's close only for
    // market entries; limit entries must be touched by a later bar
    if (raw.entryType === "limit") {
      let filled = false;
      for (let j = i + 1; j < Math.min(bars.length, i + 7); j++) {
        const b = bars[j];
        if (raw.dir === "BUY" ? b.l <= raw.entry : b.h >= raw.entry) { filled = true; break; }
      }
      if (!filled) continue;
    }
    sigs.push(resolve(raw, bars, i));
  }

  const won = sigs.filter((s) => s.status === "won");
  const lost = sigs.filter((s) => s.status === "lost");
  const expired = sigs.filter((s) => s.status === "expired");
  const grossWin = won.reduce((a, s) => a + s.resultR, 0);
  const grossLoss = Math.abs(lost.reduce((a, s) => a + s.resultR, 0));
  const totalR = sigs.reduce((a, s) => a + s.resultR, 0);
  const wr = sigs.length ? (won.length / (won.length + lost.length || 1)) * 100 : 0;
  const pf = grossLoss > 0 ? grossWin / grossLoss : grossWin > 0 ? Infinity : 0;
  const first = bars[0].t, last = bars[bars.length - 1].t;
  console.log(`${spec.key}  (${spec.mt5}, ${bars.length} REAL M15 bars, ${new Date(first * 1000).toISOString().slice(0, 10)} → ${new Date(last * 1000).toISOString().slice(0, 10)})`);
  console.log(`  source: ${source}`);
  console.log(
    `  signals ${sigs.length} · won ${won.length} · lost ${lost.length} · expired ${expired.length}` +
    ` · WR ${wr.toFixed(1)}% · PF ${Number.isFinite(pf) ? pf.toFixed(2) : "∞"} · total ${totalR >= 0 ? "+" : ""}${totalR.toFixed(2)}R`);
  const buys = sigs.filter((s) => s.dir === "BUY").length;
  console.log(`  BUY ${buys} / SELL ${sigs.length - buys} · limit ${sigs.filter((s) => s.entryType === "limit").length} / market ${sigs.filter((s) => s.entryType === "market").length}`);
  console.log("");
}

console.log(`================================================================`);
console.log(` DEEP BACKTEST — up to ${DAYS} days of REAL M15 history`);
console.log(` Source ladder: local MT5 (Exness-MT5Trial6/414350770) → AURUM Terminal`);
console.log(` remote MT5 bridge (u1m7j8csutd1-d.space-z.ai) → live web exchanges`);
console.log(` Engine: SFP (swing-failure-pivot) · RR ${CFG.rr} · expiry ${CFG.expiry_bars} bars · pessimistic both-touch`);
console.log(`================================================================`);
console.log("");
for (const spec of MARKET_SPECS) {
  await backtestPair(spec);
}
console.log(`Every bar above came from a REAL venue — the broker terminal (local or`);
console.log(`via the AURUM bridge) or a real exchange. No random data, no fabrication.`);
