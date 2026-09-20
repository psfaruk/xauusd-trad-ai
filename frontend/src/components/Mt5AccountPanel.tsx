import { useCallback, useEffect, useState } from "react";
import {
  getMt5Account, getMt5AutoTrade, getMt5History, getMt5Positions, getMt5Symbols,
  postMt5AutoTrade, postMt5Close, postMt5Order,
} from "../lib/api";
import type {
  Mt5Account, Mt5AutoTradeStatus, Mt5HistoryPosition, Mt5OpenPosition, Mt5Symbol,
  WsMt5AutoMsg,
} from "../types";

interface Mt5AccountPanelProps {
  open: boolean;
  onClose: () => void;
  token: string;
  /** D-035: instrument selected on the dashboard (XAUUSD | BTCUSD). */
  defaultSymbol?: string;
  /** D-036: live AI auto-execution events (WS mt5_auto feed). */
  autoEvents?: WsMt5AutoMsg[];
  /** D-036: only admins may arm/disarm the REAL auto-executor. */
  isAdmin?: boolean;
}

type Tab = "positions" | "history" | "order" | "auto";

/**
 * Mt5AccountPanel — the REAL Exness trading account, live through the
 * MetaTrader 5 terminal bridge (D-034). Balance/equity, open positions,
 * detailed trade history and manual market orders — all executed by the
 * genuine MT5 terminal, never simulated.
 *
 * D-036 "AI AUTO" tab: AI signal -> auto-order on the REAL account —
 * arm/disarm (typed confirm), risk summary and the live execution feed.
 */
