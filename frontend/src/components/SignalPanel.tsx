import { useState } from "react";
import type { Signal, SignalStatus, StatsResponse } from "../types";

/**
 * SignalPanel (SPEC §10 / Phase 3): active SignalCard with the decision
 * trace (✓/✗ per check), history with status chips, and a compact stats row.
 */

interface SignalPanelProps {
  signals: Signal[];
  stats: StatsResponse | null;
  onSelectSymbolless?: void;
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

export default function SignalPanel({ signals, stats }: SignalPanelProps) {
  const active = signals.find((s) => s.status === "active") ?? null;
  const history = signals.filter((s) => s.status !== "active");

  return (
    <div className="flex flex-col gap-3">
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

      {stats && stats.closed_signals > 0 && (
        <div className="grid grid-cols-4 gap-2 rounded-xl border border-zinc-800 bg-zinc-900/40 p-3 text-center">
          <div>
            <p className="text-[10px] uppercase text-zinc-500">signals</p>
            <p className="text-sm font-semibold text-zinc-200">{stats.total_signals}</p>
          </div>
          <div>
            <p className="text-[10px] uppercase text-zinc-500">win rate</p>
            <p className="text-sm font-semibold text-zinc-200">
              {stats.win_rate !== null ? `${(stats.win_rate * 100).toFixed(0)}%` : "—"}
            </p>
          </div>
          <div>
            <p className="text-[10px] uppercase text-zinc-500">total R</p>
            <p className={`text-sm font-semibold ${(stats.total_r ?? 0) >= 0 ? "text-emerald-400" : "text-red-400"}`}>
              {stats.total_r !== null ? `${stats.total_r > 0 ? "+" : ""}${stats.total_r.toFixed(1)}` : "—"}
            </p>
          </div>
          <div>
            <p className="text-[10px] uppercase text-zinc-500">PF</p>
            <p className="text-sm font-semibold text-zinc-200">
              {stats.profit_factor !== null ? stats.profit_factor.toFixed(2) : "—"}
            </p>
          </div>
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
    </div>
  );
}
