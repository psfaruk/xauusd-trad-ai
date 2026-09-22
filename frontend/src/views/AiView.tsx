/**
 * AiView (D-041/D-042/D-044) — the AI Trading tab.
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
import {
  getAnalysis,
  getTradingPositions,
  getTradingSettings,
  getTradingTrades,
  postMt5AutoTrade,
  postTradingClose,
  postTradingOrder,
  putTradingSettings,
} from "../lib/api";
import type {
  AnalysisResponse, Mt5AutoTradeStatus, OrderResult, Signal, TradeRecord,
  TradingPosition, UserSettings, WsMt5AutoMsg,
} from "../types";
import {
  Badge, Btn, Card, EmptyState, Field, SectionTitle, Stat, inputCls,
} from "../components/ui";
import { fmtTime } from "../components/SignalDetail";

interface Props {
  token: string;
  symbol: string;
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
      className={`relative h-8 w-14 shrink-0 rounded-full border transition-colors disabled:opacity-50 ${
        on ? "border-emerald-500/50 bg-emerald-500/25" : "border-zinc-700 bg-zinc-800"
      }`}
    >
      <span
        className={`absolute top-1/2 h-7 w-7 -translate-y-1/2 rounded-full shadow-lg transition-all ${
          on ? "left-[calc(100%-1.75rem)] bg-emerald-400" : "left-0.5 bg-zinc-500"
        }`}
      />
      <span className={`absolute inset-y-0 flex items-center text-[9px] font-bold tracking-wide ${
        on ? "left-2.5 text-emerald-200" : "right-2.5 text-zinc-400"
      }`}>
        {on ? labelOn : labelOff}
      </span>
    </button>
  );
}

/* ------------------------------------------- D-044 money management modal */

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
      .then(setForm)
      .catch((e) => setError(e instanceof Error ? e.message : "failed to load"));
  }, [token]);

  const set = (k: keyof UserSettings, v: string) =>
    setForm((f) => (f ? { ...f, [k]: v } : f));

  const saveAndArm = async () => {
    if (!form) return;
    setBusy(true);
    setError(null);
    try {
      await putTradingSettings(token, {
        risk_mode: form.risk_mode,
        risk_percent: parseFloat(String(form.risk_percent)) || 0.5,
        fixed_lot: parseFloat(String(form.fixed_lot)) || 0.01,
        max_positions: parseInt(String(form.max_positions), 10) || 3,
        daily_max_loss_pct: parseFloat(String(form.daily_max_loss_pct)) || 3,
        max_trades_per_day: parseInt(String(form.max_trades_per_day), 10) || 6,
        rr: parseFloat(String(form.rr)) || 1.6,
        min_sl_atr: parseFloat(String(form.min_sl_atr)) || 1.5,
        max_spread_points: parseInt(String(form.max_spread_points), 10) || 35,
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
                      ? `≈ $${((balance * (parseFloat(String(form.risk_percent)) || 0)) / 100).toFixed(2)} per trade at $${balance.toFixed(0)} balance`
                      : "0.5% keeps a losing streak survivable"
                  }
                >
                  <input
                    className={inputCls}
                    value={String(form.risk_percent)}
                    onChange={(e) => set("risk_percent", e.target.value)}
                    inputMode="decimal"
                  />
                </Field>
              ) : (
                <Field label="Lot size" hint="Fixed volume for every AI order">
                  <input
                    className={inputCls}
                    value={String(form.fixed_lot)}
                    onChange={(e) => set("fixed_lot", e.target.value)}
                    inputMode="decimal"
                  />
                </Field>
              )}

              <div className="grid grid-cols-2 gap-2.5">
                <Field label="Max open trades" hint="Multiple entries can run together">
                  <input
                    className={inputCls}
                    value={String(form.max_positions)}
                    onChange={(e) => set("max_positions", e.target.value)}
                    inputMode="numeric"
                  />
                </Field>
                <Field label="Trades per day" hint="Auto orders allowed per UTC day">
                  <input
                    className={inputCls}
                    value={String(form.max_trades_per_day)}
                    onChange={(e) => set("max_trades_per_day", e.target.value)}
                    inputMode="numeric"
                  />
                </Field>
                <Field label="Daily loss limit (%)">
                  <input
                    className={inputCls}
                    value={String(form.daily_max_loss_pct)}
                    onChange={(e) => set("daily_max_loss_pct", e.target.value)}
                    inputMode="decimal"
                  />
                </Field>
                <Field
                  label="Fallback R:R"
                  hint="TP multiple when no zone/liquidity target is near — targets are otherwise predicted from market structure"
                >
                  <input
                    className={inputCls}
                    value={String(form.rr)}
                    onChange={(e) => set("rr", e.target.value)}
                    inputMode="decimal"
                  />
                </Field>
                <Field label="Min stop (ATR×)" hint="Wider stop = less spread noise">
                  <input
                    className={inputCls}
                    value={String(form.min_sl_atr)}
                    onChange={(e) => set("min_sl_atr", e.target.value)}
                    inputMode="decimal"
                  />
                </Field>
              </div>

              <Field label="Max spread (points)" hint="AI skips signals when the spread is wider">
                <input
                  className={inputCls}
                  value={String(form.max_spread_points)}
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
              ? "Engine armed. Every M1 bar close is analyzed (H1/H4 structure, M5 + M15 confirmation, ICT zones & liquidity, order flow, news windows, pattern trigger) and confirmed signals execute on your account automatically."
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
          <Badge tone={news?.blackout_now ? "red" : "zinc"}>
            {news?.blackout_now ? "NEWS WINDOW" : "live"}
          </Badge>
        }
      />
      {/* order flow */}
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
      </div>

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
  const [loading, setLoading] = useState(true);
  const [closing, setClosing] = useState<number | null>(null);

  const reload = useCallback(() => {
    getTradingPositions(token)
      .then((r) => setPositions(r.positions ?? []))
      .catch(() => setPositions([]))
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
      <MarketIntelligenceCard token={token} symbol={symbol} />
      <ManualTradeCard token={token} symbols={symbols} refresh={bump} />
      <PositionsCard token={token} refreshKey={refreshKey + tick} onChanged={bump} />
      <HistoryCard token={token} refreshKey={refreshKey + tick} />
    </div>
  );
}
