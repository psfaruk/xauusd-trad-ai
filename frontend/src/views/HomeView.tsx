/**
 * HomeView (D-041/D-044/D-053) — mobile-app style home: live price hero,
 * the FULL analysis chart (every drawing layer + signals, fullscreen —
 * user directive), AI auto-trade status, latest signal, performance
 * stats, the USER's own trading account (practice plane — isolated per
 * user). Skeletons until REAL data arrives (user requirement: loading
 * until data comes).
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { useTick } from "../state/feed";
import { getAnalysis } from "../lib/api";
import ErrorBoundary from "../components/ErrorBoundary";
import PriceChart from "../components/PriceChart";
import {
  TIMEFRAMES,
  type AnalysisResponse,
  type BrokerConnection,
  type Candle,
  type Mt5Status,
  type Signal,
  type StatsResponse,
  type Timeframe,
  type TradingStatus,
} from "../types";
import { Badge, Btn, Card, Dot, SectionTitle, Skeleton, Stat } from "../components/ui";
import { LatestSignalCard } from "../components/SignalDetail";
import { CandlePulseCard } from "../components/StrategyRadar";
import type { TickSnapshot } from "../state/feed";

interface Props {
  symbol: string;
  symbols: string[];
  onSymbolChange: (s: string) => void;
  mt5: Mt5Status | null;
  broker: BrokerConnection | null;
  tradingAccount: TradingStatus | null;
  autoArmed: boolean;
  autoWhy: { code: string; text: string } | null;
  signals: Signal[];
  stats: StatsResponse | null;
  onOpenAi: () => void;
  onOpenSignal: (id: string) => void;
  /** D-053 — the home chart shares the global view state */
  tf: Timeframe;
  onTfChange: (tf: Timeframe) => void;
  candles: Candle[];
  candlesLoading: boolean;
  wsConnected: boolean;
  onDesync: () => void;
  token: string;
}

function PriceHero({
  symbol,
  tick,
  market,
}: {
  symbol: string;
  tick: TickSnapshot | null;
  market: "open" | "closed" | "unavailable" | "unknown";
}) {
  const loading = tick === null;
  const spread = tick ? tick.ask - tick.bid : null;
  return (
    <Card className="relative overflow-hidden">
      <div
        className="pointer-events-none absolute -right-16 -top-16 h-48 w-48 rounded-full bg-gold/10 blur-3xl"
        aria-hidden
      />
      <div className="flex min-w-0 items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <h1 className="truncate text-lg font-bold tracking-tight text-zinc-100">
              {symbol}
            </h1>
            <Badge tone="gold">XAU / Gold</Badge>
          </div>
          <p className="mt-0.5 text-[10px] text-zinc-500">
            Institutional market feed{tick?.tps != null && ` · ${tick.tps.toFixed(1)} ticks/s`}
          </p>
        </div>
        <Badge tone={market === "open" ? "green" : market === "closed" ? "amber" : "red"} pulse={market === "open"}>
          {market === "open" ? "MARKET OPEN" : market === "closed" ? "MARKET CLOSED" : "FEED OFFLINE"}
        </Badge>
      </div>

      <div className="mt-3 min-w-0">
        {loading ? (
          <Skeleton className="h-11 w-44" />
        ) : (
          <p className="truncate font-mono text-[40px] font-bold leading-none tabular-nums text-zinc-50">
            {tick!.bid.toFixed(2)}
          </p>
        )}
        <div className="mt-2 flex min-w-0 flex-wrap items-center gap-x-4 gap-y-1 text-[11px]">
          {loading ? (
            <Skeleton className="h-4 w-40" />
          ) : (
            <>
              <span className="font-mono tabular-nums text-zinc-400">
                bid <span className="text-zinc-200">{tick!.bid.toFixed(2)}</span>
                <span className="mx-1.5 text-zinc-600">/</span>
                ask <span className="text-zinc-200">{tick!.ask.toFixed(2)}</span>
              </span>
              {spread != null && (
                <span className="font-mono tabular-nums text-zinc-500">
                  spread <span className="text-zinc-300">{spread.toFixed(2)}</span>
                </span>
              )}
            </>
          )}
        </div>
      </div>
    </Card>
  );
}

/* ------------------------------------------------ D-053 home chart section */

