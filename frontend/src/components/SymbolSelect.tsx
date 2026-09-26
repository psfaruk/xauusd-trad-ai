/**
 * D-076 — the pair dropdown (user directive, Bengali, verbatim):
 * "আর সব পেয়ার গুলো একটি ড্রপ ডাউন বক্সে থাকবে, যেনো পরিবর্তন করলে সহজ হয়,
 * এতে করে জায়গা বাঁচবে" — every pair in ONE dropdown box so switching is
 * easy and space is saved (the symbol pill row is retired).
 *
 * A custom popover (not a native <select>): live market-open dot + per-market
 * label + keyboard/touch friendly — same interaction pattern as the chart's
 * TF menu. Closes on outside click / Escape.
 */
import { useEffect, useRef, useState } from "react";

import { MARKETS, marketMeta } from "../lib/markets";

export default function SymbolSelect({
  symbol,
  symbols,
  onChange,
  quote,
  compact = false,
}: {
  /** the active market key (any broker spelling is normalized) */
  symbol: string;
  /** full list to offer (backend symbols union the market list) */
  symbols: string[];
  onChange: (s: string) => void;
  /** optional live quote string for the button face ("2708.43") */
  quote?: string | null;
  compact?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const meta = marketMeta(symbol);
  const offered = symbols.length
    ? symbols
    : MARKETS.map((m) => m.key);

  return (
    <div ref={ref} className="relative min-w-0">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="listbox"
        aria-expanded={open}
        className={`flex min-w-0 items-center gap-1.5 rounded-lg border bg-ink-800/80 px-2.5 font-semibold text-cream-100 transition-colors hover:border-gold/50 ${
          compact ? "h-8 text-[11px]" : "h-9 text-xs"
        } ${open ? "border-gold/60" : "border-white/10"}`}
      >
        <span className="text-gold">{meta.key}</span>
        <span className="hidden truncate text-cream-300/70 sm:inline">
          {meta.label}
        </span>
        {quote ? (
          <span className="ml-0.5 hidden font-mono text-[10px] text-cream-300/60 md:inline">
            {quote}
          </span>
        ) : null}
        <svg
          className={`h-3.5 w-3.5 shrink-0 text-cream-300/70 transition-transform ${
            open ? "rotate-180" : ""
          }`}
          viewBox="0 0 16 16"
          fill="none"
          aria-hidden
        >
          <path
            d="M4 6l4 4 4-4"
            stroke="currentColor"
            strokeWidth="1.6"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      </button>

      {open && (
        <div
          role="listbox"
          className="absolute left-0 top-[calc(100%+6px)] z-40 w-52 overflow-hidden rounded-xl border border-white/10 bg-ink-900/95 shadow-2xl shadow-black/50 backdrop-blur"
        >
          {offered.map((s) => {
            const m = marketMeta(s);
            const active = m.key === meta.key;
            return (
              <button
                key={m.key}
                type="button"
                role="option"
                aria-selected={active}
                onClick={() => {
                  onChange(m.key);
                  setOpen(false);
                }}
                className={`flex w-full items-center justify-between gap-2 px-3 py-2.5 text-left text-xs transition-colors ${
                  active
                    ? "bg-gold/15 text-gold"
                    : "text-cream-200 hover:bg-white/5"
                }`}
              >
                <span className="min-w-0">
                  <span className="block font-semibold">{m.key}</span>
                  <span className="block truncate text-[10px] text-cream-300/60">
                    {m.name}
                  </span>
                </span>
                {active ? (
                  <svg
                    className="h-4 w-4 shrink-0"
                    viewBox="0 0 16 16"
                    fill="none"
                    aria-hidden
                  >
                    <path
                      d="M3 8.5l3.2 3.2L13 5"
                      stroke="currentColor"
                      strokeWidth="1.8"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                    />
                  </svg>
                ) : null}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
