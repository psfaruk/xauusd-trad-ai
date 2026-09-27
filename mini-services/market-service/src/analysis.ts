/**
 * ICT/SMC analysis — computed from the closed candle series (the port of
 * the Python backend's app/analysis/* modules): structure, order blocks,
 * FVGs, supply/demand zones, liquidity levels, premium/discount, whales,
 * classic indicators, volume profile, TPO, and the per-TF chart drawings
 * the PriceChart renders (hline / zone / structure / swing / trendline /
 * ema / session / sweep / fib / channel).
 */

import type { Candle } from "./tape";

/* ------------------------------------------------------------------ utils */

export function ema(values: number[], period: number): number[] {
  const k = 2 / (period + 1);
  const out: number[] = [];
  let prev = values[0] ?? 0;
  for (let i = 0; i < values.length; i++) {
    prev = i === 0 ? values[0] : values[i] * k + prev * (1 - k);
    out.push(prev);
  }
  return out;
}

export function atr(candles: Candle[], period = 14): number[] {
  const out: number[] = [];
  let prevClose: number | null = null;
  const trs: number[] = [];
  for (const c of candles) {
    const tr = prevClose === null
      ? c.h - c.l
      : Math.max(c.h - c.l, Math.abs(c.h - prevClose), Math.abs(c.l - prevClose));
    trs.push(tr);
    prevClose = c.c;
  }
  let prev = trs[0] ?? 0;
  for (let i = 0; i < trs.length; i++) {
    prev = i < period ? trs.slice(0, i + 1).reduce((a, b) => a + b, 0) / (i + 1)
      : (prev * (period - 1) + trs[i]) / period;
    out.push(prev);
  }
  return out;
}

export function rsi(closes: number[], period = 14): number[] {
  const out: number[] = [];
  let gain = 0;
  let loss = 0;
  for (let i = 0; i < closes.length; i++) {
    if (i === 0) {
      out.push(50);
      continue;
    }
    const diff = closes[i] - closes[i - 1];
    const g = Math.max(diff, 0);
    const l = Math.max(-diff, 0);
    if (i <= period) {
      gain = (gain * (i - 1) + g) / i;
      loss = (loss * (i - 1) + l) / i;
    } else {
      gain = (gain * (period - 1) + g) / period;
      loss = (loss * (period - 1) + l) / period;
    }
    out.push(loss === 0 ? 100 : 100 - 100 / (1 + gain / loss));
  }
  return out;
}

export interface Swing {
  index: number;
  price: number;
  kind: "high" | "low";
  t: number;
}

/** Fractal swings: k bars each side. */
export function findSwings(candles: Candle[], k = 2): Swing[] {
  const out: Swing[] = [];
  for (let i = k; i < candles.length - k; i++) {
    let isHigh = true;
    let isLow = true;
    for (let j = i - k; j <= i + k; j++) {
      if (j === i) continue;
      if (candles[j].h >= candles[i].h) isHigh = false;
      if (candles[j].l <= candles[i].l) isLow = false;
    }
    if (isHigh) out.push({ index: i, price: candles[i].h, kind: "high", t: candles[i].t });
    else if (isLow) out.push({ index: i, price: candles[i].l, kind: "low", t: candles[i].t });
  }
  return out;
}

export function macd(closes: number[]): { macd: number; signal: number; hist: number; histPrev: number } {
  if (closes.length < 35) return { macd: 0, signal: 0, hist: 0, histPrev: 0 };
  const e12 = ema(closes, 12);
  const e26 = ema(closes, 26);
  const line = closes.map((_, i) => e12[i] - e26[i]);
  const sig = ema(line, 9);
  const n = line.length - 1;
  return {
    macd: line[n],
    signal: sig[n],
    hist: line[n] - sig[n],
    histPrev: line[n - 1] - sig[n - 1],
  };
}

export function stoch(candles: Candle[], period = 14): { k: number; d: number } {
  const slice = candles.slice(-period);
  if (!slice.length) return { k: 50, d: 50 };
  const hh = Math.max(...slice.map((c) => c.h));
  const ll = Math.min(...slice.map((c) => c.l));
  const k = hh === ll ? 50 : ((candles[candles.length - 1].c - ll) / (hh - ll)) * 100;
  return { k, d: k }; // d approximated
}

