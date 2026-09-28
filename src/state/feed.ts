'use client';

/**
 * FeedStore (D-041) — external store for FAST market data (ticks + forming
 * bars) pushed straight from the WebSocket into leaf components via
 * useSyncExternalStore.
 *
 * WHY: the previous Dashboard kept `lastPrice` in root state — every tick
 * frame (up to 20/s) re-rendered the ENTIRE app tree (that was the "app is
 * slow" report). With this store only the tiny subscribed components
 * (price text, chart imperative updates) react to ticks.
 */

import { useSyncExternalStore } from "react";
import type {
  Candle, Timeframe, WsBarMsg, WsStrategyPulseMsg, WsTickMsg,
} from "../types";

export interface TickSnapshot {
  bid: number;
  ask: number;
  ts: number;
  tps: number | null;
  n: number | null;
  /** local receive time (ms) — staleness detection */
  at: number;
}

/* ------------------------------------------------- D-082 running-candle tape */

/** One raw tape tick (tick-rule direction computed vs the previous mid). */
export interface TapePoint {
  /** venue mid price (bid+ask)/2 */
  mid: number;
  bid: number;
  ask: number;
  /** receive time (ms, local) */
  at: number;
  /** venue timestamp (seconds) */
  ts: number;
  /** +1 up-tick, -1 down-tick, 0 unchanged vs previous mid */
  dir: 1 | -1 | 0;
  /** absolute price jump from the previous tick (in price units) */
  jump: number;
}

/** Micro-structure accumulator for the RUNNING M1 candle (D-082). */
export interface TapeStats {
  /** receive-time bucket start (ms, floor to the minute) */
  bucketStart: number;
  /** up-ticks inside this candle */
  up: number;
  /** down-ticks inside this candle */
  dn: number;
  /** unchanged ticks inside this candle */
  flat: number;
  /** summed up price distance ($) — "how hard buyers pushed" */
  upDist: number;
  /** summed down price distance ($) — "how hard sellers pushed" */
  dnDist: number;
  /** current consecutive same-direction run (+ = buy run) */
  streak: number;
  /** direction of the very last tick */
  lastDir: 1 | -1 | 0;
  /** cumulative (up - dn) tick delta after each tick — the candle footprint */
  cum: number[];
  /** trailing tape (newest last) — capped, feeds the flowing chip row */
  points: TapePoint[];
  /** venue tick rate from the frame */
  tps: number | null;
  /** receive time of the last tick (ms) */
  lastAt: number;
  /** ticks that belong to this candle */
  candleTicks: number;
  /** D-082 — the running candle rebuilt from THIS stream's ticks (mid open,
   * ask-touch high, bid-touch low, mid close): subscription-independent,
   * so the microscope works on every selected timeframe. */
  o: number;
  h: number;
  l: number;
  c: number;
}

const TAPE_POINTS_CAP = 90;
const TAPE_CUM_CAP = 480;

type Listener = () => void;

class FeedStore {
  private ticks = new Map<string, TickSnapshot>();
  private tickListeners = new Map<string, Set<Listener>>();
  private bars = new Map<string, Candle>();
  private barListeners = new Map<string, Set<Listener>>();
  /** D-051 — latest strategy_pulse frame per symbol (M1-close cadence). */
  private pulses = new Map<string, WsStrategyPulseMsg>();
  private pulseListeners = new Map<string, Set<Listener>>();
  /** D-082 — running-candle micro-structure per symbol (tick-rule tape). */
  private tapes = new Map<string, TapeStats>();
  private tapeListeners = new Map<string, Set<Listener>>();
  /** previous mid per symbol — the tick-rule reference */
  private prevMid = new Map<string, number>();

  /* ------------------------------------------------------------- ingest */

  pushTick(msg: WsTickMsg): void {
    if (!Number.isFinite(msg.bid) || !Number.isFinite(msg.ask)) return;
    const at = Date.now();
    this.ticks.set(msg.symbol, {
      bid: msg.bid,
      ask: msg.ask,
      ts: msg.ts,
      tps: msg.tps ?? null,
      n: msg.n ?? null,
      at,
    });
    this.tickListeners.get(msg.symbol)?.forEach((l) => l());
    this.pushTapePoint(msg.symbol, msg, at);
  }

