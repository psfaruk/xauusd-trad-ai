import { useEffect, useRef, useState } from "react";
import { useAuth } from "../lib/auth";
import { APP_TABS } from "./TabBar";
import type { AppTab } from "../types";

/** Functions the dot-menu can trigger (Dashboard owns the dialogs + tabs). */
export interface DotMenuActions {
  /** D-039: navigate to an app tab (desktop navigation lives here). */
  onNavigate: (tab: AppTab) => void;
  onOpenConnectBroker: () => void;  // per-user Exness connection
  onOpenPractice: () => void;       // practice (paper) account dialog
  onOpenTradePanel: () => void;     // practice manual orders + positions
  onOpenTradeHistory: () => void;   // practice history
  onOpenLogs: () => void;           // platform log viewer
  onOpenEngineSettings: () => void; // engine config (admin)
  onOpenSignals: () => void;        // charts tab → signals
  onOpenMarketData: () => void;     // charts tab → market data
}

interface DotMenuProps {
  actions: DotMenuActions;
  isAdmin: boolean;
  activeTab: AppTab;
}

interface MenuItem {
  label: string;
  hint?: string;
  onClick: () => void;
  active?: boolean;
}

/**
 * 3-dot menu (desktop navigation, user req D-039): app tabs first, then the
 * remaining functions grouped Trading / Analysis / Settings / Account.
 * Mobile uses the bottom TabBar instead (this menu stays reachable there
 * too for the less-common functions).
 */
export default function DotMenu({ actions, isAdmin, activeTab }: DotMenuProps) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const { session, signOut } = useAuth();

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const go = (tab: AppTab) => () => {
    setOpen(false);
    actions.onNavigate(tab);
  };

  const groups: { title: string; items: MenuItem[] }[] = [
    {
      title: "Go to",
      items: APP_TABS.map((t) => ({
        label: t.label,
        hint: t.id === "ai" ? "arm AI · positions · orders · history" : undefined,
        onClick: go(t.id),
        active: activeTab === t.id,
      })),
    },
    {
      title: "Trading",
      items: [
        { label: "Connect Broker", hint: "your Exness MT5 account · per-user", onClick: () => { setOpen(false); actions.onOpenConnectBroker(); } },
        { label: "Manual Order", hint: "real order · AI Trading tab", onClick: go("ai") },
        { label: "Practice Trading (paper)", hint: "simulated trades on live prices · no real money", onClick: () => { setOpen(false); actions.onOpenPractice(); } },
        { label: "Practice Panel", hint: "paper orders & positions", onClick: () => { setOpen(false); actions.onOpenTradePanel(); } },
        { label: "Practice History", hint: "my paper trades", onClick: () => { setOpen(false); actions.onOpenTradeHistory(); } },
      ],
    },
    {
      title: "Analysis",
      items: [
        { label: "Signals & Performance", hint: "AI signal list + stats", onClick: () => { setOpen(false); actions.onOpenSignals(); } },
        { label: "Market Data", hint: "live data & external references", onClick: () => { setOpen(false); actions.onOpenMarketData(); } },
        { label: "Platform Logs", hint: "engine activity + CSV export", onClick: () => { setOpen(false); actions.onOpenLogs(); } },
      ],
    },
    {
      title: "Settings",
      items: isAdmin
        ? [{ label: "Engine Settings", hint: "strategy & risk config", onClick: () => { setOpen(false); actions.onOpenEngineSettings(); } }]
        : [{ label: "Engine Settings", hint: "admin only", onClick: () => setOpen(false) }],
    },
    {
      title: "Account",
      items: [
        { label: "Signed in", hint: session?.user?.email ?? undefined, onClick: () => setOpen(false) },
        { label: "Sign out", onClick: () => void signOut() },
      ],
    },
  ];

  return (
    <div className="relative" ref={ref}>
      <button
        type="button"
        aria-label="Open menu"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        className="flex h-8 w-8 items-center justify-center rounded-md border border-zinc-700 bg-zinc-900 text-zinc-300 hover:border-zinc-500 hover:text-zinc-100"
      >
        <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor" aria-hidden>
          <circle cx="8" cy="3" r="1.4" />
          <circle cx="8" cy="8" r="1.4" />
          <circle cx="8" cy="13" r="1.4" />
        </svg>
      </button>

      {open && (
        <nav
          role="menu"
          className="absolute right-0 z-50 mt-2 max-h-[80vh] w-72 overflow-y-auto rounded-xl border border-zinc-700 bg-zinc-900/95 py-1.5 shadow-2xl backdrop-blur"
        >
          {groups.map((g) => (
            <div key={g.title} className="mb-1 last:mb-0">
              <p className="px-4 pb-1 pt-2 text-[10px] font-semibold uppercase tracking-widest text-gold/70">
                {g.title}
              </p>
              {g.items.map((item) => (
                <button
                  key={item.label}
                  type="button"
                  role="menuitem"
                  onClick={item.onClick}
                  className={`flex w-full flex-col items-start px-4 py-1.5 text-left hover:bg-zinc-800 ${
                    item.active ? "bg-zinc-800/60" : ""
                  }`}
                >
                  <span className={`text-sm ${item.active ? "text-gold" : "text-zinc-200"}`}>
                    {item.label}
                  </span>
                  {item.hint && (
                    <span className="text-[11px] text-zinc-500">{item.hint}</span>
                  )}
                </button>
              ))}
              <div className="mx-4 my-1 border-t border-zinc-800" />
            </div>
          ))}
        </nav>
      )}
    </div>
  );
}
