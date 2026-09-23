/**
 * PriceChart (D-041/D-042/D-052/D-053) — candlestick chart around the feed store.
 *
 * D-053 — the HARD drawing layer + clean-chart contract:
 *  - every mark renders with a near-opaque core line and a soft halo
 *    underlay ("শক্তিশালী / hard") while the STROKE WIDTHS went THIN
 *    (user directive: "দাগ গুলো একটু চিকন");
 *  - faded marks (broken trendlines, tested order blocks) render THIN
 *    and ghosted at ~1/3 opacity ("মোছে যাওয়া অঙ্কনের লেখা চিকন");
 *  - NO text labels on the chart canvas anymore ("FVG/OB/Liquidity…
 *    চার্টের উপরে ছোট ছোট লেখা" — removed): the marks are listed in
 *    the external MARKS legend BELOW the chart, outside the canvas;
 *  - variant="signals" (the Chart tab) draws ONLY the entry-setup box +
 *    ENTRY/SL/TP + signal markers — nothing that hides the signals;
 *  - variant="full" (the Home chart) draws everything.
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

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
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

/** Drawing-strength contract (D-053):
 *  - ACTIVE core line: thin (0.8–1.2px) but ~95% opaque, with a 3px halo
 *    underlay — reads "hard" without fat strokes;
 *  - FADED (broken / tested): 0.7px at ~1/3 alpha, no halo — the mark is
 *    visibly dissolving (মোছে যাওয়া) and its legend text renders THIN. */
const FADED_ALPHA = 0.32;
const FADED_WIDTH = 0.7;

/** D-053 — "hard" tone palette: near-opaque core + soft halo underlay. */
const TONE: Record<DrawingTone, { line: string; halo: string; text: string; fill: string }> = {
  bull: { line: "rgba(52,211,153,0.95)", halo: "rgba(52,211,153,0.18)", text: "#6ee7b7", fill: "rgba(52,211,153,0.18)" },
  bear: { line: "rgba(248,113,113,0.95)", halo: "rgba(248,113,113,0.18)", text: "#fca5a5", fill: "rgba(248,113,113,0.18)" },
  gold: { line: "rgba(212,175,55,0.95)", halo: "rgba(212,175,55,0.18)", text: "#e5c76a", fill: "rgba(212,175,55,0.18)" },
  violet: { line: "rgba(167,139,250,0.95)", halo: "rgba(167,139,250,0.18)", text: "#c4b5fd", fill: "rgba(139,92,246,0.18)" },
  neutral: { line: "rgba(154,160,170,0.85)", halo: "rgba(154,160,170,0.10)", text: "#9aa0aa", fill: "rgba(154,160,170,0.10)" },
};

/** D-052 — zone box palette; D-053 — hardened (fills 0.18–0.22, borders
 *  0.75–0.9) + halo underlay. Faded zones scale everything by
 *  FADED_ALPHA at draw time. */
const ZONE_STYLE: Record<string, { fill: string; border: string; halo: string; text: string }> = {
  supply: { fill: "rgba(239,83,80,0.22)", border: "rgba(239,83,80,0.9)", halo: "rgba(239,83,80,0.20)", text: "#fca5a5" },
  demand: { fill: "rgba(38,166,154,0.22)", border: "rgba(38,166,154,0.9)", halo: "rgba(38,166,154,0.20)", text: "#6ee7b7" },
  ob_bull: { fill: "rgba(59,130,246,0.20)", border: "rgba(59,130,246,0.85)", halo: "rgba(59,130,246,0.18)", text: "#93c5fd" },
  ob_bear: { fill: "rgba(217,119,6,0.20)", border: "rgba(217,119,6,0.85)", halo: "rgba(217,119,6,0.18)", text: "#fcd34d" },
  fvg_bull: { fill: "rgba(139,92,246,0.20)", border: "rgba(139,92,246,0.8)", halo: "rgba(139,92,246,0.16)", text: "#c4b5fd" },
  fvg_bear: { fill: "rgba(236,72,153,0.18)", border: "rgba(236,72,153,0.75)", halo: "rgba(236,72,153,0.14)", text: "#f9a8d4" },
};

