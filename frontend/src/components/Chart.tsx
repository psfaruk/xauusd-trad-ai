import { useEffect, useRef } from "react";
import {
  createChart,
  ColorType,
  CrosshairMode,
  type IChartApi,
  type ISeriesApi,
  type UTCTimestamp,
} from "lightweight-charts";
import type { Candle, Signal } from "../types";

/**
 * Candlestick chart (lightweight-charts v4) with volume histogram and live
 * updates. The parent feeds:
 *  - `candles`  -> full backfill (setData) on TF switch / reconnect heal
 *  - `liveBar`  -> the latest forming bar, updated on every bar event
 *  - `liveTick` -> the freshest broker quote (bid/ask) — D-037
 *  - `market`   -> open | closed | unavailable — status chip (D-037)
 *  - `activeSignal` -> entry/SL/TP price lines + a marker at the signal bar
 *
 * D-037 60fps smoothness: broker ticks arrive several times per second; the
 * last candle ANIMATES toward the newest price with a requestAnimationFrame
 * loop (exponential easing rendered at display refresh rate) — the chart
 * moves like the MetaTrader 5 terminal instead of stepping.
 */

interface ChartProps {
  candles: Candle[];
  liveBar: Candle | null;
  liveTick?: { bid: number; ask: number } | null;
  market?: "open" | "closed" | "unavailable" | "unknown";
  activeSignal: Signal | null;
  tf: string;
  /** D-035: called when the live update path detects a desync — the parent
   * refetches candles so the series self-heals instead of breaking. */
  onDesync?: () => void;
}

const UP = "#26a69a";
const DOWN = "#ef5350";
const GOLD = "#d4af37";
/** D-037: easing factor per 60fps frame toward the newest broker price. */
const EASE = 0.22;
const EPSILON = 1e-9;

/** D-035 chart hardening: strictly-ascending, deduped, finite-only bars.
 * Bad frames (out-of-order / duplicate / NaN) are dropped instead of ever
 * reaching lightweight-charts (which throws on non-asc data and used to
 * break the whole chart). */
function sanitizeCandles(rows: Candle[]): Candle[] {
  const clean = new Map<number, Candle>();
  for (const c of rows) {
    if (
      !c || typeof c.t !== "number" || !Number.isFinite(c.t) ||
      !Number.isFinite(c.o) || !Number.isFinite(c.h) ||
      !Number.isFinite(c.l) || !Number.isFinite(c.c) ||
      c.h < c.l || c.h <= 0
    ) {
      continue;
    }
    clean.set(c.t, c); // same timestamp -> latest wins
  }
  return [...clean.values()].sort((a, b) => a.t - b.t);
}

interface AnimState {
  /** what is on screen right now (eased) */
  cur: Candle | null;
  /** the newest authoritative bar/price we are easing toward */
  tgt: Candle | null;
}

