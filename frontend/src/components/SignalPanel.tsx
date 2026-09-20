import { useState } from "react";
import MarketDataPanel from "./MarketDataPanel";
import type { Signal, SignalStatus, StatsResponse } from "../types";

/**
 * SignalPanel (SPEC §10 / Phase 3+4): tabs — Signals (active card + trace +
 * history), Market Data (feed transparency + external references, user req #5)
 * and Performance (PerfReport per SPEC §12 Phase 4).
 */

export type SignalPanelTab = "signals" | "market" | "performance";

interface SignalPanelProps {
  signals: Signal[];
  stats: StatsResponse | null;
  token: string;
  tab: SignalPanelTab;
  onTabChange: (t: SignalPanelTab) => void;
}

const STATUS_STYLE: Record<SignalStatus, string> = {
  active: "border-gold/50 bg-gold/10 text-gold",
  won: "border-emerald-500/40 bg-emerald-500/10 text-emerald-400",
  lost: "border-red-500/40 bg-red-500/10 text-red-400",
  expired: "border-zinc-600 bg-zinc-800 text-zinc-400",
  cancelled: "border-zinc-700 bg-zinc-800/60 text-zinc-500",
};

function fmtTime(ts: string): string {
  try {
    return new Date(ts).toLocaleString([], {
      month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
    });
  } catch {
    return ts;
  }
}

function SignalCard({ signal }: { signal: Signal }) {
  const buy = signal.direction === "BUY";
  return (
    <div className="rounded-xl border border-zinc-800 bg-zinc-900/60 p-4">
      <div className="flex items-center justify-between gap-2">
        <span
          className={`rounded-md px-2 py-0.5 text-xs font-bold ${
            buy ? "bg-emerald-500/15 text-emerald-400" : "bg-red-500/15 text-red-400"
          }`}
        >
          {signal.direction}
        </span>
        <span className="text-xs text-zinc-500">{fmtTime(signal.ts)} · {signal.tf}</span>
      </div>

      <div className="mt-3 grid grid-cols-3 gap-2 text-center">
        <div className="rounded-md border border-gold/30 bg-gold/5 py-1.5">
          <p className="text-[10px] uppercase tracking-wide text-zinc-500">entry</p>
          <p className="text-sm font-semibold text-gold">{signal.entry.toFixed(2)}</p>
        </div>
        <div className="rounded-md border border-red-500/25 bg-red-500/5 py-1.5">
          <p className="text-[10px] uppercase tracking-wide text-zinc-500">stop</p>
          <p className="text-sm font-semibold text-red-400">{signal.sl.toFixed(2)}</p>
        </div>
        <div className="rounded-md border border-emerald-500/25 bg-emerald-500/5 py-1.5">
          <p className="text-[10px] uppercase tracking-wide text-zinc-500">target</p>
          <p className="text-sm font-semibold text-emerald-400">{signal.tp.toFixed(2)}</p>
        </div>
      </div>

      <div className="mt-3">
        <div className="flex items-center justify-between text-[10px] uppercase tracking-wide text-zinc-500">
          <span>confidence</span>
          <span className="text-zinc-300">{(signal.confidence * 100).toFixed(0)}%</span>
        </div>
        <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-zinc-800">
          <div
            className="h-full rounded-full bg-gradient-to-r from-gold/60 to-gold"
            style={{ width: `${Math.min(100, signal.confidence * 100)}%` }}
          />
        </div>
      </div>

      <TraceList signal={signal} />
    </div>
  );
}

function TraceList({ signal }: { signal: Signal }) {
  const checks = signal.trace?.checks ?? [];
  const allPass = checks.length > 0 && checks.every((c) => c.pass);
  return (
    <div className="mt-4">
      <p className="mb-1.5 text-[10px] font-semibold uppercase tracking-wider text-zinc-500">
        decision trace {allPass && <span className="text-emerald-400">· all checks passed</span>}
      </p>
      <ul className="space-y-1">
        {checks.map((check) => (
          <li
            key={check.name}
            className="flex items-start gap-2 rounded-md border border-zinc-800/80 bg-zinc-950/60 px-2 py-1.5"
          >
            <span
              className={`mt-px font-mono text-xs font-bold ${
                check.pass ? "text-emerald-400" : "text-red-400"
              }`}
              aria-label={check.pass ? "passed" : "failed"}
            >
              {check.pass ? "✓" : "✗"}
            </span>
            <span className="text-xs font-medium text-zinc-300">{check.name}</span>
            <span className="ml-auto max-w-[55%] truncate text-right font-mono text-[11px] text-zinc-500" title={check.value}>
              {check.value}
            </span>
          </li>
        ))}
        {checks.length === 0 && (
          <li className="px-2 py-1.5 text-xs text-zinc-600">no trace stored</li>
        )}
      </ul>
    </div>
  );
}