/** D-053 — compact legend names for the external MARKS panel. */
const ZONE_SHORT: Record<string, string> = {
  supply: "SUPPLY", demand: "DEMAND",
  ob_bull: "BULL OB", ob_bear: "BEAR OB",
  fvg_bull: "BULL FVG", fvg_bear: "BEAR FVG",
};

/** D-053 — long backend labels -> compact legend chips. */
function shortLevel(label: string): string {
  if (label === "Previous Day High") return "PDH";
  if (label === "Previous Day Low") return "PDL";
  if (label.startsWith("Point of Control")) return "POC";
  if (label.startsWith("Buy Side Liquidity")) return "BSL";
  if (label.startsWith("Sell Side Liquidity")) return "SSL";
  if (label.startsWith("Time at Price"))
    return "TAP " + (label.includes("Support") ? "SUP" : "RES");
  return label.slice(0, 10).toUpperCase();
}

/** A drawing is FADED when the backend says so (broken trendline /
 * tested order block) — legacy snapshots fall back to label sniffing. */
function isFaded(d: { state?: "active" | "faded"; broken?: boolean; label?: string }): boolean {
  if (d.state === "faded") return true;
  if (d.state === "active") return false;
  return Boolean(d.broken) || Boolean(d.label?.includes("· tested"));
}

type LayerKey = "setup" | "zones" | "levels" | "structure" | "fib" | "whales";

/** One row of the external MARKS legend (outside the chart canvas). */
export interface LegendMark {
  key: string;
  /** swatch + text color (hex) */
  swatch: string;
  /** compact chip name, e.g. "BULL FVG" / "PDH" / "BOS ↑" */
  short: string;
  /** the price / range the mark sits at */
  detail: string;
  /** the FULL-word backend label (tooltip) */
  text: string;
  faded: boolean;
  layer: LayerKey;
}

