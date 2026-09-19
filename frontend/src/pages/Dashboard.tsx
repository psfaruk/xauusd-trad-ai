import TopBar from "../components/TopBar";
import type { Timeframe } from "../types";

const TIMEFRAMES: Timeframe[] = ["M1", "M5", "M15", "M30", "H1", "H4", "D1"];

/**
 * Dashboard layout skeleton per SPEC §10 (chart + signal panel + account
 * strip). Phase 0 renders placeholders; the live chart (lightweight-charts)
 * lands in Phase 2 and the signal panel in Phase 3.
 */
export default function Dashboard() {
  return (
    <div className="flex min-h-screen flex-col bg-zinc-950">
      <TopBar />

      <main className="mx-auto flex w-full max-w-7xl flex-1 flex-col gap-4 p-4 lg:flex-row">
        {/* CHART area */}
        <section className="flex flex-1 flex-col gap-3" aria-label="Chart">
          <div className="flex flex-wrap items-center gap-1.5" role="tablist" aria-label="Timeframes">
            {TIMEFRAMES.map((tf) => (
              <span
                key={tf}
                role="tab"
                aria-selected={tf === "M15"}
                className={`rounded-md border px-2.5 py-1 text-xs font-medium ${
                  tf === "M15"
                    ? "border-gold/60 bg-gold/15 text-gold"
                    : "border-zinc-800 bg-zinc-900 text-zinc-400"
                }`}
              >
                {tf}
              </span>
            ))}
          </div>

          <div className="grid min-h-[320px] flex-1 place-items-center rounded-xl border border-zinc-800 bg-zinc-900/40 lg:min-h-[420px]">
            <div className="text-center">
              <p className="text-sm font-medium text-zinc-300">Chart</p>
              <p className="mt-1 text-xs text-zinc-500">
                lightweight-charts candlesticks · live ticks · Phase 2
              </p>
            </div>
          </div>
        </section>

        {/* SIGNAL PANEL area */}
        <section className="flex w-full flex-col gap-3 lg:w-96" aria-label="Signals">
          <div className="rounded-xl border border-zinc-800 bg-zinc-900/40 p-4">
            <p className="text-sm font-medium text-zinc-300">Active signal</p>
            <p className="mt-1 text-xs text-zinc-500">
              SignalCard + decision trace (✓/✗ per check) · Phase 3
            </p>
          </div>
          <div className="min-h-[160px] rounded-xl border border-zinc-800 bg-zinc-900/40 p-4">
            <p className="text-sm font-medium text-zinc-300">Signal history</p>
            <p className="mt-1 text-xs text-zinc-500">
              status chips, expandable traces · Phase 3
            </p>
          </div>
        </section>
      </main>

      {/* AccountStrip per SPEC §10 */}
      <footer className="border-t border-zinc-800 bg-zinc-900/60">
        <div className="mx-auto flex w-full max-w-7xl flex-wrap items-center gap-x-6 gap-y-1 px-4 py-2.5 text-xs text-zinc-400">
          <span>
            balance <span className="font-semibold text-zinc-200">—</span>
          </span>
          <span>
            equity <span className="font-semibold text-zinc-200">—</span>
          </span>
          <span>
            open positions <span className="font-semibold text-zinc-200">0</span>
          </span>
          <span className="ml-auto flex items-center gap-1.5">
            <span aria-hidden className="inline-block h-2 w-2 rounded-full bg-zinc-600" />
            engine <span className="font-semibold text-zinc-300">OFF</span>
            <span className="text-zinc-600">(auto-trade · default, Phase 4)</span>
          </span>
        </div>
      </footer>
    </div>
  );
}
