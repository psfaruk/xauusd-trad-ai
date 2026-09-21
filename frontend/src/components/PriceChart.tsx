/**
 * PriceChart (D-041) — candlestick chart reworked around the feed store.
 *
 * Hardening (user reports: "chart breaks / app crashes on pair or timeframe
 * change"):
 *  - The WS connection is NEVER torn down on a symbol/TF switch (the parent
 *    just re-subscribes); this component handles the switch by clearing the
 *    series and showing a loading overlay until the new candles arrive.
 *  - All incoming candles/bars are sanitized (finite, ascending, deduped) —
 *    malformed frames never reach lightweight-charts.
 *  - Live updates go through an imperative subscription (no React state at
 *    20fps); a rAF loop eases the last candle toward the freshest quote
 *    (the MT5-terminal feel, D-037).
 *  - Recent signals draw arrow markers; the selected signal draws
 *    entry/SL/TP price lines.
 */

import { useEffect, useRef, useState } from "react";
import {
  createChart,
  ColorType,
  CrosshairMode,
  type IChartApi,
  type ISeriesApi,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";
import { feed } from "../state/feed";
import type { Candle, Signal, Timeframe } from "../types";

const UP = "#26a69a";
const DOWN = "#ef5350";
const GOLD = "#d4af37";
const EASE = 0.22;
const EPSILON = 1e-9;

export interface ChartTfChange {
  symbol: string;
  tf: Timeframe;
}

interface PriceChartProps {
  symbol: string;
  tf: Timeframe;
  candles: Candle[];
  candlesLoading: boolean;
  signals: Signal[]; // recent signals for THIS symbol (markers)
  selectedSignal: Signal | null; // entry/SL/TP lines
  market?: "open" | "closed" | "unavailable" | "unknown";
  wsConnected: boolean;
  onDesync?: () => void;
}

/** strictly-ascending, deduped, finite-only bars (D-035 hardening). */
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
    clean.set(c.t, c);
  }
  return [...clean.values()].sort((a, b) => a.t - b.t);
}

interface AnimState {
  cur: Candle | null;
  tgt: Candle | null;
}

