/**
 * D-076 — the AUTO TRADING tab (user directives, Bengali, verbatim):
 *  - "অটো ট্রেডিং ট্যাব অপশন থাকবে, চাইলে অটো ট্রেড বন্ধ করে রাখতে পারবে
 *    ইউজার। সব গুলো পেয়ার এ এই সিস্টেম থাকবে। আগের পেয়ার গুলো যেই রকম
 *    আছে।" — an Auto Trading tab; the user can turn auto-trade off; every
 *    pair has this system, existing pairs unchanged.
 *  - "ইউজার চাইলে প্রত্যেক পেয়ার এর জন্য আলাদা করে লট সাইজ ও অন্যান্য
 *    বিষয়গুলো সেটাপ করতে পারবে।" — per-pair lot size + the money window.
 *
 * Three control layers, honestly separated:
 *  1. SIGNAL ENGINES (admin, engine config): which pairs get signal
 *     engines at all (signal_symbols — PUT /api/config).
 *  2. AUTO-TRADE EXECUTION (admin, engine config): which pairs may EXECUTE
 *     orders (auto_trade_symbols — same PUT; signals still generate).
 *  3. LOT SIZE / MONEY WINDOW (every user, user settings): the per-pair
 *     lot map (symbol_lots) + shared window (PUT /api/trading/settings).
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";

import { MARKETS } from "../lib/markets";
import {
  getConfig,
  getTradingSettings,
  postMt5AutoTrade,
  putConfig,
  putTradingSettings,
} from "../lib/api";
import type {
  EngineConfig,
  Mt5AutoTradeStatus,
  TradingStatus,
  UserSettings,
} from "../types";

/* ------------------------------------------------------------------ atoms */

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

/* -------------------------------------------------------------- the view */

export default function AutoTradeView({
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
  const why = autoStatus?.why;
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

  /* ---- master arm/disarm (admin) */
  const [armBusy, setArmBusy] = useState(false);
  const toggleArmed = useCallback(
    async (next: boolean) => {
      if (!token) return;
      setArmBusy(true);
      setError(null);
      try {
        await postMt5AutoTrade(token, {
          enabled: next,
          confirm: next ? "ENABLE" : undefined,
        });
        onArmChanged();
        flash(next ? "Auto-trade ARMED" : "Auto-trade OFF");
      } catch (e) {
        setError(e instanceof Error ? e.message : "arm failed");
      } finally {
        setArmBusy(false);
      }
    },
    [token, onArmChanged, flash],
  );

  const pairs = useMemo(() => {
    const offered = symbols.length ? symbols : MARKETS.map((m) => m.key);
    return MARKETS.filter((m) => offered.includes(m.key));
  }, [symbols]);

  const fixedLot = settings?.fixed_lot ?? cfg?.fixed_lot ?? 0.01;

  return (
    <div className="flex flex-col gap-3">
      {/* ---------------------------------------------- master arm card */}
      <div className="rounded-2xl border border-white/10 bg-ink-900/60 p-4">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <h2 className="flex items-center gap-2 text-sm font-bold text-cream-100">
              Auto Trading
              <span
                className={`rounded-full px-2 py-0.5 text-[10px] font-bold ${
                  armed
                    ? "bg-emerald-500/20 text-emerald-300"
                    : "bg-zinc-700/60 text-zinc-300"
                }`}
              >
                {armed ? "ARMED" : "OFF"}
              </span>
            </h2>
            <p className="mt-1 text-[11px] leading-relaxed text-cream-300/70">
              {armed
                ? "Every confirmed AI signal places a real order on the enabled pairs below."
                : "Signals still generate on every pair — order execution is switched off."}
            </p>
            {why ? (
              <p className="mt-1.5 text-[11px] text-cream-300/50">
                <span className="font-semibold text-cream-300/80">
                  {why.code}:
                </span>{" "}
                {why.text}
              </p>
            ) : null}
          </div>
          <ToggleSwitch
            on={armed}
            busy={armBusy}
            tone="gold"
            onToggle={(next) => {
              if (isAdmin) void toggleArmed(next);
            }}
          />
        </div>
        {!isAdmin ? (
          <p className="mt-2 text-[10px] text-cream-300/40">
            The master switch is admin-controlled; your own practice-plane
            auto-trading lives in the AI Trading tab.
          </p>
        ) : null}
      </div>

      {error ? (
        <div className="rounded-xl border border-red-500/30 bg-red-500/10 px-3 py-2 text-[11px] text-red-200">
          {error}
        </div>
      ) : null}
      {savedFlash ? (
        <div className="rounded-xl border border-emerald-500/30 bg-emerald-500/10 px-3 py-2 text-[11px] text-emerald-200">
          {savedFlash}
        </div>
      ) : null}

      {/* ---------------------------------------------- per-pair cards */}
      <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
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
      <div className="rounded-2xl border border-white/10 bg-ink-900/60 p-4">
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
    </div>
  );
}
