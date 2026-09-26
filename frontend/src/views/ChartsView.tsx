/**
 * ChartsView (D-041/D-053) — the chart tab: pair + timeframe pills, the
 * live chart, and the SIGNAL ANALYSIS panel (when each signal fired, its
 * entries, and the exact factors the AI verified — the user's requested
 * panel).
 *
 * D-053 — this tab's chart runs in "signals" mode: ONLY the entry-setup
 * drawing + signal markers render. The problem-analysis marks (FVG / OB /
 * liquidity / structure) live on the HOME chart instead — user directive:
 * they bury the signals here ("এতে করে সিগন্যাল বুঝা যায় না").
 */

import { useEffect, useMemo, useState } from "react";
import { useTick } from "../state/feed";
import { getAnalysis } from "../lib/api";
import { sameMarket } from "../lib/liveSetup";
import { marketMeta } from "../lib/markets";
import SymbolSelect from "../components/SymbolSelect";
import {
  type AnalysisResponse,
  type Candle,
  type Mt5Status,
  type Signal,
  type Timeframe,
} from "../types";
import ErrorBoundary from "../components/ErrorBoundary";
import PriceChart from "../components/PriceChart";
import { SignalDetail, SignalRow, fmtTime } from "../components/SignalDetail";
import { Badge, Card, Dot, EmptyState, SectionTitle } from "../components/ui";

interface Props {
  symbol: string;
  symbols: string[];
  onSymbolChange: (s: string) => void;
  tf: Timeframe;
  onTfChange: (tf: Timeframe) => void;
  candles: Candle[];
  candlesLoading: boolean;
  signals: Signal[];
  mt5: Mt5Status | null;
  wsState: "connecting" | "open" | "closed";
  onDesync: () => void;
  focusSignalId: string | null;
  onFocusSignalConsumed: () => void;
  token: string;
}

