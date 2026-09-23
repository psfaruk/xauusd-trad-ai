/**
 * PriceChart (D-041/D-042/D-052) — candlestick chart around the feed store.
 *
 * D-052 — the professional drawing layer, styled after the reference
 * screenshots the user supplied:
 *  - labeled zone boxes ("SUPPLY ZONE", "DEMAND ZONE", "BULLISH ORDER
 *    BLOCK", "FAIR VALUE GAP") drawn from origin to the right edge;
 *  - channels with a dashed median line and a full-word label;
 *  - trend lines labeled "Bullish / Bearish Trend Line" + projection;
 *  - liquidity sweep lines ("Liquidity Sweep High / Low");
 *  - BOS / CHoCH chips with the full words, direction arrows;
 *  - Support / Resistance levels with full-word labels;
 *  - the entry-setup box with ENTRY / STOP LOSS / TAKE PROFIT lines.
 *
 * Every timeframe picks its OWN drawing set from analysis.drawings_by_tf
 * (recent 80–150 candles of that TF) so switching timeframes never wipes
 * the overlay. The engine's ~15Hz display fill keeps the last candle
 * moving between broker ticks.
 */

import { useCallback, useEffect, useRef, useState } from "react";
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
import type {
  AnalysisResponse, Candle, ChartDrawing, DrawingTone, Signal, Timeframe,
} from "../types";

const UP = "#26a69a";
const DOWN = "#ef5350";
const GOLD = "#d4af37";
const EASE = 0.22;
const EPSILON = 1e-9;

/** D-043 — drawing tone palette (chart annotations). */
const TONE: Record<DrawingTone, { line: string; text: string; fill: string }> = {
  bull: { line: "rgba(52,211,153,0.85)", text: "#6ee7b7", fill: "rgba(52,211,153,0.10)" },
  bear: { line: "rgba(248,113,113,0.85)", text: "#fca5a5", fill: "rgba(248,113,113,0.10)" },
  gold: { line: "rgba(212,175,55,0.85)", text: "#e5c76a", fill: "rgba(212,175,55,0.10)" },
  violet: { line: "rgba(167,139,250,0.85)", text: "#c4b5fd", fill: "rgba(139,92,246,0.12)" },
  neutral: { line: "rgba(154,160,170,0.7)", text: "#9aa0aa", fill: "rgba(154,160,170,0.08)" },
};

/** D-052 — zone box palette (reference-screenshot style, alpha 0.14-0.16). */
const ZONE_STYLE: Record<string, { fill: string; border: string; text: string }> = {
  supply: { fill: "rgba(239,83,80,0.16)", border: "rgba(239,83,80,0.65)", text: "#fca5a5" },
  demand: { fill: "rgba(38,166,154,0.16)", border: "rgba(38,166,154,0.65)", text: "#6ee7b7" },
  ob_bull: { fill: "rgba(59,130,246,0.14)", border: "rgba(59,130,246,0.60)", text: "#93c5fd" },
  ob_bear: { fill: "rgba(217,119,6,0.14)", border: "rgba(217,119,6,0.60)", text: "#fcd34d" },
  fvg_bull: { fill: "rgba(139,92,246,0.14)", border: "rgba(139,92,246,0.55)", text: "#c4b5fd" },
  fvg_bear: { fill: "rgba(236,72,153,0.12)", border: "rgba(236,72,153,0.50)", text: "#f9a8d4" },
};

export interface ChartTfChange {
  symbol: string;
  tf: Timeframe;
}

