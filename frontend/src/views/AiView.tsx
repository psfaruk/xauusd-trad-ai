/**
 * AiView (D-041) — the AI Trading tab: honest "why is the AI trading / not
 * trading" status, arm/disarm, the live execution event feed, the manual
 * order pad and open positions (real terminal), and the real trade history.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  getMt5History,
  getMt5Positions,
  postMt5AutoTrade,
  postMt5Close,
  postMt5Order,
} from "../lib/api";
import type {
  Mt5AutoTradeStatus, Mt5HistoryPosition, Mt5OpenPosition,
  Mt5OrderResult, Signal, WsMt5AutoMsg,
} from "../types";
import {
  Badge, Btn, Card, EmptyState, Field, SectionTitle, Stat, inputCls,
} from "../components/ui";
import { fmtTime } from "../components/SignalDetail";

interface Props {
  token: string;
  symbol: string; // broker-side platform symbol for manual orders
  autoStatus: Mt5AutoTradeStatus | null;
  autoEvents: WsMt5AutoMsg[];
  refreshKey: number;
  signals: Signal[];
  onArmChanged: () => void;
}

/* --------------------------------------------------------------- arm card */

function ArmCard({
  status,
  token,
  onArmChanged,
}: {
  status: Mt5AutoTradeStatus | null;
  token: string;
  onArmChanged: () => void;
}) {
  const [confirmText, setConfirmText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const armed = status?.armed ?? false;
  const why = status?.why;

  const toggle = async (enable: boolean) => {
    setBusy(true);
    setError(null);
    try {
      await postMt5AutoTrade(token, {
        enabled: enable,
        confirm: enable ? confirmText.trim().toUpperCase() : undefined,
      });
      setConfirmText("");
      onArmChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : "failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card>
      <SectionTitle
        title="AI Auto-Trading"
        right={
          armed ? (
            <Badge tone="green" pulse>ARMED</Badge>
          ) : (
            <Badge tone="amber">PAUSED</Badge>
          )
        }
      />
      {!status ? (
        <div className="flex flex-col gap-2">
          <div className="h-4 w-3/4 animate-pulse rounded bg-zinc-800" />
          <div className="h-4 w-1/2 animate-pulse rounded bg-zinc-800" />
        </div>
      ) : (
        <>
          <p className="min-w-0 text-[11px] leading-relaxed text-zinc-400">
            {armed
              ? "Engine armed. Every M1 bar close is analyzed (H1 trend, M5 + M15 confirmation, pattern trigger, RSI/ATR/session/spread checks) and confirmed signals execute as real orders automatically."
              : why?.text ?? "Arm the engine to enable automatic execution."}
          </p>

          {/* terminal health row */}
          <div className="mt-3 grid grid-cols-3 gap-2">
            <Stat
              label="Terminal"
              value={status.terminal.available ? "online" : "offline"}
              tone={status.terminal.available ? "up" : "down"}
            />
            <Stat
              label="Trading"
              value={status.terminal.trade_allowed ? "allowed" : "blocked"}
              tone={status.terminal.trade_allowed ? "up" : "down"}
            />
            <Stat
              label="Balance"
              value={
                status.terminal.balance != null
                  ? status.terminal.balance.toFixed(2)
                  : "—"
              }
              hint={status.terminal.currency ?? undefined}
            />
          </div>
          {status.markets && (
            <div className="mt-2 flex flex-wrap gap-2">
              {Object.entries(status.markets).map(([sym, m]) => (
                <Badge key={sym} tone={m.open ? "green" : "amber"}>
                  {sym} {m.open ? "open" : "closed"}
                </Badge>
              ))}
            </div>
          )}
          {status.last_skip_reason && (
            <p className="mt-2 rounded-lg border border-zinc-800 bg-zinc-900/50 px-3 py-2 text-[10px] leading-relaxed text-zinc-500">
              last skip: {status.last_skip_reason}
            </p>
          )}

          {/* arm / disarm */}
          <div className="mt-3.5 border-t border-zinc-800/70 pt-3.5">
            {armed ? (
              <Btn variant="danger" onClick={() => void toggle(false)} disabled={busy} className="w-full">
                {busy ? "Disarming…" : "Disarm auto-trading"}
              </Btn>
            ) : (
              <div className="flex flex-col gap-2">
                <Field
                  label='Type "ENABLE" to confirm arming real orders'
                  hint="Real orders will be placed on your connected broker account."
                >
                  <input
                    className={inputCls}
                    value={confirmText}
                    onChange={(e) => setConfirmText(e.target.value)}
                    placeholder="ENABLE"
                    autoComplete="off"
                  />
                </Field>
                <Btn
                  variant="success"
                  onClick={() => void toggle(true)}
                  disabled={busy || confirmText.trim().toUpperCase() !== "ENABLE"}
                  className="w-full"
                >
                  {busy ? "Arming…" : "Arm auto-trading"}
                </Btn>
              </div>
            )}
            {error && (
              <p className="mt-2 break-words rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-[11px] text-red-300">
                {error}
              </p>
            )}
          </div>
        </>
      )}
    </Card>
  );
}

/* ------------------------------------------------------------ event feed */

function EventFeed({ events }: { events: WsMt5AutoMsg[] }) {
  const recent = useMemo(() => [...events].reverse().slice(0, 30), [events]);
  return (
    <Card>
      <SectionTitle title="AI Activity" right={<Badge tone="zinc">{events.length}</Badge>} />
      {recent.length === 0 ? (
        <EmptyState
          title="No AI activity yet"
          hint="Arming, skips, orders and closes stream here in real time."
        />
      ) : (
        <ul className="flex max-h-72 min-w-0 flex-col gap-1.5 overflow-y-auto pr-1">
          {recent.map((ev, i) => (
            <li
              key={`${ev.ts}-${i}`}
              className={`flex min-w-0 items-start gap-2 rounded-lg border px-3 py-2 ${
                ev.event === "order" && ev.ok
                  ? "border-emerald-500/20 bg-emerald-500/5"
                  : ev.event === "skip"
                    ? "border-zinc-800 bg-zinc-900/40"
                    : ev.event === "order"
                      ? "border-red-500/20 bg-red-500/5"
                      : "border-gold/25 bg-gold/5"
              }`}
            >
              <Badge
                tone={
                  ev.event === "armed" ? "green"
                    : ev.event === "disarmed" ? "amber"
                      : ev.event === "order" ? (ev.ok ? "green" : "red")
                        : ev.event === "close" ? "blue"
                          : "zinc"
                }
              >
                {ev.event}
              </Badge>
              <span className="min-w-0 flex-1 break-words text-[11px] leading-relaxed text-zinc-300">
                {ev.message}
                {ev.retcode != null && (
                  <span className="ml-1 font-mono text-[10px] text-zinc-500">[{ev.retcode}]</span>
                )}
              </span>
              <span className="shrink-0 text-[9px] text-zinc-600">
                {new Date(ev.ts).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" })}
              </span>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

/* ------------------------------------------------------- manual trade pad */

function ManualTradeCard({
  token,
  symbols,
  refresh,
}: {
  token: string;
  symbols: string[];
  refresh: () => void;
}) {
  const [symbol, setSymbol] = useState(symbols[0] ?? "XAUUSD");
  const [volume, setVolume] = useState("0.01");
  const [sl, setSl] = useState("");
  const [tp, setTp] = useState("");
  const [busy, setBusy] = useState<"buy" | "sell" | null>(null);
  const [result, setResult] = useState<Mt5OrderResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  const place = async (side: "buy" | "sell") => {
    setBusy(side);
    setError(null);
    setResult(null);
    try {
      const res = await postMt5Order(token, {
        symbol,
        side,
        volume: parseFloat(volume),
        sl: sl ? parseFloat(sl) : undefined,
        tp: tp ? parseFloat(tp) : undefined,
      });
      setResult(res);
      refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : "order failed");
    } finally {
      setBusy(null);
    }
  };

  return (
    <Card>
      <SectionTitle title="Manual Order" right={<Badge tone="zinc">real account</Badge>} />
      <div className="grid grid-cols-2 gap-2">
        <Field label="Symbol">
          <select
            className={inputCls}
            value={symbol}
            onChange={(e) => setSymbol(e.target.value)}
          >
            {symbols.map((s) => (
              <option key={s} value={s}>{s}</option>
            ))}
          </select>
        </Field>
        <Field label="Volume (lots)">
          <input
            className={inputCls}
            value={volume}
            onChange={(e) => setVolume(e.target.value)}
            inputMode="decimal"
          />
        </Field>
        <Field label="Stop Loss (optional)">
          <input className={inputCls} value={sl} onChange={(e) => setSl(e.target.value)} inputMode="decimal" placeholder="—" />
        </Field>
        <Field label="Take Profit (optional)">
          <input className={inputCls} value={tp} onChange={(e) => setTp(e.target.value)} inputMode="decimal" placeholder="—" />
        </Field>
      </div>
      <div className="mt-3 grid grid-cols-2 gap-2">
        <Btn variant="success" onClick={() => void place("buy")} disabled={busy != null || parseFloat(volume) <= 0}>
          {busy === "buy" ? "Sending…" : "BUY"}
        </Btn>
        <Btn variant="danger" onClick={() => void place("sell")} disabled={busy != null || parseFloat(volume) <= 0}>
          {busy === "sell" ? "Sending…" : "SELL"}
        </Btn>
      </div>
      {result && (
        <p className={`mt-2.5 break-words rounded-lg border px-3 py-2 text-[11px] ${result.ok ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-300" : "border-red-500/30 bg-red-500/10 text-red-300"}`}>
          {result.ok ? "Order filled" : "Order rejected"} · retcode {result.retcode}
          {result.price != null && ` @ ${result.price}`}
          {result.detail ? ` — ${result.detail}` : ""}
        </p>
      )}
      {error && (
        <p className="mt-2.5 break-words rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-[11px] text-red-300">
          {error}
        </p>
      )}
    </Card>
  );
}

/* ----------------------------------------------------------- positions */

function PositionsCard({
  token,
  refreshKey,
  onChanged,
}: {
  token: string;
  refreshKey: number;
  onChanged: () => void;
}) {
  const [positions, setPositions] = useState<Mt5OpenPosition[]>([]);
  const [loading, setLoading] = useState(true);
  const [closing, setClosing] = useState<number | null>(null);

  const reload = useCallback(() => {
    getMt5Positions(token)
      .then((r) => setPositions(r.positions ?? []))
      .catch(() => setPositions([]))
      .finally(() => setLoading(false));
  }, [token]);

  useEffect(() => {
    reload();
    const t = window.setInterval(reload, 10_000);
    return () => window.clearInterval(t);
  }, [reload, refreshKey]);

  const close = async (symbol: string, ticket: number) => {
    setClosing(ticket);
    try {
      await postMt5Close(token, { symbol, ticket });
      reload();
      onChanged();
    } catch {
      /* surfaced by reload */
    } finally {
      setClosing(null);
    }
  };

  return (
    <Card>
      <SectionTitle
        title="Open Positions"
        right={<Badge tone="zinc">{positions.length}</Badge>}
      />
      {loading ? (
        <div className="flex flex-col gap-2">
          <div className="h-10 animate-pulse rounded-lg bg-zinc-800/60" />
          <div className="h-10 animate-pulse rounded-lg bg-zinc-800/60" />
        </div>
      ) : positions.length === 0 ? (
        <EmptyState title="No open positions" hint="AI and manual orders appear here while open." />
      ) : (
        <ul className="flex min-w-0 flex-col gap-1.5">
          {positions.map((p) => (
            <li
              key={p.position_id}
              className="flex min-w-0 items-center gap-2.5 rounded-xl border border-zinc-800/70 bg-zinc-900/40 px-3 py-2.5"
            >
              <Badge tone={p.action.toLowerCase().includes("buy") ? "green" : "red"}>
                {p.action}
              </Badge>
              <span className="min-w-0 flex-1">
                <span className="block truncate text-xs font-semibold text-zinc-200">
                  {p.symbol} · {p.volume} lots @ {p.price_open.toFixed(2)}
                </span>
                <span className="block text-[10px] text-zinc-500">
                  #{p.position_id} · {fmtTime(p.create_time)}
                </span>
              </span>
              {p.profit != null && (
                <span
                  className={`shrink-0 font-mono text-xs font-bold tabular-nums ${
                    p.profit >= 0 ? "text-emerald-400" : "text-red-400"
                  }`}
                >
                  {p.profit >= 0 ? "+" : ""}{p.profit.toFixed(2)}
                </span>
              )}
              <Btn
                variant="default"
                disabled={closing === p.position_id}
                onClick={() => void close(p.symbol, p.position_id)}
                className="shrink-0 !px-2.5 !py-1"
              >
                {closing === p.position_id ? "…" : "Close"}
              </Btn>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

/* -------------------------------------------------------------- history */

function HistoryCard({ token, refreshKey }: { token: string; refreshKey: number }) {
  const [rows, setRows] = useState<Mt5HistoryPosition[] | null>(null);

  useEffect(() => {
    getMt5History(token, 7)
      .then((r) => setRows(r.positions ?? []))
      .catch(() => setRows([]));
  }, [token, refreshKey]);

  const totalProfit = useMemo(
    () => (rows ?? []).reduce((acc, r) => acc + (r.profit ?? 0), 0),
    [rows],
  );

  return (
    <Card>
      <SectionTitle
        title="Trade History (7 days)"
        right={
          rows ? (
            <Badge tone={totalProfit >= 0 ? "green" : "red"}>
              {totalProfit >= 0 ? "+" : ""}{totalProfit.toFixed(2)}
            </Badge>
          ) : undefined
        }
      />
      {rows === null ? (
        <div className="flex flex-col gap-2">
          <div className="h-10 animate-pulse rounded-lg bg-zinc-800/60" />
          <div className="h-10 animate-pulse rounded-lg bg-zinc-800/60" />
        </div>
      ) : rows.length === 0 ? (
        <EmptyState title="No closed trades yet" hint="Closed AI and manual trades show here." />
      ) : (
        <ul className="flex max-h-80 min-w-0 flex-col gap-1.5 overflow-y-auto pr-1">
          {rows.slice(0, 50).map((r) => (
            <li
              key={`${r.position_id}-${r.close_time}`}
              className="flex min-w-0 items-center gap-2.5 rounded-xl border border-zinc-800/70 bg-zinc-900/40 px-3 py-2"
            >
              <Badge tone={r.type.toLowerCase().includes("buy") ? "green" : "red"}>
                {r.type}
              </Badge>
              <span className="min-w-0 flex-1">
                <span className="block truncate text-xs font-semibold text-zinc-200">
                  {r.symbol} · {r.open_volume} lots @ {r.open_price.toFixed(2)} → {r.close_price.toFixed(2)}
                </span>
                <span className="block text-[10px] text-zinc-500">
                  {fmtTime(r.close_time)}
                  {r.comment ? ` · ${r.comment.slice(0, 24)}` : ""}
                </span>
              </span>
              <span
                className={`shrink-0 font-mono text-xs font-bold tabular-nums ${
                  r.profit >= 0 ? "text-emerald-400" : "text-red-400"
                }`}
              >
                {r.profit >= 0 ? "+" : ""}{r.profit.toFixed(2)}
              </span>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

/* ------------------------------------------------------------------ view */

export default function AiView({
  token,
  symbol,
  autoStatus,
  autoEvents,
  refreshKey,
  signals,
  onArmChanged,
}: Props) {
  const [tick, setTick] = useState(0);
  const bump = useCallback(() => setTick((t) => t + 1), []);
  const symbols = useMemo(() => {
    const s = [symbol, "XAUUSD", "BTCUSD"];
    return [...new Set(s.filter(Boolean))];
  }, [symbol]);
  const activeSignals = signals.filter((s) => s.status === "active");

  return (
    <div className="flex min-w-0 flex-col gap-3">
      <ArmCard status={autoStatus} token={token} onArmChanged={onArmChanged} />

      {activeSignals.length > 0 && (
        <Card>
          <SectionTitle title="Active AI Signals" right={<Badge tone="gold" pulse>{activeSignals.length}</Badge>} />
          <ul className="flex min-w-0 flex-col gap-1.5">
            {activeSignals.map((s) => (
              <li key={s.id} className="flex min-w-0 items-center gap-2.5 rounded-xl border border-gold/20 bg-gold/5 px-3 py-2">
                <Badge tone={s.direction === "BUY" ? "green" : "red"}>{s.direction}</Badge>
                <span className="min-w-0 flex-1 truncate font-mono text-xs text-zinc-200 tabular-nums">
                  {s.entry.toFixed(2)} · SL {s.sl.toFixed(2)} · TP {s.tp.toFixed(2)}
                </span>
                <span className="shrink-0 text-[10px] text-zinc-500">{fmtTime(s.ts)}</span>
              </li>
            ))}
          </ul>
        </Card>
      )}

      <EventFeed events={autoEvents} />
      <ManualTradeCard token={token} symbols={symbols} refresh={bump} />
      <PositionsCard token={token} refreshKey={refreshKey + tick} onChanged={bump} />
      <HistoryCard token={token} refreshKey={refreshKey + tick} />
    </div>
  );
}
