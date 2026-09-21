import { useCallback, useEffect, useState } from "react";
import {
  ApiError, getMt5Account, getMt5AutoTrade, getMt5History, getMt5Positions,
  getMt5Symbols, postMt5AutoTrade, postMt5Close, postMt5Order,
} from "../lib/api";
import type {
  Mt5Account, Mt5AutoTradeStatus, Mt5HistoryPosition, Mt5OpenPosition, Mt5Symbol,
  WsMt5AutoMsg,
} from "../types";

interface AiTradingViewProps {
  token: string;
  onConnectBroker: () => void;
  /** D-039: live AI auto-execution events (WS mt5_auto feed). */
  autoEvents: WsMt5AutoMsg[];
  /** Refresh trigger key — bump to refetch (WS events etc.). */
  refreshKey: number;
}

type Section = "overview" | "positions" | "order" | "history";

const card =
  "rounded-2xl border border-zinc-800 bg-zinc-900/60 p-4 transition-colors hover:border-zinc-700";
const label = "text-[10px] font-semibold uppercase tracking-widest text-zinc-500";
const btn =
  "rounded-lg border px-3 py-1.5 text-xs font-medium transition-colors disabled:opacity-50";

/**
 * D-039 — AI Trading tab (was Mt5AccountPanel "AI AUTO" + positions +
 * history, now a first-class tab). One place to see WHY the AI is or isn't
 * trading, arm/disarm it, and manage every open position / past trade on
 * YOUR broker account.
 */
