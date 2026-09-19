import { useEffect, useState } from "react";
import { postMt5Connect, postMt5Disconnect, ApiError } from "../lib/api";
import type { Mt5Status } from "../types";

/**
 * MT5 connection dialog (SPEC §10): server / login / password (+ optional
 * terminal path for a custom MT5 install). Admin-only action; with
 * DATA_SOURCE=mock any credentials work and the demo market connects.
 */

interface Mt5ConnectDialogProps {
  open: boolean;
  onClose: () => void;
  token: string;
  isAdmin: boolean;
  status: Mt5Status | null;
  dataSource: "mock" | "mt5" | "";
  onConnected: (status: Mt5Status) => void;
  onDisconnected: (status: Mt5Status) => void;
}

const inputCls =
  "w-full rounded-md border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm text-zinc-100 placeholder-zinc-600 focus:border-gold/60 focus:outline-none";

export default function Mt5ConnectDialog({
  open, onClose, token, isAdmin, status, dataSource,
  onConnected, onDisconnected,
}: Mt5ConnectDialogProps) {
  const [server, setServer] = useState("Exness-MT5Trial");
  const [login, setLogin] = useState("");
  const [password, setPassword] = useState("");
  const [terminalPath, setTerminalPath] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  useEffect(() => {
    if (open) {
      setError(null);
      setNote(null);
    }
  }, [open]);

  if (!open) return null;

  const connected = status?.status === "connected";

  const handleConnect = async () => {
    setBusy(true);
    setError(null);
    try {
      const st = await postMt5Connect(token, {
        server,
        login,
        password,
        terminal_path: terminalPath || undefined,
      });
      setNote(
        st.symbol
          ? `Connected — symbol discovered: ${st.symbol}`
          : "Connected"
      );
      onConnected(st);
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : "Connection failed");
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
      aria-label="MetaTrader 5 connection"
      onClick={(e) => e.target === e.currentTarget && onClose()}
    >
      <div className="w-full max-w-md rounded-xl border border-zinc-800 bg-zinc-950 p-5 shadow-2xl">
        <div className="mb-4 flex items-start justify-between gap-4">
          <div>
            <h2 className="text-base font-semibold text-zinc-100">
              MetaTrader 5 <span className="text-gold">connection</span>
            </h2>
            <p className="mt-0.5 text-xs text-zinc-500">
              Exness demo account — credentials are Fernet-encrypted server-side
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

        {dataSource === "mock" && (
          <p className="mb-3 rounded-md border border-gold/30 bg-gold/10 px-3 py-2 text-xs text-gold/90">
            Demo mode (DATA_SOURCE=mock): any values connect — the chart streams
            deterministic synthetic market data.
          </p>
        )}

        {connected ? (
          <div className="space-y-3">
            <div className="rounded-md border border-emerald-500/30 bg-emerald-500/10 px-3 py-2 text-xs text-emerald-300">
              Connected as {status?.account?.login ?? "—"} @ {status?.account?.server ?? "—"}
              {status?.symbol ? ` · symbol ${status.symbol}` : ""}
            </div>
            <div className="grid grid-cols-2 gap-2 text-xs text-zinc-400">
              <span>balance <b className="text-zinc-200">{status?.account?.balance ?? "—"}</b></span>
              <span>equity <b className="text-zinc-200">{status?.account?.equity ?? "—"}</b></span>
              <span>broker UTC offset <b className="text-zinc-200">{status?.broker_time_utc_offset ?? 0}m</b></span>
              <span>engine <b className="text-zinc-200">{status?.engine_running ? "running" : "stopped"}</b></span>
            </div>
            {isAdmin && (
              <button
                type="button"
                disabled={busy}
                onClick={handleDisconnect}
                className="w-full rounded-md border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm font-medium text-red-300 hover:bg-red-500/20 disabled:opacity-50"
              >
                {busy ? "working…" : "Disconnect"}
              </button>
            )}
          </div>
        ) : (
          <form
            className="space-y-3"
            onSubmit={(e) => {
              e.preventDefault();
              if (isAdmin && !busy) handleConnect();
            }}
          >
            <label className="block text-xs text-zinc-400">
              Server
              <input
                className={`${inputCls} mt-1`}
                value={server}
                onChange={(e) => setServer(e.target.value)}
                placeholder="Exness-MT5Trial"
                required
              />
            </label>
            <label className="block text-xs text-zinc-400">
              Login (account number)
              <input
                className={`${inputCls} mt-1`}
                value={login}
                onChange={(e) => setLogin(e.target.value)}
                placeholder="e.g. 15001234"
                inputMode="numeric"
                required
              />
            </label>
            <label className="block text-xs text-zinc-400">
              Investor password
              <input
                className={`${inputCls} mt-1`}
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="••••••••"
                required
              />
            </label>
            <label className="block text-xs text-zinc-400">
              Terminal path <span className="text-zinc-600">(optional, Windows VPS)</span>
              <input
                className={`${inputCls} mt-1`}
                value={terminalPath}
                onChange={(e) => setTerminalPath(e.target.value)}
                placeholder="C:\Program Files\MetaTrader 5\terminal64.exe"
              />
            </label>
            {error && (
              <p className="rounded-md border border-red-500/40 bg-red-500/10 px-3 py-2 text-xs text-red-300">
                {error}
              </p>
            )}
            {note && (
              <p className="rounded-md border border-emerald-500/30 bg-emerald-500/10 px-3 py-2 text-xs text-emerald-300">
                {note}
              </p>
            )}
            <button
              type="submit"
              disabled={busy || !isAdmin}
              className="w-full rounded-md border border-gold/50 bg-gold/15 px-3 py-2 text-sm font-semibold text-gold hover:bg-gold/25 disabled:opacity-50"
            >
              {busy ? "connecting…" : "Connect"}
            </button>
            {!isAdmin && (
              <p className="text-center text-xs text-zinc-500">Admin role required</p>
            )}
          </form>
        )}
      </div>
    </div>
  );
}
