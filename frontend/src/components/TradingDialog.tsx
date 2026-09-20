import { useEffect, useState } from "react";
import {
  getTradingStatus,
  postTradingAutoTrade,
  postTradingConnect,
  postTradingDisconnect,
} from "../lib/api";
import type { TradingStatus } from "../types";

interface TradingDialogProps {
  open: boolean;
  onClose: () => void;
  token: string;
  status: TradingStatus | null;
  onStatusChange: (st: TradingStatus) => void;
}

/**
 * TradingDialog — connect YOUR OWN MT5/Exness account (agent flow, user req #1).
 * Everyone sees the same market data; trading happens on each user's private
 * plane. Credentials are Fernet-encrypted server-side and never echoed back
 * (masked login only, SPEC §13).
 */
export default function TradingDialog({
  open, onClose, token, status, onStatusChange,
}: TradingDialogProps) {
  const [server, setServer] = useState("Exness-MT5Trial");
  const [login, setLogin] = useState("");
  const [password, setPassword] = useState("");
  const [mode, setMode] = useState<"demo" | "live">("demo");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [confirmText, setConfirmText] = useState("");

  useEffect(() => {
    if (!open) {
      setError(null);
      setNotice(null);
      setConfirmText("");
    }
  }, [open]);

  if (!open) return null;

  const connected = status?.connected === true;

  const refresh = async () => {
    try {
      onStatusChange(await getTradingStatus(token));
    } catch { /* transient */ }
  };

  const connect = async () => {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const st = await postTradingConnect(token, { server, login, password, mode });
      onStatusChange(st);
      if (st.connected) {
        setNotice(`Connected (${st.mode}) — login ${st.login_masked}`);
        setPassword("");
      } else if (st.status === "bridge_required") {
        setNotice(st.detail ?? "live bridge required");
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "connect failed");
    } finally {
      setBusy(false);
    }
  };

  const disconnect = async () => {
    setBusy(true);
    try {
      await postTradingDisconnect(token);
      await refresh();
      setNotice("Trading plane disconnected");
    } catch (e) {
      setError(e instanceof Error ? e.message : "disconnect failed");
    } finally {
      setBusy(false);
    }
  };

  const arm = async (enabled: boolean) => {
    setBusy(true);
    setError(null);
    try {
      await postTradingAutoTrade(token, enabled, enabled ? confirmText : undefined);
      setConfirmText("");
      await refresh();
      setNotice(enabled ? "Auto-trade ARMED for your account" : "Auto-trade disarmed");
    } catch (e) {
      setError(e instanceof Error ? e.message : "failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="fixed inset-0 z-40 flex items-center justify-center bg-black/70 p-4" onClick={onClose}>
      <div
        className="w-full max-w-lg rounded-2xl border border-zinc-700 bg-zinc-900 p-5 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-base font-semibold text-zinc-100">My Trading Account</h2>
          <button type="button" onClick={onClose} className="text-zinc-500 hover:text-zinc-300">✕</button>
        </div>

        {/* status card */}
        <div className="mb-4 rounded-xl border border-zinc-800 bg-zinc-950/60 p-3.5 text-sm">
          <div className="flex items-center gap-2">
            <span className={`inline-block h-2 w-2 rounded-full ${connected ? "bg-emerald-500" : "bg-zinc-600"}`} />
            <span className="font-medium text-zinc-200">
              {connected ? `Connected · ${status?.mode}` : status?.status === "bridge_required" ? "Live credentials stored (bridge pending)" : "Not connected"}
            </span>
            {status?.login_masked && <span className="ml-auto font-mono text-xs text-zinc-400">{status.login_masked}</span>}
          </div>
          {connected && status?.account && (
            <div className="mt-2 grid grid-cols-3 gap-2 text-xs text-zinc-400">
              <span>balance <b className="text-zinc-200">{status.account.balance.toFixed(2)}</b></span>
              <span>equity <b className="text-zinc-200">{status.account.equity.toFixed(2)}</b></span>
              <span className="ml-auto">
                auto-trade{" "}
                <b className={status.auto_trade ? "text-amber-400" : "text-zinc-500"}>
                  {status.auto_trade ? "ARMED" : "OFF"}
                </b>
              </span>
            </div>
          )}
        </div>

        {!connected && (
          <div className="space-y-3">
            <p className="text-xs leading-relaxed text-zinc-400">
              Everyone watches the same live market — but trades execute on your own
              account. Demo mode gives you a $10,000 paper account priced off the live
              feed; live mode connects through the MT5 bridge.
            </p>
            <div className="grid grid-cols-2 gap-3">
              <label className="text-xs text-zinc-400">
                Server
                <input
                  value={server}
                  onChange={(e) => setServer(e.target.value)}
                  className="mt-1 w-full rounded-md border border-zinc-700 bg-zinc-950 px-2.5 py-1.5 text-sm text-zinc-200 focus:border-gold/60 focus:outline-none"
                  placeholder="Exness-MT5Trial"
                />
              </label>
              <label className="text-xs text-zinc-400">
                MT5 Login
                <input
                  value={login}
                  onChange={(e) => setLogin(e.target.value)}
                  inputMode="numeric"
                  className="mt-1 w-full rounded-md border border-zinc-700 bg-zinc-950 px-2.5 py-1.5 text-sm text-zinc-200 focus:border-gold/60 focus:outline-none"
                  placeholder="e.g. 10001234"
                />
              </label>
            </div>
            <label className="block text-xs text-zinc-400">
              Password (encrypted, never shown again)
              <input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                className="mt-1 w-full rounded-md border border-zinc-700 bg-zinc-950 px-2.5 py-1.5 text-sm text-zinc-200 focus:border-gold/60 focus:outline-none"
                placeholder="••••••••"
              />
            </label>
            <div className="flex gap-2">
              {(["demo", "live"] as const).map((m) => (
                <button
                  key={m}
                  type="button"
                  onClick={() => setMode(m)}
                  className={`flex-1 rounded-md border px-3 py-1.5 text-xs font-medium ${
                    mode === m
                      ? m === "demo"
                        ? "border-emerald-500/50 bg-emerald-500/15 text-emerald-300"
                        : "border-gold/50 bg-gold/15 text-gold"
                      : "border-zinc-700 bg-zinc-950 text-zinc-400"
                  }`}
                >
                  {m === "demo" ? "Demo (paper $10k)" : "Live (bridge)"}
                </button>
              ))}
            </div>
            <button
              type="button"
              disabled={busy || !login || !password}
              onClick={connect}
              className="w-full rounded-md border border-gold/50 bg-gold/15 px-3 py-2 text-sm font-semibold text-gold hover:bg-gold/25 disabled:opacity-40"
            >
              {busy ? "connecting…" : "Connect my account"}
            </button>
          </div>
        )}

        {connected && (
          <div className="space-y-3">
            <div className="rounded-lg border border-amber-500/30 bg-amber-500/5 p-3">
              <p className="text-xs font-medium text-amber-300">Auto-trade (my account)</p>
              <p className="mt-1 text-[11px] leading-relaxed text-zinc-400">
                When armed, every AI signal is copied to your account with your risk
                settings. Type <b className="text-amber-300">ENABLE</b> to arm.
              </p>
              <div className="mt-2 flex gap-2">
                <input
                  value={confirmText}
                  onChange={(e) => setConfirmText(e.target.value)}
                  placeholder="ENABLE"
                  className="w-32 rounded-md border border-zinc-700 bg-zinc-950 px-2.5 py-1.5 text-sm text-zinc-200 focus:outline-none"
                />
                <button
                  type="button"
                  disabled={busy || confirmText !== "ENABLE"}
                  onClick={() => arm(true)}
                  className="rounded-md border border-amber-500/50 bg-amber-500/15 px-3 py-1.5 text-xs font-semibold text-amber-300 hover:bg-amber-500/25 disabled:opacity-40"
                >
                  ARM auto-trade
                </button>
                {status?.auto_trade && (
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => arm(false)}
                    className="ml-auto rounded-md border border-zinc-700 px-3 py-1.5 text-xs text-zinc-300 hover:border-zinc-500"
                  >
                    disarm
                  </button>
                )}
              </div>
            </div>
            <button
              type="button"
              disabled={busy}
              onClick={disconnect}
              className="w-full rounded-md border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm font-medium text-red-300 hover:bg-red-500/20"
            >
              Disconnect my account
            </button>
          </div>
        )}

        {error && <p className="mt-3 text-xs text-red-400">{error}</p>}
        {notice && <p className="mt-3 text-xs text-emerald-400">{notice}</p>}
      </div>
    </div>
  );
}
