import { useEffect, useState } from "react";
import { getHealth } from "../lib/api";

type BackendState = "checking" | "up" | "down";

/**
 * TopBar skeleton per SPEC §10: logo | symbol + status pill | balance/equity |
 * user | dot-menu. MT5 status, account numbers and the DotMenu arrive in
 * Phase 2/4.
 */
export default function TopBar() {
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

  const pill =
    backend === "up"
      ? "bg-emerald-500/15 text-emerald-400 border-emerald-500/30"
      : backend === "down"
        ? "bg-red-500/15 text-red-400 border-red-500/30"
        : "bg-zinc-500/15 text-zinc-400 border-zinc-500/30";

  return (
    <header className="flex items-center gap-4 border-b border-zinc-800 bg-zinc-900/60 px-4 py-3">
      <div className="flex items-center gap-2">
        <span className="inline-block h-2.5 w-2.5 rounded-full bg-gold shadow-[0_0_8px_#d4af37]" />
        <span className="text-sm font-semibold tracking-wide text-zinc-100">
          XAUUSD <span className="text-gold">AI</span> Platform
        </span>
      </div>

      <div className="flex items-center gap-2">
        <span className="rounded-md border border-zinc-700 bg-zinc-800/60 px-2 py-0.5 text-xs font-medium text-zinc-300">
          XAUUSDm
        </span>
        <span className={`rounded-full border px-2 py-0.5 text-xs font-medium ${pill}`}>
          {backend === "up" ? `backend · ${source}` : backend === "down" ? "backend · down" : "checking…"}
        </span>
      </div>

      <div className="ml-auto flex items-center gap-3">
        <span className="hidden text-xs text-zinc-400 sm:block">
          balance <span className="font-semibold text-zinc-200">—</span> · equity{" "}
          <span className="font-semibold text-zinc-200">—</span>
        </span>
        <span
          aria-hidden
          className="grid h-8 w-8 place-items-center rounded-full border border-zinc-700 bg-zinc-800 text-sm text-zinc-300"
        >
          👤
        </span>
        <span
          aria-hidden
          className="grid h-8 w-8 place-items-center rounded-full border border-zinc-700 bg-zinc-800 text-lg leading-none text-zinc-300"
        >
          ⋯
        </span>
      </div>
    </header>
  );
}
