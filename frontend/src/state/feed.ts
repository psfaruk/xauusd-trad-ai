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

type Listener = () => void;

class FeedStore {
  private ticks = new Map<string, TickSnapshot>();
  private tickListeners = new Map<string, Set<Listener>>();
  private bars = new Map<string, Candle>();
  private barListeners = new Map<string, Set<Listener>>();
  /** D-051 — latest strategy_pulse frame per symbol (M1-close cadence). */
  private pulses = new Map<string, WsStrategyPulseMsg>();
  private pulseListeners = new Map<string, Set<Listener>>();

  /* ------------------------------------------------------------- ingest */

  pushTick(msg: WsTickMsg): void {
    if (!Number.isFinite(msg.bid) || !Number.isFinite(msg.ask)) return;
    this.ticks.set(msg.symbol, {
      bid: msg.bid,
      ask: msg.ask,
      ts: msg.ts,
      tps: msg.tps ?? null,
      n: msg.n ?? null,
      at: Date.now(),
    });
    this.tickListeners.get(msg.symbol)?.forEach((l) => l());
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
    for (const key of [...this.bars.keys()]) {
      if (key.startsWith(`${symbol}|`)) {
        this.bars.delete(key);
        this.barListeners.get(key)?.forEach((l) => l());
      }
    }
    this.tickListeners.get(symbol)?.forEach((l) => l());
  }

  clearAll(): void {
    this.ticks.clear();
    this.bars.clear();
    this.pulses.clear();
    this.tickListeners.forEach((set) => set.forEach((l) => l()));
    this.barListeners.forEach((set) => set.forEach((l) => l()));
    this.pulseListeners.forEach((set) => set.forEach((l) => l()));
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