const TF_SECONDS: Record<string, number> = {
  M1: 60, M5: 300, M15: 900, M30: 1800, H1: 3600, H4: 14400, D1: 86400,
};

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
  /** D-042 — ICT/SMC analysis (zones/OB/FVG/liquidity/whales + drawings). */
  analysis?: AnalysisResponse | null;
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
  analysis,
}: PriceChartProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const overlayRef = useRef<HTMLCanvasElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volumeRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const priceLinesRef = useRef<ReturnType<ISeriesApi<"Candlestick">["createPriceLine"]>[]>([]);
  const animRef = useRef<AnimState>({ cur: null, tgt: null });
  const rafRef = useRef<number | null>(null);
  const dataKeyRef = useRef(""); // last (symbol|tf|candleCount) applied
  const [ready, setReady] = useState(false);
  // D-052 — simplified overlay layer toggles (full words, six chips)
  const [layers, setLayers] = useState({
    setup: true, // entry-setup boxes (ENTRY / SL / TP)
    zones: true, // supply/demand/OB/FVG zone boxes
    levels: true, // support/resistance/liquidity lines
    structure: true, // trend lines + channels + BOS/CHoCH + sweeps + arrows
    fib: true, // fibonacci retracement + OTE band
    whales: true, // whale/institutional event markers
  });
  const layersRef = useRef(layers);
  layersRef.current = layers;

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
      timeScale: {
        borderColor: "#262b36",
        timeVisible: true,
        secondsVisible: false,
        // D-043 — professional candle density for the deep 1200-bar window
        barSpacing: 7,
        rightOffset: 8,
      },
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
  const whalesForChart = analysis?.per_tf?.[tf]?.whales?.events ?? [];
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
      });
    // D-042 — whale/institutional events (momentum entries, stop hunts,
    // absorption) — snapped onto actual candle times of this TF
    if (layersRef.current.whales && candles.length) {
      const tfSec = TF_SECONDS[tf] ?? 60;
      for (const ev of whalesForChart.slice(-12)) {
        const wt = Math.floor(new Date(ev.t).getTime() / 1000);
        const bar = candles.find((c) => c.t <= wt && wt < c.t + tfSec);
        if (!bar) continue;
        markers.push({
          time: bar.t as UTCTimestamp,
          position: ev.side === "buy" ? "belowBar" : "aboveBar",
          color:
            ev.kind === "momentum" ? "#8b5cf6"
              : ev.kind === "sweep" ? "#f59e0b"
                : "#3b82f6",
          shape:
            ev.kind === "momentum"
              ? ev.side === "buy" ? "arrowUp" : "arrowDown"
              : "circle",
          text: ev.kind === "momentum"
            ? `WHALE ${ev.side.toUpperCase()}`
            : `${ev.kind === "sweep" ? "STOP HUNT" : "ABSORPTION"} z${ev.vol_z}`,
        });
      }
    }
    markers.sort((a, b) => (a.time as number) - (b.time as number));
    try {
      series.setMarkers(markers);
    } catch {
      /* markers need ascending times — sanitized above */
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [signals, ready, analysis, tf, candles.length]);

  /* ------------------------------------- D-052 professional drawing overlay */
  // D-052 — each timeframe picks ITS OWN drawing set (recent 80-150
  // candles of that TF); fallback to the legacy M1 set.
  const drawings: ChartDrawing[] =
    analysis?.drawings_by_tf?.[tf] ?? analysis?.drawings ?? [];

  const drawOverlay = useCallback(() => {
    const canvas = overlayRef.current;
    const chart = chartRef.current;
    const series = seriesRef.current;
    if (!canvas || !chart || !series) return;
    const L = layersRef.current;
    const w = canvas.clientWidth;
    const h = canvas.clientHeight;
    if (w === 0 || h === 0) return;
    const dpr = window.devicePixelRatio || 1;
    if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
    }
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);

    const ts = chart.timeScale();
    const axisW = 8 + (chart.priceScale("right").width() || 56);
    const rightEdge = Math.max(60, w - axisW);
    const xOf = (iso: string | null): number | null => {
      if (!iso) return null;
      const t = Math.floor(new Date(iso).getTime() / 1000) as UTCTimestamp;
      const c = ts.timeToCoordinate(t);
      return c == null ? null : c;
    };
    const yOf = (p: number): number | null => {
      const c = series.priceToCoordinate(p);
      return c == null ? null : c;
    };

    /* ------------------------------------------- label helpers (D-052) */
    const tag = (
      text: string, x: number, y: number, tone: DrawingTone, size = 10,
    ) => {
      ctx.font = `700 ${size}px ui-sans-serif, system-ui, sans-serif`;
      const tw = ctx.measureText(text).width;
      ctx.fillStyle = "rgba(11,13,18,0.85)";
      ctx.fillRect(x, y - size + 1, tw + 8, size + 4);
      ctx.fillStyle = TONE[tone].text;
      ctx.fillText(text, x + 4, y);
    };
    const rightTag = (text: string, y: number, tone: DrawingTone) => {
      ctx.font = "700 9px ui-sans-serif, system-ui, sans-serif";
      const tw = ctx.measureText(text).width;
      tag(text, rightEdge - tw - 14, y - 3, tone, 9);
    };
    const chip = (
      text: string, x: number, y: number, tone: DrawingTone,
    ) => {
      ctx.font = "600 9px ui-sans-serif, system-ui, sans-serif";
      const tw = ctx.measureText(text).width;
      const cw = Math.min(tw + 10, rightEdge - 8);
      const cx = Math.max(4, Math.min(x, rightEdge - cw - 6));
      const cy = Math.max(12, Math.min(y, h - 8));
      ctx.fillStyle = "rgba(11,13,18,0.88)";
      ctx.strokeStyle = TONE[tone].line;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.roundRect(cx, cy - 9, cw, 13, 3);
      ctx.fill();
      ctx.stroke();
      ctx.fillStyle = TONE[tone].text;
      ctx.fillText(text, cx + 5, cy);
    };
    /** segment through (t1,p1)->(t2,p2) extended to the right edge;
     * clamps/extrapolates when t1 falls left of the loaded window. */
    const segment = (
      t1: string | null, p1: number, t2: string | null, p2: number,
    ): { x1: number; y1: number; x2: number; y2: number } | null => {
      const x2 = t2 ? xOf(t2) : null;
      const y1 = yOf(p1);
      const y2 = yOf(p2);
      if (y1 == null || y2 == null) return null;
      if (x2 == null || x2 <= 0) return null;
      let x1 = t1 ? xOf(t1) : null;
      if (x1 == null || x1 >= x2) {
        // origin left of the window: extrapolate back to the left edge
        x1 = -2;
      }
      return { x1, y1, x2, y2 };
    };
    const strokeSeg = (
      s: { x1: number; y1: number; x2: number; y2: number },
      color: string, width: number, dash: number[],
    ) => {
      ctx.strokeStyle = color;
      ctx.lineWidth = width;
      ctx.setLineDash(dash);
      ctx.beginPath();
      ctx.moveTo(s.x1, s.y1);
      ctx.lineTo(s.x2, s.y2);
      ctx.stroke();
      ctx.setLineDash([]);
    };
    const projectToRight = (
      s: { x1: number; y1: number; x2: number; y2: number },
    ): { yEnd: number; slope: number } => {
      const slope = s.x2 > s.x1 ? (s.y2 - s.y1) / (s.x2 - s.x1) : 0;
      return { yEnd: s.y2 + slope * (rightEdge - s.x2), slope };
    };
    /** D-052 — filled direction arrow (triangle) at a candle point. */
    const arrow = (x: number, y: number, dir: "up" | "down", color: string) => {
      const s = 7;
      ctx.fillStyle = color;
      ctx.beginPath();
      if (dir === "up") {
        ctx.moveTo(x, y - s);
        ctx.lineTo(x - s * 0.7, y + s * 0.5);
        ctx.lineTo(x + s * 0.7, y + s * 0.5);
      } else {
        ctx.moveTo(x, y + s);
        ctx.lineTo(x - s * 0.7, y - s * 0.5);
        ctx.lineTo(x + s * 0.7, y - s * 0.5);
      }
      ctx.closePath();
      ctx.fill();
    };

    /* -------------------------------------------- zone boxes (labeled) */
    if (L.zones) {
      for (const d of drawings) {
        if (d.kind !== "zone") continue;
        const style = ZONE_STYLE[d.side] ?? ZONE_STYLE.demand;
        const y1 = yOf(d.hi);
        const y2 = yOf(d.lo);
        if (y1 == null || y2 == null || Math.abs(y2 - y1) < 1) continue;
        const xRaw = d.t ? xOf(d.t) : null;
        // origin left of the loaded window -> box runs in from the edge
        const x1 = xRaw == null ? -2 : Math.max(-2, xRaw);
        if (x1 > rightEdge) continue;
        ctx.fillStyle = style.fill;
        ctx.fillRect(x1, Math.min(y1, y2), rightEdge - x1, Math.abs(y2 - y1));
        ctx.strokeStyle = style.border;
        ctx.lineWidth = 1;
        ctx.setLineDash([]);
        ctx.strokeRect(
          x1 + 0.5, Math.min(y1, y2) + 0.5,
          rightEdge - x1 - 1, Math.abs(y2 - y1) - 1,
        );
        // full-word label INSIDE the box (reference-screenshot style)
        ctx.font = "700 10px ui-sans-serif, system-ui, sans-serif";
        ctx.fillStyle = style.text;
        const label = d.label.toUpperCase();
        if (Math.abs(y2 - y1) >= 14) {
          ctx.fillText(label, Math.max(x1 + 6, 4), Math.min(y1, y2) + 12);
        } else {
          ctx.fillText(label, Math.max(x1 + 6, 4), Math.min(y1, y2) - 3);
        }
      }
    }

    /* ---------------------------------------------- horizontal levels */
    if (L.levels) {
      for (const d of drawings) {
        if (d.kind !== "hline") continue;
        const y = yOf(d.price);
        if (y == null || y < 0 || y > h) continue;
        ctx.strokeStyle = TONE[d.tone].line;
        ctx.lineWidth = d.style === "solid" ? 1.5 : 1;
        ctx.setLineDash(d.style === "solid" ? [] : [5, 4]);
        ctx.beginPath();
        ctx.moveTo(0, Math.round(y) + 0.5);
        ctx.lineTo(rightEdge, Math.round(y) + 0.5);
        ctx.stroke();
        ctx.setLineDash([]);
        rightTag(d.label, y, d.tone);
      }
    }

    /* --------------------------------- structure: channel + trendlines */
    if (L.structure) {
      for (const d of drawings) {
        if (d.kind === "channel") {
          const upper = segment(d.upper.t1, d.upper.p1, d.upper.t2, d.upper.p2);
          const lower = segment(d.lower.t1, d.lower.p1, d.lower.t2, d.lower.p2);
          const median = segment(d.median.t1, d.median.p1, d.median.t2, d.median.p2);
          if (!upper || !lower) continue;
          const color = d.tone === "bull"
            ? "rgba(52,211,153,0.75)"
            : "rgba(248,113,113,0.75)";
          strokeSeg(upper, color, 1.4, []);
          if (lower) strokeSeg(lower, color, 1.4, []);
          if (median) strokeSeg(median, "rgba(154,160,170,0.55)", 1, [5, 4]);
          // projections to the right edge
          const up = projectToRight(upper);
          const lo = projectToRight(lower);
          ctx.setLineDash([4, 4]);
          ctx.strokeStyle = color;
          ctx.lineWidth = 1;
          ctx.beginPath();
          ctx.moveTo(upper.x2, upper.y2);
          ctx.lineTo(rightEdge, up.yEnd);
          ctx.moveTo(lower.x2, lower.y2);
          ctx.lineTo(rightEdge, lo.yEnd);
          ctx.stroke();
          ctx.setLineDash([]);
          // label along the upper line (full words)
          tag(d.label.toUpperCase(), Math.max(upper.x1 + 8, 6), upper.y1 - 6, d.tone);
        } else if (d.kind === "trendline") {
          const s = segment(d.t1, d.p1, d.t2, d.p2);
          if (!s) continue;
          const color = d.broken ? "rgba(154,160,170,0.45)" : TONE[d.tone].line;
          strokeSeg(s, color, 1.4, d.broken ? [3, 4] : []);
          if (!d.broken) {
            const proj = projectToRight(s);
            ctx.strokeStyle = color;
            ctx.lineWidth = 1;
            ctx.setLineDash([5, 4]);
            ctx.beginPath();
            ctx.moveTo(s.x2, s.y2);
            ctx.lineTo(rightEdge, proj.yEnd);
            ctx.stroke();
            ctx.setLineDash([]);
          }
          tag(d.label, Math.max(Math.min(s.x2 + 6, rightEdge - 90), 6), s.y2 - 5, d.broken ? "neutral" : d.tone);
        } else if (d.kind === "sweep") {
          const x = d.t ? xOf(d.t) : null;
          const y = yOf(d.price);
          if (y == null) continue;
          const xStart = x == null ? 0 : Math.max(0, x);
          if (xStart > rightEdge) continue;
          const color = d.side === "high"
            ? "rgba(248,113,113,0.8)"
            : "rgba(52,211,153,0.8)";
          ctx.strokeStyle = color;
          ctx.lineWidth = 1.2;
          ctx.setLineDash([2, 3]);
          ctx.beginPath();
          ctx.moveTo(xStart, Math.round(y) + 0.5);
          ctx.lineTo(rightEdge, Math.round(y) + 0.5);
          ctx.stroke();
          ctx.setLineDash([]);
          // small X at the sweep point (the stop-hunt wick)
          if (x != null && x >= 0 && x <= rightEdge) {
            ctx.strokeStyle = color;
            ctx.lineWidth = 1.4;
            ctx.beginPath();
            ctx.moveTo(x - 4, y - 4);
            ctx.lineTo(x + 4, y + 4);
            ctx.moveTo(x + 4, y - 4);
            ctx.lineTo(x - 4, y + 4);
            ctx.stroke();
          }
          ctx.font = "700 9px ui-sans-serif, system-ui, sans-serif";
          ctx.fillStyle = TONE[d.tone].text;
          ctx.fillText(
            d.label.toUpperCase(),
            Math.min(xStart + 8, rightEdge - 150),
            d.side === "high" ? y - 5 : y + 12,
          );
        } else if (d.kind === "structure") {
          const x = d.t ? xOf(d.t) : null;
          const y = yOf(d.price);
          if (x == null || y == null || x < 0 || x > rightEdge) continue;
          chip(d.label, x + 6, d.dir === "up" ? y - 14 : y + 16, d.tone);
        } else if (d.kind === "arrow") {
          const x = d.t ? xOf(d.t) : null;
          const y = yOf(d.price);
          if (y == null) continue;
          const ax = x == null ? rightEdge - 40 : Math.min(x + 10, rightEdge - 24);
          const color = d.dir === "up"
            ? "rgba(103,232,249,0.9)"
            : "rgba(248,113,113,0.9)";
          const ay = d.dir === "up" ? y + 26 : y - 26;
          arrow(ax, Math.max(12, Math.min(ay, h - 12)), d.dir, color);
          ctx.font = "600 9px ui-sans-serif, system-ui, sans-serif";
          ctx.fillStyle = d.dir === "up" ? "#67e8f9" : "#fca5a5";
          const label = d.label.toUpperCase();
          ctx.fillText(
            label,
            Math.min(ax + 12, rightEdge - ctx.measureText(label).width - 6),
            Math.max(12, Math.min(ay, h - 12)) + (d.dir === "up" ? -8 : 12),
          );
        }
      }
    }

    /* -------------------------------------------------- fibonacci fan */
    if (L.fib) {
      for (const d of drawings) {
        if (d.kind !== "fib") continue;
        const x0 = xOf(d.t0);
        const y0 = yOf(d.p0);
        const y1 = yOf(d.p1);
        const fx = x0 == null ? 0 : Math.max(-2, x0);
        // OTE band first (under the level lines)
        if (d.ote) {
          const yo1 = yOf(d.ote[1]);
          const yo0 = yOf(d.ote[0]);
          if (yo1 != null && yo0 != null) {
            ctx.fillStyle = TONE.gold.fill;
            ctx.fillRect(fx, Math.min(yo1, yo0), rightEdge - fx, Math.abs(yo0 - yo1));
            ctx.strokeStyle = "rgba(212,175,55,0.35)";
            ctx.setLineDash([2, 3]);
            ctx.strokeRect(fx + 0.5, Math.min(yo1, yo0) + 0.5, rightEdge - fx - 1, Math.abs(yo0 - yo1) - 1);
            ctx.setLineDash([]);
            tag("OTE 0.62–0.79", fx + 4, Math.min(yo1, yo0) + 10, "gold");
          }
        }
        ctx.font = "600 9px ui-sans-serif, system-ui, sans-serif";
        for (const lv of d.levels) {
          const y = yOf(lv.price);
          if (y == null || y < -5 || y > h + 5) continue;
          const key = lv.ratio === 0.618 || lv.ratio === 0.786;
          ctx.strokeStyle = key ? "rgba(212,175,55,0.8)" : "rgba(212,175,55,0.4)";
          ctx.lineWidth = key ? 1.2 : 0.8;
          ctx.setLineDash(key ? [] : [3, 3]);
          ctx.beginPath();
          ctx.moveTo(fx, Math.round(y) + 0.5);
          ctx.lineTo(rightEdge, Math.round(y) + 0.5);
          ctx.stroke();
          ctx.setLineDash([]);
          rightTag(`FIB ${(lv.ratio * 100).toFixed(1)}%`, y, "gold");
        }
        // the impulse leg itself (thin neutral diagonal)
        if (y0 != null && y1 != null && x0 != null) {
          ctx.strokeStyle = "rgba(154,160,170,0.5)";
          ctx.lineWidth = 1;
          ctx.beginPath();
          ctx.moveTo(x0, y0);
          ctx.lineTo(xOf(d.t1) ?? rightEdge, y1);
          ctx.stroke();
        }
      }
    }

    /* -------------------------------------------- D-043 entry-setup box */
    const setup = drawings.find(
      (d): d is Extract<ChartDrawing, { kind: "setup" }> => d.kind === "setup",
    );
    if (L.setup && setup) {
      const x0raw = xOf(setup.t0);
      const x0 = x0raw == null ? 0 : Math.max(-2, x0raw);
      const isBuy = setup.dir === "BUY";
      const tone: DrawingTone = isBuy ? "bull" : "bear";
      const yZhi = yOf(setup.zone[1]);
      const yZlo = yOf(setup.zone[0]);
      const yE = yOf(setup.entry);
      const yS = yOf(setup.sl);
      const yT = yOf(setup.tp);
      if (yZhi != null && yZlo != null) {
        // entry zone box
        ctx.fillStyle = TONE[tone].fill;
        ctx.fillRect(x0, Math.min(yZhi, yZlo), rightEdge - x0, Math.abs(yZlo - yZhi));
        ctx.strokeStyle = TONE[tone].line;
        ctx.lineWidth = 1.2;
        ctx.setLineDash([]);
        ctx.strokeRect(x0 + 0.5, Math.min(yZhi, yZlo) + 0.5, rightEdge - x0 - 1, Math.abs(yZlo - yZhi) - 1);
        tag(
          `${setup.dir} SETUP${setup.status === "triggered" ? " · ENTRY TAKEN" : " · FORMING"}`,
          x0 + 6,
          Math.min(yZhi, yZlo) + 10,
          tone,
        );
      }
      const line = (
        y: number | null, color: string, dash: number[], label: string, x: number,
      ) => {
        if (y == null || y < -5 || y > h + 5) return;
        ctx.strokeStyle = color;
        ctx.lineWidth = 1;
        ctx.setLineDash(dash);
        ctx.beginPath();
        ctx.moveTo(x, Math.round(y) + 0.5);
        ctx.lineTo(rightEdge, Math.round(y) + 0.5);
        ctx.stroke();
        ctx.setLineDash([]);
        rightTag(label, y, "neutral");
      };
      // risk / reward shading between entry and sl / tp
      if (yE != null && yS != null) {
        ctx.fillStyle = "rgba(248,113,113,0.07)";
        ctx.fillRect(x0, Math.min(yE, yS), rightEdge - x0, Math.abs(yS - yE));
      }
      if (yE != null && yT != null) {
        ctx.fillStyle = "rgba(52,211,153,0.07)";
        ctx.fillRect(x0, Math.min(yE, yT), rightEdge - x0, Math.abs(yT - yE));
      }
      line(yE, "rgba(212,175,55,0.9)", [], `ENTRY ${setup.entry}`, x0);
      line(yS, "rgba(248,113,113,0.85)", [5, 4], `STOP LOSS ${setup.sl}`, x0);
      line(yT, "rgba(52,211,153,0.85)", [5, 4], `TAKE PROFIT ${setup.tp} · RR ${setup.rr}`, x0);
      // the note card (what a trader writes next to the setup)
      const title = `${isBuy ? "▲" : "▼"} ${setup.dir} SETUP · ${
        setup.status === "triggered" ? "ENTRY TAKEN" : "FORMING"
      } · RR ${setup.rr}`;
      const factors = setup.factors.slice(0, 5).join(" · ");
      ctx.font = "700 10px ui-sans-serif, system-ui, sans-serif";
      const wT = ctx.measureText(title).width;
      ctx.font = "500 9px ui-sans-serif, system-ui, sans-serif";
      const wF = ctx.measureText(factors).width;
      const cardW = Math.min(Math.max(wT, wF) + 16, rightEdge - x0 - 8);
      const cardY = Math.max(
        10,
        Math.min((yZhi ?? h / 2) - 34, h - 58),
      );
      const cardX = Math.max(4, rightEdge - cardW - 10);
      ctx.fillStyle = "rgba(11,13,18,0.88)";
      ctx.strokeStyle = setup.status === "triggered" ? TONE[tone].line : "rgba(212,175,55,0.5)";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.roundRect(cardX, cardY, cardW, 40, 6);
      ctx.fill();
      ctx.stroke();
      ctx.font = "700 10px ui-sans-serif, system-ui, sans-serif";
      ctx.fillStyle = setup.status === "triggered" ? TONE[tone].text : "#e5c76a";
      ctx.fillText(title, cardX + 8, cardY + 14);
      ctx.font = "500 9px ui-sans-serif, system-ui, sans-serif";
      ctx.fillStyle = "#b7bcc6";
      ctx.fillText(
        factors.length > 70 ? factors.slice(0, 68) + "…" : factors,
        cardX + 8,
        cardY + 28,
      );
      ctx.fillStyle = "#8b91a0";
      ctx.fillText(
        setup.note.length > 74 ? setup.note.slice(0, 72) + "…" : setup.note,
        cardX + 8,
        cardY + 38,
      );
    }

    /* --------------------------------- D-043 active signal entry/SL/TP lines */
    const activeSignals = signals.filter(
      (s) => s.status === "active" && s.id !== selectedSignal?.id,
    );
    for (const sig of activeSignals.slice(0, 2)) {
      const line = (p: number, color: string, label: string) => {
        const y = yOf(p);
        if (y == null || y < 0 || y > h) return;
        ctx.strokeStyle = color;
        ctx.lineWidth = 0.8;
        ctx.setLineDash([2, 4]);
        ctx.beginPath();
        ctx.moveTo(0, Math.round(y) + 0.5);
        ctx.lineTo(rightEdge, Math.round(y) + 0.5);
        ctx.stroke();
        ctx.setLineDash([]);
        tag(label, 8, y - 3, "neutral", 9);
      };
      line(sig.entry, "rgba(212,175,55,0.55)", `${sig.direction} ENTRY`);
      line(sig.sl, "rgba(248,113,113,0.4)", `${sig.direction} STOP LOSS`);
      line(sig.tp, "rgba(52,211,153,0.4)", `${sig.direction} TAKE PROFIT`);
    }
  }, [drawings, signals, selectedSignal, tf]);

  // redraw on data/symbol changes + continuously while zooming/panning
  useEffect(() => {
    drawOverlay();
  }, [drawOverlay, ready, candles.length, symbol, tf]);
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    const cb = () => drawOverlay();
    const rangeHandler = () => window.requestAnimationFrame(cb);
    try {
      chart.timeScale().subscribeVisibleLogicalRangeChange(rangeHandler);
    } catch {
      /* chart disposed */
    }
    const iv = window.setInterval(cb, 500); // price-scale (vertical zoom) sync
    window.addEventListener("resize", cb);
    return () => {
      try {
        chart.timeScale().unsubscribeVisibleLogicalRangeChange(rangeHandler);
      } catch {
        /* disposed */
      }
      window.clearInterval(iv);
      window.removeEventListener("resize", cb);
    };
  }, [drawOverlay]);

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
  const layerChips: { key: keyof typeof layers; label: string }[] = [
    { key: "setup", label: "Setup" },
    { key: "zones", label: "Zones" },
    { key: "levels", label: "Levels" },
    { key: "structure", label: "Structure" },
    { key: "fib", label: "Fibonacci" },
    { key: "whales", label: "Whales" },
  ];
  const chip =
    market === "open" ? (
      <span className="flex items-center gap-1.5 rounded-full border border-emerald-500/40 bg-emerald-500/10 px-2.5 py-1 text-[10px] font-semibold tracking-wide text-emerald-300 backdrop-blur">
        <span className="relative flex h-1.5 w-1.5">
          <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-400 opacity-75" />
          <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-emerald-400" />
        </span>
        LIVE · INSTITUTIONAL FEED
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
      {/* D-052 — professional drawing overlay (pointer-transparent) */}
      <canvas
        ref={overlayRef}
        className="pointer-events-none absolute inset-0 h-full w-full"
        aria-hidden
      />
      {chip && <div className="absolute left-3 top-3 z-10">{chip}</div>}
      {/* D-052 — overlay layer toggles (six, full words) */}
      <div className="absolute right-3 top-3 z-10 flex max-w-[75%] flex-wrap justify-end gap-1">
        {layerChips.map((l) => (
          <button
            key={l.key}
            type="button"
            onClick={() => setLayers((s) => ({ ...s, [l.key]: !s[l.key] }))}
            className={`rounded-full border px-2 py-0.5 text-[9px] font-bold tracking-wide backdrop-blur transition-colors ${
              layers[l.key]
                ? "border-gold/50 bg-gold/15 text-gold"
                : "border-zinc-700/70 bg-zinc-900/70 text-zinc-500"
            }`}
          >
            {l.label}
          </button>
        ))}
      </div>
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
                ? "Loading institutional market history…"
                : `Loading real ${symbol} ${tf} market data…`}
          </p>
        </div>
      )}
    </div>
  );
}