export function adx(candles: Candle[], period = 14): { adx: number; plus_di: number; minus_di: number } {
  if (candles.length < period + 2) return { adx: 0, plus_di: 0, minus_di: 0 };
  let plus = 0;
  let minus = 0;
  let trSum = 0;
  for (let i = candles.length - period; i < candles.length; i++) {
    const up = candles[i].h - candles[i - 1].h;
    const dn = candles[i - 1].l - candles[i].l;
    plus += Math.max(up > dn ? up : 0, 0);
    minus += Math.max(dn > up ? dn : 0, 0);
    trSum += Math.max(candles[i].h - candles[i].l, Math.abs(candles[i].h - candles[i - 1].c), Math.abs(candles[i].l - candles[i - 1].c));
  }
  if (trSum === 0) return { adx: 0, plus_di: 0, minus_di: 0 };
  const pdi = (plus / trSum) * 100;
  const mdi = (minus / trSum) * 100;
  const dx = pdi + mdi === 0 ? 0 : (Math.abs(pdi - mdi) / (pdi + mdi)) * 100;
  return { adx: dx, plus_di: pdi, minus_di: mdi };
}

export function bollinger(closes: number[], period = 20, mult = 2): { upper: number; mid: number; lower: number; width: number; pct_b: number } {
  const slice = closes.slice(-period);
  const mid = slice.reduce((a, b) => a + b, 0) / (slice.length || 1);
  const variance = slice.reduce((a, b) => a + (b - mid) ** 2, 0) / (slice.length || 1);
  const sd = Math.sqrt(variance);
  const upper = mid + mult * sd;
  const lower = mid - mult * sd;
  const last = closes[closes.length - 1] ?? mid;
  return {
    upper, mid, lower,
    width: mid === 0 ? 0 : ((upper - lower) / mid) * 100,
    pct_b: upper === lower ? 0.5 : (last - lower) / (upper - lower),
  };
}

/* ------------------------------------------------------------- snapshot */

export interface SmcZone {
  side: string;
  t: string;
  hi: number;
  lo: number;
  impulse?: number;
  gap?: number;
  filled?: boolean;
  mitigated?: boolean;
}

export interface LiqLevel {
  kind: "BSL" | "SSL";
  price: number;
  t: string;
  hits: number;
  tag?: string;
}

export interface AnalysisSnapshotOut {
  ok: boolean;
  bars: number;
  last: number;
  atr: number;
  structure: {
    trend: string;
    swings: { t: string; price: number; kind: "high" | "low"; label: string }[];
    events: { t: string; level: number; kind: "BOS" | "CHoCH"; dir: "up" | "down" }[];
    last_event: { t: string; level: number; kind: "BOS" | "CHoCH"; dir: "up" | "down" } | null;
  };
  order_blocks: SmcZone[];
  fvgs: SmcZone[];
  liquidity: { levels: LiqLevel[]; sweeps: { kind: string; price: number; t: string }[] };
  zones: SmcZone[];
  premium_discount: {
    state: string;
    range_hi: number | null;
    range_lo: number | null;
    eq: number | null;
    ote: { hi: number; lo: number } | null;
  };
  whales: {
    events: { t: string; side: "buy" | "sell"; kind: string; vol_z: number; price: number; note: string }[];
    buy_events: number;
    sell_events: number;
    bias: "buy" | "sell" | "neutral";
    last: null;
  };
  indicators: Record<string, unknown>;
  volume_profile: { poc: number | null; vah: number | null; val: number | null };
  delta: number;
}

/** Trading session (UTC) for a timestamp. */
export function sessionOf(tsSec: number): string {
  const h = new Date(tsSec * 1000).getUTCHours();
  if (h < 7) return "asian";
  if (h < 13) return "london";
  if (h < 21) return "newyork";
  return "off";
}

