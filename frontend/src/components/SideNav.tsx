/**
 * SideNav (D-058) — the desktop LEFT rail: the mobile bottom bar's twin.
 *
 * User directive (Bengali): "অ্যাপ টি কে এমন ভাবে ডিজাইন করবে, যেনো পিসি
 * সহ সকল ডিভাইসে ফিট থাকে… 2 পাশে অনেক ফাঁকা জায়গা পরে আছে" — on PC the
 * navigation absorbs the left edge as a slim rail so the content column
 * stretches across the entire remaining viewport. No max-width gutters,
 * no dead space. Shown lg+ only (mobile keeps the bottom bar).
 */

import type { AppTab } from "../types";

interface Item {
  id: AppTab;
  label: string;
  icon: (active: boolean) => JSX.Element;
}

const stroke = (active: boolean) =>
  active ? "stroke-gold" : "stroke-zinc-500";

function HomeIcon({ active }: { active: boolean }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" className={`h-5 w-5 ${stroke(active)}`} strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M3 10.5 12 3l9 7.5" />
      <path d="M5.5 9.5V20a1 1 0 0 0 1 1H10v-6h4v6h3.5a1 1 0 0 0 1-1V9.5" />
    </svg>
  );
}
function ChartIcon({ active }: { active: boolean }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" className={`h-5 w-5 ${stroke(active)}`} strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
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
    <svg viewBox="0 0 24 24" fill="none" className={`h-5 w-5 ${stroke(active)}`} strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <rect x="5" y="7" width="14" height="12" rx="3" />
      <path d="M12 7V4" />
      <circle cx="9.5" cy="13" r="1.2" fill="currentColor" className={active ? "fill-gold" : "fill-zinc-500"} stroke="none" />
      <circle cx="14.5" cy="13" r="1.2" fill="currentColor" className={active ? "fill-gold" : "fill-zinc-500"} stroke="none" />
      <path d="M9.5 16.5h5" />
    </svg>
  );
}
function SettingsIcon({ active }: { active: boolean }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" className={`h-5 w-5 ${stroke(active)}`} strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="3.2" />
      <path d="M12 2.8v2.4M12 18.8v2.4M4.4 7.6l2 1.2M17.6 15.2l2 1.2M4.4 16.4l2-1.2M17.6 8.8l2-1.2" />
    </svg>
  );
}

const ITEMS: Item[] = [
  { id: "home", label: "Home", icon: (a) => <HomeIcon active={a} /> },
  { id: "charts", label: "Charts", icon: (a) => <ChartIcon active={a} /> },
  { id: "ai", label: "AI Trading", icon: (a) => <AiIcon active={a} /> },
  { id: "settings", label: "Settings", icon: (a) => <SettingsIcon active={a} /> },
];

export default function SideNav({
  active,
  onChange,
}: {
  active: AppTab;
  onChange: (tab: AppTab) => void;
}) {
  return (
    <nav
      aria-label="Main navigation"
      className="sticky top-0 z-40 flex h-screen w-[76px] shrink-0 flex-col border-r border-zinc-800/80 bg-zinc-950/95 backdrop-blur-md"
    >
      {/* brand mark */}
      <div className="flex h-12 items-center justify-center border-b border-zinc-800/60">
        <span className="flex h-7 w-7 items-center justify-center rounded-lg bg-gold/15 text-xs font-black text-gold">
          Au
        </span>
      </div>
      <div className="flex flex-1 flex-col gap-1 px-2 py-3">
        {ITEMS.map((item) => {
          const isActive = active === item.id;
          return (
            <button
              key={item.id}
              type="button"
              onClick={() => onChange(item.id)}
              aria-current={isActive ? "page" : undefined}
              title={item.label}
              className={`group relative flex min-w-0 flex-col items-center gap-1 rounded-xl px-1 py-3 transition-colors ${
                isActive
                  ? "bg-gold/10 text-gold"
                  : "text-zinc-500 hover:bg-zinc-900/70 hover:text-zinc-300"
              }`}
            >
              {isActive && (
                <span className="absolute inset-y-2 left-0 w-0.5 rounded-full bg-gold shadow-[0_0_8px_#d4af37]" />
              )}
              {item.icon(isActive)}
              <span className="w-full truncate text-center text-[9px] font-semibold tracking-wide">
                {item.label}
              </span>
            </button>
          );
        })}
      </div>
      <p className="pb-3 text-center text-[8px] font-bold tracking-widest text-zinc-700">
        XAU
      </p>
    </nav>
  );
}