  /**
   * D-082 — the tape-reading engine: every tick is classified with the
   * tick rule (up/down vs the previous mid — the classic tape-read for
   * "এই ক্যান্ডেল এ বায়ার আছে নাকি সেলার") and folded into the RUNNING
   * candle's micro-structure: aggression counts, pushed distance, streak,
   * cumulative delta footprint and the trailing chip row. A new minute
   * bucket resets the candle. Immutable snapshot per tick (safe for
   * useSyncExternalStore).
   */
  private pushTapePoint(symbol: string, msg: WsTickMsg, at: number): void {
    const mid = (msg.bid + msg.ask) / 2;
    const prev = this.prevMid.get(symbol);
    const dir: 1 | -1 | 0 =
      prev == null || mid === prev ? 0 : mid > prev ? 1 : -1;
    const jump = prev == null ? 0 : Math.abs(mid - prev);
    this.prevMid.set(symbol, mid);

    const bucketStart = Math.floor(at / 60_000) * 60_000;
    const cur = this.tapes.get(symbol);

    let next: TapeStats;
    if (!cur || cur.bucketStart !== bucketStart) {
      // new candle — fresh accumulator (keep the trailing points rolling)
      next = {
        bucketStart,
        up: dir === 1 ? 1 : 0,
        dn: dir === -1 ? 1 : 0,
        flat: dir === 0 ? 1 : 0,
        upDist: dir === 1 ? jump : 0,
        dnDist: dir === -1 ? jump : 0,
        streak: dir === 0 ? 0 : dir,
        lastDir: dir,
        cum: [dir],
        points: cur ? cur.points : [],
        tps: msg.tps ?? null,
        lastAt: at,
        candleTicks: 1,
        o: mid,
        h: Math.max(mid, msg.ask),
        l: Math.min(mid, msg.bid),
        c: mid,
      };
    } else {
      const streak =
        dir === 0 || dir !== cur.lastDir ? dir : cur.streak + dir;
      next = {
        bucketStart: cur.bucketStart,
        up: cur.up + (dir === 1 ? 1 : 0),
        dn: cur.dn + (dir === -1 ? 1 : 0),
        flat: cur.flat + (dir === 0 ? 1 : 0),
        upDist: cur.upDist + (dir === 1 ? jump : 0),
        dnDist: cur.dnDist + (dir === -1 ? jump : 0),
        streak,
        lastDir: dir,
        cum: dir === 0 ? cur.cum : [...cur.cum, cur.cum[cur.cum.length - 1] + dir],
        points: cur.points,
        tps: msg.tps ?? cur.tps,
        lastAt: at,
        candleTicks: cur.candleTicks + 1,
        o: cur.o,
        h: Math.max(cur.h, mid, msg.ask),
        l: Math.min(cur.l, mid, msg.bid),
        c: mid,
      };
    }
    const point: TapePoint = { mid, bid: msg.bid, ask: msg.ask, at, ts: msg.ts, dir, jump };
    next.points = [...next.points, point].slice(-TAPE_POINTS_CAP);
    next.cum = next.cum.slice(-TAPE_CUM_CAP);
    this.tapes.set(symbol, next);
    this.tapeListeners.get(symbol)?.forEach((l) => l());
  }

  pushBar(msg: WsBarMsg): void {
    const c = msg.candle;
    if (
      !c ||
      !Number.isFinite(c.t) || !Number.isFinite(c.o) || !Number.isFinite(c.h) ||
      !Number.isFinite(c.l) || !Number.isFinite(c.c) || c.h < c.l
    ) {
      return; // malformed frame — never let it near the chart
    }
    const key = `${msg.symbol}|${msg.tf}`;
    this.bars.set(key, c);
    this.barListeners.get(key)?.forEach((l) => l());
  }

  /** D-051 — strategy radar frame from the engine (every M1 close). */
  pushPulse(msg: WsStrategyPulseMsg): void {
    if (!msg.symbol) return;
    this.pulses.set(msg.symbol, msg);
    this.pulseListeners.get(msg.symbol)?.forEach((l) => l());
  }

