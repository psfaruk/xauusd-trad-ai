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
 *  - `activeSignal` -> entry/SL/TP price lines + a marker at the signal bar
 */

interface ChartProps {
  candles: Candle[];
  liveBar: Candle | null;
  activeSignal: Signal | null;
  tf: string;
  /** D-035: called when the live update path detects a desync — the parent
   * refetches candles so the series self-heals instead of breaking. */
  onDesync?: () => void;
}

const UP = "#26a69a";
const DOWN = "#ef5350";
const GOLD = "#d4af37";

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

export default function Chart({ candles, liveBar, activeSignal, tf, onDesync }: ChartProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volumeRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const priceLinesRef = useRef<ReturnType<ISeriesApi<"Candlestick">["createPriceLine"]>[]>([]);
  const lastDataRef = useRef<Candle[]>([]);
  const lastLiveRef = useRef<Candle | null>(null);

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
    chartRef.current = chart;
    seriesRef.current = series;
    volumeRef.current = volume;
    return () => {
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
    lastLiveRef.current = null;
    const clean = sanitizeCandles(candles); // D-035: never feed bad bars
    if (clean.length === 0) return;
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

  // live forming bar — series.update() with the SAME-or-newer timestamp
  useEffect(() => {
    const series = seriesRef.current;
    const volume = volumeRef.current;
    if (!series || !volume || !liveBar || liveBar === lastLiveRef.current) return;
    if (lastDataRef.current.length === 0) return;
    if (
      !Number.isFinite(liveBar.t) || !Number.isFinite(liveBar.o) ||
      !Number.isFinite(liveBar.h) || !Number.isFinite(liveBar.l) ||
      !Number.isFinite(liveBar.c) || liveBar.h < liveBar.l
    ) {
      return; // D-035: drop malformed frames
    }
    const lastKnown = lastLiveRef.current ?? lastDataRef.current[lastDataRef.current.length - 1];
    if (liveBar.t < lastKnown.t) return; // stale frame after a TF switch
    lastLiveRef.current = liveBar;
    try {
      series.update({
        time: liveBar.t as UTCTimestamp,
        open: liveBar.o,
        high: liveBar.h,
        low: liveBar.l,
        close: liveBar.c,
      });
      volume.update({
        time: liveBar.t as UTCTimestamp,
        value: liveBar.v,
        color: liveBar.c >= liveBar.o ? "#1f5f57" : "#6b3232",
      });
    } catch (err) {
      // D-035 self-heal: a rejected update means the series drifted from the
      // live stream (e.g. a bucket raced past) — ask the parent for a fresh
      // backfill instead of leaving a broken chart.
      console.warn("chart update rejected — requesting resync", err);
      lastLiveRef.current = null;
      onDesync?.();
    }
  }, [liveBar, onDesync]);

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

  return (
    <div className="relative h-full w-full">
      <div ref={containerRef} className="h-full w-full" aria-label={`${tf} candlestick chart`} />
      {candles.length === 0 && (
        <div className="pointer-events-none absolute inset-0 grid place-items-center">
          <p className="rounded-lg border border-zinc-800 bg-zinc-900/80 px-4 py-2 text-xs text-zinc-400">
            waiting for live market data… (real feed — no demo data)
          </p>
        </div>
      )}
    </div>
  );
}
