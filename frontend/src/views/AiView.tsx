/**
 * AiView (D-041/D-042/D-044/D-077) — the AI Trading tab.
 *
 * D-077 — ONE tab, per the user's directive (Bengali, verbatim):
 * "দুইটি ট্যাব ... auto trad ও Ai trading, আমি দেখতে ছি দুইটার কাজ
 * same, তোমি এই দুইটা কে মার্জ করে একটি ট্যাব রাখো, সেটি হলো Ai
 * trading ... অটো ট্রেড ট্যাব টি দরকার নাই" — the Auto Trade tab is
 * merged INTO this view: the per-pair control room (signal engines,
 * auto-trade execution, per-pair lots + the shared money window) sits
 * in sequence right under the master arm card. One tab, one truth.
 *
 * D-044 institutional multi-user model:
 *  - the auto-trading switch arms THE USER'S OWN account (practice plane:
 *    own balance, own positions, own risk settings — full isolation);
 *  - the money-management window edits the user's per-user settings and is
 *    rendered through a React PORTAL (position:fixed inside a
 *    backdrop-blur ancestor is contained by that ancestor — the D-044
 *    "panel cut in half off-screen" bug);
 *  - manual orders / positions / history all run on the user's own plane;
 *  - Market Intelligence: order-flow (USD traded value, delta, whale
 *    zones), upcoming high-impact news and weekly CFTC positioning.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  getAnalysis,
  getConfig,
  getTradingPositions,
  getTradingSettings,
  getTradingTrades,
  postMt5AutoTrade,
  postTradingClose,
  postTradingOrder,
  putConfig,
  putTradingSettings,
} from "../lib/api";
import type {
  AnalysisResponse, EngineConfig, Mt5AutoTradeStatus, OrderResult, Signal,
  TradeRecord, TradingPendingOrder, TradingPosition, TradingStatus,
  UserSettings, WsMt5AutoMsg,
} from "../types";
import {
  Badge, Btn, Card, EmptyState, Field, NumberField, SectionTitle, Stat, inputCls,
} from "../components/ui";
import { MARKETS, MARKET_KEYS } from "../lib/markets";
import { fmtTime } from "../components/SignalDetail";
import StrategyRadar from "../components/StrategyRadar";

interface Props {
  token: string;
  symbol: string;
  /** D-077 — the live symbol list (backend symbols UNION all markets). */
  symbols: string[];
  isAdmin: boolean;
  autoStatus: Mt5AutoTradeStatus | null;
  autoEvents: WsMt5AutoMsg[];
  tradingAccount: TradingStatus | null;
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
  tone = "emerald",
}: {
  on: boolean;
  busy?: boolean;
  onToggle: (next: boolean) => void;
  labelOn?: string;
  labelOff?: string;
  /** D-077 — gold tone for the execution-tier switches (was the Auto Trade
   * tab's switch; one component now serves both tiers). */
  tone?: "emerald" | "gold";
}) {
  const onCls =
    tone === "gold"
      ? "border-gold/50 bg-gold/20"
      : "border-emerald-500/50 bg-emerald-500/25";
  const knobCls = tone === "gold" ? "bg-gold" : "bg-emerald-400";
  const txtCls = tone === "gold" ? "text-gold-200" : "text-emerald-200";
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      disabled={busy}
      onClick={() => onToggle(!on)}
      className={`relative h-8 w-14 shrink-0 rounded-full border transition-colors disabled:opacity-50 ${
        on ? onCls : "border-zinc-700 bg-zinc-800"
      }`}
    >
      <span
        className={`absolute top-1/2 h-7 w-7 -translate-y-1/2 rounded-full shadow-lg transition-all ${
          on ? `left-[calc(100%-1.75rem)] ${knobCls}` : "left-0.5 bg-zinc-500"
        }`}
      />
      <span
        className={`absolute inset-y-0 flex items-center text-[9px] font-bold tracking-wide ${
          on ? `left-2.5 ${txtCls}` : "right-2.5 text-zinc-400"
        }`}
      >
        {on ? labelOn : labelOff}
      </span>
    </button>
  );
}

/* ------------------------------------------- D-052 money management modal */

/** The EXACT five questions from the user's directive (Bengali):
 * [আপনার আজকের ট্রেডিং ব্যালেন্স অ্যাড করেন]
 * [স্টপ লস কত টার্গেট প্রফিট কত usd]
 * [লট সাইজ কত হবে]
 * [আজকে কত গুলো সিগন্যাল Ai অটো প্লেস করবে]
 * [একসাথে কত গুলো ট্রেড প্লেস করবে Ai]
 * Every field must be filled before the button turns on, and the AI
 * button turns itself OFF the moment any one limit completes.
 */