export default function Chart({
  candles, liveBar, liveTick, market, activeSignal, tf, onDesync,
}: ChartProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volumeRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const priceLinesRef = useRef<ReturnType<ISeriesApi<"Candlestick">["createPriceLine"]>[]>([]);
  const lastDataRef = useRef<Candle[]>([]);
  const animRef = useRef<AnimState>({ cur: null, tgt: null });
  const rafRef = useRef<number | null>(null);

  // ------------------------------------------------------------------ utils
  const pushBar = (bar: Candle, volume = true) => {
    const series = seriesRef.current;
    if (!series) return;
    series.update({
      time: bar.t as UTCTimestamp,
      open: bar.o,
      high: bar.h,
      low: bar.l,
      close: bar.c,
    });
    if (volume && volumeRef.current) {
      volumeRef.current.update({
        time: bar.t as UTCTimestamp,
        value: bar.v,
        color: bar.c >= bar.o ? "#1f5f57" : "#6b3232",
      });
    }
  };

  // create once
  useEffect(() => {
    if (!containerRef.current) return;
    const chart = createChart(containerRef.current, {
      layout: {
        background: { type: ColorType.Solid, color: "#0c0e14" },
        textColor: "#9aa0aa",
        fontFamily: "ui-sans-serif, system-ui, sans-serif",
      },
      grid: {
        vertLines: { color: "#1c1f27" },
        horzLines: { color: "#1c1f27" },
      },
      crosshair: {
        mode: CrosshairMode.Normal,
        vertLine: { color: "#59606d", labelBackgroundColor: "#2a2e39" },
        horzLine: { color: "#59606d", labelBackgroundColor: "#2a2e39" },
      },
      rightPriceScale: { borderColor: "#262b36" },
      timeScale: { borderColor: "#262b36", timeVisible: true, secondsVisible: false },
      autoSize: true,
    });
    const series = chart.addCandlestickSeries({
      upColor: UP,
      downColor: DOWN,
      borderUpColor: UP,
      borderDownColor: DOWN,
      wickUpColor: UP,
      wickDownColor: DOWN,
      priceLineColor: GOLD,
    });
    const volume = chart.addHistogramSeries({
      priceScaleId: "",
      color: "#3d4350",
      priceFormat: { type: "volume" },
    });
    chart.priceScale("").applyOptions({ scaleMargins: { top: 0.85, bottom: 0 } });

    // D-037: 60fps easing loop — every frame moves the rendered candle
    // toward the newest target price; idles when settled.
    const step = () => {
      const { cur, tgt } = animRef.current;
      if (cur && tgt && tgt.t === cur.t) {
        const dc = tgt.c - cur.c;
        if (Math.abs(dc) > EPSILON || cur.h < tgt.h || cur.l > tgt.l) {
          const next = { ...cur };
          next.c = Math.abs(dc) < 1e-7 ? tgt.c : cur.c + dc * EASE;
          next.h = Math.max(cur.h, tgt.h, next.c);
          next.l = Math.min(cur.l, tgt.l, next.c);
          animRef.current.cur = next;
          try {
            pushBar(next);
          } catch {
            /* handled by the liveBar effect's self-heal */
          }
        } else {
          animRef.current.cur = { ...tgt }; // settled — snap exact
          try {
            pushBar(animRef.current.cur);
          } catch {
            /* ignore */
          }
        }
      }
      rafRef.current = requestAnimationFrame(step);
    };
    rafRef.current = requestAnimationFrame(step);

    chartRef.current = chart;
    seriesRef.current = series;
    volumeRef.current = volume;
    return () => {
      if (rafRef.current != null) cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
      chart.remove();
      chartRef.current = null;
      seriesRef.current = null;
      volumeRef.current = null;
      priceLinesRef.current = [];
    };
  }, []);

  // backfill (setData) when the parent provides a NEW candles array
  useEffect(() => {
    const series = seriesRef.current;
    const volume = volumeRef.current;
    if (!series || !volume || candles === lastDataRef.current) return;
    lastDataRef.current = candles;
    const clean = sanitizeCandles(candles); // D-035: never feed bad bars
    if (clean.length === 0) return;
    animRef.current = { cur: null, tgt: null };
    try {
      series.setData(
        clean.map((c) => ({
          time: c.t as UTCTimestamp,
          open: c.o,
          high: c.h,
          low: c.l,
          close: c.c,
        }))
      );
      volume.setData(
        clean.map((c) => ({
          time: c.t as UTCTimestamp,
          value: c.v,
          color: c.c >= c.o ? "#1f5f57" : "#6b3232",
        }))
      );
      chartRef.current?.timeScale().scrollToRealTime();
    } catch (err) {
      // one malformed frame must never blank the chart
      console.warn("chart setData failed — dropping this batch", err);
    }
  }, [candles]);

  // live forming bar -> ANIMATION TARGET (never a hard jump — D-037)
  useEffect(() => {
    if (!liveBar) return;
    if (lastDataRef.current.length === 0) return;
    if (
      !Number.isFinite(liveBar.t) || !Number.isFinite(liveBar.o) ||
      !Number.isFinite(liveBar.h) || !Number.isFinite(liveBar.l) ||
      !Number.isFinite(liveBar.c) || liveBar.h < liveBar.l
    ) {
      return; // D-035: drop malformed frames
    }
    const lastKnown =
      animRef.current.tgt ?? animRef.current.cur ??
      lastDataRef.current[lastDataRef.current.length - 1];
    if (liveBar.t < lastKnown.t) return; // stale frame after a TF switch
    if (
      animRef.current.cur == null ||
      animRef.current.cur.t !== liveBar.t ||
      animRef.current.tgt == null ||
      animRef.current.tgt.t !== liveBar.t
    ) {
      // new bucket / first live bar: seed the animation from the bar itself
      animRef.current.cur = { ...liveBar };
      try {
        pushBar(liveBar);
      } catch (err) {
        console.warn("chart update rejected — requesting resync", err);
        animRef.current = { cur: null, tgt: null };
        onDesync?.();
      }
    }
    animRef.current.tgt = { ...liveBar };
  }, [liveBar, onDesync]);

  // freshest broker quote -> retarget the animation between bar frames
  // (every real tick nudges the candle — the MT5-terminal feel, D-037)
  useEffect(() => {
    if (!liveTick || !Number.isFinite(liveTick.bid) || !Number.isFinite(liveTick.ask)) return;
    const mid = (liveTick.bid + liveTick.ask) / 2;
    const tgt = animRef.current.tgt;
    if (tgt == null) return;
    if (mid <= 0) return;
    animRef.current.tgt = {
      ...tgt,
      c: mid,
      h: Math.max(tgt.h, mid),
      l: Math.min(tgt.l, mid),
    };
  }, [liveTick]);

  // active signal -> entry/SL/TP price lines
  useEffect(() => {
    const series = seriesRef.current;
    if (!series) return;
    priceLinesRef.current.forEach((line) => series.removePriceLine(line));
    priceLinesRef.current = [];
    if (!activeSignal) return;
    const mk = (price: number, color: string, title: string) =>
      series.createPriceLine({
        price,
        color,
        lineWidth: 1,
        lineStyle: 2, // dashed
        axisLabelVisible: true,
        title,
      });
    priceLinesRef.current = [
      mk(activeSignal.entry, GOLD, "ENTRY"),
      mk(activeSignal.sl, DOWN, "SL"),
      mk(activeSignal.tp, UP, "TP"),
    ];
  }, [activeSignal]);

  const chip =
    market === "open" ? (
      <span className="flex items-center gap-1.5 rounded-full border border-emerald-500/40 bg-emerald-500/10 px-2.5 py-1 text-[10px] font-semibold tracking-wide text-emerald-300 backdrop-blur">
        <span className="relative flex h-1.5 w-1.5">
          <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-400 opacity-75" />
          <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-emerald-400" />
        </span>
        LIVE · MT5
      </span>
    ) : market === "closed" ? (
      <span className="rounded-full border border-amber-500/40 bg-amber-500/10 px-2.5 py-1 text-[10px] font-semibold tracking-wide text-amber-300 backdrop-blur">
        MARKET CLOSED
      </span>
    ) : market === "unavailable" ? (
      <span className="rounded-full border border-red-500/40 bg-red-500/10 px-2.5 py-1 text-[10px] font-semibold tracking-wide text-red-300 backdrop-blur">
        FEED OFFLINE
      </span>
    ) : null;

  return (
    <div className="relative h-full w-full">
      <div ref={containerRef} className="h-full w-full" aria-label={`${tf} candlestick chart`} />
      {chip && <div className="absolute left-3 top-3 z-10">{chip}</div>}
      {candles.length === 0 && (
        <div className="pointer-events-none absolute inset-0 grid place-items-center">
          <p className="rounded-lg border border-zinc-800 bg-zinc-900/80 px-4 py-2 text-xs text-zinc-400">
            {market === "closed"
              ? "market closed — showing real broker history from MetaTrader 5"
              : market === "unavailable"
                ? "MetaTrader 5 terminal unreachable — no demo data, retrying…"
                : "waiting for live market data… (real feed — no demo data)"}
          </p>
        </div>
      )}
    </div>
  );
}
