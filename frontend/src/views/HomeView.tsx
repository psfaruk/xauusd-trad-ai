/**
 * HomeView (D-041/D-044) — mobile-app style home: live price hero, AI
 * auto-trade status, latest signal, performance stats, the USER's own
 * trading account (practice plane — isolated per user). Skeletons until
 * REAL data arrives (user requirement: loading until data comes).
 */

import { useTick } from "../state/feed";
import type {
  BrokerConnection, Mt5Status, Signal, StatsResponse, TradingStatus,
} from "../types";
import { Badge, Btn, Card, Dot, SectionTitle, Skeleton, Stat } from "../components/ui";
import { LatestSignalCard } from "../components/SignalDetail";
import type { TickSnapshot } from "../state/feed";

interface Props {
  symbol: string;
  symbols: string[];
  onSymbolChange: (s: string) => void;
  mt5: Mt5Status | null;
  broker: BrokerConnection | null;
  tradingAccount: TradingStatus | null;
  isAdmin: boolean;
  autoArmed: boolean;
  autoWhy: { code: string; text: string } | null;
  signals: Signal[];
  stats: StatsResponse | null;
  onOpenAi: () => void;
  onOpenSettings: () => void;
  onOpenSignal: (id: string) => void;
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

export default function HomeView({
  symbol,
  symbols,
  onSymbolChange,
  mt5,
  broker,
  tradingAccount,
  isAdmin,
  autoArmed,
  autoWhy,
  signals,
  stats,
  onOpenAi,
  onOpenSettings,
  onOpenSignal,
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
