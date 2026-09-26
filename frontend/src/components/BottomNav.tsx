/**
 * BottomNav (D-041) — THE navigation: four tabs, bottom bar, identical on
 * mobile and desktop (user requirement — one layout everywhere, no 3-dot
 * menu). Safe-area aware, thumb-friendly, gold active state.
 */

import type { AppTab } from "../types";

interface Item {
  id: AppTab;
  label: string;
  href: string;
  icon: (active: boolean) => JSX.Element;
}

const stroke = (active: boolean) =>
  active ? "stroke-gold" : "stroke-zinc-500";

function HomeIcon({ active }: { active: boolean }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" className={`h-6 w-6 ${stroke(active)}`} strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M3 10.5 12 3l9 7.5" />
      <path d="M5.5 9.5V20a1 1 0 0 0 1 1H10v-6h4v6h3.5a1 1 0 0 0 1-1V9.5" />
    </svg>
  );
}
function ChartIcon({ active }: { active: boolean }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" className={`h-6 w-6 ${stroke(active)}`} strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M3 21h18" />
      <path d="M6 17V9" />
      <path d="M11 17V5" />
      <path d="M16 17v-6" />
      <path d="M21 17V7" />
    </svg>
  );
}
function AiIcon({ active }: { active: boolean }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" className={`h-6 w-6 ${stroke(active)}`} strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <rect x="5" y="7" width="14" height="12" rx="3" />
      <path d="M12 7V4" />
      <circle cx="9.5" cy="13" r="1.2" fill="currentColor" className={active ? "fill-gold" : "fill-zinc-500"} stroke="none" />
      <circle cx="14.5" cy="13" r="1.2" fill="currentColor" className={active ? "fill-gold" : "fill-zinc-500"} stroke="none" />
      <path d="M9.5 16.5h5" />
    </svg>
  );
}
function AutoIcon({ active }: { active: boolean }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" className={`h-6 w-6 ${stroke(active)}`} strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      {/* D-076 — the Auto Trading tab: a bolt inside a rounded square */}
      <rect x="4" y="4" width="16" height="16" rx="4" />
      <path d="M12.8 7.5l-3.3 4.4h2.6l-.6 4.6 3.3-4.8h-2.5z" fill="currentColor" className={active ? "fill-gold" : "fill-zinc-500"} stroke="none" />
    </svg>
  );
}
function SettingsIcon({ active }: { active: boolean }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" className={`h-6 w-6 ${stroke(active)}`} strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="3.2" />
      <path d="M12 2.8v2.4M12 18.8v2.4M4.4 7.6l2 1.2M17.6 15.2l2 1.2M4.4 16.4l2-1.2M17.6 8.8l2-1.2" />
    </svg>
  );
}

const ITEMS: Item[] = [
  { id: "home", label: "Home", href: "/", icon: (a) => <HomeIcon active={a} /> },
  { id: "charts", label: "Charts", href: "/charts", icon: (a) => <ChartIcon active={a} /> },
  { id: "ai", label: "AI Trading", href: "/ai", icon: (a) => <AiIcon active={a} /> },
  { id: "autotrade", label: "Auto Trade", href: "/autotrade", icon: (a) => <AutoIcon active={a} /> },
  { id: "settings", label: "Settings", href: "/settings", icon: (a) => <SettingsIcon active={a} /> },
];

export default function BottomNav({
  active,
  onChange,
}: {
  active: AppTab;
  onChange: (tab: AppTab) => void;
}) {
  return (
    <nav
      aria-label="Main navigation"
      className="fixed inset-x-0 bottom-0 z-40 border-t border-zinc-800/80 bg-zinc-950/95 backdrop-blur-md"
      style={{ paddingBottom: "env(safe-area-inset-bottom)" }}
    >
      <div className="mx-auto flex max-w-3xl items-stretch justify-around">
        {ITEMS.map((item) => {
          const isActive = active === item.id;
          return (
            <button
              key={item.id}
              type="button"
              onClick={() => onChange(item.id)}
              aria-current={isActive ? "page" : undefined}
              className={`relative flex min-w-0 flex-1 flex-col items-center gap-0.5 px-1 py-2.5 transition-colors ${
                isActive ? "text-gold" : "text-zinc-500 hover:text-zinc-300"
              }`}
            >
              {isActive && (
                <span className="absolute inset-x-6 top-0 h-0.5 rounded-full bg-gold shadow-[0_0_8px_#d4af37]" />
              )}
              {item.icon(isActive)}
              <span className="truncate text-[10px] font-semibold tracking-wide">
                {item.label}
              </span>
            </button>
          );
        })}
      </div>
    </nav>
  );
}
