/**
 * SignalDetail (D-041) — the "why did the AI take this signal" panel the
 * user asked for: full analysis checklist (every factor the engine verified,
 * with real values), entry/SL/TP geometry, confidence and outcome.
 */

import type { Signal, SignalStatus } from "../types";
import { Badge, Card } from "./ui";

export function statusTone(status: SignalStatus): "green" | "red" | "amber" | "gold" | "blue" | "zinc" {
  if (status === "won") return "green";
  if (status === "lost") return "red";
  if (status === "expired") return "amber";
  if (status === "active") return "gold";
  if (status === "pending") return "blue"; // D-050 — waiting for the limit fill
  return "zinc";
}

export function fmtTime(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** Pretty label for an engine check name (trace.checks[].name). */
function prettyCheck(name: string): string {
  const map: Record<string, string> = {
    trend_h1: "H1 Trend",
    mtf_m5: "M5 Confirmation",
    mtf_m15: "M15 Confirmation",
    mtf_m30: "M30 Confirmation",
    sfp_sweep: "Liquidity Sweep (SFP)",
    pullback: "Pullback Rejection",
    rsi: "RSI Window",
    atr: "Volatility (ATR)",
    session: "Session",
    news: "News Filter",
    spread: "Spread Cap",
    spread_risk: "Spread vs Risk",
    // D-042 ICT/SMC confluence factors
    structure_m1: "M1 Market Structure",
    htf_structure: "HTF Structure Bias",
    ob_retest: "Order-Block Retest",
    fvg_fill: "Fair Value Gap",
    liquidity_sweep: "Liquidity Sweep",
    zone: "Supply/Demand Zone",
    volume: "Institutional Volume",
    killzone: "ICT Kill Zone",
    whale_bias: "Whale Bias",
    confluence: "ICT Confluence",
    // D-050 pending-entry stage
    entry_mode: "Pending Entry (POI)",
  };
  return map[name] ?? name;
}

export function SignalRow({
  signal,
  selected,
  onSelect,
}: {
  signal: Signal;
  selected: boolean;
  onSelect: () => void;
}) {
  const buy = signal.direction === "BUY";
  return (
    <button
      type="button"
      onClick={onSelect}
      className={`flex w-full min-w-0 items-center gap-2.5 rounded-xl border px-3 py-2.5 text-left transition-colors ${
        selected
          ? "border-gold/50 bg-gold/10"
          : "border-zinc-800/70 bg-zinc-900/40 hover:border-zinc-700"
      }`}
    >
      <span
        className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-[10px] font-bold ${
          buy ? "bg-emerald-500/15 text-emerald-400" : "bg-red-500/15 text-red-400"
        }`}
      >
        {buy ? "BUY" : "SELL"}
      </span>
      <span className="flex min-w-0 flex-1 flex-col gap-0.5">
        <span className="flex min-w-0 items-center gap-2">
          <span className="truncate font-mono text-xs font-semibold text-zinc-200 tabular-nums">
            {signal.entry.toFixed(2)}
          </span>
          <span className="shrink-0 text-[10px] text-zinc-500">{fmtTime(signal.ts)}</span>
        </span>
        <span className="flex min-w-0 items-center gap-1.5">
          <span className="truncate text-[10px] text-zinc-500">
            conf {Math.round(signal.confidence * 100)}%
            {signal.trace?.trigger ? ` · ${signal.trace.trigger}` : ""}
          </span>
        </span>
      </span>
      <span className="flex shrink-0 flex-col items-end gap-0.5">
        <Badge tone={statusTone(signal.status)}>{signal.status}</Badge>
        {signal.result_r != null && (
          <span
            className={`font-mono text-[10px] font-semibold tabular-nums ${
              signal.result_r >= 0 ? "text-emerald-400" : "text-red-400"
            }`}
          >
            {signal.result_r >= 0 ? "+" : ""}
            {signal.result_r.toFixed(2)}R
          </span>
        )}
      </span>
    </button>
  );
}

export function SignalDetail({ signal }: { signal: Signal }) {
  const checks = signal.trace?.checks ?? [];
  const factors = signal.trace?.confluence_factors ?? [];
  const trigger = signal.trace?.trigger ?? "sfp";
  const buy = signal.direction === "BUY";
  return (
    <div className="flex min-w-0 flex-col gap-3">
      {/* header */}
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <span
          className={`rounded-lg px-2.5 py-1 text-xs font-bold ${
            buy ? "bg-emerald-500/15 text-emerald-400" : "bg-red-500/15 text-red-400"
          }`}
        >
          {signal.direction}
        </span>
        <Badge tone="zinc">{signal.symbol} · {signal.tf}</Badge>
        <Badge tone={statusTone(signal.status)}>{signal.status}</Badge>
        {/* D-050 — POI pending limit entry badge */}
        {signal.entry_type === "limit" && (
          <Badge tone="blue">PENDING LIMIT</Badge>
        )}
        <Badge tone="blue">
          {trigger === "sfp"
            ? "Liquidity Sweep"
            : trigger === "zone"
              ? "POI Zone Retest"
              : "Trend Pullback"}
        </Badge>
        <span className="ml-auto text-[11px] text-zinc-500">{fmtTime(signal.ts)}</span>
      </div>

      {/* entry geometry */}
      <div className="grid grid-cols-3 gap-2">
        <div className="rounded-xl border border-gold/25 bg-gold/5 px-3 py-2">
          <p className="text-[10px] font-semibold uppercase tracking-wider text-gold/80">
            {signal.entry_type === "limit" ? "Entry (Limit)" : "Entry"}
          </p>
          <p className="truncate font-mono text-sm font-semibold text-zinc-100 tabular-nums">
            {signal.entry.toFixed(2)}
          </p>
          {signal.entry_type === "limit" && signal.market_ref != null && (
            <p className="truncate font-mono text-[10px] text-zinc-500">
              market {signal.market_ref.toFixed(2)}
            </p>
          )}
        </div>
        <div className="rounded-xl border border-red-500/25 bg-red-500/5 px-3 py-2">
          <p className="text-[10px] font-semibold uppercase tracking-wider text-red-400/80">
            Stop Loss
          </p>
          <p className="truncate font-mono text-sm font-semibold text-zinc-100 tabular-nums">
            {signal.sl.toFixed(2)}
          </p>
        </div>
        <div className="rounded-xl border border-emerald-500/25 bg-emerald-500/5 px-3 py-2">
          <p className="text-[10px] font-semibold uppercase tracking-wider text-emerald-400/80">
            Take Profit{signal.rr ? ` · ${signal.rr.toFixed(1)}R` : ""}
          </p>
          <p className="truncate font-mono text-sm font-semibold text-zinc-100 tabular-nums">
            {signal.tp.toFixed(2)}
          </p>
        </div>
      </div>

      {/* D-049 — where the TP is predicted to sit (structure target) */}
      {signal.target_note && (
        <p className="rounded-lg border border-zinc-700/60 bg-zinc-800/40 px-3 py-1.5 text-[10px] leading-relaxed text-zinc-400">
          <span className="font-semibold text-zinc-300">TP predicted at</span>{" "}
          {signal.target_note}
        </p>
      )}

      {/* D-050 — the POI anchor the pending limit sits at */}
      {signal.entry_type === "limit" && signal.entry_note && (
        <p className="rounded-lg border border-blue-500/25 bg-blue-500/5 px-3 py-1.5 text-[10px] leading-relaxed text-zinc-400">
          <span className="font-semibold text-blue-300">Pending entry at</span>{" "}
          {signal.entry_note}
          {signal.filled_at ? ` — filled ${fmtTime(signal.filled_at)}` : " — waiting for the market to retrace"}
        </p>
      )}

      {/* D-061 — the institutional-cycle context (AMD phase / trap risk /
       * session / news) the engine stamped at signal time: the user sees
       * exactly WHICH side of the manipulation this trade took
       * ("এই বিষয় টা কিভাবে আমার অ্যাপ বুজবে। এবং আমিও দেখতে পারবো"). */}
      {(() => {
        const ctx = signal.context ?? signal.trace?.context ?? null;
        if (!ctx) return null;
        const phase = ctx.amd?.phase ?? null;
        const trap = ctx.trap ?? null;
        const ses = ctx.session ?? null;
        if (!phase && !trap && !ses && !ctx.news) return null;
        return (
          <div className="min-w-0 rounded-xl border border-zinc-700/60 bg-zinc-800/40 p-2.5">
            <p className="mb-1.5 text-[10px] font-semibold uppercase tracking-wider text-zinc-500">
              Market context · Institutional cycle
            </p>
            <div className="flex min-w-0 flex-wrap items-center gap-1.5">
              {phase && (
                <Badge tone={phase === "manipulation" ? "amber" : phase === "distribution" ? "green" : "gold"}>
                  {phase.toUpperCase()}
                </Badge>
              )}
              {trap && trap.risk > 0 && (
                <Badge tone={trap.risk >= 0.7 ? "red" : trap.risk >= 0.4 ? "amber" : "zinc"}>
                  TRAP RISK {Math.round(trap.risk * 100)}%
                </Badge>
              )}
              {ses && (
                <Badge tone={ses.judas_window ? "amber" : "zinc"}>
                  {ses.judas_window ? "JUDAS WINDOW" : (ses.name ?? "session").toUpperCase()}
                </Badge>
              )}
            </div>
            {ctx.amd?.note && (
              <p className="mt-1.5 text-[10px] leading-relaxed text-zinc-400">
                {ctx.amd.note}
              </p>
            )}
            {trap && trap.reasons.length > 0 && (
              <ul className="mt-1.5 flex min-w-0 flex-col gap-1">
                {trap.reasons.slice(0, 3).map((r, i) => (
                  <li
                    key={i}
                    className={`rounded-lg border px-2.5 py-1.5 text-[10px] leading-relaxed ${
                      trap.warned || trap.risk >= 0.4
                        ? "border-red-500/25 bg-red-500/5 text-zinc-300"
                        : "border-zinc-800 bg-zinc-900/40 text-zinc-400"
                    }`}
                  >
                    {r}
                  </li>
                ))}
              </ul>
            )}
            {(ses?.note || ctx.news) && (
              <p className="mt-1.5 text-[9px] leading-relaxed text-zinc-500">
                {ses?.note ? `${ses.note}. ` : ""}
                {ctx.news ? `News: ${ctx.news}.` : ""}
              </p>
            )}
          </div>
        );
      })()}

      {/* confidence */}
      <div className="min-w-0">
        <div className="mb-1 flex items-center justify-between text-[10px] font-semibold uppercase tracking-wider text-zinc-500">
          <span>AI Confidence</span>
          <span className="font-mono text-gold tabular-nums">
            {Math.round(signal.confidence * 100)}%
          </span>
        </div>
        <div className="h-1.5 w-full overflow-hidden rounded-full bg-zinc-800">
          <div
            className="h-full rounded-full bg-gradient-to-r from-gold/60 to-gold"
            style={{ width: `${Math.round(signal.confidence * 100)}%` }}
          />
        </div>
      </div>

      {/* D-042 — the ICT/SMC confluence factors (zones, structure, whales) */}
      {factors.length > 0 && (
        <div className="min-w-0">
          <p className="mb-1.5 text-[10px] font-semibold uppercase tracking-wider text-zinc-500">
            ICT / Smart-Money confluence
          </p>
          <ul className="flex min-w-0 flex-col gap-1.5">
            {factors.map((f, i) => (
              <li
                key={i}
                className={`flex min-w-0 items-start gap-2.5 rounded-lg border px-3 py-2 ${
                  f.ok
                    ? "border-violet-500/20 bg-violet-500/5"
                    : "border-zinc-800 bg-zinc-900/40"
                }`}
              >
                <span
                  className={`mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded text-[10px] font-bold ${
                    f.ok ? "bg-violet-500/20 text-violet-300" : "bg-zinc-700/40 text-zinc-500"
                  }`}
                >
                  {f.ok ? "✓" : "–"}
                </span>
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-xs font-semibold text-zinc-200">
                    {prettyCheck(f.name)}
                  </span>
                  <span className="block break-words font-mono text-[10px] leading-relaxed text-zinc-500">
                    {f.detail}
                  </span>
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* the analysis checklist */}
      <div className="min-w-0">
        <p className="mb-1.5 text-[10px] font-semibold uppercase tracking-wider text-zinc-500">
          Analysis — what the AI verified
        </p>
        <ul className="flex min-w-0 flex-col gap-1.5">
          {checks.length === 0 && (
            <li className="rounded-lg border border-zinc-800 bg-zinc-900/40 px-3 py-2 text-xs text-zinc-500">
              No analysis trace stored for this signal.
            </li>
          )}
          {checks.map((c, i) => (
            <li
              key={i}
              className={`flex min-w-0 items-start gap-2.5 rounded-lg border px-3 py-2 ${
                c.pass
                  ? "border-emerald-500/20 bg-emerald-500/5"
                  : "border-red-500/20 bg-red-500/5"
              }`}
            >
              <span
                className={`mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded text-[10px] font-bold ${
                  c.pass ? "bg-emerald-500/20 text-emerald-400" : "bg-red-500/20 text-red-400"
                }`}
              >
                {c.pass ? "✓" : "✕"}
              </span>
              <span className="min-w-0 flex-1">
                <span className="block truncate text-xs font-semibold text-zinc-200">
                  {prettyCheck(c.name)}
                </span>
                <span className="block break-words font-mono text-[10px] leading-relaxed text-zinc-500">
                  {c.value}
                </span>
              </span>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}

/** Compact card for the latest signal on the Home tab. */
export function LatestSignalCard({
  signal,
  onOpen,
}: {
  signal: Signal | null;
  onOpen: () => void;
}) {
  if (!signal) {
    return (
      <Card>
        <p className="text-xs font-semibold text-zinc-400">Latest AI Signal</p>
        <p className="mt-2 text-[11px] leading-relaxed text-zinc-500">
          No signal yet — the engine evaluates every M1 close and will appear here the
          moment all checks pass.
        </p>
      </Card>
    );
  }
  const buy = signal.direction === "BUY";
  return (
    <Card>
      <div className="flex items-center justify-between gap-2">
        <p className="text-xs font-semibold text-zinc-400">Latest AI Signal</p>
        <Badge tone={statusTone(signal.status)}>{signal.status}</Badge>
      </div>
      <div className="mt-2.5 flex min-w-0 items-center gap-3">
        <span
          className={`rounded-lg px-2.5 py-1.5 text-xs font-bold ${
            buy ? "bg-emerald-500/15 text-emerald-400" : "bg-red-500/15 text-red-400"
          }`}
        >
          {signal.direction}
        </span>
        <div className="min-w-0 flex-1">
          <p className="truncate font-mono text-sm font-semibold text-zinc-100 tabular-nums">
            {signal.entry.toFixed(2)}
            <span className="ml-2 text-[10px] font-normal text-zinc-500">
              SL {signal.sl.toFixed(2)} · TP {signal.tp.toFixed(2)}
            </span>
          </p>
          <p className="text-[10px] text-zinc-500">
            {fmtTime(signal.ts)} · conf {Math.round(signal.confidence * 100)}%
            {signal.result_r != null && (
              <span
                className={`ml-1.5 font-semibold ${
                  signal.result_r >= 0 ? "text-emerald-400" : "text-red-400"
                }`}
              >
                {signal.result_r >= 0 ? "+" : ""}
                {signal.result_r.toFixed(2)}R
              </span>
            )}
          </p>
        </div>
        <button
          type="button"
          onClick={onOpen}
          className="shrink-0 rounded-xl border border-zinc-700 bg-zinc-800 px-3 py-1.5 text-[10px] font-semibold text-zinc-300 hover:bg-zinc-700"
        >
          Analysis
        </button>
      </div>
    </Card>
  );
}