export default function ChartsView({
  symbol,
  symbols,
  onSymbolChange,
  tf,
  onTfChange,
  candles,
  candlesLoading,
  signals,
  mt5,
  wsState,
  onDesync,
  focusSignalId,
  onFocusSignalConsumed,
  token,
}: Props) {
  const tick = useTick(symbol);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [analysis, setAnalysis] = useState<AnalysisResponse | null>(null);

  // D-042 — poll the ICT/SMC analysis snapshot (backend caches ~20s;
  // the analysis itself only changes on bar close)
  useEffect(() => {
    let alive = true;
    const load = () => {
      getAnalysis(token, symbol)
        .then((res) => alive && setAnalysis(res))
        .catch(() => {
          /* analysis stays on the last snapshot during blips */
        });
    };
    load();
    const iv = window.setInterval(load, 20_000);
    return () => {
      alive = false;
      window.clearInterval(iv);
    };
  }, [token, symbol]);

  // D-074 — sameMarket: a suffixed symbol spelling (XAUUSDm vs XAUUSD)
  // must never hide this pair's signals from the panel again
  const symbolSignals = useMemo(
    () => signals.filter((s) => sameMarket(s.symbol, symbol)),
    [signals, symbol],
  );
  const chartSignals = useMemo(
    () => symbolSignals.slice(0, 40),
    [symbolSignals],
  );

  // follow "open analysis" deep-links from other tabs
  useEffect(() => {
    if (focusSignalId) {
      setSelectedId(focusSignalId);
      onFocusSignalConsumed();
    }
  }, [focusSignalId, onFocusSignalConsumed]);

  // default selection: newest signal
  const selected = useMemo(
    () =>
      symbolSignals.find((s) => s.id === selectedId) ??
      symbolSignals[0] ??
      null,
    [symbolSignals, selectedId],
  );

  const market = mt5?.feed?.symbols?.[symbol]?.market ?? "unknown";
  const feedProvider = mt5?.feed?.symbols?.[symbol];

  return (
    <div className="flex min-w-0 flex-col gap-3">
      {/* header: pair dropdown + live quote (the TF selector lives in the
       *  chart's own header now — D-058 user directive; the pairs moved
       *  into ONE dropdown — D-076 user directive: "জায়গা বাঁচবে").
       *  relative z-30 — Card's backdrop-blur creates a stacking context
       *  that TRAPS the dropdown popover (z-40) inside it; without this
       *  the chart's loading overlay (absolute inset-0 z-10, root context)
       *  painted OVER the open dropdown options (the "can't click USTEC"
       *  e2e catch). z-30 > z-10 lifts the whole toolbar above the chart. */}
      <Card padded={false} className="relative z-30 p-2.5">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <SymbolSelect
            symbol={symbol}
            symbols={symbols}
            onChange={onSymbolChange}
            compact
            quote={tick ? tick.bid.toFixed(marketMeta(symbol).digits) : null}
          />
          <div className="ml-auto flex shrink-0 items-center gap-2">
            {tick ? (
              <span className="font-mono text-sm font-bold tabular-nums text-zinc-100">
                {tick.bid.toFixed(marketMeta(symbol).digits)}
              </span>
            ) : (
              <span className="h-4 w-14 animate-pulse rounded bg-zinc-800" />
            )}
            <span
              className={`flex items-center gap-1 text-[10px] font-semibold ${
                wsState === "open"
                  ? "text-emerald-400"
                  : wsState === "connecting"
                    ? "text-amber-400"
                    : "text-red-400"
              }`}
            >
              <Dot tone={wsState === "open" ? "green" : wsState === "connecting" ? "amber" : "red"} />
              {wsState === "open" ? "live" : wsState === "connecting" ? "connecting" : "offline"}
            </span>
            {feedProvider?.mt5 && (
              <span className="shrink-0 self-center text-[10px] text-zinc-600">
                Institutional feed
              </span>
            )}
          </div>
        </div>
      </Card>

      {/* D-058 — the desktop split: chart fills the main column (TF
       *  dropdown + marks + fullscreen live in its own header), the
       *  analysis + signal panels stack in the right rail. Mobile keeps
       *  the single column. */}
      <div className="flex min-w-0 flex-col gap-3 xl:flex-row xl:items-start">
        {/* chart — D-053: signals variant (setup + signal marks ONLY) */}
        <div className="min-w-0 flex-1 xl:sticky xl:top-16">
          <div className="h-[52vh] min-h-[300px] w-full min-w-0 sm:min-h-[320px] md:h-[60vh] lg:h-[calc(100vh-10rem)]">
            <ErrorBoundary label="Chart">
              <PriceChart
                symbol={symbol}
                tf={tf}
                onTfChange={onTfChange}
                candles={candles}
                candlesLoading={candlesLoading}
                signals={chartSignals}
                /* D-074 — inspection lines for an EXPLICIT row pick (any
                 * status — pending/active/won/lost); the default (no
                 * pick) draws nothing extra: the live setup ink already
                 * IS the current trade on the canvas */
                selectedSignal={selectedId ? selected : null}
                market={market}
                wsConnected={wsState === "open"}
                onDesync={onDesync}
                analysis={analysis}
                variant="signals"
                headerQuote={tick?.bid ?? null}
              />
            </ErrorBoundary>
          </div>
        </div>

        {/* the analysis rail */}
        <div className="flex w-full min-w-0 flex-col gap-3 xl:w-[340px] 2xl:w-[380px] xl:shrink-0">
          {/* D-042 — live ICT/SMC analysis strip */}
          <LiveAnalysisStrip analysis={analysis} tf={tf} />

          {/* signal analysis panel */}
          <Card>
            <SectionTitle
              title="Signal Analysis"
              right={
                <Badge tone="zinc">
                  {symbolSignals.length} signal{symbolSignals.length === 1 ? "" : "s"}
                </Badge>
              }
            />
            {symbolSignals.length === 0 ? (
              <EmptyState
                title="No signals yet for this pair"
                hint="The engine analyzes every M1 close (H1 trend + M5/M15 confirmation + pattern trigger). The first confirmed signal appears here with its full analysis."
              />
            ) : (
              <div className="flex min-w-0 flex-col gap-3">
                {/* recent signals list */}
                <div className="flex max-h-64 min-w-0 flex-col gap-1.5 overflow-y-auto pr-1 xl:max-h-[46vh]">
                  {symbolSignals.slice(0, 30).map((s) => (
                    <SignalRow
                      key={s.id}
                      signal={s}
                      selected={selected?.id === s.id}
                      onSelect={() => setSelectedId(s.id)}
                    />
                  ))}
                </div>
                {selected && (
                  <div className="border-t border-zinc-800/70 pt-3">
                    <SignalDetail signal={selected} />
                  </div>
                )}
              </div>
            )}
          </Card>

          {/* engine activity (recent log lines) */}
          {selected && (
            <p className="px-1 text-center text-[10px] text-zinc-600">
              Signal times are UTC · entry {selected.entry.toFixed(2)} · taken {fmtTime(selected.ts)}
            </p>
          )}
        </div>
      </div>
    </div>
  );
}

/* ------------------------------------------- D-042 live analysis strip */

function trendBadge(trend: string | undefined): { tone: "green" | "red" | "zinc"; label: string } {
  if (trend === "bullish") return { tone: "green", label: "BULL" };
  if (trend === "bearish") return { tone: "red", label: "BEAR" };
  return { tone: "zinc", label: "FLAT" };
}