/** D-053 — build the external marks list from the active drawing set. */
function buildLegend(drawings: ChartDrawing[], tf: string): LegendMark[] {
  const marks: LegendMark[] = [];
  drawings.forEach((d, i) => {
    const key = `${d.kind}-${i}`;
    const faded = isFaded(d as Parameters<typeof isFaded>[0]);
    switch (d.kind) {
      case "zone": {
        const st = ZONE_STYLE[d.side] ?? ZONE_STYLE.demand;
        const htf = d.source_tf && d.source_tf !== tf ? ` ${d.source_tf}` : "";
        marks.push({
          key, swatch: st.text,
          short: (ZONE_SHORT[d.side] ?? "ZONE") + htf,
          detail: `${d.lo.toFixed(2)}–${d.hi.toFixed(2)}`,
          text: d.label, faded, layer: "zones",
        });
        break;
      }
      case "hline":
        marks.push({
          key, swatch: TONE[d.tone].text,
          short: shortLevel(d.label), detail: d.price.toFixed(2),
          text: d.label, faded, layer: "levels",
        });
        break;
      case "sweep":
        marks.push({
          key, swatch: TONE[d.tone].text,
          short: d.side === "high" ? "SWEEP HIGH" : "SWEEP LOW",
          detail: d.price.toFixed(2), text: d.label, faded, layer: "structure",
        });
        break;
      case "structure":
        marks.push({
          key, swatch: TONE[d.tone].text,
          short: `${d.label.split(" ")[0]} ${d.dir === "up" ? "↑" : "↓"}`,
          detail: d.price.toFixed(2), text: d.label, faded, layer: "structure",
        });
        break;
      case "trendline":
        marks.push({
          key, swatch: TONE[d.tone].text,
          short: faded ? "TREND · BROKEN" : "TREND",
          detail: d.p2.toFixed(2), text: d.label, faded, layer: "structure",
        });
        break;
      case "channel":
        marks.push({
          key, swatch: TONE[d.tone].text,
          short: `CHANNEL ${d.dir === "up" ? "↑" : "↓"}`,
          detail: "", text: d.label, faded, layer: "structure",
        });
        break;
      case "arrow":
        marks.push({
          key, swatch: TONE[d.tone].text,
          short: d.label.toUpperCase().split(" · ")[0].slice(0, 12),
          detail: d.price.toFixed(2), text: d.label, faded, layer: "structure",
        });
        break;
      case "fib":
        marks.push({
          key, swatch: TONE.gold.text,
          short: "FIB",
          detail: d.ote
            ? `OTE ${d.ote[0].toFixed(2)}–${d.ote[1].toFixed(2)}`
            : `${d.levels.length} levels`,
          text: "Fibonacci retracement + OTE band", faded, layer: "fib",
        });
        break;
      case "note":
        marks.push({
          key, swatch: TONE[d.tone].text,
          short: d.text.slice(0, 12).toUpperCase(), detail: "",
          text: d.text, faded, layer: "structure",
        });
        break;
      case "setup":
        marks.push({
          key, swatch: TONE.gold.text,
          short: `${d.dir} SETUP`,
          detail: `RR ${d.rr}`,
          text: `Entry setup (${d.status}) — ${d.note}`,
          faded: false, layer: "setup",
        });
        break;
    }
  });
  return marks;
}

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
  /** D-053 — "full" (Home chart): every drawing layer. "signals" (the
   *  Chart tab): ONLY the entry-setup drawing + signal markers — the
   *  analysis layers must never bury the signals (user directive). */
  variant?: "full" | "signals";
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
  variant = "full",
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
  const isSignals = variant === "signals";
  // D-052 — simplified overlay layer toggles (full words, six chips);
  // D-053 — they live OUTSIDE the chart, in the MARKS legend header
  // (user directive: nothing small written on top of the canvas).
  const [layers, setLayers] = useState<Record<LayerKey, boolean>>({
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
        // D-052 — user directive: the drawings must be VISIBLE within the
        // recent 80-150 candles ("এই ড্রয়িং গুলো রিসেন্ট 80 থেকে 150
        // ক্যান্ডেল এ দেখলেই হবে") — a denser default zoom keeps ~110
        // bars (and their zone boxes / levels / structure marks) on screen.
        barSpacing: 4,
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
      chartRef.current?.timeScale().setVisibleLogicalRange(
        { from: Math.max(0, clean.length - 110), to: clean.length + 6 },
      );
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
    // absorption) — snapped onto actual candle times of this TF.
    // D-053 — FULL variant only: the Chart tab shows signals + the entry
    // setup, never the analysis noise (user directive).
    if (layersRef.current.whales && !isSignals && candles.length) {
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
          // D-053 — shapes only: the "WHALE BUY / STOP HUNT z…" texts were
          // part of the small-writing clutter the user asked to remove
          // from the chart canvas.
          text: "",
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
  }, [signals, ready, analysis, tf, candles.length, isSignals]);

  /* ------------------------------------- D-052 professional drawing overlay */
  // D-052 — each timeframe picks ITS OWN drawing set (recent 80-150
  // candles of that TF); fallback to the legacy M1 set.
  // D-053 — the signals variant strips every analysis layer except the
  // entry-setup drawing (Chart tab = setup + signals only).
  const allDrawings: ChartDrawing[] =
    analysis?.drawings_by_tf?.[tf] ?? analysis?.drawings ?? [];
  const drawings: ChartDrawing[] = isSignals
    ? allDrawings.filter((d) => d.kind === "setup")
    : allDrawings;

  const drawOverlay = useCallback(() => {
    const canvas = overlayRef.current;
    const chart = chartRef.current;
    const series = seriesRef.current;
    if (!canvas || !chart || !series) return;
    const L = layersRef.current;
    try {
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

    /* -------------------------------------- D-053 hard-stroke helpers */
    // On-chart TEXT is gone (user directive: the small "FVG / OB /
    // LIQUIDITY…" writings moved OUT of the chart, into the MARKS
    // legend below the canvas). Only the entry-setup tags remain —
    // they ARE the signal contract (ENTRY / STOP LOSS / TAKE PROFIT).
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
    /** HARD stroke: a soft halo pass under a thin near-opaque core —
     * the "শক্তিশালী / hard-coded" look without fat lines. */
    const hardSeg = (
      x1: number, y1: number, x2: number, y2: number,
      color: string, halo: string, width: number, dash: number[],
    ) => {
      ctx.strokeStyle = halo;
      ctx.lineWidth = width + 2.6;
      ctx.setLineDash([]);
      ctx.beginPath();
      ctx.moveTo(x1, y1);
      ctx.lineTo(x2, y2);
      ctx.stroke();
      ctx.strokeStyle = color;
      ctx.lineWidth = width;
      ctx.setLineDash(dash);
      ctx.beginPath();
      ctx.moveTo(x1, y1);
      ctx.lineTo(x2, y2);
      ctx.stroke();
      ctx.setLineDash([]);
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
    /** D-052 — filled direction arrow (triangle) at a candle point.
     *  D-053 — halo underlay + no text (labels live in the legend). */
    const arrow = (
      x: number, y: number, dir: "up" | "down",
      color: string, halo: string,
    ) => {
      const s = 7;
      const tri = (r: number) => {
        ctx.beginPath();
        if (dir === "up") {
          ctx.moveTo(x, y - r);
          ctx.lineTo(x - r * 0.7, y + r * 0.5);
          ctx.lineTo(x + r * 0.7, y + r * 0.5);
        } else {
          ctx.moveTo(x, y + r);
          ctx.lineTo(x - r * 0.7, y - r * 0.5);
          ctx.lineTo(x + r * 0.7, y - r * 0.5);
        }
        ctx.closePath();
        ctx.fill();
      };
      ctx.fillStyle = halo;
      tri(s + 2.5);
      ctx.fillStyle = color;
      tri(s);
    };
    /** D-053 — BOS / CHoCH event marker: a hard diamond with halo (the
     *  text chip moved to the external legend). */
    const diamond = (x: number, y: number, r: number) => {
      ctx.beginPath();
      ctx.moveTo(x, y - r);
      ctx.lineTo(x + r, y);
      ctx.lineTo(x, y + r);
      ctx.lineTo(x - r, y);
      ctx.closePath();
      ctx.fill();
    };

    /* -------------------------------------------- zone boxes (D-053) */
    if (L.zones) {
      for (const d of drawings) {
        if (d.kind !== "zone") continue;
        const style = ZONE_STYLE[d.side] ?? ZONE_STYLE.demand;
        const faded = isFaded(d);
        const y1 = yOf(d.hi);
        const y2 = yOf(d.lo);
        if (y1 == null || y2 == null || Math.abs(y2 - y1) < 1) continue;
        const xRaw = d.t ? xOf(d.t) : null;
        // origin left of the loaded window -> box runs in from the edge
        const x1 = xRaw == null ? -2 : Math.max(-2, xRaw);
        if (x1 > rightEdge) continue;
        if (faded) ctx.globalAlpha = FADED_ALPHA;
        ctx.fillStyle = style.fill;
        ctx.fillRect(x1, Math.min(y1, y2), rightEdge - x1, Math.abs(y2 - y1));
        // halo underlay — the "hard" border pass (active marks only)
        if (!faded) {
          ctx.strokeStyle = style.halo;
          ctx.lineWidth = 3.4;
          ctx.setLineDash([]);
          ctx.strokeRect(
            x1 - 0.5, Math.min(y1, y2) - 0.5,
            rightEdge - x1 + 1, Math.abs(y2 - y1) + 1,
          );
        }
        ctx.strokeStyle = style.border;
        ctx.lineWidth = faded ? FADED_WIDTH : 1;
        ctx.setLineDash([]);
        ctx.strokeRect(
          x1 + 0.5, Math.min(y1, y2) + 0.5,
          rightEdge - x1 - 1, Math.abs(y2 - y1) - 1,
        );
        ctx.globalAlpha = 1;
        // D-053 — NO text on the canvas: the full-word label lives in
        // the external MARKS legend below the chart (user directive).
      }
    }

    /* ---------------------------------------------- horizontal levels */
    if (L.levels) {
      for (const d of drawings) {
        if (d.kind !== "hline") continue;
        const y = yOf(d.price);
        if (y == null || y < 0 || y > h) continue;
        const solid = d.style === "solid";
        if (solid) {
          // halo pass under solid key levels (PDH/PDL/POC/TAP)
          hardSeg(0, Math.round(y) + 0.5, rightEdge, Math.round(y) + 0.5,
            TONE[d.tone].line, TONE[d.tone].halo, 1.1, []);
        } else {
          ctx.strokeStyle = TONE[d.tone].line;
          ctx.lineWidth = 0.9;
          ctx.setLineDash([5, 4]);
          ctx.beginPath();
          ctx.moveTo(0, Math.round(y) + 0.5);
          ctx.lineTo(rightEdge, Math.round(y) + 0.5);
          ctx.stroke();
          ctx.setLineDash([]);
        }
        // D-053 — no right-edge tag: the level name + price live in the
        // external MARKS legend (user directive: nothing on the canvas).
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
          const t = TONE[d.tone];
          // D-053 — thin core + halo: the hard look without fat strokes
          hardSeg(upper.x1, upper.y1, upper.x2, upper.y2, t.line, t.halo, 1.1, []);
          hardSeg(lower.x1, lower.y1, lower.x2, lower.y2, t.line, t.halo, 1.1, []);
          if (median) strokeSeg(median, "rgba(154,160,170,0.45)", 0.7, [5, 4]);
          // projections to the right edge
          const up = projectToRight(upper);
          const lo = projectToRight(lower);
          ctx.setLineDash([4, 4]);
          ctx.strokeStyle = t.line;
          ctx.lineWidth = 0.8;
          ctx.beginPath();
          ctx.moveTo(upper.x2, upper.y2);
          ctx.lineTo(rightEdge, up.yEnd);
          ctx.moveTo(lower.x2, lower.y2);
          ctx.lineTo(rightEdge, lo.yEnd);
          ctx.stroke();
          ctx.setLineDash([]);
          // D-053 — no label on the canvas (lives in the MARKS legend)
        } else if (d.kind === "trendline") {
          const s = segment(d.t1, d.p1, d.t2, d.p2);
          if (!s) continue;
          const faded = isFaded(d);
          if (faded) {
            // D-053 — broken lines: THIN, ghosted, never projected
            ctx.globalAlpha = FADED_ALPHA;
            strokeSeg(s, TONE[d.tone].line, FADED_WIDTH, [3, 4]);
            ctx.globalAlpha = 1;
          } else {
            const t = TONE[d.tone];
            hardSeg(s.x1, s.y1, s.x2, s.y2, t.line, t.halo, 1.1, []);
            const proj = projectToRight(s);
            ctx.strokeStyle = t.line;
            ctx.lineWidth = 0.8;
            ctx.setLineDash([5, 4]);
            ctx.beginPath();
            ctx.moveTo(s.x2, s.y2);
            ctx.lineTo(rightEdge, proj.yEnd);
            ctx.stroke();
            ctx.setLineDash([]);
          }
          // D-053 — no label on the canvas (lives in the MARKS legend)
        } else if (d.kind === "sweep") {
          const x = d.t ? xOf(d.t) : null;
          const y = yOf(d.price);
          if (y == null) continue;
          const xStart = x == null ? 0 : Math.max(0, x);
          if (xStart > rightEdge) continue;
          const color = d.side === "high"
            ? "rgba(248,113,113,0.9)"
            : "rgba(52,211,153,0.9)";
          ctx.strokeStyle = color;
          ctx.lineWidth = 1;
          ctx.setLineDash([2, 3]);
          ctx.beginPath();
          ctx.moveTo(xStart, Math.round(y) + 0.5);
          ctx.lineTo(rightEdge, Math.round(y) + 0.5);
          ctx.stroke();
          ctx.setLineDash([]);
          // the stop-hunt X — halo + thin hard core
          if (x != null && x >= 0 && x <= rightEdge) {
            hardSeg(x - 4, y - 4, x + 4, y + 4, color, TONE[d.tone].halo, 1.1, []);
            hardSeg(x + 4, y - 4, x - 4, y + 4, color, TONE[d.tone].halo, 1.1, []);
          }
          // D-053 — no label on the canvas (lives in the MARKS legend)
        } else if (d.kind === "structure") {
          const x = d.t ? xOf(d.t) : null;
          const y = yOf(d.price);
          if (x == null || y == null || x < 0 || x > rightEdge) continue;
          // D-053 — BOS/CHoCH diamond marker (halo + core); the words
          // ("Break of Structure" etc.) live in the MARKS legend now.
          const t = TONE[d.tone];
          const cy = d.dir === "up" ? y + 12 : y - 12;
          ctx.fillStyle = t.halo;
          diamond(x, Math.max(10, Math.min(cy, h - 10)), 8);
          ctx.fillStyle = t.line;
          diamond(x, Math.max(10, Math.min(cy, h - 10)), 4.5);
        } else if (d.kind === "arrow") {
          const x = d.t ? xOf(d.t) : null;
          const y = yOf(d.price);
          if (y == null) continue;
          const ax = x == null ? rightEdge - 40 : Math.min(x + 10, rightEdge - 24);
          const color = d.dir === "up"
            ? "rgba(103,232,249,0.95)"
            : "rgba(248,113,113,0.95)";
          const halo = d.dir === "up"
            ? "rgba(103,232,249,0.18)"
            : "rgba(248,113,113,0.18)";
          const ay = d.dir === "up" ? y + 26 : y - 26;
          arrow(ax, Math.max(12, Math.min(ay, h - 12)), d.dir, color, halo);
          // D-053 — no text next to the arrow (lives in the MARKS legend)
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
            ctx.strokeStyle = "rgba(212,175,55,0.4)";
            ctx.lineWidth = 0.8;
            ctx.setLineDash([2, 3]);
            ctx.strokeRect(fx + 0.5, Math.min(yo1, yo0) + 0.5, rightEdge - fx - 1, Math.abs(yo0 - yo1) - 1);
            ctx.setLineDash([]);
            // D-053 — no OTE text on the canvas (legend carries it)
          }
        }
        for (const lv of d.levels) {
          const y = yOf(lv.price);
          if (y == null || y < -5 || y > h + 5) continue;
          const key = lv.ratio === 0.618 || lv.ratio === 0.786;
          if (key) {
            // golden pocket: halo + hard core
            hardSeg(fx, Math.round(y) + 0.5, rightEdge, Math.round(y) + 0.5,
              "rgba(212,175,55,0.9)", TONE.gold.halo, 1, []);
          } else {
            ctx.strokeStyle = "rgba(212,175,55,0.35)";
            ctx.lineWidth = 0.7;
            ctx.setLineDash([3, 3]);
            ctx.beginPath();
            ctx.moveTo(fx, Math.round(y) + 0.5);
            ctx.lineTo(rightEdge, Math.round(y) + 0.5);
            ctx.stroke();
            ctx.setLineDash([]);
          }
          // D-053 — no FIB % tags on the canvas (legend carries it)
        }
        // the impulse leg itself (thin neutral diagonal)
        if (y0 != null && y1 != null && x0 != null) {
          ctx.strokeStyle = "rgba(154,160,170,0.4)";
          ctx.lineWidth = 0.7;
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
        // entry zone box — D-053 halo underlay + thin hard border
        ctx.fillStyle = TONE[tone].fill;
        ctx.fillRect(x0, Math.min(yZhi, yZlo), rightEdge - x0, Math.abs(yZlo - yZhi));
        ctx.strokeStyle = TONE[tone].halo;
        ctx.lineWidth = 3.4;
        ctx.setLineDash([]);
        ctx.strokeRect(x0 - 0.5, Math.min(yZhi, yZlo) - 0.5, rightEdge - x0 + 1, Math.abs(yZlo - yZhi) + 1);
        ctx.strokeStyle = TONE[tone].line;
        ctx.lineWidth = 1.1;
        ctx.strokeRect(x0 + 0.5, Math.min(yZhi, yZlo) + 0.5, rightEdge - x0 - 1, Math.abs(yZlo - yZhi) - 1);
        tag(
          `${setup.dir} SETUP${setup.status === "triggered" ? " · ENTRY TAKEN" : " · FORMING"}`,
          x0 + 6,
          Math.min(yZhi, yZlo) + 10,
          tone,
        );
      }
      const line = (
        y: number | null, color: string, halo: string, dash: number[],
        label: string, x: number,
      ) => {
        if (y == null || y < -5 || y > h + 5) return;
        // D-053 — the entry-setup lines are the one place the hard halo
        // really matters: these ARE the trade (user directive).
        hardSeg(x, Math.round(y) + 0.5, rightEdge, Math.round(y) + 0.5,
          color, halo, 1.1, dash);
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
      line(yE, "rgba(212,175,55,0.95)", TONE.gold.halo, [], `ENTRY ${setup.entry}`, x0);
      line(yS, "rgba(248,113,113,0.9)", TONE.bear.halo, [5, 4], `STOP LOSS ${setup.sl}`, x0);
      line(yT, "rgba(52,211,153,0.9)", TONE.bull.halo, [5, 4], `TAKE PROFIT ${setup.tp} · RR ${setup.rr}`, x0);
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
        ctx.lineWidth = 0.7;
        ctx.setLineDash([2, 4]);
        ctx.beginPath();
        ctx.moveTo(0, Math.round(y) + 0.5);
        ctx.lineTo(rightEdge, Math.round(y) + 0.5);
        ctx.stroke();
        ctx.setLineDash([]);
        // D-053 — subtle THIN label (secondary info, never the star)
        ctx.font = "400 9px ui-sans-serif, system-ui, sans-serif";
        const tw = ctx.measureText(label).width;
        ctx.fillStyle = "rgba(11,13,18,0.8)";
        ctx.fillRect(6, y - 11, tw + 6, 13);
        ctx.fillStyle = "rgba(154,160,170,0.75)";
        ctx.fillText(label, 9, y - 2);
      };
      line(sig.entry, "rgba(212,175,55,0.6)", `${sig.direction} ENTRY`);
      line(sig.sl, "rgba(248,113,113,0.45)", `${sig.direction} SL`);
      line(sig.tp, "rgba(52,211,153,0.45)", `${sig.direction} TP`);
    }
    } catch (err) {
      // D-052 — a drawing failure must NEVER blank the whole chart: log
      // and keep the candles + markers alive.
      console.warn("[PriceChart] overlay draw skipped:", err);
    }
  }, [drawings, signals, selectedSignal, tf, isSignals]);

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
    // D-053 — container resizes (fullscreen enter/exit, layout shifts)
    // must re-sync the overlay bitmap immediately, not 500ms later.
    const ro = typeof ResizeObserver !== "undefined"
      ? new ResizeObserver(() => window.requestAnimationFrame(cb))
      : null;
    if (ro && containerRef.current) ro.observe(containerRef.current);
    return () => {
      try {
        chart.timeScale().unsubscribeVisibleLogicalRangeChange(rangeHandler);
      } catch {
        /* disposed */
      }
      window.clearInterval(iv);
      window.removeEventListener("resize", cb);
      ro?.disconnect();
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
  const layerChips: { key: LayerKey; label: string }[] = [
    { key: "setup", label: "Setup" },
    { key: "zones", label: "Zones" },
    { key: "levels", label: "Levels" },
    { key: "structure", label: "Structure" },
    { key: "fib", label: "Fibonacci" },
    { key: "whales", label: "Whales" },
  ];
  // D-053 — the external MARKS legend lives BELOW the canvas (outside
  // the chart): the on-chart small writings are gone, the words + prices
  // live here instead (user directive).
  const legendMarks = useMemo(
    () => (isSignals ? [] : buildLegend(allDrawings, tf)),
    [isSignals, allDrawings, tf],
  );
  const visibleLegendMarks = useMemo(
    () => legendMarks.filter((m) => m.layer === "setup" || layersRef.current[m.layer]),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [legendMarks, layers],
  );
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
    <div className="flex h-full min-h-0 w-full flex-col overflow-hidden rounded-2xl border border-zinc-800/80 bg-[#0b0d12]">
      {/* chart area (the canvas proper — everything below is OUTSIDE) */}
      <div className="relative min-h-0 flex-1">
        <div ref={containerRef} className="h-full w-full" aria-label={`${symbol} ${tf} chart`} />
        {/* D-052 — professional drawing overlay (pointer-transparent).
         *  z-10 is REQUIRED: lightweight-charts paints its own canvases at
         *  z-index 1/2/3 — an z-auto overlay would be buried UNDER the
         *  chart (that was the "drawings invisible" bug). */}
        <canvas
          ref={overlayRef}
          className="pointer-events-none absolute inset-0 z-10 h-full w-full"
          aria-hidden
        />
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
                  ? "Loading institutional market history…"
                  : `Loading real ${symbol} ${tf} market data…`}
            </p>
          </div>
        )}
      </div>

      {/* D-053 — external MARKS legend: the layer toggles + every drawing
       *  listed with its compact name and price. Full-variant only — the
       *  Chart tab stays pure (setup + signals). */}
      {!isSignals && (
        <div className="shrink-0 border-t border-zinc-800/80 bg-zinc-950/70 px-2.5 pb-2 pt-1.5">
          <div className="flex min-w-0 items-center gap-1.5">
            <span className="shrink-0 text-[9px] font-bold uppercase tracking-wider text-zinc-600">
              Marks
            </span>
            <span className="shrink-0 text-[9px] font-mono tabular-nums text-zinc-500">
              {visibleLegendMarks.length}
            </span>
            <div className="flex min-w-0 flex-1 gap-1 overflow-x-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
              {layerChips.map((l) => (
                <button
                  key={l.key}
                  type="button"
                  onClick={() => setLayers((s) => ({ ...s, [l.key]: !s[l.key] }))}
                  className={`shrink-0 rounded-full border px-2 py-0.5 text-[9px] font-bold tracking-wide transition-colors ${
                    layers[l.key]
                      ? "border-gold/50 bg-gold/15 text-gold"
                      : "border-zinc-700/70 bg-zinc-900/70 text-zinc-500"
                  }`}
                >
                  {l.label}
                </button>
              ))}
            </div>
          </div>
          <div className="mt-1.5 flex max-h-[52px] flex-wrap gap-1 overflow-y-auto [scrollbar-width:thin]">
            {visibleLegendMarks.length === 0 ? (
              <span className="px-0.5 py-0.5 text-[9px] text-zinc-600">
                No marks in view — zones, levels and structure appear here as
                the engine maps them.
              </span>
            ) : (
              <>
                {visibleLegendMarks.slice(0, 14).map((m) => (
                  <span
                    key={m.key}
                    title={m.text}
                    className={`flex shrink-0 items-center gap-1 rounded-md border px-1.5 py-0.5 font-mono text-[9px] leading-none ${
                      m.faded
                        ? "border-zinc-800/60 bg-zinc-900/40 font-normal text-zinc-500"
                        : "border-zinc-700/80 bg-zinc-900/70 font-semibold"
                    }`}
                  >
                    <span
                      className="h-1.5 w-1.5 shrink-0 rounded-[2px]"
                      style={{
                        backgroundColor: m.swatch,
                        opacity: m.faded ? 0.45 : 1,
                      }}
                    />
                    <span style={{ color: m.faded ? undefined : m.swatch }}>
                      {m.short}
                    </span>
                    {m.detail && (
                      <span className="text-zinc-500">{m.detail}</span>
                    )}
                  </span>
                ))}
                {visibleLegendMarks.length > 14 && (
                  <span className="flex shrink-0 items-center rounded-md border border-zinc-800 bg-zinc-900/50 px-1.5 py-0.5 font-mono text-[9px] leading-none text-zinc-500">
                    +{visibleLegendMarks.length - 14}
                  </span>
                )}
              </>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
