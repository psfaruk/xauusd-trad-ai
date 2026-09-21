import { useEffect, useState } from "react";
import { postMt5Connect, postMt5Disconnect, ApiError } from "../lib/api";
import type { BrokerConnection, Mt5Status } from "../types";

/**
 * Broker connection dialog (D-037): the user connects THEIR OWN MetaTrader 5
 * broker account (e.g. Exness demo). The backend verifies the entered
 * (server, login) against the REAL terminal session and binds the user —
 * balance / positions / history / orders / AI auto-trade then serve only
 * THAT user's connection (per-user trade isolation).
 */

interface Mt5ConnectDialogProps {
  open: boolean;
  onClose: () => void;
  token: string;
  broker: BrokerConnection | null;
  onConnected: (st: Mt5Status) => void;
  onDisconnected: (st: Mt5Status) => void;
}

const inputCls =
  "w-full rounded-md border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm text-zinc-100 placeholder-zinc-600 focus:border-gold/60 focus:outline-none";

export default function Mt5ConnectDialog({
  open, onClose, token, broker, onConnected, onDisconnected,
}: Mt5ConnectDialogProps) {
  const [server, setServer] = useState("Exness-MT5Trial6");
  const [login, setLogin] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (open) setError(null);
  }, [open]);

  if (!open) return null;

  const connected = broker?.status === "connected";
  const acct = broker?.account ?? null;

  const handleConnect = async () => {
    setBusy(true);
    setError(null);
    try {
      const st = await postMt5Connect(token, {
        server,
        login,
        password,
      });
      onConnected(st);
    } catch (exc) {
      const msg =
        exc instanceof ApiError ? exc.message : "Connection failed — retry";
      setError(msg);
    } finally {
      setBusy(false);
    }
  };

  const handleDisconnect = async () => {
    setBusy(true);
    setError(null);
    try {
      const st = await postMt5Disconnect(token);
      onDisconnected(st);
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : "Disconnect failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 grid place-items-center bg-black/70 p-4"
      role="dialog"
      aria-modal="true"
      aria-label="Connect your MetaTrader 5 broker account"
      onClick={(e) => e.target === e.currentTarget && onClose()}
    >
      <div className="max-h-[92vh] w-full max-w-md overflow-y-auto rounded-xl border border-zinc-800 bg-zinc-950 p-5 shadow-2xl">
        <div className="mb-4 flex items-start justify-between gap-4">
          <div>
            <h2 className="text-base font-semibold text-zinc-100">
              Connect <span className="text-gold">broker account</span>
            </h2>
            <p className="mt-0.5 text-xs text-zinc-500">
              Your Exness MetaTrader 5 account · per-user, encrypted at rest
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="rounded-md border border-zinc-800 px-2 py-0.5 text-xs text-zinc-400 hover:text-zinc-200"
          >
            ✕
          </button>
        </div>

        {connected ? (
          <div className="space-y-3">
            <div className="flex items-center gap-2 rounded-md border border-emerald-500/30 bg-emerald-500/10 px-3 py-2 text-xs text-emerald-300">
              <span className="inline-block h-2 w-2 animate-pulse rounded-full bg-emerald-400" />
              Connected as {broker?.login ?? acct?.login ?? "—"} @{" "}
              {broker?.server ?? acct?.server ?? "—"}
            </div>
            <div className="grid grid-cols-2 gap-2 text-xs text-zinc-400">
              <span>balance <b className="text-zinc-200">{acct?.balance ?? "—"}</b></span>
              <span>equity <b className="text-zinc-200">{acct?.equity ?? "—"}</b></span>
              <span>broker <b className="text-zinc-200">{acct?.broker ?? "—"}</b></span>
              <span>currency <b className="text-zinc-200">{acct?.currency ?? "USD"}</b></span>
            </div>
            <p className="rounded-md border border-zinc-800 bg-zinc-900/60 px-3 py-2 text-[11px] leading-relaxed text-zinc-500">
              Your balance, positions, trade history and AI auto-trade now run
              on this account — visible only inside your session.
            </p>
            <button
              type="button"
              disabled={busy}
              onClick={handleDisconnect}
              className="w-full rounded-md border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm font-medium text-red-300 hover:bg-red-500/20 disabled:opacity-50"
            >
              {busy ? "working…" : "Disconnect"}
            </button>
          </div>
        ) : (
          <form
            className="space-y-3"
            onSubmit={(e) => {
              e.preventDefault();
              if (!busy) handleConnect();
            }}
          >
            <label className="block text-xs text-zinc-400">
              Server
              <input
                className={`${inputCls} mt-1`}
                value={server}
                onChange={(e) => setServer(e.target.value)}
                placeholder="Exness-MT5Trial6"
                required
              />
            </label>
            <label className="block text-xs text-zinc-400">
              Login (account number)
              <input
                className={`${inputCls} mt-1`}
                value={login}
                onChange={(e) => setLogin(e.target.value)}
                placeholder="e.g. 414350770"
                inputMode="numeric"
                required
              />
            </label>
            <label className="block text-xs text-zinc-400">
              Password
              <input
                className={`${inputCls} mt-1`}
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="••••••••"
                required
              />
            </label>
            {error && (
              <p className="rounded-md border border-red-500/40 bg-red-500/10 px-3 py-2 text-xs leading-relaxed text-red-300">
                {error}
              </p>
            )}
            <button
              type="submit"
              disabled={busy || !login || !password || !server}
              className="w-full rounded-md border border-gold/50 bg-gold/15 px-3 py-2 text-sm font-semibold text-gold hover:bg-gold/25 disabled:opacity-50"
            >
              {busy ? "connecting…" : "Connect broker"}
            </button>
            <p className="text-center text-[11px] leading-relaxed text-zinc-600">
              Verified against the real MetaTrader 5 terminal session — only
              accounts provisioned on this host can connect.
            </p>
          </form>
        )}
      </div>
    </div>
  );
}