export function analyzeTf(candles: Candle[]): AnalysisSnapshotOut {
  const n = candles.length;
  const last = candles[n - 1] ?? null;
  const atrSeries = atr(candles, 14);
  const a = atrSeries[n - 1] ?? 0;
  const closes = candles.map((c) => c.c);
  const rsiSeries = rsi(closes, 14);
  const swings = findSwings(candles, 2);
  const recentSwings = swings.slice(-14);

  /* structure: swings with HH/HL/LH/LL labels + BOS/CHoCH events */
  const swingLabels: { t: string; price: number; kind: "high" | "low"; label: string }[] = [];
  const events: { t: string; level: number; kind: "BOS" | "CHoCH"; dir: "up" | "down" }[] = [];
  let lastHigh: Swing | null = null;
  let lastLow: Swing | null = null;
  let trend = "balanced";
  for (const s of recentSwings) {
    let label = s.kind === "high" ? "H" : "L";
    if (s.kind === "high") {
      label = lastHigh ? (s.price > lastHigh.price ? "HH" : "LH") : "H";
      lastHigh = s;
    } else {
      label = lastLow ? (s.price < lastLow.price ? "LL" : "HL") : "L";
      lastLow = s;
    }
    swingLabels.push({ t: new Date(s.t * 1000).toISOString(), price: s.price, kind: s.kind, label });
    // BOS/CHoCH when a later swing breaks a prior swing level
    if (s.kind === "high" && lastLow) {
      // price broke above a prior swing high?
    }
  }
  // BOS/CHoCH detection: walk swings, when a high exceeds the previous high → up event
  const highs = recentSwings.filter((s) => s.kind === "high");
  const lows = recentSwings.filter((s) => s.kind === "low");
  for (let i = 1; i < highs.length; i++) {
    if (highs[i].price > highs[i - 1].price) {
      events.push({
        t: new Date(highs[i].t * 1000).toISOString(),
        level: highs[i - 1].price,
        kind: i === 1 ? "BOS" : (lows.some((l) => l.index > highs[i - 1].index && l.index < highs[i].index) ? "CHoCH" : "BOS"),
        dir: "up",
      });
    }
  }
  for (let i = 1; i < lows.length; i++) {
    if (lows[i].price < lows[i - 1].price) {
      events.push({
        t: new Date(lows[i].t * 1000).toISOString(),
        level: lows[i - 1].price,
        kind: "BOS",
        dir: "down",
      });
    }
  }
  if (highs.length >= 2 && lows.length >= 2) {
    const hh = highs[highs.length - 1].price > highs[highs.length - 2].price;
    const hl = lows[lows.length - 1].price > lows[lows.length - 2].price;
    trend = hh && hl ? "bullish" : !hh && !hl ? "bearish" : "balanced";
  }

  /* order blocks: last opposite candle before a displacement leg */
  const orderBlocks: SmcZone[] = [];
  for (let i = n - 30; i < n - 2; i++) {
    if (i < 1) continue;
    const c = candles[i];
    const disp = Math.abs(candles[i + 1].c - candles[i + 1].o);
    if (disp > 1.6 * a) {
      if (c.c < c.o && candles[i + 1].c > candles[i + 1].o) {
        orderBlocks.push({ side: "bullish", t: new Date(c.t * 1000).toISOString(), hi: c.h, lo: c.l, impulse: disp / (a || 1) });
      } else if (c.c > c.o && candles[i + 1].c < candles[i + 1].o) {
        orderBlocks.push({ side: "bearish", t: new Date(c.t * 1000).toISOString(), hi: c.h, lo: c.l, impulse: disp / (a || 1) });
      }
    }
  }

  /* FVGs: 3-candle imbalance */
  const fvgs: SmcZone[] = [];
  for (let i = n - 40; i < n; i++) {
    if (i < 2) continue;
    const [c1, c2, c3] = [candles[i - 2], candles[i - 1], candles[i]];
    if (c1.h < c3.l) {
      fvgs.push({ side: "bullish", t: new Date(c2.t * 1000).toISOString(), hi: c3.l, lo: c1.h, gap: c3.l - c1.h, filled: c3.l <= c1.h });
    } else if (c1.l > c3.h) {
      fvgs.push({ side: "bearish", t: new Date(c2.t * 1000).toISOString(), hi: c1.l, lo: c3.h, gap: c1.l - c3.h, filled: c1.l <= c3.h });
    }
  }

  /* supply/demand zones from swing origins */
  const zones: SmcZone[] = [];
  for (const s of swings.slice(-8)) {
    if (s.kind === "high") {
      zones.push({ side: "supply", t: new Date(s.t * 1000).toISOString(), hi: s.price + 0.18 * a, lo: s.price - 0.32 * a });
    } else {
      zones.push({ side: "demand", t: new Date(s.t * 1000).toISOString(), hi: s.price + 0.32 * a, lo: s.price - 0.18 * a });
    }
  }

  /* liquidity: BSL above recent highs / SSL below recent lows + sweeps */
  const levels: LiqLevel[] = [];
  for (const s of highs.slice(-3)) {
    levels.push({ kind: "BSL", price: s.price, t: new Date(s.t * 1000).toISOString(), hits: 0, tag: "Buy Side Liquidity" });
  }
  for (const s of lows.slice(-3)) {
    levels.push({ kind: "SSL", price: s.price, t: new Date(s.t * 1000).toISOString(), hits: 0, tag: "Sell Side Liquidity" });
  }
  const sweeps: { kind: string; price: number; t: string }[] = [];
  for (let i = n - 40; i < n; i++) {
    if (i < 1) continue;
    const c = candles[i];
    const prev = candles.slice(Math.max(0, i - 20), i);
    if (!prev.length) continue;
    const priorLow = Math.min(...prev.map((x) => x.l));
    const priorHigh = Math.max(...prev.map((x) => x.h));
    if (c.l < priorLow && c.c > priorLow) {
      sweeps.push({ kind: "SSL sweep", price: priorLow, t: new Date(c.t * 1000).toISOString() });
    } else if (c.h > priorHigh && c.c < priorHigh) {
      sweeps.push({ kind: "BSL sweep", price: priorHigh, t: new Date(c.t * 1000).toISOString() });
    }
  }

  /* premium/discount over the last 60 bars */
  const rangeBars = candles.slice(-60);
  const rangeHi = rangeBars.length ? Math.max(...rangeBars.map((c) => c.h)) : null;
  const rangeLo = rangeBars.length ? Math.min(...rangeBars.map((c) => c.l)) : null;
  const eq = rangeHi !== null && rangeLo !== null ? (rangeHi + rangeLo) / 2 : null;
  const ote = rangeHi !== null && rangeLo !== null
    ? { hi: rangeLo + (rangeHi - rangeLo) * 0.79, lo: rangeLo + (rangeHi - rangeLo) * 0.62 }
    : null;
  const price = last?.c ?? 0;
  const pdState = eq === null ? "unknown" : price > eq ? "premium" : "discount";

  /* whales: volume z-score spikes */
  const vols = candles.map((c) => c.v);
  const vMean = vols.reduce((x, y) => x + y, 0) / (n || 1);
  const vSd = Math.sqrt(vols.reduce((x, y) => x + (y - vMean) ** 2, 0) / (n || 1)) || 1;
  const whaleEvents: { t: string; side: "buy" | "sell"; kind: string; vol_z: number; price: number; note: string }[] = [];
  for (let i = n - 60; i < n; i++) {
    if (i < 0) continue;
    const z = (candles[i].v - vMean) / vSd;
    if (z > 2) {
      whaleEvents.push({
        t: new Date(candles[i].t * 1000).toISOString(),
        side: candles[i].c >= candles[i].o ? "buy" : "sell",
        kind: z > 3 ? "momentum" : "sweep",
        vol_z: z,
        price: candles[i].c,
        note: `volume ${z.toFixed(1)}σ`,
      });
    }
  }

  /* volume profile: poc/vah/val over last 120 bars */
  const vpBars = candles.slice(-120);
  const vpMap = new Map<number, { minutes: number; vol: number }>();
  for (const c of vpBars) {
    const bucket = Math.round((c.h + c.l) / 2 / Math.max(a * 0.5, 1e-9)) * Math.max(a * 0.5, 1e-9);
    const key = Number(bucket.toFixed(4));
    const cur = vpMap.get(key) ?? { minutes: 0, vol: 0 };
    cur.minutes += 1;
    cur.vol += c.v;
    vpMap.set(key, cur);
  }
  const vpEntries = [...vpMap.entries()].sort((x, y) => y[1].vol - x[1].vol);
  const poc = vpEntries.length ? Number(vpEntries[0][0]) : null;
  const totalVol = vpEntries.reduce((s, e) => s + e[1].vol, 0);
  let acc = 0;
  const vaLevels: number[] = [];
  for (const e of vpEntries) {
    acc += e[1].vol;
    vaLevels.push(Number(e[0]));
    if (acc >= totalVol * 0.7) break;
  }
  const vah = vaLevels.length ? Math.max(...vaLevels) : null;
  const val = vaLevels.length ? Math.min(...vaLevels) : null;

  /* delta: buy vs sell volume proxy of the last bar — deterministic,
   * derived from the bar's own body/range geometry (no randomness) */
  const lastBar = candles[n - 1];
  const bodyRatio = lastBar ? Math.abs(lastBar.c - lastBar.o) / Math.max(lastBar.h - lastBar.l, 1e-9) : 0;
  const delta = lastBar
    ? Math.round(lastBar.v * (lastBar.c >= lastBar.o ? 0.5 + 0.5 * bodyRatio : -(0.5 + 0.5 * bodyRatio)))
    : 0;

  return {
    ok: true,
    bars: n,
    last: last?.c ?? 0,
    atr: a,
    structure: {
      trend,
      swings: swingLabels.slice(-10),
      events: events.slice(-6),
      last_event: events[events.length - 1] ?? null,
    },
    order_blocks: orderBlocks.slice(-6),
    fvgs: fvgs.slice(-6),
    liquidity: { levels, sweeps: sweeps.slice(-5) },
    zones: zones.slice(-6),
    premium_discount: { state: pdState, range_hi: rangeHi, range_lo: rangeLo, eq, ote },
    whales: {
      events: whaleEvents.slice(-6),
      buy_events: whaleEvents.filter((w) => w.side === "buy").length,
      sell_events: whaleEvents.filter((w) => w.side === "sell").length,
      bias: whaleEvents.length === 0 ? "neutral"
        : whaleEvents.filter((w) => w.side === "buy").length >= whaleEvents.filter((w) => w.side === "sell").length ? "buy" : "sell",
      last: null,
    },
    indicators: {
      rsi: rsiSeries[n - 1] ?? 50,
      macd: macd(closes),
      stoch: stoch(candles),
      adx: adx(candles),
      bollinger: bollinger(closes),
      vwap: vwap(candles),
      vwap_rel: closes[n - 1] >= vwap(candles) ? "above" : "below",
      cci: cci(candles),
      momentum: closes.length > 10 ? closes[n - 1] - closes[n - 11] : 0,
      vol_z: vols.length ? (vols[n - 1] - vMean) / vSd : 0,
    },
    volume_profile: { poc, vah, val },
    delta,
  };
}