export default function AiTradingView({
  token, onConnectBroker, autoEvents, refreshKey,
}: AiTradingViewProps) {
  const [section, setSection] = useState<Section>("overview");
  const [account, setAccount] = useState<Mt5Account | null>(null);
  const [auto, setAuto] = useState<Mt5AutoTradeStatus | null>(null);
  const [positions, setPositions] = useState<Mt5OpenPosition[]>([]);
  const [history, setHistory] = useState<Mt5HistoryPosition[]>([]);
  const [confirmText, setConfirmText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  /* manual order form (D-039 — moved from the old MT5 panel "order" tab) */
  const [ordSymbol, setOrdSymbol] = useState("");
  const [ordSide, setOrdSide] = useState<"BUY" | "SELL">("BUY");
  const [ordVolume, setOrdVolume] = useState("0.01");
  const [ordSl, setOrdSl] = useState("");
  const [ordTp, setOrdTp] = useState("");
  const [symbols, setSymbols] = useState<Mt5Symbol[]>([]);

  const refresh = useCallback(async () => {
    if (!token) return;
    try {
      const [acct, pos, st] = await Promise.all([
        getMt5Account(token),
        getMt5Positions(token),
        getMt5AutoTrade(token),
      ]);
      setAccount(acct);
      setPositions(pos.positions);
      setAuto(st);
    } catch (e) {
      if (e instanceof ApiError && e.status === 428) setAccount(null);
    }
    getMt5History(token, 90)
      .then((r) => setHistory(r.positions))
      .catch(() => undefined);
    getMt5Symbols(token)
      .then((r) => {
        setSymbols(r.symbols);
        setOrdSymbol((cur) =>
          cur ||
          r.symbols.find((s) => /XAU/i.test(s.symbol))?.symbol ||
          r.symbols[0]?.symbol ||
          ""
        );
      })
      .catch(() => undefined);
  }, [token]);

  useEffect(() => {
    void refresh();
  }, [refresh, refreshKey]);

  const connected = account?.connected === true;
  const why = auto?.why ?? null;

  const setArmed = async (enabled: boolean) => {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const res = await postMt5AutoTrade(token, {
        enabled,
        confirm: enabled ? confirmText.trim() : undefined,
      });
      setNotice(
        res.armed
          ? "LIVE AUTO-TRADE ARMED — every AI signal now places a REAL order"
          : "live auto-trade disarmed"
      );
      setConfirmText("");
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : "arm/disarm failed");
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
      } else {
        setError(`close failed: retcode ${res.retcode} — ${res.detail ?? ""}`);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "close failed");
    } finally {
      setBusy(false);
    }
  };

  const placeOrder = async () => {
    setBusy(true);
    setError(null);
    try {
      const res = await postMt5Order(token, {
        symbol: ordSymbol,
        side: ordSide.toLowerCase() as "buy" | "sell",
        volume: parseFloat(ordVolume),
        sl: ordSl ? parseFloat(ordSl) : undefined,
        tp: ordTp ? parseFloat(ordTp) : undefined,
      });
      if (res.ok) {
        setNotice(
          `${ordSide} ${res.volume} ${res.symbol} filled @ ${res.price} (deal ${res.deal})`
        );
        setOrdSl("");
        setOrdTp("");
        setSection("positions");
        await refresh();
      } else {
        setError(`order rejected: retcode ${res.retcode} — ${res.detail ?? ""}`);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "order failed");
    } finally {
      setBusy(false);
    }
  };

  const fmt = (n: number | null | undefined, d = 2) =>
    n === null || n === undefined ? "—" : n.toFixed(d);

  return (
    <div className="flex flex-col gap-4">
      {/* section switcher */}
      <div className="flex gap-1.5">
        {(["overview", "positions", "order", "history"] as Section[]).map((s) => (
          <button
            key={s}
            type="button"
            onClick={() => setSection(s)}
            className={`rounded-lg border px-3 py-1.5 text-xs font-medium capitalize transition-colors ${
              section === s
                ? "border-gold/50 bg-gold/15 text-gold"
                : "border-zinc-700 text-zinc-400 hover:text-zinc-200"
            }`}
          >
            {s === "order" ? "manual order" : s}
            {s === "positions" && positions.length > 0 && (
              <span className="ml-1.5 rounded bg-zinc-800 px-1 font-mono text-[10px] text-zinc-300">
                {positions.length}
              </span>
            )}
          </button>
        ))}
      </div>

      {error && (
        <p className="rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-300">
          {error}
        </p>
      )}
      {notice && (
        <p className="rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-3 py-2 text-xs text-emerald-300">
          {notice}
        </p>
      )}

      {/* not connected — honest CTA */}
      {!connected && (
        <div className={`${card} flex flex-col items-center gap-3 py-10 text-center`}>
          <p className="max-w-md text-sm leading-relaxed text-zinc-400">
            Connect your Exness MetaTrader 5 account — AI auto-trade, positions
            and history all run on <b className="text-zinc-200">your account only</b>.
          </p>
          <button
            type="button"
            onClick={onConnectBroker}
            className="rounded-lg border border-gold/50 bg-gold/15 px-4 py-2 text-sm font-semibold text-gold transition-colors hover:bg-gold/25"
          >
            Connect broker account
          </button>
        </div>
      )}

      {connected && section === "overview" && (
        <>
          {/* account + AI state grid */}
          <div className="grid gap-4 sm:grid-cols-2">
            <section className={card}>
              <div className="flex items-center justify-between">
                <p className={label}>broker account</p>
                <span className="rounded-full bg-emerald-500/15 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-emerald-300">
                  ● live
                </span>
              </div>
              <div className="mt-2 grid grid-cols-2 gap-3">
                <div>
                  <p className="text-xs text-zinc-500">balance</p>
                  <p className="font-mono text-2xl font-semibold text-zinc-100">
                    {fmt(account?.balance)}{" "}
                    <span className="text-xs text-zinc-500">{account?.currency ?? "USD"}</span>
                  </p>
                </div>
                <div>
                  <p className="text-xs text-zinc-500">equity</p>
                  <p className="font-mono text-2xl font-semibold text-zinc-100">
                    {fmt(account?.equity)}
                  </p>
                </div>
              </div>
              <p className="mt-2 text-[11px] text-zinc-500">
                {account?.login} @ {account?.server} · {account?.broker}
              </p>
            </section>

            <section className={card}>
              <div className="flex items-center justify-between">
                <p className={label}>AI auto-trade</p>
                {auto?.armed ? (
                  <span className="rounded-full bg-red-500/20 px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider text-red-300">
                    ● armed
                  </span>
                ) : (
                  <span className="rounded-full bg-zinc-800 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-zinc-400">
                    off
                  </span>
                )}
              </div>
              {/* D-039: the honest WHY line */}
              {why && (
                <p
                  className={`mt-2 rounded-lg border px-3 py-2 text-xs leading-relaxed ${
                    why.code === "ready"
                      ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-300"
                      : why.code === "not_armed"
                        ? "border-zinc-700 bg-zinc-800/60 text-zinc-300"
                        : "border-amber-500/30 bg-amber-500/10 text-amber-300"
                  }`}
                >
                  {why.text}
                </p>
              )}
              {/* per-symbol market state */}
              {auto?.markets && Object.keys(auto.markets).length > 0 && (
                <div className="mt-2 flex flex-wrap gap-1.5">
                  {Object.entries(auto.markets).map(([sym, m]) => (
                    <span
                      key={sym}
                      className={`rounded px-2 py-0.5 text-[10px] font-medium ${
                        m.open
                          ? "bg-emerald-500/15 text-emerald-300"
                          : "bg-amber-500/15 text-amber-300"
                      }`}
                      title={m.detail}
                    >
                      {sym} {m.open ? "open" : "closed"}
                    </span>
                  ))}
                </div>
              )}
              {/* arm / disarm */}
              {auto?.armed ? (
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => void setArmed(false)}
                  className={`${btn} mt-3 w-full border-red-500/40 bg-red-500/10 font-semibold text-red-300 hover:bg-red-500/20`}
                >
                  Disarm auto-trade
                </button>
              ) : (
                <div className="mt-3 space-y-2">
                  <input
                    value={confirmText}
                    onChange={(e) => setConfirmText(e.target.value)}
                    placeholder='type "ENABLE" to arm REAL auto-trading'
                    className="w-full rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-2 font-mono text-xs text-zinc-100 placeholder-zinc-600 focus:border-gold/60 focus:outline-none"
                  />
                  <button
                    type="button"
                    disabled={busy || confirmText.trim() !== "ENABLE"}
                    onClick={() => void setArmed(true)}
                    className={`${btn} w-full border-gold/50 bg-gold/15 font-semibold text-gold hover:bg-gold/25`}
                  >
                    Arm auto-trade — REAL orders on my account
                  </button>
                  <p className="text-[10px] leading-relaxed text-zinc-500">
                    ⚠ REAL MONEY: armed AI signals place real orders on your
                    broker account with SL/TP and the risk guards below
                    (risk {auto?.risk.risk_percent ?? 0.5}%/trade · max{" "}
                    {auto?.risk.max_positions ?? 1} position · daily stop −
                    {auto?.risk.daily_max_loss_pct ?? 3}%).
                  </p>
                </div>
              )}
            </section>
          </div>

          {/* live AI events */}
          <section className={card}>
            <p className={label}>live AI events</p>
            <div className="mt-2 max-h-56 space-y-1.5 overflow-y-auto">
              {autoEvents.length === 0 ? (
                <p className="text-xs text-zinc-500">
                  No AI events yet — signal → order activity appears here in
                  real time.
                </p>
              ) : (
                [...autoEvents].reverse().map((ev, i) => (
                  <div
                    key={`${ev.ts}-${i}`}
                    className={`rounded-lg border px-3 py-1.5 text-xs ${
                      ev.event === "order" && ev.ok
                        ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-300"
                        : ev.event === "skip" || (ev.event === "order" && !ev.ok)
                          ? "border-amber-500/30 bg-amber-500/10 text-amber-300"
                          : "border-zinc-700 bg-zinc-800/60 text-zinc-300"
                    }`}
                  >
                    <span className="font-mono text-[10px] text-zinc-500">
                      {new Date(ev.ts).toLocaleTimeString()}
                    </span>{" "}
                    {ev.message}
                  </div>
                ))
              )}
            </div>
          </section>
        </>
      )}

      {connected && section === "positions" && (
        <section className={card}>
          <p className={label}>open positions · your account only</p>
          {positions.length === 0 ? (
            <p className="mt-3 text-sm text-zinc-500">No open positions.</p>
          ) : (
            <div className="mt-3 overflow-x-auto">
              <table className="w-full text-left text-xs">
                <thead>
                  <tr className="border-b border-zinc-800 text-zinc-500">
                    <th className="py-2 pr-4 font-medium">ticket</th>
                    <th className="py-2 pr-4 font-medium">symbol</th>
                    <th className="py-2 pr-4 font-medium">side</th>
                    <th className="py-2 pr-4 font-medium">lots</th>
                    <th className="py-2 pr-4 font-medium">open price</th>
                    <th className="py-2 pr-4 font-medium">P/L</th>
                    <th className="py-2 pr-4 font-medium"></th>
                  </tr>
                </thead>
                <tbody>
                  {positions.map((p) => (
                    <tr key={p.position_id} className="border-b border-zinc-800/60">
                      <td className="py-2 pr-4 font-mono text-zinc-400">{p.position_id}</td>
                      <td className="py-2 pr-4 font-medium text-zinc-200">{p.symbol}</td>
                      <td
                        className={`py-2 pr-4 font-semibold ${
                          p.action === "BUY" ? "text-emerald-400" : "text-red-400"
                        }`}
                      >
                        {p.action}
                      </td>
                      <td className="py-2 pr-4 font-mono text-zinc-300">{p.volume}</td>
                      <td className="py-2 pr-4 font-mono text-zinc-300">{fmt(p.price_open)}</td>
                      <td
                        className={`py-2 pr-4 font-mono font-semibold ${
                          (p.profit ?? 0) >= 0 ? "text-emerald-400" : "text-red-400"
                        }`}
                      >
                        {fmt(p.profit)}
                      </td>
                      <td className="py-2 pr-4">
                        <button
                          type="button"
                          disabled={busy}
                          onClick={() => void close(p.symbol, p.position_id)}
                          className={`${btn} border-zinc-700 text-zinc-300 hover:border-red-500/40 hover:text-red-300`}
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
        </section>
      )}

      {connected && section === "order" && (
        <section className={card}>
          <p className={label}>manual order · real broker execution</p>
          <div className="mt-3 grid gap-3 sm:grid-cols-2">
            <label className="flex flex-col gap-1 text-xs text-zinc-500">
              symbol
              <select
                value={ordSymbol}
                onChange={(e) => setOrdSymbol(e.target.value)}
                className="rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm text-zinc-100 focus:border-gold/60 focus:outline-none"
              >
                {symbols.length === 0 && <option value="">loading…</option>}
                {symbols.map((s) => (
                  <option key={s.symbol} value={s.symbol}>
                    {s.symbol}
                  </option>
                ))}
              </select>
            </label>
            <div className="flex items-end gap-2">
              <button
                type="button"
                onClick={() => setOrdSide("BUY")}
                className={`flex-1 rounded-lg border px-3 py-2 text-sm font-bold transition-colors ${
                  ordSide === "BUY"
                    ? "border-emerald-500/50 bg-emerald-500/20 text-emerald-300"
                    : "border-zinc-700 text-zinc-400"
                }`}
              >
                BUY
              </button>
              <button
                type="button"
                onClick={() => setOrdSide("SELL")}
                className={`flex-1 rounded-lg border px-3 py-2 text-sm font-bold transition-colors ${
                  ordSide === "SELL"
                    ? "border-red-500/50 bg-red-500/20 text-red-300"
                    : "border-zinc-700 text-zinc-400"
                }`}
              >
                SELL
              </button>
            </div>
            <label className="flex flex-col gap-1 text-xs text-zinc-500">
              volume (lots)
              <input
                value={ordVolume}
                onChange={(e) => setOrdVolume(e.target.value)}
                inputMode="decimal"
                className="rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-2 font-mono text-sm text-zinc-100 focus:border-gold/60 focus:outline-none"
              />
            </label>
            <div className="grid grid-cols-2 gap-2">
              <label className="flex flex-col gap-1 text-xs text-zinc-500">
                stop loss (opt.)
                <input
                  value={ordSl}
                  onChange={(e) => setOrdSl(e.target.value)}
                  inputMode="decimal"
                  placeholder="—"
                  className="rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-2 font-mono text-sm text-zinc-100 placeholder-zinc-600 focus:border-gold/60 focus:outline-none"
                />
              </label>
              <label className="flex flex-col gap-1 text-xs text-zinc-500">
                take profit (opt.)
                <input
                  value={ordTp}
                  onChange={(e) => setOrdTp(e.target.value)}
                  inputMode="decimal"
                  placeholder="—"
                  className="rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-2 font-mono text-sm text-zinc-100 placeholder-zinc-600 focus:border-gold/60 focus:outline-none"
                />
              </label>
            </div>
          </div>
          <button
            type="button"
            disabled={busy || !ordSymbol || !ordVolume}
            onClick={() => void placeOrder()}
            className={`${btn} mt-4 w-full py-2.5 text-sm font-semibold ${
              ordSide === "BUY"
                ? "border-emerald-500/50 bg-emerald-500/15 text-emerald-300 hover:bg-emerald-500/25"
                : "border-red-500/50 bg-red-500/15 text-red-300 hover:bg-red-500/25"
            }`}
          >
            {ordSide} {ordVolume} {ordSymbol || "—"} @ market
          </button>
          <p className="mt-2 text-[10px] leading-relaxed text-zinc-500">
            Real market order through the MetaTrader 5 terminal on YOUR
            connected account. Optional SL/TP attach broker-side.
          </p>
        </section>
      )}

      {connected && section === "history" && (
        <section className={card}>
          <p className={label}>trade history · 90 days · your account only</p>
          {history.length === 0 ? (
            <p className="mt-3 text-sm text-zinc-500">No closed trades yet.</p>
          ) : (
            <div className="mt-3 overflow-x-auto">
              <table className="w-full text-left text-xs">
                <thead>
                  <tr className="border-b border-zinc-800 text-zinc-500">
                    <th className="py-2 pr-4 font-medium">closed</th>
                    <th className="py-2 pr-4 font-medium">symbol</th>
                    <th className="py-2 pr-4 font-medium">side</th>
                    <th className="py-2 pr-4 font-medium">lots</th>
                    <th className="py-2 pr-4 font-medium">in → out</th>
                    <th className="py-2 pr-4 font-medium">P/L</th>
                  </tr>
                </thead>
                <tbody>
                  {history.map((h) => (
                    <tr key={h.position_id} className="border-b border-zinc-800/60">
                      <td className="py-2 pr-4 font-mono text-zinc-400">
                        {h.close_time ? new Date(h.close_time).toLocaleDateString() : "—"}
                      </td>
                      <td className="py-2 pr-4 font-medium text-zinc-200">{h.symbol}</td>
                      <td
                        className={`py-2 pr-4 font-semibold ${
                          h.type === "BUY" ? "text-emerald-400" : "text-red-400"
                        }`}
                      >
                        {h.type}
                      </td>
                      <td className="py-2 pr-4 font-mono text-zinc-300">{h.open_volume}</td>
                      <td className="py-2 pr-4 font-mono text-zinc-400">
                        {fmt(h.open_price)} → {fmt(h.close_price)}
                      </td>
                      <td
                        className={`py-2 pr-4 font-mono font-semibold ${
                          (h.profit ?? 0) >= 0 ? "text-emerald-400" : "text-red-400"
                        }`}
                      >
                        {fmt(h.profit)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      )}
    </div>
  );
}
