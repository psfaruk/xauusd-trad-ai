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
}

const UP = "#26a69a";
const DOWN = "#ef5350";
const GOLD = "#d4af37";

export default function Chart({ candles, liveBar, activeSignal, tf }: ChartProps) {
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
    if (candles.length === 0) return;
    series.setData(
      candles.map((c) => ({
        time: c.t as UTCTimestamp,
        open: c.o,
        high: c.h,
        low: c.l,
        close: c.c,
      }))
    );
    volume.setData(
      candles.map((c) => ({
        time: c.t as UTCTimestamp,
        value: c.v,
        color: c.c >= c.o ? "#1f5f57" : "#6b3232",
      }))
    );
    chartRef.current?.timeScale().scrollToRealTime();
  }, [candles]);

  // live forming bar — series.update() with the SAME-or-newer timestamp
  useEffect(() => {
    const series = seriesRef.current;
    const volume = volumeRef.current;
    if (!series || !volume || !liveBar || liveBar === lastLiveRef.current) return;
    if (lastDataRef.current.length === 0) return;
    const lastKnown = lastLiveRef.current ?? lastDataRef.current[lastDataRef.current.length - 1];
    if (liveBar.t < lastKnown.t) return; // stale frame after a TF switch
    lastLiveRef.current = liveBar;
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
  }, [liveBar]);

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
            waiting for candles… (connect MT5 / demo data)
          </p>
        </div>
      )}
    </div>
  );
}
