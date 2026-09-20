import { useEffect, useRef, useState } from "react";
import { getHealth } from "../lib/api";
import { useAuth } from "../lib/auth";
import DotMenu, { type DotMenuActions } from "./DotMenu";
import type { Mt5Status } from "../types";

type BackendState = "checking" | "up" | "down";
type DataSourceName = "mock" | "mt5" | "live" | "";

interface TopBarProps {
  mt5: Mt5Status | null;
  dataSource: DataSourceName;
  tradingConnected: boolean;
  menuActions: DotMenuActions;
  isAdmin: boolean;
  onOpenTrade: () => void;
  lastPrice: { bid: number; ask: number } | null;
  lastTickAt: number | null;
  tps: number | null;
}

/**
 * TopBar per SPEC §10 + user req #3: logo | symbol + connection pill |
 * LIVE feed badge + real-time price (D-030) | health pill | quick trade |
 * 3-dot menu with every grouped function.
 */
export default function TopBar({
  mt5, dataSource, tradingConnected, menuActions, isAdmin, onOpenTrade, lastPrice, lastTickAt, tps,
}: TopBarProps) {
  const { session } = useAuth();
  const email = session?.user?.email ?? null;
  const [backend, setBackend] = useState<BackendState>("checking");
  const [source, setSource] = useState<string>("");

  useEffect(() => {
    let alive = true;
    const check = () =>
      getHealth()
        .then((info) => {
          if (!alive) return;
          setBackend(info.status === "ok" ? "up" : "down");
          setSource(info.data_source);
        })
        .catch(() => alive && setBackend("down"));
    check();
    const timer = window.setInterval(check, 15_000);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, []);

  void email;

  const status = mt5?.status ?? "disconnected";
  const pill =
    status === "connected"
      ? "bg-emerald-500/15 text-emerald-400 border-emerald-500/30"
      : status === "reconnecting"
        ? "bg-amber-500/15 text-amber-400 border-amber-500/30"
        : "bg-red-500/15 text-red-400 border-red-500/30";

  const healthPill =
    backend === "up"
      ? "bg-emerald-500/15 text-emerald-400 border-emerald-500/30"
      : backend === "down"
        ? "bg-red-500/15 text-red-400 border-red-500/30"
        : "bg-zinc-500/15 text-zinc-400 border-zinc-500/30";

  /* D-030 live-feed badge: real-time provider + freshness pulse.
   * Liveness is derived from ACTUAL WS tick recency (updates every ~2s);
   * the status-payload feed age is the fallback before the first tick. */
  const feed = mt5?.feed;
  const tickAgeS =
    lastTickAt !== null ? Math.max(0, (Date.now() - lastTickAt) / 1000) : null;
  const dataAgeS = tickAgeS ?? feed?.last_tick_age_s ?? null;
  const liveActive =
    dataSource === "live" &&
    status === "connected" &&
    !!feed &&
    feed.provider !== "degraded" &&
    (dataAgeS ?? 999) < 15;
  const providerLabel =
    feed?.provider === "aggregate"
      ? "5-venue real-time"
      : feed?.provider === "binance"
        ? "Binance PAXG"
        : feed?.provider === "goldapi"
          ? "gold-api XAU"
          : feed?.provider ?? "live";

  /* price direction flash (up=green / down=red / flat=neutral) */
  const prevPrice = useRef<number | null>(null);
  const [flash, setFlash] = useState<"up" | "down" | "flat">("flat");
  useEffect(() => {
    if (!lastPrice) return;
    const mid = (lastPrice.bid + lastPrice.ask) / 2;
    const prev = prevPrice.current;
    prevPrice.current = mid;
    if (prev === null || mid === prev) {
      setFlash("flat");
      return;
    }
    setFlash(mid > prev ? "up" : "down");
  }, [lastPrice]);

  return (
    <header className="flex flex-wrap items-center gap-3 border-b border-zinc-800 bg-zinc-900/60 px-4 py-3">
      <div className="flex items-center gap-2">
        <span className="inline-block h-2.5 w-2.5 rounded-full bg-gold shadow-[0_0_8px_#d4af37]" />
        <span className="text-sm font-semibold tracking-wide text-zinc-100">
          XAUUSD <span className="text-gold">AI</span> Platform
        </span>
      </div>

      {/* real-time price ticker (D-030) */}
      {lastPrice && (
        <span
          className={`rounded-md border px-2.5 py-1 font-mono text-sm font-semibold tabular-nums transition-colors duration-300 ${
            flash === "up"
              ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-300"
              : flash === "down"
                ? "border-red-500/40 bg-red-500/10 text-red-300"
                : "border-zinc-700 bg-zinc-800/60 text-zinc-200"
          }`}
          title={feed?.detail ?? undefined}
        >
          {lastPrice.bid.toFixed(2)}
          <span className="mx-1 text-[10px] text-zinc-500">/</span>
          {lastPrice.ask.toFixed(2)}
        </span>
      )}

      <span className={`rounded-full border px-2.5 py-0.5 text-xs font-medium ${pill}`}>
        {status === "connected" ? "● " : status === "reconnecting" ? "◌ " : "○ "}
        {mt5?.symbol ?? "MT5"} · {status}
        {dataSource === "mock" && <span className="ml-1 text-gold/80">(demo)</span>}
      </span>

      {/* D-033: this banner can ONLY appear when a developer explicitly set
       * ALLOW_DEMO=1 locally — deployments can never show demo prices. */}
      {source === "mock" && (
        <span
          className="rounded-full border border-red-500/40 bg-red-500/10 px-2.5 py-0.5 text-xs font-semibold text-red-300"
          title="ALLOW_DEMO=1 is set — synthetic dev prices. Unset ALLOW_DEMO and restart for the REAL live market."
        >
          ⚠ DEV DEMO · synthetic prices
        </span>
      )}

      {liveActive && (
        <span
          className="flex items-center gap-1.5 rounded-full border border-emerald-500/40 bg-emerald-500/10 px-2.5 py-0.5 text-xs font-semibold text-emerald-300"
          title={`${feed?.detail ?? ""} — updated ${Math.round(dataAgeS ?? 0)}s ago`}
        >
          <span className="relative flex h-2 w-2">
            <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-400 opacity-75" />
            <span className="relative inline-flex h-2 w-2 rounded-full bg-emerald-500" />
          </span>
          LIVE · {providerLabel}
          {tps != null && (
            <span
              className="rounded bg-emerald-500/20 px-1 font-mono tabular-nums text-[10px] font-bold text-emerald-200"
              title="REAL market events per second (all venues, 5s window) — every one of them updates the forming candle"
            >
              ⚡ {Math.round(tps)} t/s
            </span>
          )}
          {dataAgeS !== null && (
            <span className="font-normal text-emerald-400/70">+{Math.round(dataAgeS)}s</span>
          )}
        </span>
      )}
      {dataSource === "live" && !liveActive && status === "connected" && (
        <span
          className="rounded-full border border-amber-500/40 bg-amber-500/10 px-2.5 py-0.5 text-xs font-medium text-amber-300"
          title={feed?.detail ?? "waiting for live data"}
        >
          live feed {feed?.provider === "degraded" ? "degraded — retrying" : "connecting…"}
        </span>
      )}

      <span className={`rounded-full border px-2 py-0.5 text-[11px] ${healthPill}`}>
        api {backend}
        {source && <span className="ml-1 opacity-70">{source}</span>}
      </span>

      <div className="ml-auto flex items-center gap-2">
        <button
          type="button"
          onClick={onOpenTrade}
          className={`rounded-md border px-2.5 py-1 text-xs font-medium ${
            tradingConnected
              ? "border-emerald-500/50 bg-emerald-500/15 text-emerald-300 hover:bg-emerald-500/25"
              : "border-gold/50 bg-gold/15 text-gold hover:bg-gold/25"
          }`}
        >
          {tradingConnected ? "my trades ●" : "start trading"}
        </button>
        <DotMenu actions={menuActions} isAdmin={isAdmin} />
      </div>
    </header>
  );
}