  /** Drop all fast state for a symbol (symbol switch / disconnect). */
  clearSymbol(symbol: string): void {
    this.ticks.delete(symbol);
    this.tapes.delete(symbol);
    this.prevMid.delete(symbol);
    for (const key of [...this.bars.keys()]) {
      if (key.startsWith(`${symbol}|`)) {
        this.bars.delete(key);
        this.barListeners.get(key)?.forEach((l) => l());
      }
    }
    this.tickListeners.get(symbol)?.forEach((l) => l());
    this.tapeListeners.get(symbol)?.forEach((l) => l());
  }

  clearAll(): void {
    this.ticks.clear();
    this.bars.clear();
    this.pulses.clear();
    this.tapes.clear();
    this.prevMid.clear();
    this.tickListeners.forEach((set) => set.forEach((l) => l()));
    this.barListeners.forEach((set) => set.forEach((l) => l()));
    this.pulseListeners.forEach((set) => set.forEach((l) => l()));
    this.tapeListeners.forEach((set) => set.forEach((l) => l()));
  }

  /* ---------------------------------------------------------- subscribe */

  getTick(symbol: string): TickSnapshot | null {
    return this.ticks.get(symbol) ?? null;
  }

  getBar(symbol: string, tf: Timeframe): Candle | null {
    return this.bars.get(`${symbol}|${tf}`) ?? null;
  }

  subscribeTick(symbol: string, listener: Listener): () => void {
    let set = this.tickListeners.get(symbol);
    if (!set) {
      set = new Set();
      this.tickListeners.set(symbol, set);
    }
    set.add(listener);
    return () => set!.delete(listener);
  }

  subscribeBar(symbol: string, tf: Timeframe, listener: Listener): () => void {
    const key = `${symbol}|${tf}`;
    let set = this.barListeners.get(key);
    if (!set) {
      set = new Set();
      this.barListeners.set(key, set);
    }
    set.add(listener);
    return () => set!.delete(listener);
  }

  getPulse(symbol: string): WsStrategyPulseMsg | null {
    return this.pulses.get(symbol) ?? null;
  }

  subscribePulse(symbol: string, listener: Listener): () => void {
    let set = this.pulseListeners.get(symbol);
    if (!set) {
      set = new Set();
      this.pulseListeners.set(symbol, set);
    }
    set.add(listener);
    return () => set!.delete(listener);
  }

  getTape(symbol: string): TapeStats | null {
    return this.tapes.get(symbol) ?? null;
  }

  subscribeTape(symbol: string, listener: Listener): () => void {
    let set = this.tapeListeners.get(symbol);
    if (!set) {
      set = new Set();
      this.tapeListeners.set(symbol, set);
    }
    set.add(listener);
    return () => set!.delete(listener);
  }
}

export const feed = new FeedStore();

/* -------------------------------------------------------------- hooks */

export function useTick(symbol: string): TickSnapshot | null {
  return useSyncExternalStore(
    (cb) => feed.subscribeTick(symbol, cb),
    () => feed.getTick(symbol),
    () => null,
  );
}

export function useFormingBar(symbol: string, tf: Timeframe): Candle | null {
  return useSyncExternalStore(
    (cb) => feed.subscribeBar(symbol, tf, cb),
    () => feed.getBar(symbol, tf),
    () => null,
  );
}

/** D-051 — the latest strategy_pulse frame for a market. */
export function usePulse(symbol: string): WsStrategyPulseMsg | null {
  return useSyncExternalStore(
    (cb) => feed.subscribePulse(symbol, cb),
    () => feed.getPulse(symbol),
    () => null,
  );
}

/**
 * D-082 — the RUNNING candle's micro-structure for a market: re-renders on
 * EVERY incoming tick frame (sub-second venue events), so the microscope
 * visibly moves at the tape's own speed.
 */
export function useTape(symbol: string): TapeStats | null {
  return useSyncExternalStore(
    (cb) => feed.subscribeTape(symbol, cb),
    () => feed.getTape(symbol),
    () => null,
  );
}