export default function Mt5AccountPanel({ open, onClose, token, defaultSymbol, autoEvents = [], isAdmin = false }: Mt5AccountPanelProps) {
  const [tab, setTab] = useState<Tab>("positions");
  const [account, setAccount] = useState<Mt5Account | null>(null);
  const [positions, setPositions] = useState<Mt5OpenPosition[] | null>(null);
  const [history, setHistory] = useState<Mt5HistoryPosition[] | null>(null);
  const [symbols, setSymbols] = useState<Mt5Symbol[]>([]);
  const [symbol, setSymbol] = useState(defaultSymbol ?? "XAUUSDm");
  const [volume, setVolume] = useState("0.10");
  const [sl, setSl] = useState("");
  const [tp, setTp] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  // D-036 — AI AUTO tab state
  const [auto, setAuto] = useState<Mt5AutoTradeStatus | null>(null);
  const [confirmText, setConfirmText] = useState("");
  const [autoBusy, setAutoBusy] = useState(false);

  const refreshAuto = useCallback(async () => {
    try {
      setAuto(await getMt5AutoTrade(token));
    } catch {
      setAuto(null);
    }
  }, [token]);

  const refresh = useCallback(async () => {
    try {
      const [acct, pos] = await Promise.all([
        getMt5Account(token),
        getMt5Positions(token),
      ]);
      setAccount(acct);
      setPositions(pos.positions);
    } catch {
      setAccount(null);
      setPositions([]);
    }
  }, [token]);

  useEffect(() => {
    if (!open) return;
    void refresh();
    const id = setInterval(() => void refresh(), 5000);
    return () => clearInterval(id);
  }, [open, refresh]);

  useEffect(() => {
    if (!open) return;
    getMt5Symbols(token)
      .then((r) => setSymbols(r.symbols))
      .catch(() => setSymbols([]));
    getMt5History(token, 90)
      .then((r) => setHistory(r.positions))
      .catch(() => setHistory([]));
    void refreshAuto();
  }, [open, token, refreshAuto]);

  useEffect(() => {
    if (!open || tab !== "auto") return;
    const id = setInterval(() => void refreshAuto(), 5000);
    return () => clearInterval(id);
  }, [open, tab, refreshAuto]);

  if (!open) return null;

  const vol = parseFloat(volume);

  const order = async (side: "buy" | "sell") => {
    if (!Number.isFinite(vol) || vol <= 0) {
      setError("volume must be a positive number");
      return;
    }
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const res = await postMt5Order(token, {
        symbol,
        side,
        volume: vol,
        sl: sl ? parseFloat(sl) : undefined,
        tp: tp ? parseFloat(tp) : undefined,
      });
      if (res.ok) {
        setNotice(
          `${side.toUpperCase()} ${res.volume} ${res.symbol} filled @ ${res.price} (deal ${res.deal})`
        );
        setTab("positions");
        await refresh();
        getMt5History(token, 90).then((r) => setHistory(r.positions)).catch(() => {});
      } else {
        setError(`order rejected: retcode ${res.retcode} — ${res.detail ?? ""}`);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "order failed");
    } finally {
      setBusy(false);
    }
  };

  const close = async (sym: string, ticket: number) => {
    setBusy(true);
    setError(null);
    try {
      const res = await postMt5Close(token, { symbol: sym, ticket });
      if (res.ok) {
        setNotice(`position #${ticket} closed @ ${res.price}`);
        await refresh();
        getMt5History(token, 90).then((r) => setHistory(r.positions)).catch(() => {});
      } else {
        setError(`close failed: retcode ${res.retcode} — ${res.detail ?? ""}`);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "close failed");
    } finally {
      setBusy(false);
    }
  };

  const setArmed = async (enabled: boolean) => {
    setAutoBusy(true);
    setError(null);
    setNotice(null);
    try {
      const res = await postMt5AutoTrade(token, {
        enabled,
        confirm: enabled ? confirmText.trim() : undefined,
      });
      if (res.armed) {
        setNotice("LIVE AUTO-TRADE ARMED — every AI signal now places a REAL order");
      } else {
        setNotice("live auto-trade disarmed");
      }
      setConfirmText("");
      await refreshAuto();
    } catch (e) {
      setError(e instanceof Error ? e.message : "arm/disarm failed");
    } finally {
      setAutoBusy(false);
    }
  };

  const fmt = (n: number | null | undefined, d = 2) =>
    n === null || n === undefined ? "—" : n.toFixed(d);

  const connected = account?.connected === true;
  const goldSymbols = symbols.filter((s) => /XAU|GOLD|BTC|ETH|EURUSD|GBPUSD|USDJPY/i.test(s.symbol));

  return (
    <div className="fixed inset-0 z-40 flex items-center justify-center bg-black/70 p-4" onClick={onClose}>
      <div
        className="flex max-h-[88vh] w-full max-w-3xl flex-col rounded-2xl border border-zinc-700 bg-zinc-900 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        {/* header */}
        <div className="flex items-center justify-between border-b border-zinc-800 px-5 py-3">
          <div className="flex items-center gap-3">
            <h2 className="text-base font-semibold text-zinc-100">MT5 Account</h2>
            {account ? (
              <span
                className={`rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider ${
                  connected
                    ? "bg-emerald-500/15 text-emerald-300"
                    : "bg-red-500/15 text-red-300"
                }`}
              >
                {connected ? "● live" : "● disconnected"}
              </span>
            ) : (
              <span className="rounded-full bg-amber-500/15 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-amber-300">
                bridge offline
              </span>
            )}
          </div>
          <div className="flex items-center gap-2">
            <div className="flex rounded-md border border-zinc-700 p-0.5 text-xs">
              {(["positions", "history", "order", "auto"] as Tab[]).map((t) => (
                <button
                  key={t}
                  type="button"
                  onClick={() => setTab(t)}
                  className={`rounded px-3 py-1 ${tab === t ? "bg-gold/15 text-gold" : "text-zinc-400 hover:text-zinc-200"}`}
                >
                  {t === "auto" ? "AI AUTO" : t}
                </button>
              ))}
            </div>
            {auto?.armed && (
              <span className="rounded-full bg-red-500/20 px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider text-red-300">
                ● ai auto armed
              </span>
            )}
            <button type="button" onClick={onClose} className="text-zinc-500 hover:text-zinc-300">✕</button>
          </div>
        </div>

        {/* account summary */}
        <div className="grid grid-cols-2 gap-3 border-b border-zinc-800 px-5 py-4 sm:grid-cols-4">
          <div>
            <p className="text-[10px] uppercase tracking-wider text-zinc-500">Balance</p>
            <p className="mt-0.5 font-mono text-xl font-bold text-gold">
              {fmt(account?.balance)}{" "}
              <span className="text-xs text-zinc-400">{account?.currency ?? ""}</span>
            </p>
          </div>
          <div>
            <p className="text-[10px] uppercase tracking-wider text-zinc-500">Equity</p>
            <p className="mt-0.5 font-mono text-xl font-bold text-zinc-100">{fmt(account?.equity)}</p>
          </div>
          <div>
            <p className="text-[10px] uppercase tracking-wider text-zinc-500">Free margin</p>
            <p className="mt-0.5 font-mono text-lg text-zinc-200">{fmt(account?.margin_free)}</p>
          </div>
          <div>
            <p className="text-[10px] uppercase tracking-wider text-zinc-500">Floating P/L</p>
            <p
              className={`mt-0.5 font-mono text-lg font-semibold ${
                (account?.profit ?? 0) >= 0 ? "text-emerald-400" : "text-red-400"
              }`}
            >
              {(account?.profit ?? 0) >= 0 ? "+" : ""}
              {fmt(account?.profit)}
            </p>
          </div>
          <div className="col-span-2 text-xs text-zinc-500 sm:col-span-4">
            {account ? (
              <>
                <span className="text-zinc-300">{account.server}</span> · login{" "}
                <span className="font-mono text-zinc-300">{account.login}</span>
                {account.name ? ` · ${account.name}` : ""} ·{" "}
                <span className="uppercase">{account.type}</span> · {account.margin_mode} ·{" "}
                {account.broker} · via MetaTrader 5 terminal (build {account.build})
              </>
            ) : (
              "MT5 terminal bridge unreachable — is the terminal running?"
            )}
          </div>
        </div>

        {/* body */}
        <div className="flex-1 overflow-y-auto p-5">
          {tab === "positions" && (
            <div>
              <h3 className="mb-2 text-xs font-semibold uppercase tracking-wider text-zinc-500">
                Open positions ({positions?.length ?? "…"})
              </h3>
              {!positions ? (
                <p className="p-3 text-xs text-zinc-500">loading…</p>
              ) : positions.length === 0 ? (
                <p className="rounded-lg border border-zinc-800 bg-zinc-950/40 p-3 text-xs text-zinc-500">
                  No open positions on the Exness account.
                </p>
              ) : (
                <div className="overflow-hidden rounded-lg border border-zinc-800">
                  <table className="w-full text-xs">
                    <thead className="bg-zinc-950/80 text-zinc-500">
                      <tr>
                        <th className="px-3 py-2 text-left font-medium">#</th>
                        <th className="px-3 py-2 text-left font-medium">symbol</th>
                        <th className="px-3 py-2 text-left font-medium">side</th>
                        <th className="px-3 py-2 text-right font-medium">lots</th>
                        <th className="px-3 py-2 text-right font-medium">open</th>
                        <th className="px-3 py-2 text-right font-medium">now</th>
                        <th className="px-3 py-2 text-right font-medium">P/L</th>
                        <th className="px-3 py-2" />
                      </tr>
                    </thead>
                    <tbody>
                      {positions.map((p) => (
                        <tr key={p.position_id} className="border-t border-zinc-800 text-zinc-300">
                          <td className="px-3 py-2 font-mono text-zinc-500">{p.position_id}</td>
                          <td className="px-3 py-2 font-semibold text-zinc-200">{p.symbol}</td>
                          <td
                            className={`px-3 py-2 font-semibold ${
                              p.action.toLowerCase() === "buy" ? "text-emerald-400" : "text-red-400"
                            }`}
                          >
                            {p.action.toUpperCase()}
                          </td>
                          <td className="px-3 py-2 text-right">{p.volume.toFixed(2)}</td>
                          <td className="px-3 py-2 text-right font-mono">{fmt(p.price_open)}</td>
                          <td className="px-3 py-2 text-right font-mono text-zinc-400">
                            {fmt(p.price_last)}
                          </td>
                          <td
                            className={`px-3 py-2 text-right font-mono font-semibold ${
                              (p.profit ?? 0) >= 0 ? "text-emerald-400" : "text-red-400"
                            }`}
                          >
                            {(p.profit ?? 0) >= 0 ? "+" : ""}
                            {fmt(p.profit)}
                          </td>
                          <td className="px-3 py-2 text-right">
                            <button
                              type="button"
                              disabled={busy}
                              onClick={() => void close(p.symbol, p.position_id)}
                              className="rounded border border-zinc-700 px-2 py-0.5 text-zinc-400 hover:border-red-500/50 hover:text-red-300"
                            >
                              close
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          )}

          {tab === "history" && (
            <div>
              <div className="mb-2 flex items-center justify-between">
                <h3 className="text-xs font-semibold uppercase tracking-wider text-zinc-500">
                  Trade history — 90 days ({history?.length ?? "…"})
                </h3>
                <button
                  type="button"
                  onClick={() => getMt5History(token, 90).then((r) => setHistory(r.positions)).catch(() => {})}
                  className="rounded border border-zinc-700 px-2 py-0.5 text-xs text-zinc-400 hover:border-zinc-500"
                >
                  refresh
                </button>
              </div>
              {!history ? (
                <p className="p-3 text-xs text-zinc-500">loading…</p>
              ) : history.length === 0 ? (
                <p className="rounded-lg border border-zinc-800 bg-zinc-950/40 p-3 text-xs text-zinc-500">
                  No closed trades yet.
                </p>
              ) : (
                <div className="overflow-hidden rounded-lg border border-zinc-800">
                  <table className="w-full text-xs">
                    <thead className="bg-zinc-950/80 text-zinc-500">
                      <tr>
                        <th className="px-3 py-2 text-left font-medium">opened</th>
                        <th className="px-3 py-2 text-left font-medium">symbol</th>
                        <th className="px-3 py-2 text-left font-medium">side</th>
                        <th className="px-3 py-2 text-right font-medium">lots</th>
                        <th className="px-3 py-2 text-right font-medium">open → close</th>
                        <th className="px-3 py-2 text-left font-medium">closed</th>
                        <th className="px-3 py-2 text-right font-medium">P/L</th>
                      </tr>
                    </thead>
                    <tbody>
                      {history.map((h) => (
                        <tr key={h.position_id} className="border-t border-zinc-800 text-zinc-300">
                          <td className="px-3 py-2 font-mono text-zinc-500">{h.open_time}</td>
                          <td className="px-3 py-2 font-semibold text-zinc-200">{h.symbol}</td>
                          <td
                            className={`px-3 py-2 font-semibold ${
                              h.type.toLowerCase() === "buy" ? "text-emerald-400" : "text-red-400"
                            }`}
                          >
                            {h.type.toUpperCase()}
                          </td>
                          <td className="px-3 py-2 text-right">{h.open_volume.toFixed(2)}</td>
                          <td className="px-3 py-2 text-right font-mono">
                            {fmt(h.open_price)} → {fmt(h.close_price)}
                          </td>
                          <td className="px-3 py-2 font-mono text-zinc-500">{h.close_time}</td>
                          <td
                            className={`px-3 py-2 text-right font-mono font-semibold ${
                              h.profit >= 0 ? "text-emerald-400" : "text-red-400"
                            }`}
                          >
                            {h.profit >= 0 ? "+" : ""}
                            {fmt(h.profit)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          )}

          {tab === "order" && (
            <div className="rounded-xl border border-zinc-800 bg-zinc-950/60 p-4">
              <div className="mb-3 flex items-center justify-between text-xs text-zinc-400">
                <span>Market order · real account (executed by MetaTrader 5)</span>
                {account?.mcp_trade_allowed === false && (
                  <span className="text-amber-300">auto-trading disabled in terminal</span>
                )}
              </div>
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                <label className="text-xs text-zinc-400">
                  Symbol
                  <select
                    value={symbol}
                    onChange={(e) => setSymbol(e.target.value)}
                    className="mt-1 w-full rounded-md border border-zinc-700 bg-zinc-950 px-2.5 py-1.5 text-sm text-zinc-200 focus:border-gold/60 focus:outline-none"
                  >
                    {(goldSymbols.length ? goldSymbols : [{ symbol: "XAUUSDm" } as Mt5Symbol]).map(
                      (s) => (
                        <option key={s.symbol} value={s.symbol}>
                          {s.symbol}
                        </option>
                      )
                    )}
                  </select>
                </label>
                <label className="text-xs text-zinc-400">
                  Volume (lots)
                  <input
                    value={volume}
                    onChange={(e) => setVolume(e.target.value)}
                    inputMode="decimal"
                    className="mt-1 w-full rounded-md border border-zinc-700 bg-zinc-950 px-2.5 py-1.5 text-sm text-zinc-200 focus:border-gold/60 focus:outline-none"
                  />
                </label>
                <label className="text-xs text-zinc-400">
                  Stop loss (opt.)
                  <input
                    value={sl}
                    onChange={(e) => setSl(e.target.value)}
                    inputMode="decimal"
                    placeholder="—"
                    className="mt-1 w-full rounded-md border border-zinc-700 bg-zinc-950 px-2.5 py-1.5 text-sm text-zinc-200 focus:border-gold/60 focus:outline-none"
                  />
                </label>
                <label className="text-xs text-zinc-400">
                  Take profit (opt.)
                  <input
                    value={tp}
                    onChange={(e) => setTp(e.target.value)}
                    inputMode="decimal"
                    placeholder="—"
                    className="mt-1 w-full rounded-md border border-zinc-700 bg-zinc-950 px-2.5 py-1.5 text-sm text-zinc-200 focus:border-gold/60 focus:outline-none"
                  />
                </label>
              </div>
              <div className="mt-3 grid grid-cols-2 gap-3">
                <button
                  type="button"
                  disabled={busy || !connected}
                  onClick={() => void order("buy")}
                  className="rounded-md border border-emerald-500/50 bg-emerald-500/15 px-3 py-2.5 text-sm font-bold text-emerald-300 hover:bg-emerald-500/25 disabled:opacity-40"
                >
                  BUY / LONG
                </button>
                <button
                  type="button"
                  disabled={busy || !connected}
                  onClick={() => void order("sell")}
                  className="rounded-md border border-red-500/50 bg-red-500/15 px-3 py-2.5 text-sm font-bold text-red-300 hover:bg-red-500/25 disabled:opacity-40"
                >
                  SELL / SHORT
                </button>
              </div>
              <p className="mt-3 text-[11px] leading-relaxed text-zinc-500">
                Orders are routed through the real MetaTrader 5 terminal connected to
                {" "}{account?.server ?? "your broker"}. Demo account funds are virtual, but
                execution, fills and history are 100% real broker behavior.
              </p>
            </div>
          )}

          {tab === "auto" && (
            <div className="space-y-4">
              {/* arm state */}
              <div
                className={`rounded-xl border p-4 ${
                  auto?.armed
                    ? "border-red-500/40 bg-red-500/5"
                    : "border-zinc-800 bg-zinc-950/60"
                }`}
              >
                <div className="flex items-center justify-between">
                  <div>
                    <p
                      className={`text-sm font-bold uppercase tracking-wider ${
                        auto?.armed ? "text-red-300" : "text-zinc-400"
                      }`}
                    >
                      {auto === null
                        ? "AI SIGNAL → AUTO-ORDER (loading…)"
                        : auto.armed
                          ? "● AI AUTO-TRADE ARMED"
                          : "AI AUTO-TRADE DISARMED"}
                    </p>
                    <p className="mt-1 text-xs text-zinc-500">
                      {auto?.armed
                        ? `Every AI signal (XAUUSD + BTCUSD, ${auto.risk.timeframe} engine) now places a REAL market order on this account — SL/TP attached, §9 risk guards enforced.`
                        : "When armed, every AI signal places a REAL order through the MetaTrader 5 terminal with the risk guards below."}
                    </p>
                  </div>
                  {auto?.terminal.available ? (
                    <span className="rounded-full bg-emerald-500/15 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-emerald-300">
                      terminal ready
                    </span>
                  ) : (
                    <span className="rounded-full bg-red-500/15 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-red-300">
                      terminal offline
                    </span>
                  )}
                </div>
                {auto && (
                  <p className="mt-2 text-[11px] text-zinc-500">
                    {auto.terminal.server ?? "—"} · login{" "}
                    <span className="font-mono">{auto.terminal.login ?? "—"}</span> ·
                    equity <span className="font-mono">{fmt(auto.terminal.equity)}</span>{" "}
                    {auto.terminal.currency ?? "USD"}
                    {auto.terminal.available && !auto.terminal.trade_allowed && (
                      <span className="text-amber-300"> · automated trading disabled in the terminal</span>
                    )}
                    {auto.armed_at && (
                      <> · armed {new Date(auto.armed_at).toLocaleString()}</>
                    )}
                  </p>
                )}
                {auto?.armed && (
                  <p className="mt-2 rounded-md border border-red-500/30 bg-red-500/10 p-2 text-[11px] text-red-200">
                    REAL MONEY MODE — orders execute on the live Exness account. The
                    daily-loss kill switch closes everything and disarms automatically.
                  </p>
                )}
              </div>

              {/* risk summary + controls */}
              <div className="grid gap-4 sm:grid-cols-2">
                <div className="rounded-xl border border-zinc-800 bg-zinc-950/60 p-4">
                  <h4 className="mb-2 text-[10px] font-semibold uppercase tracking-wider text-zinc-500">
                    Risk guards (§9 · engine config)
                  </h4>
                  {auto ? (
                    <dl className="space-y-1.5 text-xs">
                      <div className="flex justify-between">
                        <dt className="text-zinc-500">Risk per trade</dt>
                        <dd className="font-mono text-zinc-200">
                          {auto.risk.risk_mode === "percent"
                            ? `${auto.risk.risk_percent}% of equity`
                            : `${auto.risk.fixed_lot} lots fixed`}
                        </dd>
                      </div>
                      <div className="flex justify-between">
                        <dt className="text-zinc-500">Max open positions</dt>
                        <dd className="font-mono text-zinc-200">{auto.risk.max_positions}</dd>
                      </div>
                      <div className="flex justify-between">
                        <dt className="text-zinc-500">Daily loss kill switch</dt>
                        <dd className="font-mono text-zinc-200">-{auto.risk.daily_max_loss_pct}%</dd>
                      </div>
                      <div className="flex justify-between">
                        <dt className="text-zinc-500">Max spread</dt>
                        <dd className="font-mono text-zinc-200">{auto.risk.max_spread_points} pts</dd>
                      </div>
                      <div className="flex justify-between">
                        <dt className="text-zinc-500">Signal engine</dt>
                        <dd className="font-mono text-zinc-200">{auto.risk.timeframe} SFP</dd>
                      </div>
                    </dl>
                  ) : (
                    <p className="text-xs text-zinc-500">loading…</p>
                  )}
                  <p className="mt-2 text-[11px] leading-relaxed text-zinc-500">
                    Lot size is computed from the REAL account equity and the signal's
                    SL distance. Broker-side SL/TP ride with every order; signal expiry
                    closes the position; forex weekends are skipped honestly (BTCUSD
                    trades 24/7).
                  </p>
                </div>

                <div className="rounded-xl border border-zinc-800 bg-zinc-950/60 p-4">
                  <h4 className="mb-2 text-[10px] font-semibold uppercase tracking-wider text-zinc-500">
                    Arm / disarm (admin)
                  </h4>
                  {!isAdmin ? (
                    <p className="text-xs text-zinc-500">
                      Only admins can arm live auto-execution.
                    </p>
                  ) : auto?.armed ? (
                    <button
                      type="button"
                      disabled={autoBusy}
                      onClick={() => void setArmed(false)}
                      className="w-full rounded-md border border-zinc-500/50 bg-zinc-500/10 px-3 py-2.5 text-sm font-bold text-zinc-200 hover:bg-zinc-500/20 disabled:opacity-40"
                    >
                      DISARM AUTO-TRADE
                    </button>
                  ) : (
                    <>
                      <label className="text-xs text-zinc-400">
                        Type <span className="font-mono font-bold text-gold">ENABLE</span> to arm
                        <input
                          value={confirmText}
                          onChange={(e) => setConfirmText(e.target.value)}
                          placeholder="ENABLE"
                          className="mt-1 w-full rounded-md border border-zinc-700 bg-zinc-950 px-2.5 py-1.5 font-mono text-sm text-zinc-200 focus:border-red-500/60 focus:outline-none"
                        />
                      </label>
                      <button
                        type="button"
                        disabled={autoBusy || confirmText.trim() !== "ENABLE" || !auto?.terminal.available || !auto?.terminal.trade_allowed}
                        onClick={() => void setArmed(true)}
                        className="mt-2 w-full rounded-md border border-red-500/50 bg-red-500/15 px-3 py-2.5 text-sm font-bold text-red-300 hover:bg-red-500/25 disabled:opacity-40"
                      >
                        ARM LIVE AUTO-TRADE
                      </button>
                      <p className="mt-2 text-[11px] text-zinc-500">
                        Requires the terminal bridge to be online and automated trading
                        allowed in MetaTrader 5.
                      </p>
                    </>
                  )}
                  {auto?.last_skip_reason && (
                    <p className="mt-2 text-[11px] text-amber-300/90">
                      last skip: {auto.last_skip_reason}
                    </p>
                  )}
                </div>
              </div>

              {/* live execution feed */}
              <div className="rounded-xl border border-zinc-800 bg-zinc-950/60 p-4">
                <h4 className="mb-2 text-[10px] font-semibold uppercase tracking-wider text-zinc-500">
                  AI execution feed (live)
                </h4>
                {autoEvents.length === 0 ? (
                  <p className="text-xs text-zinc-500">
                    No auto-execution events yet. Orders, skips (weekend/market-closed,
                    risk guards) and closes will appear here in real time.
                  </p>
                ) : (
                  <ul className="max-h-56 space-y-1.5 overflow-y-auto text-xs">
                    {autoEvents.map((ev, i) => (
                      <li key={`${ev.ts}-${i}`} className="flex items-start gap-2">
                        <span
                          className={`mt-1 h-1.5 w-1.5 shrink-0 rounded-full ${
                            ev.event === "order" && ev.ok
                              ? "bg-emerald-400"
                              : ev.event === "order" || ev.event === "skip"
                                ? "bg-amber-400"
                                : ev.event === "close"
                                  ? "bg-sky-400"
                                  : "bg-red-400"
                          }`}
                        />
                        <div>
                          <p className="text-zinc-300">{ev.message}</p>
                          <p className="text-[10px] text-zinc-600">
                            {new Date(ev.ts).toLocaleTimeString()} ·{" "}
                            {ev.event.toUpperCase()}
                            {ev.signal_id ? ` · signal ${ev.signal_id.slice(0, 8)}` : ""}
                          </p>
                        </div>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            </div>
          )}
        </div>

        {error && <p className="border-t border-zinc-800 px-5 py-2 text-xs text-red-400">{error}</p>}
        {notice && <p className="border-t border-zinc-800 px-5 py-2 text-xs text-emerald-400">{notice}</p>}
      </div>
    </div>
  );
}
