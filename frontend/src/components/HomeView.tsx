import { useEffect, useRef, useState } from "react";
import type {
  AppTab, BrokerConnection, Mt5Account, Mt5OpenPosition, Mt5Status, Signal,
  StatsResponse,
} from "../types";

interface HomeViewProps {
  symbol: string;
  symbols: string[];
  onSymbolChange: (s: string) => void;
  lastPrice: { bid: number; ask: number } | null;
  lastTickAt: number | null;
  tps: number | null;
  mt5: Mt5Status | null;
  broker: BrokerConnection | null;
  brokerAccount: Mt5Account | null;
  brokerPositions: Mt5OpenPosition[];
  autoArmed: boolean;
  autoWhy?: { code: string; text: string } | null;
  signals: Signal[];
  stats: StatsResponse | null;
  engineRunning: boolean;
  onConnectBroker: () => void;
  onNavigate: (tab: AppTab) => void;
}

const card =
  "rounded-2xl border border-zinc-800 bg-zinc-900/60 p-4 transition-colors hover:border-zinc-700";
const label = "text-[10px] font-semibold uppercase tracking-widest text-zinc-500";

/**
 * D-039 — Home tab: everything at a glance. Live price hero, broker account
 * card, AI auto-trade state with the honest "why" diagnosis, latest signal
 * and performance. All navigation flows into the other tabs.
 */
