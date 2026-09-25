/**
 * HomeView (D-041/D-044/D-053) — mobile-app style home: live price hero,
 * the FULL analysis chart (every drawing layer + signals, fullscreen —
 * user directive), AI auto-trade status, latest signal, performance
 * stats, the USER's own trading account (practice plane — isolated per
 * user). Skeletons until REAL data arrives (user requirement: loading
 * until data comes).
 */

import { useEffect, useMemo, useState } from "react";
import { useTick } from "../state/feed";
import { getAnalysis } from "../lib/api";
import { sameMarket } from "../lib/liveSetup";
import ErrorBoundary from "../components/ErrorBoundary";
import PriceChart from "../components/PriceChart";
import {
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
  /** D-075 — separate user interfaces: the ADMIN's account card shows
   * the INSTITUTION Exness terminal account (their real trading
   * account); regular users keep their own practice plane. */
  isAdmin: boolean;
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

/* ------------------------------------------------ D-053/D-058 home chart */

/**
 * HomeChartSection — the SAME chart as the Chart tab, in "full" variant:
 * every drawing layer (FVG / OB / liquidity / structure / fib / whales /
 * EMA momentum / kill-zone bands) + the entry setup + signal markers.
 * D-058: the chart card carries its OWN header (TF dropdown LEFT, marks
 * dropdown, live quote, fullscreen) — this wrapper is now a thin frame,
 * and on desktop the chart takes the whole main column.
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

  // D-074 — sameMarket: broker-suffixed spellings (XAUUSDm) and the
  // platform name (XAUUSD) are ONE market — the exact mismatch that
  // made "নতুন এন্ট্রি সিগন্যাল চার্টে ড্রয়িং করে না" before
  const symbolSignals = useMemo(
    () => signals.filter((s) => sameMarket(s.symbol, symbol)).slice(0, 40),
    [signals, symbol],
  );
  const activeSignal = useMemo(
    () =>
      signals.find((s) => sameMarket(s.symbol, symbol) && s.status === "active") ?? null,
    [signals, symbol],
  );

  return (
    <section
      className="flex min-w-0 flex-col"
      aria-label="Live chart"
    >
      {/* the chart — everything on it (zones, levels, structure, fib,
       * whales, momentum ribbon, sessions, setup, signals); its header
       * row holds the TF dropdown + marks dropdown + fullscreen (D-058) */}
      <div
        className="h-[52vh] min-h-[300px] sm:min-h-[320px] md:h-[60vh] lg:h-[calc(100vh-8.5rem)]"
      >
        <ErrorBoundary label="Home chart">
          <PriceChart
            symbol={symbol}
            tf={tf}
            onTfChange={onTfChange}
            candles={candles}
            candlesLoading={candlesLoading}
            signals={symbolSignals}
            selectedSignal={activeSignal}
            market={market}
            wsConnected={wsConnected}
            onDesync={onDesync}
            analysis={analysis}
            variant="full"
            headerQuote={tick?.bid ?? null}
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
  isAdmin,
}: Props) {
  const tick = useTick(symbol);
  const market = mt5?.feed?.symbols?.[symbol]?.market ?? "unknown";
  const feedProvider = mt5?.feed?.symbols?.[symbol];
  const latest = signals[0] ?? null;
  const account = tradingAccount?.account ?? null;
  const brokerLinked = broker?.status === "connected" || broker?.status === "linked";
  const winRate = stats?.win_rate;
  const expectancy = stats?.expectancy;

  /* D-075 — separate user interfaces, honest status everywhere:
   * the ADMIN's account card is the INSTITUTION Exness terminal
   * account (the account the auto-trader actually books orders on —
   * "Fronted এ exness not connected দেখাচ্ছে, কিন্তু অটো সিগন্যাল এন্ট্রি
   * হচ্ছে" ended exactly here: the admin watched a practice-plane card
   * while real entries flowed on the terminal). Regular users keep
   * their own isolated practice plane — total separation. */
  const adminTerminalUp =
    mt5?.status === "connected" || broker?.status === "connected";
  const adminAcct = isAdmin ? mt5?.account ?? null : null;
  const shownBalance = isAdmin
    ? adminAcct?.balance ?? broker?.account?.balance ?? null
    : account?.balance ?? null;
  const shownEquity = isAdmin
    ? adminAcct?.equity ?? broker?.account?.equity ?? null
    : account?.equity ?? null;
  const shownCurrency = isAdmin
    ? adminAcct?.currency ?? broker?.account?.currency ?? "USD"
    : account?.currency ?? "USD";
  const acctConnected = isAdmin ? adminTerminalUp : tradingAccount?.connected;
  const acctFootnote = isAdmin
    ? brokerLinked
      ? `Exness terminal · ${broker?.login_masked ?? broker?.login ?? adminAcct?.login ?? "—"} @ ${broker?.server ?? adminAcct?.server ?? "—"}`
      : "Institution terminal · link your Exness account in Settings"
    : brokerLinked
      ? `Broker linked · ${broker?.login_masked ?? broker?.server ?? "—"}`
      : "Practice account · link your broker in Settings";

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

      {/* D-058 — the desktop split: the chart takes the WHOLE main
       *  column (no gutters, no empty space), the intel cards stack in
       *  the right rail (user directive: "2 পাশে অনেক ফাঁকা জায়গা পরে
       *  আছে… কোথাও কোনো ফাঁকা থাকতে পারবে না"). Mobile keeps the
       *  single column. */}
      <div className="flex min-w-0 flex-col gap-3 xl:flex-row xl:items-start">
        {/* the chart column — full width of the main area, tall */}
        <div className="min-w-0 flex-1 xl:sticky xl:top-16">
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
        </div>

        {/* the intel rail — hero + pulse + AI + signals + account */}
        <div className="flex w-full min-w-0 flex-col gap-3 xl:w-[340px] 2xl:w-[380px] xl:shrink-0">
          <PriceHero symbol={symbol} tick={tick} market={market} />

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

          {/* the user's trading account — D-044 isolated practice plane
              (regular users); D-075: the ADMIN sees the institution
              Exness terminal account (their real trading account) */}
          <Card>
            <SectionTitle
              title={isAdmin ? "Exness Trading Account" : "Your Trading Account"}
              right={
                acctConnected ? (
                  <span className="flex items-center gap-1.5 text-[10px] font-semibold text-emerald-400">
                    <Dot tone="green" /> {isAdmin ? "exness connected" : "active"}
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
                value={shownBalance != null ? shownBalance.toFixed(2) : "—"}
                loading={tradingAccount === null && !isAdmin}
                hint={shownCurrency}
              />
              <Stat
                label="Equity"
                value={shownEquity != null ? shownEquity.toFixed(2) : "—"}
                loading={tradingAccount === null && !isAdmin}
                tone={
                  shownBalance != null && shownEquity != null
                    ? shownEquity >= shownBalance ? "up" : "down"
                    : "default"
                }
              />
            </div>
            <p className="mt-2.5 truncate text-[10px] text-zinc-500">
              {acctFootnote}
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
      </div>
    </div>
  );
}