const MM_NUM_FIELDS = [
  "day_start_balance",
  "daily_loss_usd",
  "daily_profit_usd",
  "fixed_lot",
  "max_trades_per_day",
  "max_positions",
] as const;
type MmField = (typeof MM_NUM_FIELDS)[number];

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
  const [form, setForm] = useState<UserSettings | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getTradingSettings(token)
      .then((s) => {
        // prefill today's balance from the account when not set yet
        if (!s.day_start_balance && balance != null && balance > 0) {
          s.day_start_balance = Math.round(balance * 100) / 100;
        }
        setForm(s);
      })
      .catch((e) => setError(e instanceof Error ? e.message : "failed to load"));
  }, [token, balance]);

  // D-054 — fields commit through NumberField (draft-while-focused,
  // select-all on focus, ONE parsed commit on blur/Enter). The old
  // `parseFloat(v) || 0` on every keystroke made the current value
  // impossible to clear or edit in place (ghost "0" + eaten "-").
  const set = (k: MmField, v: number) =>
    setForm((f) => (f ? { ...f, [k]: v } : f));

  const num = (k: MmField): number => {
    const v = form?.[k];
    return typeof v === "number" && Number.isFinite(v) ? v : 0;
  };
  const missing = MM_NUM_FIELDS.filter((k) => num(k) <= 0);
  const allFilled = missing.length === 0;

  const saveAndArm = async () => {
    if (!form || !allFilled) return;
    setBusy(true);
    setError(null);
    try {
      await putTradingSettings(token, {
        // D-052 money-management window (the user's five questions)
        day_start_balance: num("day_start_balance"),
        daily_loss_usd: num("daily_loss_usd"),
        daily_profit_usd: num("daily_profit_usd"),
        fixed_lot: num("fixed_lot"),
        max_trades_per_day: Math.round(num("max_trades_per_day")),
        max_positions: Math.round(num("max_positions")),
        // lot size governs every AI order (fixed-lot mode)
        risk_mode: "fixed",
        // the USD stop loss replaces the percent fallback for today
        daily_max_loss_pct: 100,
      });
      await postMt5AutoTrade(token, { enabled: true });
      onDone();
    } catch (e) {
      setError(e instanceof Error ? e.message : "failed to arm");
    } finally {
      setBusy(false);
    }
  };

  // D-044 — PORTAL: render at document.body. position:fixed inside the
  // Card (backdrop-blur ancestor) is positioned relative to that ancestor,
  // which pushed this panel halfway off the screen.
  return createPortal(
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-black/70 p-0 backdrop-blur-sm sm:items-center sm:p-4">
      <div className="max-h-[88vh] w-full max-w-md overflow-y-auto rounded-t-2xl border border-zinc-700/80 bg-zinc-900 shadow-2xl sm:rounded-2xl">
        <div className="sticky top-0 z-10 flex items-center justify-between gap-2 border-b border-zinc-800 bg-zinc-900/95 px-4 py-3 backdrop-blur">
          <div className="min-w-0">
            <p className="text-sm font-bold text-zinc-100">Money Management</p>
            <p className="text-[10px] text-zinc-500">
              Fill every field to turn the AI on — it trades inside these limits
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
              {/* 1 — today's trading balance */}
              <Field
                label="Today's Trading Balance (USD)"
                hint={
                  balance != null && balance > 0
                    ? `Your account balance right now: $${balance.toFixed(2)}`
                    : "The balance all daily limits are measured from"
                }
              >
                <NumberField
                  value={form.day_start_balance}
                  onCommit={(v) => set("day_start_balance", v)}
                  emptyCommitsZero
                  placeholder="0.00"
                />
              </Field>

              {/* 2 — stop loss / target profit (USD) */}
              <div className="grid grid-cols-2 gap-2.5">
                <Field label="Stop Loss (USD)" hint="AI turns OFF if the day loses this much">
                  <NumberField
                    value={form.daily_loss_usd}
                    onCommit={(v) => set("daily_loss_usd", v)}
                    emptyCommitsZero
                    placeholder="0.00"
                  />
                </Field>
                <Field label="Target Profit (USD)" hint="AI turns OFF when profit reaches this">
                  <NumberField
                    value={form.daily_profit_usd}
                    onCommit={(v) => set("daily_profit_usd", v)}
                    emptyCommitsZero
                    placeholder="0.00"
                  />
                </Field>
              </div>

              {/* 3 — lot size */}
              <Field label="Lot Size" hint="Every AI order uses this volume">
                <NumberField
                  value={form.fixed_lot}
                  onCommit={(v) => set("fixed_lot", v)}
                  min={0.01}
                  max={100}
                  emptyCommitsZero
                  placeholder="0.01"
                />
              </Field>

              {/* 4 + 5 — signals today / trades at once */}
              <div className="grid grid-cols-2 gap-2.5">
                <Field label="Signals AI Will Place Today" hint="Daily auto-signal budget">
                  <NumberField
                    value={form.max_trades_per_day}
                    onCommit={(v) => set("max_trades_per_day", Math.round(v))}
                    min={1}
                    max={100}
                    emptyCommitsZero
                    placeholder="6"
                  />
                </Field>
                <Field label="Trades at the Same Time" hint="Maximum open positions together">
                  <NumberField
                    value={form.max_positions}
                    onCommit={(v) => set("max_positions", Math.round(v))}
                    min={1}
                    max={50}
                    emptyCommitsZero
                    placeholder="3"
                  />
                </Field>
              </div>

              {/* the auto-off rule, stated clearly */}
              <div className="rounded-xl border border-amber-500/25 bg-amber-500/5 px-3 py-2.5">
                <p className="text-[11px] leading-relaxed text-amber-200/90">
                  <span className="font-semibold text-amber-300">Auto-OFF rule:</span>{" "}
                  when any ONE of these completes — stop loss hit, target profit
                  reached, or the day's signal count finished — the AI button
                  turns itself OFF automatically.
                </p>
              </div>

              {/* missing-fields checklist (the gate) */}
              {!allFilled && (
                <div className="rounded-xl border border-zinc-800 bg-zinc-900/60 px-3 py-2">
                  <p className="mb-1 text-[10px] font-semibold uppercase tracking-wider text-zinc-500">
                    Fill these to enable the button
                  </p>
                  <ul className="flex flex-col gap-0.5">
                    {missing.map((k) => (
                      <li key={k} className="text-[11px] text-zinc-400">
                        <span className="mr-1.5 text-amber-400">•</span>
                        {
                          {
                            day_start_balance: "Today's Trading Balance",
                            daily_loss_usd: "Stop Loss (USD)",
                            daily_profit_usd: "Target Profit (USD)",
                            fixed_lot: "Lot Size",
                            max_trades_per_day: "Signals AI Will Place Today",
                            max_positions: "Trades at the Same Time",
                          }[k]
                        }
                      </li>
                    ))}
                  </ul>
                </div>
              )}

              {error && (
                <p className="break-words rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-[11px] text-red-300">
                  {error}
                </p>
              )}

              <div className="flex flex-col gap-2 pt-1">
                <Btn
                  variant="success"
                  onClick={() => void saveAndArm()}
                  disabled={busy || !allFilled}
                  className="w-full"
                >
                  {busy
                    ? "Arming…"
                    : allFilled
                      ? "Save & Turn ON Auto-Trading"
                      : "Fill every field to turn ON"}
                </Btn>
                <p className="text-center text-[10px] leading-relaxed text-zinc-500">
                  Orders execute on YOUR account with SL/TP attached and
                  managed by the app.
                </p>
              </div>
            </>
          )}
        </div>
      </div>
    </div>,
    document.body,
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
  const account = status?.account ?? null; // D-044: the user's OWN account
  const isAdminScope = status?.scope === "institution";

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
              ? "Engine armed. Every M1 bar close is analyzed (H1/H4 structure, M5 + M15 confirmation, ICT zones & liquidity, order flow, news windows, pattern trigger) and confirmed signals execute on your account automatically. The button turns itself OFF when your stop loss, target profit, or the day's signal count completes."
              : why?.text ?? "Turn the switch on — the money-management window opens first: today's balance, stop loss, target profit, lot size, signals today and trades at once."}
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

          {/* account health row — D-044: the user's OWN account (admins see
              the institution-terminal block from their auto-trade status) */}
          <div className="mt-3 grid grid-cols-3 gap-2">
            <Stat
              label="Account"
              value={
                isAdminScope
                  ? (status.terminal?.available ? "online" : "offline")
                  : account ? "active" : "—"
              }
              tone={
                isAdminScope
                  ? (status.terminal?.available ? "up" : "down")
                  : account ? "up" : "default"
              }
            />
            <Stat
              label="Balance"
              value={
                (isAdminScope ? status.terminal?.balance : account?.balance) != null
                  ? (isAdminScope ? status.terminal!.balance! : account!.balance!).toFixed(2)
                  : "—"
              }
              hint={(isAdminScope ? status.terminal?.currency : account?.currency) ?? undefined}
            />
            <Stat
              label="Equity"
              value={
                (isAdminScope ? status.terminal?.equity : account?.equity) != null
                  ? (isAdminScope ? status.terminal!.equity! : account!.equity!).toFixed(2)
                  : "—"
              }
              tone={
                (isAdminScope ? status.terminal : account) != null &&
                (isAdminScope ? status.terminal!.equity : account!.equity) != null &&
                (isAdminScope ? status.terminal!.balance : account!.balance) != null
                  ? (isAdminScope ? status.terminal!.equity! : account!.equity!) >=
                    (isAdminScope ? status.terminal!.balance! : account!.balance!)
                    ? "up" : "down"
                  : "default"
              }
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
          {/* D-055 — live pending book: the institution terminal's WAITING
              limit orders (admin scope) so real Exness orders are provable,
              not just the paper plane's book */}
          {isAdminScope && (status.pending_count ?? 0) > 0 && (
            <div className="mt-2 rounded-lg border border-amber-500/25 bg-amber-500/5 px-3 py-2">
              <p className="text-[10px] font-semibold text-amber-200/90">
                {status.pending_count} waiting order{status.pending_count === 1 ? "" : "s"} on the terminal
              </p>
              <div className="mt-1 flex flex-col gap-0.5">
                {(status.pending_orders ?? []).slice(0, 4).map((o) => (
                  <p key={o.ticket} className="text-[10px] leading-relaxed text-zinc-400">
                    #{o.ticket} · {o.symbol} · {o.order_type} ·{" "}
                    {o.volume != null ? `${o.volume.toFixed(2)} lots` : "—"} @{" "}
                    {o.price != null ? o.price.toFixed(2) : "—"}
                  </p>
                ))}
              </div>
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
          balance={account?.balance ?? null}
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

/* ------------------------------------- D-077 pair control room (merged) */

/** D-077 — the Auto Trade tab's content, merged INTO AI Trading (user
 * directive: one tab). Three control layers, honestly separated:
 *  1. SIGNAL ENGINES (admin, engine config): which pairs get signal
 *     engines at all (signal_symbols — PUT /api/config).
 *  2. AUTO-TRADE EXECUTION (admin, engine config): which pairs may EXECUTE
 *     orders (auto_trade_symbols — same PUT; signals still generate).
 *  3. LOT SIZE / MONEY WINDOW (every user, user settings): the per-pair
 *     lot map (symbol_lots) + shared window (PUT /api/trading/settings).
 *
 * The MASTER arm switch is the ArmCard above — exactly ONE master per tab
 * (the old Auto Trade tab carried a second, duplicate one).
 */
function LotField({
  value,
  fallback,
  onCommit,
  busy,
}: {
  value: number | undefined;
  fallback: number;
  onCommit: (v: number | undefined) => void;
  busy?: boolean;
}) {
  const [draft, setDraft] = useState<string>("");
  const [focused, setFocused] = useState(false);
  const shown = focused
    ? draft
    : value != null
      ? String(value)
      : "";
  const placeholder = focused ? String(fallback) : `auto (${fallback})`;
  return (
    <div className="flex items-center gap-2">
      <input
        type="text"
        inputMode="decimal"
        value={shown}
        placeholder={placeholder}
        disabled={busy}
        onFocus={(e) => {
          setFocused(true);
          setDraft(value != null ? String(value) : "");
          requestAnimationFrame(() => e.target.select());
        }}
        onBlur={() => {
          setFocused(false);
          const parsed = parseFloat(draft);
          if (Number.isFinite(parsed) && parsed >= 0.01 && parsed <= 100) {
            onCommit(Math.round(parsed * 100) / 100);
          } else if (draft.trim() === "") {
            onCommit(undefined); // back to the fallback
          }
        }}
        onKeyDown={(e) => {
          if (e.key === "Enter") e.currentTarget.blur();
        }}
        onChange={(e) => setDraft(e.target.value)}
        className="h-9 w-24 rounded-lg border border-white/10 bg-ink-800/80 px-2.5 text-right font-mono text-xs text-cream-100 outline-none transition-colors focus:border-gold/60 disabled:opacity-50"
      />
      <span className="text-[10px] text-cream-300/60">lots</span>
    </div>
  );
}

function MarketDot({ state }: { state: "open" | "closed" | "unknown" }) {
  const color =
    state === "open"
      ? "bg-emerald-400"
      : state === "closed"
        ? "bg-zinc-500"
        : "bg-amber-400";
  return <span className={`inline-block h-2 w-2 rounded-full ${color}`} />;
}

function PairControlRoom({
  token,
  symbols,
  autoStatus,
  isAdmin,
  tradingAccount,
  onArmChanged,
}: {
  token: string;
  symbols: string[];
  autoStatus: Mt5AutoTradeStatus | null;
  isAdmin: boolean;
  tradingAccount: TradingStatus | null;
  onArmChanged: () => void;
}) {
  const queryClient = useQueryClient();
  const [busyPair, setBusyPair] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [savedFlash, setSavedFlash] = useState<string | null>(null);
  const [settings, setSettings] = useState<UserSettings | null>(null);
  const [lotsDraft, setLotsDraft] = useState<Record<string, number>>({});

  const configQuery = useQuery({
    queryKey: ["autotrade-config"],
    enabled: !!token,
    queryFn: () => getConfig(token),
    refetchOnWindowFocus: false,
    staleTime: 15_000,
  });
  const cfg: EngineConfig | null = configQuery.data?.config ?? null;

  useEffect(() => {
    if (!token) return;
    getTradingSettings(token)
      .then((s) => {
        setSettings(s);
        setLotsDraft(s.symbol_lots ?? {});
      })
      .catch(() => undefined);
  }, [token]);

  const armed = autoStatus?.armed ?? false;
  const markets = autoStatus?.markets ?? {};

  const flash = useCallback((msg: string) => {
    setSavedFlash(msg);
    window.setTimeout(() => setSavedFlash(null), 2600);
  }, []);

  /* ---- engine-level toggles (admin): signal_symbols + auto_trade_symbols */
  const patchConfig = useCallback(
    async (patch: Partial<EngineConfig>) => {
      if (!cfg || !token) return;
      setBusyPair("cfg");
      setError(null);
      try {
        await putConfig(token, { ...cfg, ...patch });
        await queryClient.invalidateQueries({ queryKey: ["autotrade-config"] });
        onArmChanged();
        flash("Engine config saved — runtimes reconcile live");
      } catch (e) {
        setError(e instanceof Error ? e.message : "config save failed");
      } finally {
        setBusyPair(null);
      }
    },
    [cfg, token, queryClient, onArmChanged, flash],
  );

  const toggleSignal = useCallback(
    (key: string, next: boolean) => {
      if (!cfg) return;
      const cur = cfg.signal_symbols ?? [];
      const nextList = next
        ? [...new Set([...cur, key])]
        : cur.filter((s) => s !== key);
      if (nextList.length === 0) return; // never empty
      void patchConfig({ signal_symbols: nextList });
    },
    [cfg, patchConfig],
  );

  const toggleAuto = useCallback(
    (key: string, next: boolean) => {
      if (!cfg) return;
      const signals = cfg.signal_symbols ?? [];
      const cur = cfg.auto_trade_symbols ?? [];
      let nextList = next
        ? [...new Set([...cur, key])]
        : cur.filter((s) => s !== key);
      // the auto-trade set must stay a subset of the signal set
      nextList = nextList.filter((s) => signals.includes(s));
      void patchConfig({ auto_trade_symbols: nextList });
    },
    [cfg, patchConfig],
  );

  /* ---- per-pair lots (user settings) */
  const commitLots = useCallback(
    async (next: Record<string, number>) => {
      if (!token) return;
      setBusyPair("lots");
      setError(null);
      try {
        await putTradingSettings(token, { symbol_lots: next });
        setLotsDraft(next);
        flash("Per-pair lot sizes saved");
      } catch (e) {
        setError(e instanceof Error ? e.message : "lot save failed");
      } finally {
        setBusyPair(null);
      }
    },
    [token, flash],
  );

  const setLot = useCallback(
    (key: string, v: number | undefined) => {
      const next = { ...lotsDraft };
      if (v == null) delete next[key];
      else next[key] = v;
      void commitLots(next);
    },
    [lotsDraft, commitLots],
  );

  /* ---- the money window (user settings) */
  const winBusy = busyPair === "window";
  const patchWindow = useCallback(
    async (patch: Partial<UserSettings>) => {
      if (!token) return;
      setBusyPair("window");
      setError(null);
      try {
        const clean = await putTradingSettings(token, patch);
        setSettings((s) => ({ ...(s ?? ({} as UserSettings)), ...clean }));
        flash("Money window saved");
      } catch (e) {
        setError(e instanceof Error ? e.message : "save failed");
      } finally {
        setBusyPair(null);
      }
    },
    [token, flash],
  );

  const pairs = useMemo(() => {
    const offered = symbols.length ? symbols : MARKETS.map((m) => m.key);
    return MARKETS.filter((m) => offered.includes(m.key));
  }, [symbols]);

  const fixedLot = settings?.fixed_lot ?? cfg?.fixed_lot ?? 0.01;

  return (
    <Card>
      <SectionTitle
        title="Pairs & Lots"
        right={
          armed ? (
            <Badge tone="green" pulse>ARMED</Badge>
          ) : (
            <Badge tone="amber">SIGNALS ONLY</Badge>
          )
        }
      />
      <p className="text-[11px] leading-relaxed text-zinc-400">
        Every pair in one place: signal engines, auto-trade execution and
        per-pair lot sizes. The master switch above arms execution; signals
        keep generating on every enabled pair either way.
      </p>

      {error ? (
        <div className="mt-2 rounded-xl border border-red-500/30 bg-red-500/10 px-3 py-2 text-[11px] text-red-200">
          {error}
        </div>
      ) : null}
      {savedFlash ? (
        <div className="mt-2 rounded-xl border border-emerald-500/30 bg-emerald-500/10 px-3 py-2 text-[11px] text-emerald-200">
          {savedFlash}
        </div>
      ) : null}

      <div className="mt-3 grid grid-cols-1 gap-3 md:grid-cols-2">
        {pairs.map((m) => {
          const key = m.key;
          const signalOn = (cfg?.signal_symbols ?? []).includes(key);
          const autoOn = (cfg?.auto_trade_symbols ?? []).includes(key);
          const mkState = markets[key]?.open;
          const dotState =
            mkState === true
              ? "open"
              : mkState === false
                ? "closed"
                : "unknown";
          const lot = lotsDraft[key];
          const busy = busyPair === "cfg" || busyPair === "lots";
          return (
            <div
              key={key}
              className={`rounded-2xl border p-4 transition-colors ${
                autoOn && armed
                  ? "border-emerald-500/30 bg-emerald-500/5"
                  : "border-white/10 bg-ink-900/60"
              }`}
            >
              <div className="flex items-center justify-between gap-2">
                <div className="flex min-w-0 items-center gap-2">
                  <MarketDot state={dotState} />
                  <div className="min-w-0">
                    <div className="text-sm font-bold text-cream-100">
                      {key}
                    </div>
                    <div className="truncate text-[10px] text-cream-300/60">
                      {m.name}
                      {markets[key]?.detail
                        ? ` · ${markets[key].detail}`
                        : ""}
                    </div>
                  </div>
                </div>
                <span
                  className={`shrink-0 rounded-full px-2 py-0.5 text-[10px] font-bold ${
                    autoOn
                      ? "bg-emerald-500/20 text-emerald-300"
                      : "bg-zinc-700/60 text-zinc-300"
                  }`}
                >
                  {autoOn ? (armed ? "TRADING" : "ENABLED") : "OFF"}
                </span>
              </div>

              <div className="mt-3 flex flex-col gap-2.5">
                {/* signals engine (admin) */}
                <div className="flex items-center justify-between gap-2">
                  <div className="min-w-0">
                    <div className="text-[11px] font-semibold text-cream-200">
                      Signals
                    </div>
                    <div className="text-[10px] text-cream-300/50">
                      engine + chart drawings for this pair
                    </div>
                  </div>
                  <ToggleSwitch
                    on={signalOn}
                    busy={busy}
                    onToggle={(next) => {
                      if (isAdmin) toggleSignal(key, next);
                    }}
                  />
                </div>

                {/* auto-trade execution (admin) */}
                <div className="flex items-center justify-between gap-2">
                  <div className="min-w-0">
                    <div className="text-[11px] font-semibold text-cream-200">
                      Auto trade
                    </div>
                    <div className="text-[10px] text-cream-300/50">
                      execute orders on this pair
                    </div>
                  </div>
                  <ToggleSwitch
                    on={autoOn}
                    busy={busy}
                    tone="gold"
                    onToggle={(next) => {
                      if (isAdmin) toggleAuto(key, next);
                    }}
                  />
                </div>

                {/* per-pair lot (user) */}
                <div className="flex items-center justify-between gap-2 border-t border-white/5 pt-2.5">
                  <div className="min-w-0">
                    <div className="text-[11px] font-semibold text-cream-200">
                      Lot size
                    </div>
                    <div className="text-[10px] text-cream-300/50">
                      empty = window default ({fixedLot})
                    </div>
                  </div>
                  <LotField
                    value={lot}
                    fallback={fixedLot}
                    busy={busyPair === "lots"}
                    onCommit={(v) => setLot(key, v)}
                  />
                </div>
              </div>

              {!isAdmin ? (
                <p className="mt-2.5 text-[10px] text-cream-300/40">
                  Signal/auto-trade switches are admin-controlled.
                </p>
              ) : null}
            </div>
          );
        })}
      </div>

      {/* ---------------------------------------------- money window */}
      <div className="mt-3 rounded-2xl border border-white/10 bg-ink-900/60 p-4">
        <h3 className="text-sm font-bold text-cream-100">
          Money window{" "}
          <span className="text-[10px] font-normal text-cream-300/50">
            shared by every pair (per-pair lots above override the default)
          </span>
        </h3>
        <div className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
          {(
            [
              ["day_start_balance", "Today's balance", "USD"],
              ["daily_loss_usd", "Daily stop loss", "USD"],
              ["daily_profit_usd", "Daily target", "USD"],
              ["max_trades_per_day", "Signals / day", ""],
              ["max_positions", "Open trades", ""],
            ] as const
          ).map(([k, label, unit]) => (
            <label key={k} className="flex min-w-0 flex-col gap-1">
              <span className="text-[10px] font-semibold text-cream-300/70">
                {label}
                {unit ? ` (${unit})` : ""}
              </span>
              <input
                type="text"
                inputMode="decimal"
                disabled={winBusy}
                defaultValue={
                  settings?.[k] != null ? String(settings[k]) : ""
                }
                key={`${k}-${settings?.[k] ?? ""}`}
                onBlur={(e) => {
                  const parsed = parseFloat(e.target.value);
                  if (!Number.isFinite(parsed)) return;
                  void patchWindow({
                    [k]:
                      k === "max_trades_per_day" || k === "max_positions"
                        ? Math.round(parsed)
                        : Math.round(parsed * 100) / 100,
                  } as Partial<UserSettings>);
                }}
                className="h-9 rounded-lg border border-white/10 bg-ink-800/80 px-2.5 text-xs text-cream-100 outline-none transition-colors focus:border-gold/60 disabled:opacity-50"
              />
            </label>
          ))}
        </div>
        <div className="mt-3 flex items-center justify-between gap-3">
          <p className="text-[10px] text-cream-300/50">
            Practice balance:{" "}
            <span className="font-mono text-cream-200">
              {tradingAccount?.account?.balance?.toFixed(2) ?? "—"}{" "}
              {tradingAccount?.account?.currency ?? "USD"}
            </span>
          </p>
          <label className="flex items-center gap-2 text-[11px] text-cream-200">
            risk mode
            <select
              value={settings?.risk_mode ?? "fixed"}
              disabled={winBusy}
              onChange={(e) =>
                void patchWindow({
                  risk_mode: e.target.value as "fixed" | "percent",
                })
              }
              className="h-8 rounded-lg border border-white/10 bg-ink-800/80 px-2 text-xs text-cream-100 outline-none focus:border-gold/60"
            >
              <option value="fixed">fixed lot</option>
              <option value="percent">% risk</option>
            </select>
          </label>
        </div>
      </div>
    </Card>
  );
}

/* -------------------------------------------- D-044 market intelligence */

function fmtUsd(v: number | undefined | null): string {
  if (v == null || !isFinite(v)) return "—";
  if (v >= 1e9) return `$${(v / 1e9).toFixed(1)}B`;
  if (v >= 1e6) return `$${(v / 1e6).toFixed(1)}M`;
  if (v >= 1e3) return `$${(v / 1e3).toFixed(0)}K`;
  return `$${v.toFixed(0)}`;
}

function MarketIntelligenceCard({ token, symbol }: { token: string; symbol: string }) {
  const [snap, setSnap] = useState<AnalysisResponse | null>(null);
  // D-052 — collapsed by default: the summary row stays, the detail
  // blocks (POI zones, TPO, news, CFTC) open on tap — a decluttered
  // mobile layout (user directive: গুছানো সহজ ডিজাইন)
  const [open, setOpen] = useState(false);

  const reload = useCallback(() => {
    getAnalysis(token, symbol)
      .then(setSnap)
      .catch(() => undefined);
  }, [token, symbol]);

  useEffect(() => {
    reload();
    const t = window.setInterval(reload, 20_000); // realtime refresh
    return () => window.clearInterval(t);
  }, [reload]);

  const flow = snap?.flow;
  const news = snap?.news;
  const cot = snap?.cot;
  const tpo = snap?.tpo;
  const poi = snap?.poi;

  return (
    <Card>
      <SectionTitle
        title="Market Intelligence"
        right={
          <button
            type="button"
            onClick={() => setOpen((v) => !v)}
            className="rounded-full border border-zinc-700 bg-zinc-800/60 px-3 py-1 text-[10px] font-bold text-zinc-400 hover:text-zinc-200"
          >
            {open ? "Hide details ▲" : "Details ▼"}
          </button>
        }
      />
      {/* order flow — always visible summary */}
      <div className="grid grid-cols-3 gap-2">
        <Stat label="Flow 24h" value={fmtUsd(flow?.usd_24h)} hint="est. traded value" />
        <Stat label="Flow 1h" value={fmtUsd(flow?.usd_1h)} hint="est. traded value" />
        <Stat
          label="Buy pressure 1h"
          value={flow?.buy_pct_1h != null ? `${flow.buy_pct_1h.toFixed(0)}%` : "—"}
          tone={
            flow?.buy_pct_1h != null
              ? flow.buy_pct_1h >= 55 ? "up" : flow.buy_pct_1h <= 45 ? "down" : "default"
              : "default"
          }
        />
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-2 text-[10px]">
        {flow?.velocity != null && (
          <Badge tone={flow.velocity >= 1.5 ? "green" : flow.velocity <= 0.5 ? "amber" : "zinc"}>
            tape speed {flow.velocity.toFixed(2)}×
          </Badge>
        )}
        {flow?.delta_1h != null && (
          <Badge tone={flow.delta_1h >= 0 ? "green" : "red"}>
            delta 1h {flow.delta_1h >= 0 ? "+" : ""}{flow.delta_1h.toFixed(0)}
          </Badge>
        )}
        {flow?.bias && (
          <Badge tone={flow.bias === "buy" ? "green" : flow.bias === "sell" ? "red" : "zinc"}>
            whale flow {flow.bias}
          </Badge>
        )}
        {news?.blackout_now && <Badge tone="red">NEWS WINDOW</Badge>}
      </div>

      {!open ? null : (
        <>
      {/* institutional entry zones (whale) */}
      {flow?.whale_zones && flow.whale_zones.length > 0 && (
        <div className="mt-3 min-w-0">
          <p className="mb-1.5 text-[10px] font-semibold uppercase tracking-wider text-zinc-500">
            Institutional activity zones
          </p>
          <ul className="flex min-w-0 flex-col gap-1.5">
            {flow.whale_zones.slice(0, 3).map((z, i) => (
              <li
                key={i}
                className="flex min-w-0 items-center gap-2 rounded-lg border border-zinc-800/70 bg-zinc-900/40 px-3 py-2"
              >
                <Badge tone={z.side === "buy" ? "green" : z.side === "sell" ? "red" : "zinc"}>
                  {z.kind}
                </Badge>
                <span className="min-w-0 flex-1 truncate font-mono text-[11px] tabular-nums text-zinc-300">
                  {z.lo.toFixed(2)} – {z.hi.toFixed(2)}
                </span>
                <span className="shrink-0 text-[9px] text-zinc-500">
                  vol z{z.vol_z.toFixed(1)} · {fmtTime(z.t)}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* D-048 — unified POI zones: where the engine watches for retests */}
      {poi?.zones && poi.zones.length > 0 && (
        <div className="mt-3 min-w-0">
          <p className="mb-1.5 text-[10px] font-semibold uppercase tracking-wider text-zinc-500">
            POI zones — engine watchlist
          </p>
          <ul className="flex min-w-0 flex-col gap-1.5">
            {poi.zones.slice(0, 5).map((z, i) => (
              <li
                key={i}
                className="flex min-w-0 items-center gap-2 rounded-lg border border-zinc-800/70 bg-zinc-900/40 px-3 py-2"
              >
                <Badge tone={z.side === "demand" ? "green" : "red"}>
                  {z.side === "demand" ? "DEMAND" : "SUPPLY"}
                </Badge>
                <span className="min-w-0 flex-1 font-mono text-[11px] tabular-nums text-zinc-300">
                  {z.lo.toFixed(2)}–{z.hi.toFixed(2)}
                  <span className="ml-1.5 text-[9px] font-semibold uppercase text-zinc-500">
                    {z.source}
                    {z.htf ? " ·HTF" : ""}
                  </span>
                </span>
                <span className="flex shrink-0 items-center gap-1.5">
                  <span className="h-1.5 w-10 overflow-hidden rounded-full bg-zinc-800">
                    <span
                      className={`block h-full rounded-full ${
                        z.quality >= 0.7
                          ? "bg-amber-400"
                          : z.quality >= 0.45
                            ? "bg-emerald-400"
                            : "bg-zinc-500"
                      }`}
                      style={{ width: `${Math.round(z.quality * 100)}%` }}
                    />
                  </span>
                  <span className="w-7 text-right font-mono text-[9px] tabular-nums text-zinc-500">
                    {z.quality.toFixed(2)}
                  </span>
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* D-047 — time-at-price levels: where the market SPENT TIME */}
      {tpo?.levels && tpo.levels.length > 0 && (
        <div className="mt-3 min-w-0">
          <p className="mb-1.5 text-[10px] font-semibold uppercase tracking-wider text-zinc-500">
            Time-at-price levels (24h)
          </p>
          <ul className="flex min-w-0 flex-col gap-1.5">
            {tpo.levels.slice(0, 4).map((lv, i) => (
              <li
                key={i}
                className="flex min-w-0 items-center gap-2 rounded-lg border border-zinc-800/70 bg-zinc-900/40 px-3 py-2"
              >
                <Badge tone={lv.side === "support" ? "green" : "red"}>
                  {lv.side === "support" ? "S" : "R"}
                </Badge>
                <span className="min-w-0 flex-1 font-mono text-[11px] tabular-nums text-zinc-300">
                  {lv.price.toFixed(2)}
                  {lv.price === tpo.poc && (
                    <span className="ml-1.5 text-[9px] font-bold text-amber-400">POC</span>
                  )}
                </span>
                <span className="shrink-0 text-[9px] text-zinc-500">
                  held {Math.round(lv.minutes)}m
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* economic events */}
      <div className="mt-3 min-w-0">
        <p className="mb-1.5 text-[10px] font-semibold uppercase tracking-wider text-zinc-500">
          High-impact economic events
        </p>
        {news?.events?.length ? (
          <ul className="flex max-h-44 min-w-0 flex-col gap-1.5 overflow-y-auto pr-1">
            {news.events.slice(0, 6).map((e, i) => {
              const mins = (new Date(e.time).getTime() - Date.now()) / 60000;
              const when =
                mins > 0
                  ? mins < 60 ? `in ${Math.round(mins)}m` : `in ${(mins / 60).toFixed(1)}h`
                  : "passed";
              return (
                <li
                  key={i}
                  className="flex min-w-0 items-center gap-2 rounded-lg border border-zinc-800/70 bg-zinc-900/40 px-3 py-2"
                >
                  <Badge tone={e.impact === "high" ? "red" : "amber"}>{e.impact}</Badge>
                  <span className="min-w-0 flex-1 truncate text-[11px] text-zinc-300">{e.title}</span>
                  <span className="shrink-0 text-[9px] text-zinc-500">{when}</span>
                </li>
              );
            })}
          </ul>
        ) : (
          <p className="text-[11px] text-zinc-500">
            {news?.available === false
              ? "Calendar temporarily unavailable."
              : "No high-impact USD events in the next 48 hours."}
          </p>
        )}
      </div>

      {/* CFTC positioning */}
      <div className="mt-3 min-w-0">
        <p className="mb-1.5 text-[10px] font-semibold uppercase tracking-wider text-zinc-500">
          Institutional positioning (CFTC weekly)
        </p>
        {cot?.large_speculators ? (
          <>
            <div className="grid grid-cols-3 gap-2">
              <Stat
                label="Large specs net"
                value={`${(cot.large_speculators.net / 1000).toFixed(0)}K`}
                tone={cot.large_speculators.net >= 0 ? "up" : "down"}
              />
              <Stat
                label="Weekly change"
                value={`${cot.large_speculators.net_change >= 0 ? "+" : ""}${(cot.large_speculators.net_change / 1000).toFixed(1)}K`}
                tone={cot.large_speculators.net_change >= 0 ? "up" : "down"}
              />
              <Stat
                label="52w percentile"
                value={cot.net_percentile_52w != null ? `${cot.net_percentile_52w.toFixed(0)}%` : "—"}
                hint={cot.report_date}
              />
            </div>
            {cot.note && (
              <p className="mt-1.5 text-[10px] leading-relaxed text-zinc-500">{cot.note}</p>
            )}
          </>
        ) : (
          <p className="text-[11px] text-zinc-500">Positioning report unavailable.</p>
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
                (ev.event === "order" || ev.event === "log") && ev.ok !== false
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
  const [result, setResult] = useState<OrderResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  const place = async (side: "BUY" | "SELL") => {
    setBusy(side === "BUY" ? "buy" : "sell");
    setError(null);
    setResult(null);
    try {
      const res = await postTradingOrder(token, {
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
      <SectionTitle title="Manual Order" right={<Badge tone="zinc">your account</Badge>} />
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
        <Btn variant="success" onClick={() => void place("BUY")} disabled={busy != null || parseFloat(volume) <= 0}>
          {busy === "buy" ? "Sending…" : "BUY"}
        </Btn>
        <Btn variant="danger" onClick={() => void place("SELL")} disabled={busy != null || parseFloat(volume) <= 0}>
          {busy === "sell" ? "Sending…" : "SELL"}
        </Btn>
      </div>
      {result && (
        <p className={`mt-2.5 break-words rounded-lg border px-3 py-2 text-[11px] ${result.ok ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-300" : "border-red-500/30 bg-red-500/10 text-red-300"}`}>
          {result.ok ? "Order filled" : "Order rejected"}{result.retcode != null && ` · ${result.retcode}`}
          {result.price != null && ` @ ${result.price}`}
          {result.comment ? ` — ${result.comment}` : ""}
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
  const [positions, setPositions] = useState<TradingPosition[]>([]);
  const [pending, setPending] = useState<TradingPendingOrder[]>([]);
  const [loading, setLoading] = useState(true);
  const [closing, setClosing] = useState<number | null>(null);

  const reload = useCallback(() => {
    getTradingPositions(token)
      .then((r) => {
        setPositions(r.positions ?? []);
        // D-054 — waiting limit orders now visible (they used to sit in
        // the pending book invisible, looking like "order never created")
        setPending(r.pending ?? []);
      })
      .catch(() => {
        setPositions([]);
        setPending([]);
      })
      .finally(() => setLoading(false));
  }, [token]);

  useEffect(() => {
    reload();
    const t = window.setInterval(reload, 10_000);
    return () => window.clearInterval(t);
  }, [reload, refreshKey]);

  const close = async (ticket: number) => {
    setClosing(ticket);
    try {
      await postTradingClose(token, ticket);
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
        right={
          <span className="flex items-center gap-1.5">
            {pending.length > 0 && (
              <Badge tone="amber">{pending.length} pending</Badge>
            )}
            <Badge tone="zinc">{positions.length}</Badge>
          </span>
        }
      />
      {loading ? (
        <div className="flex flex-col gap-2">
          <div className="h-10 animate-pulse rounded-lg bg-zinc-800/60" />
          <div className="h-10 animate-pulse rounded-lg bg-zinc-800/60" />
        </div>
      ) : positions.length === 0 && pending.length === 0 ? (
        <EmptyState title="No open positions" hint="AI and manual orders appear here while open." />
      ) : (
        <ul className="flex min-w-0 flex-col gap-1.5">
          {/* D-054 — waiting pending limit entries (created, sitting at
          their fill price until the market retraces to them) */}
          {pending.map((p) => (
            <li
              key={`p-${p.ticket}`}
              className="flex min-w-0 items-center gap-2.5 rounded-xl border border-amber-500/20 bg-amber-500/5 px-3 py-2.5"
            >
              <Badge tone="amber">{p.order_type.replace("_", " ").toUpperCase()}</Badge>
              <span className="min-w-0 flex-1">
                <span className="block truncate text-xs font-semibold text-amber-100/90">
                  {p.symbol} · {p.volume} lots @ {p.price != null ? p.price.toFixed(2) : "—"}
                </span>
                <span className="block text-[10px] text-amber-200/50">
                  #{p.ticket} · waiting for fill price
                  {p.sl != null && ` · SL ${p.sl.toFixed(2)}`}
                  {p.tp != null && ` · TP ${p.tp.toFixed(2)}`}
                </span>
              </span>
              <span className="shrink-0 text-[10px] font-semibold uppercase tracking-wider text-amber-300/70">
                pending
              </span>
            </li>
          ))}
          {positions.map((p) => (
            <li
              key={p.ticket}
              className="flex min-w-0 items-center gap-2.5 rounded-xl border border-zinc-800/70 bg-zinc-900/40 px-3 py-2.5"
            >
              <Badge tone={p.side.toLowerCase().includes("buy") ? "green" : "red"}>
                {p.side}
              </Badge>
              <span className="min-w-0 flex-1">
                <span className="block truncate text-xs font-semibold text-zinc-200">
                  {p.symbol} · {p.volume} lots @ {p.price_open.toFixed(2)}
                </span>
                <span className="block text-[10px] text-zinc-500">
                  #{p.ticket} · {fmtTime(p.time)}
                  {p.sl != null && ` · SL ${p.sl.toFixed(2)}`}
                  {p.tp != null && ` · TP ${p.tp.toFixed(2)}`}
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
                disabled={closing === p.ticket}
                onClick={() => void close(p.ticket)}
                className="shrink-0 !px-2.5 !py-1"
              >
                {closing === p.ticket ? "…" : "Close"}
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
  const [rows, setRows] = useState<TradeRecord[] | null>(null);

  useEffect(() => {
    getTradingTrades(token, 100)
      .then((r) => setRows(r.trades ?? []))
      .catch(() => setRows([]));
  }, [token, refreshKey]);

  const closed = useMemo(
    () => (rows ?? []).filter((r) => r.closed_at != null).slice(0, 50),
    [rows],
  );
  const totalProfit = useMemo(
    () => closed.reduce((acc, r) => acc + (r.profit ?? 0), 0),
    [closed],
  );

  return (
    <Card>
      <SectionTitle
        title="Trade History"
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
      ) : closed.length === 0 ? (
        <EmptyState title="No closed trades yet" hint="Closed AI and manual trades show here." />
      ) : (
        <ul className="flex max-h-80 min-w-0 flex-col gap-1.5 overflow-y-auto pr-1">
          {closed.map((r, i) => (
            <li
              key={`${r.ticket}-${i}`}
              className="flex min-w-0 items-center gap-2.5 rounded-xl border border-zinc-800/70 bg-zinc-900/40 px-3 py-2"
            >
              <Badge tone={r.side.toLowerCase().includes("buy") ? "green" : "red"}>
                {r.side}
              </Badge>
              <span className="min-w-0 flex-1">
                <span className="block truncate text-xs font-semibold text-zinc-200">
                  {r.volume} lots @ {r.price_open.toFixed(2)}
                  {r.price_close != null && ` → ${r.price_close.toFixed(2)}`}
                </span>
                <span className="block text-[10px] text-zinc-500">
                  {r.closed_at ? fmtTime(r.closed_at) : ""}
                  {r.signal_id ? " · AI signal" : " · manual"}
                </span>
              </span>
              <span
                className={`shrink-0 font-mono text-xs font-bold tabular-nums ${
                  (r.profit ?? 0) >= 0 ? "text-emerald-400" : "text-red-400"
                }`}
              >
                {(r.profit ?? 0) >= 0 ? "+" : ""}{(r.profit ?? 0).toFixed(2)}
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
  symbols,
  isAdmin,
  autoStatus,
  autoEvents,
  tradingAccount,
  refreshKey,
  signals,
  onArmChanged,
}: Props) {
  const [tick, setTick] = useState(0);
  const bump = useCallback(() => setTick((t) => t + 1), []);
  const orderSymbols = useMemo(() => {
    // D-077 — the manual-order pad uses the live symbol list (backend
    // symbols UNION every market) with the active pair first
    const s = [symbol, ...symbols, ...MARKET_KEYS];
    return [...new Set(s.filter(Boolean))];
  }, [symbol, symbols]);
  const activeSignals = signals.filter((s) => s.status === "active");

  return (
    <div className="flex min-w-0 flex-col gap-3">
      <ArmCard status={autoStatus} token={token} onArmChanged={onArmChanged} />

      {/* D-077 — the merged Auto Trade control room: per-pair signal /
       * execute switches + per-pair lots + the shared money window, in
       * sequence under the ONE master arm card above. */}
      <PairControlRoom
        token={token}
        symbols={symbols}
        autoStatus={autoStatus}
        isAdmin={isAdmin}
        tradingAccount={tradingAccount}
        onArmChanged={onArmChanged}
      />

      {/* D-051 — live per-strategy state, every M1 close (all markets) */}
      <StrategyRadar symbols={orderSymbols} />

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

      {/* D-052 — ordered mobile-app flow: what the AI does now, then your
          account (positions + history), then manual tools, market context
          last (collapsible) */}
      <EventFeed events={autoEvents} />
      <PositionsCard token={token} refreshKey={refreshKey + tick} onChanged={bump} />
      <ManualTradeCard token={token} symbols={orderSymbols} refresh={bump} />
      <HistoryCard token={token} refreshKey={refreshKey + tick} />
      <MarketIntelligenceCard token={token} symbol={symbol} />
    </div>
  );
}