function LiveAnalysisStrip({
  analysis,
  tf,
}: {
  analysis: AnalysisResponse | null;
  tf: Timeframe;
}) {
  const snap = analysis?.per_tf?.[tf] ?? null;
  const mtf = analysis?.mtf;
  return (
    <Card>
      <SectionTitle
        title="Live ICT Analysis"
        right={
          mtf ? (
            <Badge tone={mtf.bias === "bullish" ? "green" : mtf.bias === "bearish" ? "red" : "zinc"}>
              MTF {mtf.bias} · {mtf.score > 0 ? "+" : ""}{mtf.score}
            </Badge>
          ) : undefined
        }
      />
      {!snap || !snap.ok ? (
        <EmptyState
          title="Analysis warming up"
          hint="Market structure, order blocks, liquidity pools and whale activity stream here on every bar close."
        />
      ) : (
        <div className="flex min-w-0 flex-col gap-2.5">
          {/* per-TF structure row */}
          <div className="flex min-w-0 gap-1.5 overflow-x-auto pb-0.5 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
            {Object.entries(analysis?.per_tf ?? {})
              .filter(([, s]) => s?.ok)
              .map(([tfKey, s]) => {
                const b = trendBadge(s.structure?.trend);
                return (
                  <span
                    key={tfKey}
                    className={`flex shrink-0 items-center gap-1.5 rounded-lg border px-2.5 py-1.5 ${
                      tfKey === tf ? "border-gold/40 bg-gold/5" : "border-zinc-800 bg-zinc-900/50"
                    }`}
                  >
                    <span className="font-mono text-[10px] font-bold text-zinc-400">{tfKey}</span>
                    <Badge tone={b.tone}>{b.label}</Badge>
                    {s.structure?.last_event && (
                      <span className="text-[9px] text-zinc-500">
                        {s.structure.last_event.kind}{" "}
                        {s.structure.last_event.dir === "up" ? "↑" : "↓"}
                      </span>
                    )}
                  </span>
                );
              })}
          </div>

          {/* key numbers */}
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
            <StripStat label="RSI" value={snap.indicators.rsi.toFixed(0)} />
            <StripStat
              label="ADX"
              value={snap.indicators.adx.adx.toFixed(0)}
              hint={snap.indicators.adx.adx > 25 ? "trending" : "ranging"}
            />
            <StripStat label="ATR" value={snap.atr.toFixed(2)} />
            <StripStat
              label="VWAP"
              value={snap.indicators.vwap.toFixed(2)}
              hint={snap.indicators.vwap_rel}
            />
          </div>

          {/* whale / manipulation context */}
          <div className="flex min-w-0 flex-col gap-1.5">
            {snap.whales?.last ? (
              <p className="rounded-lg border border-violet-500/20 bg-violet-500/5 px-3 py-2 text-[11px] leading-relaxed text-zinc-300">
                <span className="font-semibold text-violet-300">Whale watch:</span>{" "}
                {snap.whales.last.note} · {fmtTime(snap.whales.last.t)} (vol z{snap.whales.last.vol_z})
                {snap.whales.bias !== "neutral" && (
                  <span className="ml-1 font-semibold text-zinc-400">
                    — recent flow: {snap.whales.buy_events} buy vs {snap.whales.sell_events} sell events
                  </span>
                )}
              </p>
            ) : (
              <p className="rounded-lg border border-zinc-800 bg-zinc-900/40 px-3 py-2 text-[11px] text-zinc-500">
                No unusual institutional volume in the last window — whale events
                (bank entries, stop hunts, absorption) appear here the moment
                volume spikes.
              </p>
            )}
            {snap.premium_discount?.state && (
              <p className="text-[10px] text-zinc-500">
                Dealing range {snap.premium_discount.range_lo?.toFixed(2)}–{snap.premium_discount.range_hi?.toFixed(2)} · price in{" "}
                <span className="font-semibold text-zinc-400">{snap.premium_discount.state}</span>
                {snap.volume_profile?.poc != null && (
                  <> · POC {snap.volume_profile.poc.toFixed(2)}</>
                )}
                {snap.liquidity?.levels?.length ? (
                  <> · {snap.liquidity.levels.length} liquidity pools mapped</>
                ) : null}
              </p>
            )}
          </div>
        </div>
      )}
    </Card>
  );
}

function StripStat({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="min-w-0 rounded-lg border border-zinc-800/80 bg-zinc-900/40 px-2.5 py-1.5">
      <p className="text-[9px] font-semibold uppercase tracking-wider text-zinc-500">{label}</p>
      <p className="truncate font-mono text-xs font-semibold text-zinc-200 tabular-nums">
        {value}
        {hint && <span className="ml-1 text-[9px] font-normal text-zinc-500">{hint}</span>}
      </p>
    </div>
  );
}