export default function PriceChart({
  symbol,
  tf,
  candles,
  candlesLoading,
  signals,
  selectedSignal,
  market,
  wsConnected,
  onDesync,
}: PriceChartProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volumeRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const priceLinesRef = useRef<ReturnType<ISeriesApi<"Candlestick">["createPriceLine"]>[]>([]);
  const animRef = useRef<AnimState>({ cur: null, tgt: null });
  const rafRef = useRef<number | null>(null);
  const dataKeyRef = useRef(""); // last (symbol|tf|candleCount) applied
  const [ready, setReady] = useState(false);

  /* ------------------------------------------------------ create once */
  useEffect(() => {
    if (!containerRef.current) return;
    const chart = createChart(containerRef.current, {
      layout: {
        background: { type: ColorType.Solid, color: "#0b0d12" },
        textColor: "#9aa0aa",
        fontFamily: "ui-sans-serif, system-ui, sans-serif",
      },
      grid: {
        vertLines: { color: "#1a1d26" },
        horzLines: { color: "#1a1d26" },
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

    const pushBar = (bar: Candle) => {
      series.update({
        time: bar.t as UTCTimestamp,
        open: bar.o,
        high: bar.h,
        low: bar.l,
        close: bar.c,
      });
      volume.update({
        time: bar.t as UTCTimestamp,
        value: bar.v,
        color: bar.c >= bar.o ? "#1f5f57" : "#6b3232",
      });
    };

    // rAF easing loop toward the freshest target (idles when settled)
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
            /* self-heal below */
          }
        } else {
          animRef.current.cur = { ...tgt };
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
    setReady(true);
    return () => {
      if (rafRef.current != null) cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
      chart.remove();
      chartRef.current = null;
      seriesRef.current = null;
      volumeRef.current = null;
      priceLinesRef.current = [];
      setReady(false);
    };
  }, []);

  /* ------------------------------------ symbol/TF switch -> reset series */
  useEffect(() => {
    const key = `${symbol}|${tf}`;
    if (dataKeyRef.current.split("|").slice(0, 2).join("|") !== key) {
      // pair or timeframe changed: wipe everything until fresh data lands
      dataKeyRef.current = `${key}|0`;
      animRef.current = { cur: null, tgt: null };
      try {
        seriesRef.current?.setData([]);
        volumeRef.current?.setData([]);
      } catch {
        /* ignore */
      }
    }
  }, [symbol, tf]);

  /* ----------------------------------------- backfill when data changes */
  useEffect(() => {
    const series = seriesRef.current;
    const volume = volumeRef.current;
    if (!series || !volume || !ready) return;
    const clean = sanitizeCandles(candles);
    const key = `${symbol}|${tf}|${clean.length}|${clean.length ? clean[clean.length - 1].t : 0}`;
    if (key === dataKeyRef.current) return;
    if (clean.length === 0) return; // keep the loading overlay on
    dataKeyRef.current = key;
    animRef.current = { cur: null, tgt: null };
    try {
      series.setData(
        clean.map((c) => ({
          time: c.t as UTCTimestamp,
          open: c.o,
          high: c.h,
          low: c.l,
          close: c.c,
        })),
      );
      volume.setData(
        clean.map((c) => ({
          time: c.t as UTCTimestamp,
          value: c.v,
          color: c.c >= c.o ? "#1f5f57" : "#6b3232",
        })),
      );
      chartRef.current?.timeScale().scrollToRealTime();
    } catch (err) {
      console.warn("chart setData failed — dropping this batch", err);
    }
  }, [candles, symbol, tf, ready]);

  /* ------------------ live forming bar + tick -> imperative, no re-render */
  useEffect(() => {
    const offBar = feed.subscribeBar(symbol, tf, () => {
      const bar = feed.getBar(symbol, tf);
      if (!bar) return;
      const lastKnown =
        animRef.current.tgt ?? animRef.current.cur;
      if (lastKnown && bar.t < lastKnown.t) return; // stale after switch
      if (
        animRef.current.cur == null ||
        animRef.current.cur.t !== bar.t ||
        animRef.current.tgt == null ||
        animRef.current.tgt.t !== bar.t
      ) {
        animRef.current.cur = { ...bar };
        try {
          seriesRef.current?.update({
            time: bar.t as UTCTimestamp,
            open: bar.o,
            high: bar.h,
            low: bar.l,
            close: bar.c,
          });
          volumeRef.current?.update({
            time: bar.t as UTCTimestamp,
            value: bar.v,
            color: bar.c >= bar.o ? "#1f5f57" : "#6b3232",
          });
        } catch (err) {
          console.warn("chart update rejected — requesting resync", err);
          animRef.current = { cur: null, tgt: null };
          onDesync?.();
        }
      }
      animRef.current.tgt = { ...bar };
    });

    const offTick = feed.subscribeTick(symbol, () => {
      const t = feed.getTick(symbol);
      if (!t) return;
      const mid = (t.bid + t.ask) / 2;
      if (!Number.isFinite(mid) || mid <= 0) return;
      const tgt = animRef.current.tgt;
      if (!tgt) return;
      animRef.current.tgt = {
        ...tgt,
        c: mid,
        h: Math.max(tgt.h, mid),
        l: Math.min(tgt.l, mid),
      };
    });

    return () => {
      offBar();
      offTick();
    };
  }, [symbol, tf, onDesync]);

  /* ------------------------------------------------ signal chart markers */
  useEffect(() => {
    const series = seriesRef.current;
    if (!series || !ready) return;
    const markers: SeriesMarker<Time>[] = signals
      .slice(0, 40)
      .map((s) => {
        const t = Math.floor(new Date(s.ts).getTime() / 1000) as UTCTimestamp;
        return {
          time: t,
          position: s.direction === "BUY" ? "belowBar" : "aboveBar",
          color: s.direction === "BUY" ? UP : DOWN,
          shape: s.direction === "BUY" ? "arrowUp" : "arrowDown",
          text: `${s.direction} ${Math.round(s.confidence * 100)}%`,
        } as SeriesMarker<Time>;
      })
      .sort((a, b) => (a.time as number) - (b.time as number));
    try {
      series.setMarkers(markers);
    } catch {
      /* markers need ascending times — sanitized above */
    }
  }, [signals, ready]);

  /* --------------------------------------------- selected signal lines */
  useEffect(() => {
    const series = seriesRef.current;
    if (!series) return;
    priceLinesRef.current.forEach((line) => series.removePriceLine(line));
    priceLinesRef.current = [];
    if (!selectedSignal) return;
    const mk = (price: number, color: string, title: string) =>
      series.createPriceLine({
        price,
        color,
        lineWidth: 1,
        lineStyle: 2,
        axisLabelVisible: true,
        title,
      });
    priceLinesRef.current = [
      mk(selectedSignal.entry, GOLD, "ENTRY"),
      mk(selectedSignal.sl, DOWN, "SL"),
      mk(selectedSignal.tp, UP, "TP"),
    ];
  }, [selectedSignal]);

  /* ------------------------------------------------------------ overlay */
  const dataApplied = dataKeyRef.current.endsWith("|0") ? false : candles.length > 0;
  const showLoading = candlesLoading || (!dataApplied && market !== "closed");
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
    <div className="relative h-full min-h-0 w-full overflow-hidden rounded-2xl border border-zinc-800/80 bg-[#0b0d12]">
      <div ref={containerRef} className="h-full w-full" aria-label={`${symbol} ${tf} chart`} />
      {chip && <div className="absolute left-3 top-3 z-10">{chip}</div>}
      {showLoading && (
        <div className="absolute inset-0 z-10 flex flex-col items-center justify-center gap-3 bg-zinc-950/70 backdrop-blur-sm">
          <svg className="h-7 w-7 animate-spin text-gold" viewBox="0 0 24 24" fill="none">
            <circle className="opacity-20" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="3" />
            <path className="opacity-90" d="M12 2a10 10 0 0 1 10 10" stroke="currentColor" strokeWidth="3" strokeLinecap="round" />
          </svg>
          <p className="px-6 text-center text-xs text-zinc-400">
            {market === "unavailable" && !wsConnected
              ? "Reconnecting to real market data…"
              : market === "closed"
                ? "Loading real broker history from MetaTrader 5…"
                : `Loading real ${symbol} ${tf} data from MetaTrader 5…`}
          </p>
        </div>
      )}
    </div>
  );
}