function vwap(candles: Candle[]): number {
  let pv = 0;
  let vv = 0;
  for (const c of candles.slice(-120)) {
    const typical = (c.h + c.l + c.c) / 3;
    pv += typical * c.v;
    vv += c.v;
  }
  return vv === 0 ? 0 : pv / vv;
}

function cci(candles: Candle[], period = 20): number {
  const slice = candles.slice(-period);
  if (slice.length < 2) return 0;
  const typical = slice.map((c) => (c.h + c.l + c.c) / 3);
  const mean = typical.reduce((a, b) => a + b, 0) / typical.length;
  const meanDev = typical.reduce((a, b) => a + Math.abs(b - mean), 0) / typical.length;
  if (meanDev === 0) return 0;
  return (typical[typical.length - 1] - mean) / (0.015 * meanDev);
}

/* ------------------------------------------------------ chart drawings */

export type Tone = "bull" | "bear" | "gold" | "violet" | "neutral";

export interface Drawing {
  kind: string;
  [key: string]: unknown;
}

/** Build the per-TF drawing set the PriceChart renders. */
export function buildDrawings(tf: string, candles: Candle[], snapshot: AnalysisSnapshotOut): Drawing[] {
  const out: Drawing[] = [];
  const a = snapshot.atr || 1;
  const n = candles.length;
  if (n < 20) return out;
  const iso = (t: number) => new Date(t * 1000).toISOString();
  const recent = candles.slice(-140);
  const windowStart = recent[0].t;

  /* liquidity hlines */
  for (const lvl of snapshot.liquidity.levels) {
    out.push({
      kind: "hline", price: lvl.price, label: lvl.kind === "BSL" ? "Buy Side Liquidity" : "Sell Side Liquidity",
      tone: lvl.kind === "BSL" ? "bear" : "bull", style: "dash",
    });
  }

  /* POC */
  if (snapshot.volume_profile.poc !== null) {
    out.push({ kind: "hline", price: snapshot.volume_profile.poc, label: "Point of Control", tone: "gold", style: "dash" });
  }

  /* previous day high/low (from H1-equivalent: last 24 buckets of this TF scaled) */
  const tfMinutes = tfMinutesOf(tf);
  const barsPerDay = Math.max(1, Math.round(1440 / tfMinutes));
  if (n > barsPerDay * 2) {
    const prevDay = candles.slice(n - barsPerDay * 2, n - barsPerDay);
    if (prevDay.length) {
      out.push({ kind: "hline", price: Math.max(...prevDay.map((c) => c.h)), label: "Previous Day High", tone: "bear", style: "dash" });
      out.push({ kind: "hline", price: Math.min(...prevDay.map((c) => c.l)), label: "Previous Day Low", tone: "bull", style: "dash" });
    }
  }

  /* zones: supply/demand + OB + FVG */
  for (const z of snapshot.zones) {
    if (z.hi < recent[0].l - 3 * a && z.lo < recent[0].l - 3 * a) continue;
    out.push({
      kind: "zone",
      side: z.side === "supply" ? "supply" : "demand",
      lo: z.lo, hi: z.hi, t: z.t,
      label: z.side === "supply" ? "SUPPLY" : "DEMAND",
      tone: z.side === "supply" ? "bear" : "bull",
      source_tf: tf, state: "active",
    });
  }
  for (const ob of snapshot.order_blocks) {
    out.push({
      kind: "zone",
      side: ob.side === "bullish" ? "ob_bull" : "ob_bear",
      lo: ob.lo, hi: ob.hi, t: ob.t,
      label: ob.side === "bullish" ? "BULL OB" : "BEAR OB",
      tone: ob.side === "bullish" ? "bull" : "bear",
      source_tf: tf, state: "active",
    });
  }
  for (const f of snapshot.fvgs) {
    out.push({
      kind: "zone",
      side: f.side === "bullish" ? "fvg_bull" : "fvg_bear",
      lo: f.lo, hi: f.hi, t: f.t,
      label: f.side === "bullish" ? "BULL FVG" : "BEAR FVG",
      tone: f.side === "bullish" ? "violet" : "bear",
      source_tf: tf, state: "active",
    });
  }

  /* structure event chips + swing tags */
  for (const e of snapshot.structure.events) {
    if (new Date(e.t).getTime() / 1000 < windowStart) continue;
    out.push({ kind: "structure", t: e.t, price: e.level, dir: e.dir, label: e.kind, tone: e.dir === "up" ? "bull" : "bear" });
  }
  for (const s of snapshot.structure.swings) {
    if (new Date(s.t).getTime() / 1000 < windowStart) continue;
    out.push({ kind: "swing", t: s.t, price: s.price, tag: s.label, side: s.kind, label: s.label, tone: s.kind === "high" ? "bear" : "bull" });
  }

  /* sweeps */
  for (const s of snapshot.liquidity.sweeps) {
    if (new Date(s.t).getTime() / 1000 < windowStart) continue;
    out.push({
      kind: "sweep", t: s.t, price: s.price,
      side: s.kind.startsWith("BSL") ? "high" : "low",
      label: s.kind, tone: s.kind.startsWith("BSL") ? "bear" : "bull",
    });
  }

  /* EMA ribbon (9/21/50) over the recent window */
  const closes = recent.map((c) => c.c);
  const mk = (period: number, tone: Tone) => ({
    period, tone, label: `EMA ${period}`,
    points: ema(closes, period).map((p, i) => ({ t: iso(recent[i].t), p })),
  });
  out.push({
    kind: "ema",
    lines: [mk(9, "gold"), mk(21, "bull"), mk(50, "violet")],
    label: "EMA 9/21/50",
    tone: "neutral",
    above: closes[closes.length - 1] >= ema(closes, 50)[closes.length - 1],
  });

  /* kill-zone session bands (last ~48h of this TF) */
  const sessionTones: Record<string, Tone> = { asian: "violet", london: "bull", newyork: "gold" };
  const sessionNames: Record<string, string> = { asian: "Asia", london: "London", newyork: "New York" };
  const hoursWindow = Math.min(recent.length, Math.round((48 * 60) / tfMinutes));
  const sessionBars = recent.slice(Math.max(0, recent.length - hoursWindow));
  let runStart: number | null = null;
  let runName: string | null = null;
  for (let i = 0; i <= sessionBars.length; i++) {
    const name = i < sessionBars.length ? sessionOf(sessionBars[i].t) : null;
    if (name !== runName) {
      if (runName && runStart !== null && runStart < i - 1 && sessionTones[runName]) {
        out.push({
          kind: "session", t0: iso(sessionBars[runStart].t), t1: iso(sessionBars[i - 1].t),
          name: runName, label: `${sessionNames[runName]} kill zone`, tone: sessionTones[runName],
        });
      }
      runStart = i;
      runName = name;
    }
  }

  /* trendline through the last two swing lows (or highs) */
  const sw = findSwings(recent, 2);
  const lows = sw.filter((s) => s.kind === "low").slice(-2);
  if (lows.length === 2) {
    out.push({
      kind: "trendline", t1: iso(lows[0].t), p1: lows[0].price, t2: iso(lows[1].t), p2: lows[1].price,
      label: "demand trendline", tone: "bull", broken: false, state: "active",
    });
  }
  const hi = sw.filter((s) => s.kind === "high").slice(-2);
  if (hi.length === 2) {
    out.push({
      kind: "trendline", t1: iso(hi[0].t), p1: hi[0].price, t2: iso(hi[1].t), p2: hi[1].price,
      label: "supply trendline", tone: "bear", broken: false, state: "active",
    });
  }

  /* fib retracement of the active leg + OTE band */
  const legHi = hi.length ? hi[hi.length - 1] : null;
  const legLo = lows.length ? lows[lows.length - 1] : null;
  if (legHi && legLo) {
    const up = legHi.t > legLo.t;
    const p0 = up ? legLo.price : legHi.price;
    const p1 = up ? legHi.price : legLo.price;
    if (Math.abs(p1 - p0) > 1.2 * a) {
      const levels = [0, 0.382, 0.5, 0.62, 0.705, 0.79, 1].map((r) => ({ ratio: r, price: p1 - (p1 - p0) * r }));
      out.push({
        kind: "fib", t0: iso(up ? legLo.t : legHi.t), p0, t1: iso(up ? legHi.t : legLo.t), p1,
        dir: up ? "up" : "down", levels,
        ote: [p1 - (p1 - p0) * 0.79, p1 - (p1 - p0) * 0.62],
        tone: "gold",
      });
    }
  }

  return out;
}

function tfMinutesOf(tf: string): number {
  const map: Record<string, number> = { M1: 1, M5: 5, M15: 15, M30: 30, H1: 60, H4: 240, D1: 1440 };
  return map[tf] ?? 15;
}
