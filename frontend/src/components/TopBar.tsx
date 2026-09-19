import { useEffect, useState } from "react";
import { getHealth } from "../lib/api";
import { useAuth } from "../lib/auth";
import type { Mt5Status, Position } from "../types";

type BackendState = "checking" | "up" | "down";

interface TopBarProps {
  mt5: Mt5Status | null;
  dataSource: "mock" | "mt5" | "";
  onOpenConnect: () => void;
  onOpenSettings: () => void;
}

/**
 * TopBar per SPEC §10: logo | symbol + MT5 status pill | health pill |
 * settings + connect buttons | user + sign-out. The dot-menu lands in Phase 4.
 */
export default function TopBar({ mt5, dataSource, onOpenConnect, onOpenSettings }: TopBarProps) {
  const { session, signOut } = useAuth();
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
          onClick={onOpenSettings}
          className="rounded-md border border-zinc-700 bg-zinc-900 px-2.5 py-1 text-xs text-zinc-300 hover:border-zinc-500"
        >
          ⚙ settings
        </button>
        <button
          type="button"
          onClick={onOpenConnect}
          className="rounded-md border border-gold/50 bg-gold/15 px-2.5 py-1 text-xs font-medium text-gold hover:bg-gold/25"
        >
          MT5 connect
        </button>
        <div className="hidden items-center gap-2 sm:flex">
          <span className="text-xs text-zinc-400">{email}</span>
          <button
            type="button"
            onClick={() => void signOut()}
            className="rounded-md border border-zinc-700 bg-zinc-900 px-2 py-1 text-xs text-zinc-400 hover:text-zinc-200"
          >
            sign out
          </button>
        </div>
      </div>
    </header>
  );
}

export interface AccountState {
  balance: number | null;
  equity: number | null;
  currency: string;
  positions: Position[] | { ticket: number; symbol: string; side: string; volume: number; profit: number }[];
}
