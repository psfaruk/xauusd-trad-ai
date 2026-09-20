import { useEffect, useState } from "react";
import { getHealth } from "../lib/api";
import { useAuth } from "../lib/auth";
import DotMenu, { type DotMenuActions } from "./DotMenu";
import type { Mt5Status } from "../types";

type BackendState = "checking" | "up" | "down";

interface TopBarProps {
  mt5: Mt5Status | null;
  dataSource: "mock" | "mt5" | "";
  tradingConnected: boolean;
  menuActions: DotMenuActions;
  isAdmin: boolean;
  onOpenTrade: () => void;
}

/**
 * TopBar per SPEC §10 + user req #3: logo | symbol + MT5 status pill |
 * health pill | quick trade button | 3-dot menu with every grouped function.
 */
export default function TopBar({
  mt5, dataSource, tradingConnected, menuActions, isAdmin, onOpenTrade,
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

  return (
    <header className="flex flex-wrap items-center gap-3 border-b border-zinc-800 bg-zinc-900/60 px-4 py-3">
      <div className="flex items-center gap-2">
        <span className="inline-block h-2.5 w-2.5 rounded-full bg-gold shadow-[0_0_8px_#d4af37]" />
        <span className="text-sm font-semibold tracking-wide text-zinc-100">
          XAUUSD <span className="text-gold">AI</span> Platform
        </span>
      </div>

      <span className={`rounded-full border px-2.5 py-0.5 text-xs font-medium ${pill}`}>
        {status === "connected" ? "● " : status === "reconnecting" ? "◌ " : "○ "}
        {mt5?.symbol ?? "MT5"} · {status}
        {dataSource === "mock" && <span className="ml-1 text-gold/80">(demo)</span>}
      </span>

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