function HistoryRow({ signal }: { signal: Signal }) {
  const [open, setOpen] = useState(false);
  return (
    <li className="rounded-lg border border-zinc-800 bg-zinc-900/40">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left"
      >
        <span
          className={`rounded px-1.5 py-0.5 text-[10px] font-bold ${
            signal.direction === "BUY"
              ? "bg-emerald-500/15 text-emerald-400"
              : "bg-red-500/15 text-red-400"
          }`}
        >
          {signal.direction}
        </span>
        <span className="text-xs text-zinc-300">{signal.entry.toFixed(2)}</span>
        <span className="text-[11px] text-zinc-500">{fmtTime(signal.ts)}</span>
        <span className={`ml-auto rounded border px-1.5 py-0.5 text-[10px] font-medium ${STATUS_STYLE[signal.status]}`}>
          {signal.status}
          {signal.result_r !== null && signal.result_r !== undefined && (
            <span className="ml-1 font-mono">
              {signal.result_r > 0 ? "+" : ""}
              {signal.result_r.toFixed(2)}R
            </span>
          )}
        </span>
      </button>
      {open && <TraceList signal={signal} />}
    </li>
  );
}

function PerformanceTab({ stats }: { stats: StatsResponse | null }) {
  if (!stats || stats.closed_signals === 0) {
    return (
      <div className="rounded-xl border border-zinc-800 bg-zinc-900/40 p-4 text-center text-xs text-zinc-500">
        No closed signals yet — performance appears after the first wins/losses.
      </div>
    );
  }
  const rows: [string, string][] = [
    ["Total signals", String(stats.total_signals)],
    ["Closed", String(stats.closed_signals)],
    ["Won / Lost / Expired", `${stats.won} / ${stats.lost} / ${stats.expired}`],
    ["Win rate", stats.win_rate !== null ? `${(stats.win_rate * 100).toFixed(1)}%` : "—"],
    ["Average R", stats.avg_r !== null ? stats.avg_r.toFixed(3) : "—"],
    ["Expectancy (R)", stats.expectancy !== null ? stats.expectancy.toFixed(3) : "—"],
    ["Profit factor", stats.profit_factor !== null ? stats.profit_factor.toFixed(2) : "—"],
    ["Max drawdown (R)", stats.max_drawdown_r.toFixed(2)],
    ["Total R", stats.total_r !== null ? `${stats.total_r > 0 ? "+" : ""}${stats.total_r.toFixed(2)}` : "—"],
  ];
  return (
    <div className="space-y-3">
      <div className="overflow-hidden rounded-xl border border-zinc-800">
        <table className="w-full text-xs">
          <tbody>
            {rows.map(([k, v]) => (
              <tr key={k} className="border-b border-zinc-800/70 last:border-0">
                <td className="bg-zinc-950/60 px-3 py-2 text-zinc-500">{k}</td>
                <td className="px-3 py-2 text-right font-mono font-semibold text-zinc-200">{v}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div>
        <p className="mb-1.5 text-[10px] font-semibold uppercase tracking-widest text-gold/70">by session</p>
        <div className="space-y-1">
          {Object.entries(stats.by_session).map(([name, s]) => (
            <div key={name} className="flex items-center gap-2 rounded-lg border border-zinc-800 bg-zinc-900/40 px-3 py-2 text-xs">
              <span className="w-20 text-zinc-400">{name}</span>
              <span className="text-zinc-500">{s.signals} sig</span>
              <span className="ml-auto text-emerald-400">{s.won}W</span>
              <span className="text-red-400">{s.lost}L</span>
              <span className="text-zinc-500">{s.expired}E</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

export default function SignalPanel({ signals, stats, token, tab, onTabChange }: SignalPanelProps) {
  const active = signals.find((s) => s.status === "active") ?? null;
  const history = signals.filter((s) => s.status !== "active");

  const tabs: { id: SignalPanelTab; label: string }[] = [
    { id: "signals", label: "signals" },
    { id: "market", label: "market data" },
    { id: "performance", label: "performance" },
  ];

  return (
    <div className="flex h-full flex-col gap-3">
      <div className="flex rounded-lg border border-zinc-800 bg-zinc-900/60 p-0.5">
        {tabs.map((t) => (
          <button
            key={t.id}
            type="button"
            onClick={() => onTabChange(t.id)}
            className={`flex-1 rounded-md px-2 py-1.5 text-xs font-medium capitalize transition-colors ${
              tab === t.id ? "bg-gold/15 text-gold" : "text-zinc-500 hover:text-zinc-300"
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {tab === "signals" && (
        <>
          {active ? (
            <SignalCard signal={active} />
          ) : (
            <div className="rounded-xl border border-zinc-800 bg-zinc-900/40 p-4 text-center">
              <p className="text-sm font-medium text-zinc-300">No active signal</p>
              <p className="mt-1 text-xs text-zinc-500">
                engine evaluates every M15 close — 7-check SFP pipeline
              </p>
            </div>
          )}

          <div className="rounded-xl border border-zinc-800 bg-zinc-900/40 p-3">
            <p className="mb-2 text-xs font-semibold uppercase tracking-wider text-zinc-500">
              history
            </p>
            {history.length === 0 ? (
              <p className="px-1 py-2 text-xs text-zinc-600">no closed signals yet</p>
            ) : (
              <ul className="max-h-[420px] space-y-1.5 overflow-y-auto pr-1">
                {history.map((s) => (
                  <HistoryRow key={s.id} signal={s} />
                ))}
              </ul>
            )}
          </div>
        </>
      )}

      {tab === "market" && <MarketDataPanel token={token} />}

      {tab === "performance" && <PerformanceTab stats={stats} />}
    </div>
  );
}