/**
 * HomeChartSection — the SAME chart as the Chart tab, in "full" variant:
 * every drawing layer (FVG / OB / liquidity / structure / fib / whales)
 * + the entry setup + signal markers. The user can blow it up to FULL
 * SCREEN (native Fullscreen API; CSS-overlay fallback for iOS Safari).
 */
function HomeChartSection({
  symbol,
  tf,
  onTfChange,
  candles,
  candlesLoading,
  signals,
  market,
  wsConnected,
  onDesync,
  token,
  tick,
}: {
  symbol: string;
  tf: Timeframe;
  onTfChange: (tf: Timeframe) => void;
  candles: Candle[];
  candlesLoading: boolean;
  signals: Signal[];
  market: "open" | "closed" | "unavailable" | "unknown";
  wsConnected: boolean;
  onDesync: () => void;
  token: string;
  tick: TickSnapshot | null;
}) {
  const [analysis, setAnalysis] = useState<AnalysisResponse | null>(null);

  // D-052 pattern — poll the ICT/SMC analysis snapshot (backend caches
  // ~20s; the analysis itself only changes on bar close)
  useEffect(() => {
    if (!token) return;
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

  /* -------------------------------------------------- fullscreen (D-053) */
  const wrapRef = useRef<HTMLElement | null>(null);
  const [overlayFs, setOverlayFs] = useState(false); // CSS fallback (iOS)
  const [nativeFs, setNativeFs] = useState(false);

  useEffect(() => {
    const onFs = () =>
      setNativeFs(document.fullscreenElement === wrapRef.current);
    document.addEventListener("fullscreenchange", onFs);
    return () => document.removeEventListener("fullscreenchange", onFs);
  }, []);

  // ESC + scroll-lock while the overlay fallback is up
  useEffect(() => {
    if (!overlayFs) return;
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOverlayFs(false);
    };
    window.addEventListener("keydown", onKey);
    return () => {
      document.body.style.overflow = prev;
      window.removeEventListener("keydown", onKey);
    };
  }, [overlayFs]);

  const isFs = overlayFs || nativeFs;

  const enterFs = async () => {
    const el = wrapRef.current;
    if (!el) return;
    const req = (
      el as HTMLElement & {
        requestFullscreen?: (o?: FullscreenOptions) => Promise<void>;
      }
    ).requestFullscreen;
    if (typeof req === "function") {
      try {
        await req.call(el);
        return;
      } catch (err) {
        const name = (err as { name?: string })?.name ?? "";
        if (name === "AbortError") return; // user cancelled — stay put
        /* fall through to the CSS-overlay fallback */
      }
    }
    setOverlayFs(true);
  };

  const exitFs = async () => {
    if (document.fullscreenElement === wrapRef.current) {
      try {
        await document.exitFullscreen();
      } catch {
        /* ignore */
      }
    }
    setOverlayFs(false);
  };

  const symbolSignals = useMemo(
    () => signals.filter((s) => s.symbol === symbol).slice(0, 40),
    [signals, symbol],
  );
  const activeSignal = useMemo(
    () =>
      signals.find((s) => s.symbol === symbol && s.status === "active") ?? null,
    [signals, symbol],
  );

  return (
    <section
      ref={wrapRef}
      className={`flex min-w-0 flex-col rounded-2xl ${
        isFs
          ? "fixed inset-0 z-[130] bg-zinc-950 p-2 sm:p-3"
          : ""
      }`}
      aria-label="Live chart"
    >
      {/* header: title + price + fullscreen toggle */}
      <div className="mb-2 flex min-w-0 items-center gap-2">
        <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md bg-gold/15 text-[11px] font-black text-gold">
          <svg className="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2">
            <path d="M3 20h18M7 16V9m5 7V5m5 11v-5" strokeLinecap="round" />
          </svg>
        </span>
        <h2 className="shrink-0 text-sm font-bold tracking-tight text-zinc-100">
          Live Chart
        </h2>
        <span className="shrink-0 text-[10px] font-semibold text-zinc-500">
          {symbol} · {tf}
        </span>
        {tick ? (
          <span className="ml-1 truncate font-mono text-sm font-bold tabular-nums text-zinc-200">
            {tick.bid.toFixed(2)}
          </span>
        ) : null}
        <span className="ml-auto flex shrink-0 items-center gap-1.5">
          {isFs ? (
            <button
              type="button"
              onClick={() => void exitFs()}
              className="flex items-center gap-1 rounded-lg border border-gold/50 bg-gold/15 px-2.5 py-1.5 text-[11px] font-bold text-gold"
              aria-label="Exit fullscreen"
            >
              <svg className="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4">
                <path d="M9 3v6H3m12-6v6h6M9 21v-6H3m12 6v-6h6" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
              Exit
            </button>
          ) : (
            <button
              type="button"
              onClick={() => void enterFs()}
              className="flex items-center gap-1 rounded-lg border border-zinc-700 bg-zinc-900 px-2.5 py-1.5 text-[11px] font-bold text-zinc-300 transition-colors hover:border-gold/50 hover:text-gold"
              aria-label="View chart fullscreen"
            >
              <svg className="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4">
                <path d="M15 3h6v6M9 21H3v-6m18-9-7 7M3 15l7-7" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
              Fullscreen
            </button>
          )}
        </span>
      </div>

      {/* timeframe pills (shared with the Chart tab) */}
      <div className="mb-2 flex min-w-0 gap-1.5 overflow-x-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
        {TIMEFRAMES.map((t) => (
          <button
            key={t}
            type="button"
            onClick={() => onTfChange(t)}
            className={`shrink-0 rounded-lg border px-3 py-1 font-mono text-[11px] font-bold transition-colors ${
              t === tf
                ? "border-gold/60 bg-gold/15 text-gold"
                : "border-zinc-800 bg-zinc-900/60 text-zinc-500 hover:text-zinc-300"
            }`}
          >
            {t}
          </button>
        ))}
      </div>

      {/* the chart — everything on it (zones, levels, structure, fib,
       * whales, setup, signals) + the external MARKS legend */}
      <div
        className={
          isFs
            ? "min-h-0 flex-1"
            : "h-[44vh] min-h-[300px] sm:h-[46vh] lg:h-[48vh]"
        }
      >
        <ErrorBoundary label="Home chart">
          <PriceChart
            symbol={symbol}
            tf={tf}
            candles={candles}
            candlesLoading={candlesLoading}
            signals={symbolSignals}
            selectedSignal={activeSignal}
            market={market}
            wsConnected={wsConnected}
            onDesync={onDesync}
            analysis={analysis}
            variant="full"
          />
        </ErrorBoundary>
      </div>
    </section>
  );
}

export default function HomeView({
  symbol,
  symbols,
  onSymbolChange,
  mt5,
  broker,
  tradingAccount,
  autoArmed,
  autoWhy,
  signals,
  stats,
  onOpenAi,
  onOpenSignal,
  tf,
  onTfChange,
  candles,
  candlesLoading,
  wsConnected,
  onDesync,
  token,
}: Props) {
  const tick = useTick(symbol);
  const market = mt5?.feed?.symbols?.[symbol]?.market ?? "unknown";
  const feedProvider = mt5?.feed?.symbols?.[symbol];
  const latest = signals[0] ?? null;
  const account = tradingAccount?.account ?? null;
  const brokerLinked = broker?.status === "connected" || broker?.status === "linked";
  const winRate = stats?.win_rate;
  const expectancy = stats?.expectancy;

  return (
    <div className="flex min-w-0 flex-col gap-3">
      {/* symbol pills */}
      {symbols.length > 1 && (
        <div className="flex min-w-0 gap-2 overflow-x-auto pb-0.5 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
          {symbols.map((s) => (
            <button
              key={s}
              type="button"
              onClick={() => onSymbolChange(s)}
              className={`shrink-0 rounded-full border px-4 py-1.5 text-xs font-semibold transition-colors ${
                s === symbol
                  ? "border-gold/60 bg-gold/15 text-gold"
                  : "border-zinc-700 bg-zinc-900 text-zinc-400 hover:text-zinc-200"
              }`}
            >
              {s}
            </button>
          ))}
        </div>
      )}

      <PriceHero symbol={symbol} tick={tick} market={market} />

      {/* D-053 — the FULL analysis chart (every drawing + signals), with
       * fullscreen (user directive: "হোম পেজে চার্টটি ইউজার full screen
       * করে দেখতে পারবে, এবং সমস্ত ড্রয়িং হোম ট্যাবের চার্ট থেকে দেখা
       * যাবে") */}
      <HomeChartSection
        symbol={symbol}
        tf={tf}
        onTfChange={onTfChange}
        candles={candles}
        candlesLoading={candlesLoading}
        signals={signals}
        market={market}
        wsConnected={wsConnected}
        onDesync={onDesync}
        token={token}
        tick={tick}
      />

      {/* D-051 — live per-candle buyer/seller dominance (tick-driven) */}
      <CandlePulseCard symbol={symbol} />

      {/* AI auto trading */}
      <Card>
        <SectionTitle
          title="AI Auto-Trading"
          right={
            autoArmed ? (
              <Badge tone="green" pulse>ARMED</Badge>
            ) : (
              <Badge tone="amber">PAUSED</Badge>
            )
          }
        />
        <p className="min-w-0 text-[11px] leading-relaxed text-zinc-400">
          {autoArmed
            ? "Engine armed — every M1 close is analyzed and confirmed signals place real orders automatically."
            : autoWhy
              ? autoWhy.text
              : "Arm the engine to let it place real orders on confirmed signals."}
        </p>
        <div className="mt-3 grid grid-cols-3 gap-2">
          <Stat label="Signals 30d" value={stats?.total_signals ?? "—"}
            loading={stats === null} />
          <Stat
            label="Win rate"
            value={winRate != null ? `${(winRate * 100).toFixed(0)}%` : "—"}
            tone={winRate != null && winRate >= 0.5 ? "up" : winRate != null ? "down" : "default"}
            loading={stats === null}
          />
          <Stat
            label="Expectancy"
            value={expectancy != null ? `${expectancy >= 0 ? "+" : ""}${expectancy.toFixed(2)}R` : "—"}
            tone={expectancy != null && expectancy >= 0 ? "up" : expectancy != null ? "down" : "default"}
            loading={stats === null}
          />
        </div>
        <div className="mt-3">
          <Btn variant="gold" onClick={onOpenAi} className="w-full">
            Open AI Trading →
          </Btn>
        </div>
      </Card>

      <LatestSignalCard signal={latest} onOpen={() => latest && onOpenSignal(latest.id)} />

      {/* the user's own trading account (D-044 — isolated practice plane) */}
      <Card>
        <SectionTitle
          title="Your Trading Account"
          right={
            tradingAccount?.connected ? (
              <span className="flex items-center gap-1.5 text-[10px] font-semibold text-emerald-400">
                <Dot tone="green" /> active
              </span>
            ) : (
              <span className="flex items-center gap-1.5 text-[10px] font-semibold text-amber-400">
                <Dot tone="amber" /> starting…
              </span>
            )
          }
        />
        <div className="grid grid-cols-2 gap-2">
          <Stat
            label="Balance"
            value={account?.balance != null ? account.balance.toFixed(2) : "—"}
            loading={tradingAccount === null}
            hint={account?.currency ?? "USD"}
          />
          <Stat
            label="Equity"
            value={account?.equity != null ? account.equity.toFixed(2) : "—"}
            loading={tradingAccount === null}
            tone={
              account?.balance != null && account?.equity != null
                ? account.equity >= account.balance ? "up" : "down"
                : "default"
            }
          />
        </div>
        <p className="mt-2.5 truncate text-[10px] text-zinc-500">
          {brokerLinked
            ? `Broker linked · ${broker?.login_masked ?? broker?.server ?? "—"}`
            : "Practice account · link your broker in Settings"}
        </p>
        <div className="mt-3">
          <Btn variant="gold" onClick={onOpenAi} className="w-full">
            Trade with AI →
          </Btn>
        </div>
      </Card>

      {/* feed transparency */}
      <Card>
        <SectionTitle title="Market Data" />
        <div className="flex flex-wrap items-center gap-2 text-[10px] text-zinc-500">
          <Badge tone={feedProvider?.mt5 || feedProvider?.provider ? "green" : "zinc"}>
            {feedProvider?.mt5 ? "Institutional feed" : feedProvider?.provider ?? "—"}
          </Badge>
          {mt5?.status === "connected" && <Badge tone="green">platform connected</Badge>}
          {mt5?.status === "reconnecting" && <Badge tone="amber" pulse>reconnecting…</Badge>}
          {mt5?.status === "disconnected" && <Badge tone="red">disconnected</Badge>}
          {feedProvider?.spread != null && (
            <span className="font-mono tabular-nums">spread {feedProvider.spread.toFixed(2)}</span>
          )}
        </div>
      </Card>
    </div>
  );
}
