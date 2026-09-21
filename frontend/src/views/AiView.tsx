/**
 * AiView (D-041/D-042) — the AI Trading tab: the auto-trading switch with
 * its money-management setup window (D-042 — replaced the typed "ENABLE"),
 * honest "why is the AI trading / not trading" status, the live execution
 * event feed, the manual order pad, open positions and trade history.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  getConfig,
  getMt5History,
  getMt5Positions,
  postMt5AutoTrade,
  postMt5Close,
  postMt5Order,
  putConfig,
} from "../lib/api";
import type {
  EngineConfig, Mt5AutoTradeStatus, Mt5HistoryPosition, Mt5OpenPosition,
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

/* ------------------------------------------------------- D-042 switch */

function ToggleSwitch({
  on,
  busy,
  onToggle,
  labelOn = "ON",
  labelOff = "OFF",
}: {
  on: boolean;
  busy?: boolean;
  onToggle: (next: boolean) => void;
  labelOn?: string;
  labelOff?: string;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      disabled={busy}
      onClick={() => onToggle(!on)}
      className={`relative flex h-9 w-[76px] shrink-0 items-center rounded-full border transition-colors disabled:opacity-50 ${
        on ? "border-emerald-500/60 bg-emerald-500/20" : "border-zinc-700 bg-zinc-800"
      }`}
    >
      <span
        className={`absolute top-1/2 h-7 w-7 -translate-y-1/2 rounded-full shadow-lg transition-all ${
          on ? "left-[44px] bg-emerald-400" : "left-1 bg-zinc-500"
        } ${busy ? "animate-pulse" : ""}`}
      />
      <span
        className={`select-none px-2.5 text-[10px] font-bold tracking-wide ${
          on ? "text-emerald-300" : "text-zinc-500"
        }`}
        style={{ marginLeft: on ? "6px" : "40px" }}
      >
        {busy ? "…" : on ? labelOn : labelOff}
      </span>
    </button>
  );
}

/* --------------------------------------- D-042 money-management window */

interface MoneyForm {
  risk_mode: "percent" | "fixed";
  fixed_lot: string;
  risk_percent: string;
  max_positions: string;
  daily_max_loss_pct: string;
  rr: string;
  min_sl_atr: string;
  max_spread_points: string;
}