export default function HomeView({
  symbol, symbols, onSymbolChange, lastPrice, lastTickAt, tps, mt5,
  broker, brokerAccount, brokerPositions, autoArmed, autoWhy, signals, stats,
  engineRunning, onConnectBroker, onNavigate,
}: HomeViewProps) {
  /* price flash (up/down) — same feel as the TopBar ticker */
  const prevMid = useRef<number | null>(null);
  const [flash, setFlash] = useState<"up" | "down" | "flat">("flat");
  useEffect(() => {
    if (!lastPrice) return;
    const mid = (lastPrice.bid + lastPrice.ask) / 2;
    const prev = prevMid.current;
    prevMid.current = mid;
    if (prev === null || mid === prev) setFlash("flat");
    else setFlash(mid > prev ? "up" : "down");
  }, [lastPrice]);

  const mid = lastPrice ? (lastPrice.bid + lastPrice.ask) / 2 : null;
  const spread = lastPrice ? lastPrice.ask - lastPrice.bid : null;
  const brokerConnected = broker?.status === "connected";
  const symFeed = mt5?.feed?.symbols?.[symbol];
  const marketOpen = symFeed?.market !== "closed";
  const tickAgeS =
    lastTickAt !== null ? Math.max(0, (Date.now() - lastTickAt) / 1000) : null;
  const latestSignal =
    signals.find((s) => !(["expired", "cancelled"] as string[]).includes(s.status)) ??
    signals[0] ??
    null;
  const openCount = brokerPositions.length;
  const floating = brokerPositions.reduce((acc, p) => acc + (p.profit ?? 0), 0);

  return (
    <div className="flex flex-col gap-4">
      {/* price hero */}
      <section className={`${card} p-5`}>
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <p className={label}>live price · MetaTrader 5 broker feed</p>
            <div className="mt-1 flex items-baseline gap-3">
              <span
                className={`font-mono text-4xl font-bold tabular-nums transition-colors duration-200 sm:text-5xl ${
                  flash === "up"
                    ? "text-emerald-400"
                    : flash === "down"
                      ? "text-red-400"
                      : "text-zinc-100"
                }`}
              >
                {mid !== null ? mid.toFixed(2) : "—"}
              </span>
              <span className="text-sm font-medium text-zinc-400">{symbol}</span>
            </div>
            <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-zinc-500">
              <span>
                bid <b className="font-mono text-zinc-300">{lastPrice?.bid.toFixed(2) ?? "—"}</b>
              </span>
              <span>
                ask <b className="font-mono text-zinc-300">{lastPrice?.ask.toFixed(2) ?? "—"}</b>
              </span>
              <span>
                spread <b className="font-mono text-zinc-300">{spread !== null ? spread.toFixed(2) : "—"}</b>
              </span>
              {tps != null && (
                <span className="rounded bg-emerald-500/15 px-1.5 py-0.5 font-mono text-[10px] font-bold text-emerald-300">
                  ⚡ {Math.round(tps)} t/s
                </span>
              )}
              {tickAgeS !== null && <span>+{tickAgeS.toFixed(1)}s</span>}
            </div>
          </div>
          <div className="flex flex-col items-end gap-2">
            <span
              className={`rounded-full border px-2.5 py-0.5 text-[11px] font-semibold ${
                marketOpen
                  ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-300"
                  : "border-amber-500/40 bg-amber-500/10 text-amber-300"
              }`}
            >
              {marketOpen ? "● market open" : "◌ market closed"}
            </span>
            {/* instrument chips */}
            <div className="flex gap-1.5">
              {symbols.map((s) => (
                <button
                  key={s}
                  type="button"
                  onClick={() => onSymbolChange(s)}
                  className={`rounded-full border px-3 py-1 text-xs font-medium transition-colors ${
                    s === symbol
                      ? "border-gold/50 bg-gold/15 text-gold"
                      : "border-zinc-700 text-zinc-400 hover:text-zinc-200"
                  }`}
                >
                  {s}
                </button>
              ))}
            </div>
          </div>
        </div>
      </section>

      {/* cards grid */}
      <div className="grid gap-4 sm:grid-cols-2">
        {/* broker account */}
        <section className={card}>
          <div className="flex items-center justify-between">
            <p className={label}>broker account</p>
            {brokerConnected ? (
              <span className="rounded-full bg-emerald-500/15 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-emerald-300">
                ● connected
              </span>
            ) : (
              <span className="rounded-full bg-zinc-800 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-zinc-400">
                not connected
              </span>
            )}
          </div>
          {brokerConnected && brokerAccount ? (
            <>
              <div className="mt-2 grid grid-cols-2 gap-3">
                <div>
                  <p className="text-xs text-zinc-500">balance</p>
                  <p className="font-mono text-xl font-semibold text-zinc-100">
                    {brokerAccount.balance?.toFixed(2) ?? "—"}{" "}
                    <span className="text-xs text-zinc-500">{brokerAccount.currency ?? "USD"}</span>
                  </p>
                </div>
                <div>
                  <p className="text-xs text-zinc-500">equity</p>
                  <p className="font-mono text-xl font-semibold text-zinc-100">
                    {brokerAccount.equity?.toFixed(2) ?? "—"}
                  </p>
                </div>
                <div>
                  <p className="text-xs text-zinc-500">floating P/L</p>
                  <p
                    className={`font-mono text-lg font-semibold ${
                      floating >= 0 ? "text-emerald-400" : "text-red-400"
                    }`}
                  >
                    {floating >= 0 ? "+" : ""}
                    {floating.toFixed(2)}
                  </p>
                </div>
                <div>
                  <p className="text-xs text-zinc-500">open positions</p>
                  <p className="font-mono text-lg font-semibold text-zinc-100">{openCount}</p>
                </div>
              </div>
              <p className="mt-2 text-[11px] text-zinc-500">
                {broker?.login ?? brokerAccount.login} @ {broker?.server ?? brokerAccount.server}
              </p>
              <button
                type="button"
                onClick={() => onNavigate("ai")}
                className="mt-3 w-full rounded-lg border border-zinc-700 bg-zinc-800/60 px-3 py-2 text-xs font-medium text-zinc-200 transition-colors hover:border-gold/40 hover:text-gold"
              >
                Open AI Trading → positions · orders · history
              </button>
            </>
          ) : (
            <>
              <p className="mt-2 text-sm leading-relaxed text-zinc-400">
                Connect your Exness account — balance, positions, trade history
                and AI auto-trade run on <b className="text-zinc-200">your account only</b>.
              </p>
              <button
                type="button"
                onClick={onConnectBroker}
                className="mt-3 w-full rounded-lg border border-gold/50 bg-gold/15 px-3 py-2 text-xs font-semibold text-gold transition-colors hover:bg-gold/25"
              >
                Connect broker account
              </button>
            </>
          )}
        </section>

        {/* AI auto-trade */}
        <section className={card}>
          <div className="flex items-center justify-between">
            <p className={label}>AI auto-trade</p>
            {autoArmed ? (
              <span className="rounded-full bg-red-500/20 px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider text-red-300">
                ● armed
              </span>
            ) : (
              <span className="rounded-full bg-zinc-800 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-zinc-400">
                off
              </span>
            )}
          </div>
          <p className="mt-2 text-sm leading-relaxed text-zinc-300">
            {autoWhy?.text ?? "AI signal → real order through MetaTrader 5."}
          </p>
          {autoWhy?.code === "no_balance" && (
            <p className="mt-2 rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-[11px] leading-relaxed text-amber-300">
              Your Exness demo balance was reset to 0.00 by the broker. Top it
              up (or create a new demo) so AI orders can execute.
            </p>
          )}
          <button
            type="button"
            onClick={() => onNavigate("ai")}
            className={`mt-3 w-full rounded-lg border px-3 py-2 text-xs font-semibold transition-colors ${
              autoArmed
                ? "border-red-500/40 bg-red-500/10 text-red-300 hover:bg-red-500/20"
                : "border-zinc-700 bg-zinc-800/60 text-zinc-200 hover:border-gold/40 hover:text-gold"
            }`}
          >
            {autoArmed ? "Manage AI auto-trade" : "Arm AI auto-trade"} →
          </button>
        </section>

        {/* latest signal */}
        <section className={card}>
          <p className={label}>latest AI signal</p>
          {latestSignal ? (
            <>
              <div className="mt-2 flex items-center gap-2">
                <span
                  className={`rounded px-2 py-0.5 text-xs font-bold ${
                    latestSignal.direction === "BUY"
                      ? "bg-emerald-500/15 text-emerald-300"
                      : "bg-red-500/15 text-red-300"
                  }`}
                >
                  {latestSignal.direction}
                </span>
                <span className="text-sm font-medium text-zinc-200">{latestSignal.symbol}</span>
                <span className="text-[11px] text-zinc-500">{latestSignal.tf}</span>
                <span className="ml-auto text-[11px] text-zinc-500">
                  {latestSignal.ts ? new Date(latestSignal.ts).toLocaleString() : ""}
                </span>
              </div>
              <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-zinc-500">
                <span>
                  entry <b className="font-mono text-zinc-300">{latestSignal.entry?.toFixed(2) ?? "—"}</b>
                </span>
                <span>
                  SL <b className="font-mono text-red-300">{latestSignal.sl?.toFixed(2) ?? "—"}</b>
                </span>
                <span>
                  TP <b className="font-mono text-emerald-300">{latestSignal.tp?.toFixed(2) ?? "—"}</b>
                </span>
                <span>
                  confidence <b className="font-mono text-zinc-300">{latestSignal.confidence?.toFixed(0)}%</b>
                </span>
              </div>
            </>
          ) : (
            <p className="mt-2 text-sm text-zinc-500">
              No signals yet — the engine is watching the market.
            </p>
          )}
          <button
            type="button"
            onClick={() => onNavigate("charts")}
            className="mt-3 w-full rounded-lg border border-zinc-700 bg-zinc-800/60 px-3 py-2 text-xs font-medium text-zinc-200 transition-colors hover:border-gold/40 hover:text-gold"
          >
            View chart & all signals →
          </button>
        </section>

        {/* performance */}
        <section className={card}>
          <p className={label}>performance · 30 days</p>
          {stats ? (
            <div className="mt-2 grid grid-cols-3 gap-3">
              <div>
                <p className="text-xs text-zinc-500">win rate</p>
                <p className="font-mono text-lg font-semibold text-zinc-100">
                  {stats.win_rate != null ? `${(stats.win_rate * 100).toFixed(0)}%` : "—"}
                </p>
              </div>
              <div>
                <p className="text-xs text-zinc-500">expectancy</p>
                <p
                  className={`font-mono text-lg font-semibold ${
                    (stats.expectancy ?? 0) >= 0 ? "text-emerald-400" : "text-red-400"
                  }`}
                >
                  {stats.expectancy != null
                    ? `${stats.expectancy >= 0 ? "+" : ""}${stats.expectancy.toFixed(2)}R`
                    : "—"}
                </p>
              </div>
              <div>
                <p className="text-xs text-zinc-500">signals</p>
                <p className="font-mono text-lg font-semibold text-zinc-100">{stats.total_signals ?? "—"}</p>
              </div>
            </div>
          ) : (
            <p className="mt-2 text-sm text-zinc-500">Loading statistics…</p>
          )}
          <p className="mt-3 flex items-center gap-1.5 text-[11px] text-zinc-500">
            <span
              aria-hidden
              className={`inline-block h-2 w-2 rounded-full ${engineRunning ? "bg-emerald-500" : "bg-zinc-600"}`}
            />
            engine {engineRunning ? "running" : "stopped"} · data source: MetaTrader 5 only
          </p>
        </section>
      </div>
    </div>
  );
}
