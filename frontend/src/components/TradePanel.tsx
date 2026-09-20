import { useState } from "react";
import { getTradingTrades, postTradingClose, postTradingOrder } from "../lib/api";
import type { TradingPosition, TradeRecord } from "../types";

interface TradePanelProps {
  open: boolean;
  onClose: () => void;
  token: string;
  connected: boolean;
  positions: TradingPosition[];
  lastPrice: { bid: number; ask: number } | null;
  onPositionsChanged: () => void;
  initialTab?: "trade" | "history";
}

type Tab = "trade" | "history";

/**
 * TradePanel — manual market orders + live positions (close per ticket) +
 * personal trade history, all on the user's OWN isolated plane.
 */
export default function TradePanel({
  open, onClose, token, connected, positions, lastPrice, onPositionsChanged, initialTab = "trade",
}: TradePanelProps) {
  const [tab, setTab] = useState<Tab>(initialTab);
  const [volume, setVolume] = useState("0.10");
  const [sl, setSl] = useState("");
  const [tp, setTp] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [history, setHistory] = useState<TradeRecord[] | null>(null);

  if (!open) return null;

  const vol = parseFloat(volume);

  const order = async (side: "BUY" | "SELL") => {
    if (!Number.isFinite(vol) || vol <= 0) {
      setError("volume must be a positive number");
      return;
    }
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const res = await postTradingOrder(token, {
        side,
        volume: vol,
        sl: sl ? parseFloat(sl) : undefined,
        tp: tp ? parseFloat(tp) : undefined,
      });
      if (res.ok) {
        setNotice(`${side} ${vol} filled @ ${res.price?.toFixed(2)} (ticket ${res.ticket})`);
        onPositionsChanged();
      } else {
        setError(`order rejected: retcode ${res.retcode} — ${res.comment}`);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "order failed");
    } finally {
      setBusy(false);
    }
  };

  const close = async (ticket: number) => {
    setBusy(true);
    setError(null);
    try {
      const res = await postTradingClose(token, ticket);
      if (res.ok) {
        setNotice(`position #${ticket} closed @ ${res.price?.toFixed(2)}`);
        onPositionsChanged();
      } else {
        setError(`close failed: ${res.comment}`);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "close failed");
    } finally {
      setBusy(false);
    }
  };

  const loadHistory = async () => {
    try {
      const res = await getTradingTrades(token, 100);
      setHistory(res.trades);
    } catch {
      setHistory([]);
    }
  };

  const switchTab = (t: Tab) => {
    setTab(t);
    if (t === "history" && history === null) void loadHistory();
  };

  const fmt = (n: number | null | undefined, d = 2) =>
    n === null || n === undefined ? "—" : n.toFixed(d);

  return (
    <div className="fixed inset-0 z-40 flex items-center justify-center bg-black/70 p-4" onClick={onClose}>
      <div
        className="flex max-h-[85vh] w-full max-w-2xl flex-col rounded-2xl border border-zinc-700 bg-zinc-900 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-zinc-800 px-5 py-3">
          <h2 className="text-base font-semibold text-zinc-100">Trade Panel</h2>
          <div className="flex items-center gap-2">
            <div className="flex rounded-md border border-zinc-700 p-0.5 text-xs">
              {(["trade", "history"] as Tab[]).map((t) => (
                <button
                  key={t}
                  type="button"
                  onClick={() => switchTab(t)}
                  className={`rounded px-3 py-1 ${tab === t ? "bg-gold/15 text-gold" : "text-zinc-400 hover:text-zinc-200"}`}
                >
                  {t === "trade" ? "order + positions" : "history"}
                </button>
              ))}
            </div>
            <button type="button" onClick={onClose} className="text-zinc-500 hover:text-zinc-300">✕</button>
          </div>
        </div>

        <div className="flex-1 overflow-y-auto p-5">
          {!connected && (
            <p className="rounded-lg border border-amber-500/30 bg-amber-500/5 p-3 text-xs text-amber-300">
              Connect your trading account first (menu → Trading → My Trading Account).
            </p>
          )}

          {tab === "trade" ? (
            <div className="space-y-5">
              {/* order ticket */}
              <div className="rounded-xl border border-zinc-800 bg-zinc-950/60 p-4">
                <div className="mb-3 flex items-center justify-between text-xs text-zinc-400">
                  <span>Market order · XAUUSD</span>
                  {lastPrice && (
                    <span className="font-mono">
                      bid <b className="text-zinc-200">{lastPrice.bid.toFixed(2)}</b>{" "}
                      / ask <b className="text-zinc-200">{lastPrice.ask.toFixed(2)}</b>
                    </span>
                  )}
                </div>
                <div className="grid grid-cols-3 gap-3">
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
                    onClick={() => order("BUY")}
                    className="rounded-md border border-emerald-500/50 bg-emerald-500/15 px-3 py-2.5 text-sm font-bold text-emerald-300 hover:bg-emerald-500/25 disabled:opacity-40"
                  >
                    BUY / LONG
                  </button>
                  <button
                    type="button"
                    disabled={busy || !connected}
                    onClick={() => order("SELL")}
                    className="rounded-md border border-red-500/50 bg-red-500/15 px-3 py-2.5 text-sm font-bold text-red-300 hover:bg-red-500/25 disabled:opacity-40"
                  >
                    SELL / SHORT
                  </button>
                </div>
              </div>

              {/* open positions */}
              <div>
                <h3 className="mb-2 text-xs font-semibold uppercase tracking-wider text-zinc-500">
                  Open positions ({positions.length})
                </h3>
                {positions.length === 0 ? (
                  <p className="rounded-lg border border-zinc-800 bg-zinc-950/40 p-3 text-xs text-zinc-500">
                    No open positions on your account.
                  </p>
                ) : (
                  <div className="overflow-hidden rounded-lg border border-zinc-800">
                    <table className="w-full text-xs">
                      <thead className="bg-zinc-950/80 text-zinc-500">
                        <tr>
                          <th className="px-3 py-2 text-left font-medium">#</th>
                          <th className="px-3 py-2 text-left font-medium">side</th>
                          <th className="px-3 py-2 text-right font-medium">lots</th>
                          <th className="px-3 py-2 text-right font-medium">open</th>
                          <th className="px-3 py-2 text-right font-medium">SL / TP</th>
                          <th className="px-3 py-2 text-right font-medium">P/L</th>
                          <th className="px-3 py-2" />
                        </tr>
                      </thead>
                      <tbody>
                        {positions.map((p) => (
                          <tr key={p.ticket} className="border-t border-zinc-800 text-zinc-300">
                            <td className="px-3 py-2 font-mono text-zinc-500">{p.ticket}</td>
                            <td className={`px-3 py-2 font-semibold ${p.side === "BUY" ? "text-emerald-400" : "text-red-400"}`}>
                              {p.side}
                            </td>
                            <td className="px-3 py-2 text-right">{p.volume.toFixed(2)}</td>
                            <td className="px-3 py-2 text-right font-mono">{fmt(p.price_open)}</td>
                            <td className="px-3 py-2 text-right font-mono text-zinc-500">
                              {fmt(p.sl)} / {fmt(p.tp)}
                            </td>
                            <td className={`px-3 py-2 text-right font-mono font-semibold ${p.profit >= 0 ? "text-emerald-400" : "text-red-400"}`}>
                              {p.profit >= 0 ? "+" : ""}{fmt(p.profit)}
                            </td>
                            <td className="px-3 py-2 text-right">
                              <button
                                type="button"
                                disabled={busy}
                                onClick={() => close(p.ticket)}
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
            </div>
          ) : (
            /* history tab */
            <div>
              <div className="mb-2 flex items-center justify-between">
                <h3 className="text-xs font-semibold uppercase tracking-wider text-zinc-500">
                  My trades ({history?.length ?? "…"})
                </h3>
                <button
                  type="button"
                  onClick={() => void loadHistory()}
                  className="rounded border border-zinc-700 px-2 py-0.5 text-xs text-zinc-400 hover:border-zinc-500"
                >
                  refresh
                </button>
              </div>
              {history === null ? (
                <p className="p-3 text-xs text-zinc-500">loading…</p>
              ) : history.length === 0 ? (
                <p className="rounded-lg border border-zinc-800 bg-zinc-950/40 p-3 text-xs text-zinc-500">
                  No trades yet — place your first order or arm auto-trade.
                </p>
              ) : (
                <div className="overflow-hidden rounded-lg border border-zinc-800">
                  <table className="w-full text-xs">
                    <thead className="bg-zinc-950/80 text-zinc-500">
                      <tr>
                        <th className="px-3 py-2 text-left font-medium">opened</th>
                        <th className="px-3 py-2 text-left font-medium">side</th>
                        <th className="px-3 py-2 text-left font-medium">source</th>
                        <th className="px-3 py-2 text-right font-medium">lots</th>
                        <th className="px-3 py-2 text-right font-medium">price</th>
                        <th className="px-3 py-2 text-right font-medium">closed</th>
                      </tr>
                    </thead>
                    <tbody>
                      {history.map((t, i) => (
                        <tr key={`${t.ticket}-${i}`} className="border-t border-zinc-800 text-zinc-300">
                          <td className="px-3 py-2 font-mono text-zinc-500">
                            {t.opened_at ? new Date(t.opened_at).toLocaleString() : "—"}
                          </td>
                          <td className={`px-3 py-2 font-semibold ${t.side === "BUY" ? "text-emerald-400" : "text-red-400"}`}>
                            {t.side}
                          </td>
                          <td className="px-3 py-2">
                            <span className={`rounded px-1.5 py-0.5 text-[10px] ${
                              t.signal_id
                                ? "bg-gold/15 text-gold"
                                : "bg-zinc-800 text-zinc-400"
                            }`}>
                              {t.signal_id ? "AI signal" : "manual"}
                            </span>
                          </td>
                          <td className="px-3 py-2 text-right">{t.volume.toFixed(2)}</td>
                          <td className="px-3 py-2 text-right font-mono">{fmt(t.price_open)}</td>
                          <td className="px-3 py-2 text-right font-mono text-zinc-500">
                            {t.price_close ? fmt(t.price_close) : "open"}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          )}
        </div>

        {error && <p className="border-t border-zinc-800 px-5 py-2 text-xs text-red-400">{error}</p>}
        {notice && <p className="border-t border-zinc-800 px-5 py-2 text-xs text-emerald-400">{notice}</p>}
      </div>
    </div>
  );
}
