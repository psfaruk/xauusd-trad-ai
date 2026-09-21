import type { Mt5Status } from "../types";

interface SymbolSwitcherProps {
  symbols: string[];
  value: string;
  onChange: (symbol: string) => void;
  mt5?: Mt5Status | null;
}

/**
 * SymbolSwitcher (D-035) — instrument chips (XAUUSD | BTCUSD).
 * Each chip carries a live dot: gold = real broker feed via the MT5
 * terminal, blue = crypto composite (forex closed / terminal down).
 */
export default function SymbolSwitcher({ symbols, value, onChange, mt5 }: SymbolSwitcherProps) {
  if (symbols.length <= 1) return null;
  const feedFor = (sym: string) => mt5?.feed?.symbols?.[sym];

  return (
    <div className="flex rounded-lg border border-zinc-700 bg-zinc-900/60 p-0.5" role="tablist" aria-label="Instrument">
      {symbols.map((sym) => {
        const active = sym === value;
        const fs = feedFor(sym);
        const mt5On = fs?.mt5 === true;
        return (
          <button
            key={sym}
            type="button"
            role="tab"
            aria-selected={active}
            onClick={() => onChange(sym)}
            title={
              fs?.detail ??
              (mt5On ? "real broker feed via MetaTrader 5" : "live feed")
            }
            className={`flex items-center gap-1.5 rounded-md px-3 py-1.5 text-xs font-semibold transition-colors ${
              active
                ? "bg-gold/15 text-gold"
                : "text-zinc-400 hover:text-zinc-200"
            }`}
          >
            <span
              className={`inline-block h-1.5 w-1.5 rounded-full ${
                mt5On
                  ? "bg-emerald-400 shadow-[0_0_6px_#34d399]"
                  : active
                    ? "bg-gold/70"
                    : "bg-zinc-600"
              }`}
            />
            {sym}
          </button>
        );
      })}
    </div>
  );
}
