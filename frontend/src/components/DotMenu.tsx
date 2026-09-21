import { useEffect, useRef, useState } from "react";
import { useAuth } from "../lib/auth";

/** Functions the dot-menu can trigger (Dashboard owns the dialogs). */
export interface DotMenuActions {
  onOpenTrading: () => void;      // Trading: my account connect/arming
  onOpenTradePanel: () => void;   // Trading: manual orders + positions
  onOpenTradeHistory: () => void; // Trading: my trade history
  onOpenMt5Account: () => void;   // Trading: REAL MT5 account panel (D-034)
  onOpenSignals: () => void;      // Analysis: signals + performance tab
  onOpenMarketData: () => void;   // Analysis: external reference data tab
  onOpenLogs: () => void;         // Analysis: platform log viewer
  onOpenSettings: () => void;     // Settings: engine config (admin)
  onOpenPlatformMt5: () => void;  // Trading: connect YOUR broker account (D-037, all users)
}

interface DotMenuProps {
  actions: DotMenuActions;
  isAdmin: boolean;
}

interface MenuItem {
  label: string;
  hint?: string;
  onClick: () => void;
}

/**
 * 3-dot overflow menu (user req #3) — every platform function grouped:
 * Trading / Analysis / Settings / Account. Closes on outside click or Esc.
 */
export default function DotMenu({ actions, isAdmin }: DotMenuProps) {
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

  const groups: { title: string; items: MenuItem[] }[] = [
    {
      title: "Trading",
      items: [
        { label: "Connect Broker", hint: "your Exness MT5 account · per-user", onClick: actions.onOpenPlatformMt5 },
        { label: "MT5 Account (live)", hint: "real Exness balance · positions · orders", onClick: actions.onOpenMt5Account },
        { label: "Practice Trading (paper)", hint: "simulated trades on live prices · no real money", onClick: actions.onOpenTrading },
        { label: "Trade Panel", hint: "manual orders & open positions", onClick: actions.onOpenTradePanel },
        { label: "Trade History", hint: "my executed trades", onClick: actions.onOpenTradeHistory },
      ],
    },
    {
      title: "Analysis",
      items: [
        { label: "Signals & Performance", hint: "AI signal list + stats", onClick: actions.onOpenSignals },
        { label: "Market Data", hint: "live data & external references", onClick: actions.onOpenMarketData },
        { label: "Platform Logs", hint: "engine activity + CSV export", onClick: actions.onOpenLogs },
      ],
    },
    {
      title: "Settings",
      items: isAdmin
        ? [
            { label: "Engine Settings", hint: "strategy & risk config", onClick: actions.onOpenSettings },
          ]
        : [
            { label: "Engine Settings", hint: "admin only", onClick: actions.onOpenSettings },
          ],
    },
    {
      title: "Account",
      items: [
        {
          label: "Signed in",
          hint: session?.user?.email ?? undefined,
          onClick: () => setOpen(false),
        },
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
          className="absolute right-0 z-50 mt-2 max-w-[calc(100vw-2rem)] w-72 overflow-hidden rounded-xl border border-zinc-700 bg-zinc-900/95 py-1.5 shadow-2xl backdrop-blur"
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
                  onClick={() => {
                    setOpen(false);
                    item.onClick();
                  }}
                  className="flex w-full flex-col items-start px-4 py-1.5 text-left hover:bg-zinc-800"
                >
                  <span className="text-sm text-zinc-200">{item.label}</span>
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
