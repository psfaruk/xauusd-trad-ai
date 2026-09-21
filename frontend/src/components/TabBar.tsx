import type { AppTab } from "../types";

/** D-039 tab definitions — shared by the mobile bottom bar and the ⋮ menu. */
export const APP_TABS: { id: AppTab; label: string; short: string; icon: JSX.Element }[] = [
  {
    id: "home",
    label: "Home",
    short: "Home",
    icon: (
      <svg width="20" height="20" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
        <path d="M3.5 8.5 10 3l6.5 5.5V16a1 1 0 0 1-1 1h-3.5v-4.5h-4V17H4.5a1 1 0 0 1-1-1V8.5Z" />
      </svg>
    ),
  },
  {
    id: "charts",
    label: "Chart & Signals",
    short: "Chart",
    icon: (
      <svg width="20" height="20" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
        <path d="M3 17V9m4 8V4m4 13v-6m4 6V7" />
      </svg>
    ),
  },
  {
    id: "ai",
    label: "AI Trading",
    short: "AI",
    icon: (
      <svg width="20" height="20" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
        <circle cx="10" cy="10" r="2.2" />
        <path d="M10 2.5v3M10 14.5v3M2.5 10h3M14.5 10h3M4.8 4.8l2.1 2.1M13.1 13.1l2.1 2.1M15.2 4.8l-2.1 2.1M6.9 13.1l-2.1 2.1" />
      </svg>
    ),
  },
  {
    id: "settings",
    label: "Settings",
    short: "Settings",
    icon: (
      <svg width="20" height="20" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
        <circle cx="10" cy="10" r="2.6" />
        <path d="M10 2.8v2M10 15.2v2M3.4 6.6l1.7 1M14.9 12.4l1.7 1M3.4 13.4l1.7-1M14.9 7.6l1.7-1" />
      </svg>
    ),
  },
];

interface TabBarProps {
  active: AppTab;
  onChange: (tab: AppTab) => void;
}

/**
 * D-039 — mobile bottom navigation (Home / Chart & Signals / AI Trading /
 * Settings). Fixed to the viewport bottom with safe-area padding; hidden on
 * lg+ screens where navigation lives in the 3-dot menu (user req).
 */
export default function TabBar({ active, onChange }: TabBarProps) {
  return (
    <nav
      aria-label="App sections"
      className="fixed inset-x-0 bottom-0 z-40 border-t border-zinc-800 bg-zinc-900/95 backdrop-blur lg:hidden"
      style={{ paddingBottom: "env(safe-area-inset-bottom)" }}
    >
      <div className="mx-auto flex max-w-lg items-stretch">
        {APP_TABS.map((t) => {
          const isActive = active === t.id;
          return (
            <button
              key={t.id}
              type="button"
              onClick={() => onChange(t.id)}
              aria-current={isActive ? "page" : undefined}
              className={`flex flex-1 flex-col items-center gap-0.5 px-1 py-2 text-[10px] font-medium transition-colors ${
                isActive ? "text-gold" : "text-zinc-500 hover:text-zinc-300"
              }`}
            >
              <span
                className={`rounded-lg px-2.5 py-1 transition-colors ${
                  isActive ? "bg-gold/15" : ""
                }`}
              >
                {t.icon}
              </span>
              <span className="truncate">{t.short}</span>
            </button>
          );
        })}
      </div>
    </nav>
  );
}