function MoneyManagementModal({
  token,
  balance,
  onDone,
  onClose,
}: {
  token: string;
  balance: number | null;
  onDone: () => void;
  onClose: () => void;
}) {
  const [form, setForm] = useState<MoneyForm | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getConfig(token)
      .then((res) => {
        const c = res.config;
        setForm({
          risk_mode: (c.risk_mode as "percent" | "fixed") ?? "percent",
          fixed_lot: String(c.fixed_lot ?? 0.01),
          risk_percent: String(c.risk_percent ?? 0.5),
          max_positions: String(c.max_positions ?? 3),
          daily_max_loss_pct: String(c.daily_max_loss_pct ?? 3),
          rr: String(c.rr ?? 1.1),
          min_sl_atr: String(c.min_sl_atr ?? 1.3),
          max_spread_points: String(c.max_spread_points ?? 35),
        });
      })
      .catch((e) => setError(e instanceof Error ? e.message : "config load failed"));
  }, [token]);

  const set = (k: keyof MoneyForm, v: string) =>
    setForm((f) => (f ? { ...f, [k]: v } : f));

  const saveAndArm = async () => {
    if (!form) return;
    setBusy(true);
    setError(null);
    try {
      const cur = await getConfig(token);
      const merged: EngineConfig = {
        ...cur.config,
        risk_mode: form.risk_mode,
        fixed_lot: Math.max(0.01, parseFloat(form.fixed_lot) || 0.01),
        risk_percent: Math.min(100, Math.max(0.05, parseFloat(form.risk_percent) || 0.5)),
        max_positions: Math.min(10, Math.max(1, parseInt(form.max_positions, 10) || 3)),
        daily_max_loss_pct: Math.min(90, Math.max(0.5, parseFloat(form.daily_max_loss_pct) || 3)),
        rr: Math.max(0.2, parseFloat(form.rr) || 1.1),
        min_sl_atr: Math.max(0.3, parseFloat(form.min_sl_atr) || 1.3),
        max_spread_points: Math.max(5, parseInt(form.max_spread_points, 10) || 35),
      };
      await putConfig(token, merged);
      await postMt5AutoTrade(token, { enabled: true });
      onDone();
    } catch (e) {
      setError(e instanceof Error ? e.message : "failed to arm");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-black/70 p-0 backdrop-blur-sm sm:items-center sm:p-4">
      <div className="max-h-[92vh] w-full max-w-md overflow-y-auto rounded-t-2xl border border-zinc-700/80 bg-zinc-900 shadow-2xl sm:rounded-2xl">
        <div className="sticky top-0 z-10 flex items-center justify-between gap-2 border-b border-zinc-800 bg-zinc-900/95 px-4 py-3 backdrop-blur">
          <div className="min-w-0">
            <p className="text-sm font-bold text-zinc-100">Money Management</p>
            <p className="text-[10px] text-zinc-500">
              Protect your balance — the AI trades inside these limits
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="shrink-0 rounded-lg border border-zinc-700 bg-zinc-800 px-2.5 py-1 text-xs text-zinc-400 hover:text-zinc-200"
          >
            ✕
          </button>
        </div>

        <div className="flex flex-col gap-3.5 px-4 py-4">
          {!form ? (
            <div className="flex flex-col gap-2">
              <div className="h-9 animate-pulse rounded-lg bg-zinc-800" />
              <div className="h-9 animate-pulse rounded-lg bg-zinc-800" />
              <div className="h-9 animate-pulse rounded-lg bg-zinc-800" />
            </div>
          ) : (
            <>
              {/* lot sizing mode */}
              <Field label="Position sizing">
                <div className="grid grid-cols-2 gap-2">
                  {(["percent", "fixed"] as const).map((m) => (
                    <button
                      key={m}
                      type="button"
                      onClick={() => set("risk_mode", m)}
                      className={`rounded-lg border px-3 py-2 text-xs font-semibold transition-colors ${
                        form.risk_mode === m
                          ? "border-gold/60 bg-gold/15 text-gold"
                          : "border-zinc-700 bg-zinc-900 text-zinc-400"
                      }`}
                    >
                      {m === "percent" ? "% of balance" : "Fixed lot"}
                    </button>
                  ))}
                </div>
              </Field>

              {form.risk_mode === "percent" ? (
                <Field
                  label="Risk per trade (% of balance)"
                  hint={
                    balance != null && balance > 0
                      ? `≈ $${((balance * (parseFloat(form.risk_percent) || 0)) / 100).toFixed(2)} per trade at $${balance.toFixed(0)} balance`
                      : "0.5% keeps a losing streak survivable"
                  }
                >
                  <input
                    className={inputCls}
                    value={form.risk_percent}
                    onChange={(e) => set("risk_percent", e.target.value)}
                    inputMode="decimal"
                  />
                </Field>
              ) : (
                <Field label="Lot size" hint="Fixed volume for every AI order">
                  <input
                    className={inputCls}
                    value={form.fixed_lot}
                    onChange={(e) => set("fixed_lot", e.target.value)}
                    inputMode="decimal"
                  />
                </Field>
              )}

              <div className="grid grid-cols-2 gap-2.5">
                <Field label="Max open trades" hint="Multiple entries can run together">
                  <input
                    className={inputCls}
                    value={form.max_positions}
                    onChange={(e) => set("max_positions", e.target.value)}
                    inputMode="numeric"
                  />
                </Field>
                <Field label="Daily loss limit (%)">
                  <input
                    className={inputCls}
                    value={form.daily_max_loss_pct}
                    onChange={(e) => set("daily_max_loss_pct", e.target.value)}
                    inputMode="decimal"
                  />
                </Field>
                <Field label="Reward : Risk" hint="Take-profit multiple of the stop distance">
                  <input
                    className={inputCls}
                    value={form.rr}
                    onChange={(e) => set("rr", e.target.value)}
                    inputMode="decimal"
                  />
                </Field>
                <Field label="Min stop (ATR×)" hint="Wider stop = less spread noise">
                  <input
                    className={inputCls}
                    value={form.min_sl_atr}
                    onChange={(e) => set("min_sl_atr", e.target.value)}
                    inputMode="decimal"
                  />
                </Field>
              </div>

              <Field label="Max spread (points)" hint="AI skips signals when the spread is wider">
                <input
                  className={inputCls}
                  value={form.max_spread_points}
                  onChange={(e) => set("max_spread_points", e.target.value)}
                  inputMode="numeric"
                />
              </Field>

              {error && (
                <p className="break-words rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-[11px] text-red-300">
                  {error}
                </p>
              )}

              <div className="flex flex-col gap-2 pt-1">
                <Btn variant="success" onClick={() => void saveAndArm()} disabled={busy} className="w-full">
                  {busy ? "Arming…" : "Save & Turn ON Auto-Trading"}
                </Btn>
                <p className="text-center text-[10px] leading-relaxed text-zinc-500">
                  Real orders will be placed on your connected broker account
                  with SL/TP attached and managed by the app.
                </p>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
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
  const [busy, setBusy] = useState(false);
  const [showMoney, setShowMoney] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const armed = status?.armed ?? false;
  const why = status?.why;

  const toggle = async (enable: boolean) => {
    if (enable) {
      setShowMoney(true); // D-042 — money-management window first
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await postMt5AutoTrade(token, { enabled: false });
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
              ? "Engine armed. Every M1 bar close is analyzed (H1/H4 structure, M5 + M15 confirmation, ICT zones & liquidity, pattern trigger) and confirmed signals execute as real orders automatically."
              : why?.text ?? "Turn the switch on to enable automatic execution."}
          </p>

          {/* the switch (D-042 — replaces typing "ENABLE") */}
          <div className="mt-3 flex items-center justify-between gap-3 rounded-xl border border-zinc-800/80 bg-zinc-900/50 px-3.5 py-3">
            <div className="min-w-0">
              <p className="text-xs font-semibold text-zinc-200">Auto-Trading Engine</p>
              <p className="text-[10px] text-zinc-500">
                {armed ? "Running — orders execute automatically" : "Off — signals only, no orders"}
              </p>
            </div>
            <ToggleSwitch on={armed} busy={busy} onToggle={(next) => void toggle(next)} />
          </div>

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
          {error && (
            <p className="mt-2 break-words rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-[11px] text-red-300">
              {error}
            </p>
          )}
        </>
      )}
      {showMoney && status && (
        <MoneyManagementModal
          token={token}
          balance={status.terminal.balance ?? null}
          onDone={() => {
            setShowMoney(false);
            onArmChanged();
          }}
          onClose={() => setShowMoney(false)}
        />
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
