/**
 * Real market tape — candle store backed by REAL market data (see
 * providers.ts / webfeed.ts). Bars arrive from the MT5 terminal's MCP server
 * (Exness broker feed) or from the live web exchange feed (Binance order
 * books / NYMEX-CME futures), get merged into every timeframe, and the
 * forming bar tracks the venue's own partial candle. Nothing here is
 * generated — when the market is closed (weekend FX/metals), the tape
 * simply stops advancing.
 */

import {
  aggregateCandles,
  TIMEFRAMES,
  type Candle,
  type MarketSpec,
  type SourceSpec,
} from "./providers";

export {
  MARKET_SPECS,
  MARKET_MAP,
  TIMEFRAMES,
  type Candle,
  type MarketSpec,
} from "./providers";

export interface IngestResult {
  /** newly closed M1 candles (ascending) that were appended this call */
  newClosed: Candle[];
  /** TF → last candle that CLOSED its bucket among the new M1 closes */
  tfClosed: Map<string, Candle>;
}

export class Tape {
  readonly spec: MarketSpec;
  readonly source: SourceSpec;
  /** epoch seconds of the first closed M1 bar */
  readonly originSec: number;
  /** closed candles per timeframe (ascending) */
  private readonly series: Record<string, Candle[]> = {};
  /** the exchange's own partial M1 bar (forming) */
  forming: Candle | null = null;
  /** last time real data touched this tape (ms) */
  lastDataMs = 0;

  constructor(spec: MarketSpec, source: SourceSpec, history: Record<string, Candle[]>) {
    this.spec = spec;
    this.source = source;
    this.series.M1 = history.M1 ?? [];
    this.series.M5 = history.M5 ?? aggregateCandles(this.series.M1, 5);
    this.series.M15 = history.M15 ?? aggregateCandles(this.series.M1, 15);
    this.series.M30 = history.M30 ?? aggregateCandles(this.series.M1, 30);
    for (const tf of ["H1", "H4", "D1"] as const) {
      this.series[tf] = history[tf] ?? [];
    }
    this.originSec = this.series.M1.length ? this.series.M1[0].t : Math.floor(Date.now() / 1000);
    this.lastDataMs = Date.now();
  }

  /** Merge newly closed M1 bars + the forming partial bar from the exchange. */
  ingest(closed: Candle[], forming: Candle | null): IngestResult {
    const newClosed: Candle[] = [];
    const tfClosed = new Map<string, Candle>();
    const m1 = this.series.M1;
    for (const c of closed) {
      const last = m1[m1.length - 1];
      if (last && c.t <= last.t) continue; // duplicate / stale
      m1.push(c);
      newClosed.push(c);
      tfClosed.set("M1", c);
      for (const [tf, tfMin] of Object.entries(TIMEFRAMES)) {
        if (tfMin === 1) continue;
        const tfSec = tfMin * 60;
        const bucket = Math.floor(c.t / tfSec) * tfSec;
        const s = (this.series[tf] ??= []);
        const tail = s[s.length - 1];
        if (tail && tail.t === bucket) {
          tail.h = Math.max(tail.h, c.h);
          tail.l = Math.min(tail.l, c.l);
          tail.c = c.c;
          tail.v += c.v;
        } else {
          s.push({ t: bucket, o: c.o, h: c.h, l: c.l, c: c.c, v: c.v });
        }
        if ((c.t + 60) % tfSec === 0) {
          tfClosed.set(tf, { ...(s[s.length - 1] as Candle) });
        }
      }
    }
    if (forming) {
      const last = m1[m1.length - 1];
      const ok = !last || forming.t > last.t;
      if (ok) this.forming = forming;
    }
    if (newClosed.length || forming) this.lastDataMs = Date.now();
    return { newClosed, tfClosed };
  }

  /** True when the exchange is producing fresh bars/ticks (vs market closed). */
  get live(): boolean {
    return Date.now() - this.lastDataMs < 120_000;
  }

  lastPrice(): number {
    if (this.forming) return this.forming.c;
    const m1 = this.series.M1;
    return m1.length ? m1[m1.length - 1].c : 0;
  }

  /** Last `limit` CLOSED candles of `tf` (ascending). */
  getClosed(tf: string, limit: number): Candle[] {
    const s = this.series[tf] ?? [];
    if (limit >= s.length) return s.slice();
    return s.slice(s.length - limit);
  }

  /**
   * The forming bucket of `tf` — closed M1s of the current bucket plus the
   * exchange's partial M1. Returns null when the bucket has no data yet.
   */
  formingBucket(tf: string): Candle | null {
    const tfSec = (TIMEFRAMES[tf] ?? 1) * 60;
    const ref = this.forming?.t ?? this.bucketAfterLastClosed();
    if (ref == null) return null;
    const bucket = Math.floor(ref / tfSec) * tfSec;
    let o: number | null = null;
    let h = -Infinity;
    let l = Infinity;
    let v = 0;
    let c: number | null = null;
    const m1 = this.series.M1;
    for (let i = m1.length - 1; i >= 0; i--) {
      if (m1[i].t < bucket) break;
      if (o === null) o = m1[i].o;
      h = Math.max(h, m1[i].h);
      l = Math.min(l, m1[i].l);
      v += m1[i].v;
      c = m1[i].c;
    }
    if (this.forming && Math.floor(this.forming.t / tfSec) * tfSec === bucket) {
      if (o === null) o = this.forming.o;
      h = Math.max(h, this.forming.h);
      l = Math.min(l, this.forming.l);
      v += this.forming.v;
      c = this.forming.c;
    }
    if (o === null || c === null || !Number.isFinite(h)) return null;
    return { t: bucket, o, h, l, c, v };
  }

  /** Open time (sec) of the bucket right after the last closed M1 bar. */
  private bucketAfterLastClosed(): number | null {
    const m1 = this.series.M1;
    if (!m1.length) return null;
    return m1[m1.length - 1].t + 60;
  }
}
