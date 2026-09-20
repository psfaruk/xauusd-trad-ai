import { TIMEFRAMES, type Timeframe } from "../types";

interface TimeframeSwitcherProps {
  tf: Timeframe;
  onChange: (tf: Timeframe) => void;
}

export default function TimeframeSwitcher({ tf, onChange }: TimeframeSwitcherProps) {
  return (
    <div
      className="flex flex-wrap items-center gap-1.5"
      role="tablist"
      aria-label="Timeframes"
    >
      {TIMEFRAMES.map((item) => (
        <button
          key={item}
          type="button"
          role="tab"
          aria-selected={item === tf}
          onClick={() => onChange(item)}
          className={`rounded-md border px-2.5 py-1 text-xs font-medium transition-colors ${
            item === tf
              ? "border-gold/60 bg-gold/15 text-gold"
              : "border-zinc-800 bg-zinc-900 text-zinc-400 hover:border-zinc-700 hover:text-zinc-200"
          }`}
        >
          {item}
        </button>
      ))}
    </div>
  );
}
