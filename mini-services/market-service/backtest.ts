/**
 * DEEP BACKTEST — 100% real MetaTrader 5 history.
 *
 * Loads ~90 days of NATIVE broker M15 bars per pair directly from the MT5
 * terminal (Exness-MT5Trial6 / login 414350770) via the MCP bridge and runs
 * the SAME SFP signal engine the live app uses (swing-failure-pivot detection
 * with ATR guards, deterministic limit-vs-market entry, pessimistic
 * both-touch resolution), then prints honest per-pair performance stats.
 *
 * Run: bun run backtest.ts
 */

import {
  MARKET_SPECS,
  mt5History,
  type Candle,
} from "./src/providers";
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

async function backtestPair(key: string, mt5: string): Promise<void> {
  const nowSec = Math.floor(Date.now() / 1000);
  const from = nowSec - DAYS * 86_400;
  const bars = await mt5History(mt5, "M15", from, nowSec + 900, 20_000);
  if (bars.length < 100) {
    console.log(`${key}: insufficient broker history (${bars.length} M15 bars in ${DAYS}d) — skipped`);
    return;
  }
  const sigs: BtSignal[] = [];
  for (let i = 60; i < bars.length - 1; i += CFG.stride) {
    const raw = detectSfp(key, bars, i);
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
  console.log(`${key}  (${mt5}, ${bars.length} broker M15 bars, ${new Date(first * 1000).toISOString().slice(0, 10)} → ${new Date(last * 1000).toISOString().slice(0, 10)})`);
  console.log(
    `  signals ${sigs.length} · won ${won.length} · lost ${lost.length} · expired ${expired.length}` +
    ` · WR ${wr.toFixed(1)}% · PF ${Number.isFinite(pf) ? pf.toFixed(2) : "∞"} · total ${totalR >= 0 ? "+" : ""}${totalR.toFixed(2)}R`);
  const buys = sigs.filter((s) => s.dir === "BUY").length;
  console.log(`  BUY ${buys} / SELL ${sigs.length - buys} · limit ${sigs.filter((s) => s.entryType === "limit").length} / market ${sigs.filter((s) => s.entryType === "market").length}`);
  console.log("");
}

console.log(`================================================================`);
console.log(` DEEP BACKTEST — ${DAYS} days of REAL MetaTrader 5 M15 history`);
console.log(` Terminal: Exness-MT5Trial6 · login 414350770 · build 6231`);
console.log(` Engine: SFP (swing-failure-pivot) · RR ${CFG.rr} · expiry ${CFG.expiry_bars} bars · pessimistic both-touch`);
console.log(`================================================================`);
console.log("");
for (const spec of MARKET_SPECS) {
  await backtestPair(spec.key, spec.mt5);
}
console.log(`Every bar above was served by the broker through the MT5 terminal —`);
console.log(`no exchange API, no random data, no fallback source was involved.`);
