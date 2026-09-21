import { useAuth } from "../lib/auth";
import type { BrokerConnection, Mt5Status } from "../types";

interface SettingsViewProps {
  token: string;
  broker: BrokerConnection | null;
  mt5: Mt5Status | null;
  isAdmin: boolean;
  dataSource: string;
  wsState: "connecting" | "open" | "closed";
  onConnectBroker: () => void;
  onOpenEngineSettings: () => void;
  onOpenPractice: () => void;
  onOpenLogs: () => void;
}

const card =
  "rounded-2xl border border-zinc-800 bg-zinc-900/60 p-4 transition-colors hover:border-zinc-700";
const label = "text-[10px] font-semibold uppercase tracking-widest text-zinc-500";
const btn =
  "rounded-lg border px-3 py-2 text-xs font-semibold transition-colors disabled:opacity-50";

/**
 * D-039 — Settings tab: broker connection, engine config (admin), practice
 * account (clearly optional), platform logs and account/sign-out. One clear
 * place, no duplicates elsewhere (user req: functions live under tabs).
 */
export default function SettingsView({
  broker, mt5, isAdmin, dataSource, wsState, onConnectBroker,
  onOpenEngineSettings, onOpenPractice, onOpenLogs,
}: SettingsViewProps) {
  const { session, signOut } = useAuth();
  const brokerConnected = broker?.status === "connected";
  const email = session?.user?.email ?? "—";

  return (
    <div className="grid gap-4 sm:grid-cols-2">
      {/* broker connection */}
      <section className={card}>
        <p className={label}>broker connection</p>
        {brokerConnected ? (
          <>
            <p className="mt-2 text-sm text-zinc-300">
              Connected as{" "}
              <b className="text-zinc-100">
                {broker?.login} @ {broker?.server}
              </b>
            </p>
            <p className="mt-1 text-xs leading-relaxed text-zinc-500">
              Balance, positions, history, manual orders and AI auto-trade all
              run on this account — visible only in your session.
            </p>
          </>
        ) : (
          <>
            <p className="mt-2 text-sm leading-relaxed text-zinc-400">
              Connect your Exness MetaTrader 5 account. You only need the same
              login · password · server you use in the MT5 app.
            </p>
            <button
              type="button"
              onClick={onConnectBroker}
              className={`${btn} mt-3 w-full border-gold/50 bg-gold/15 text-gold hover:bg-gold/25`}
            >
              Connect broker account
            </button>
          </>
        )}
      </section>

      {/* engine settings */}
      <section className={card}>
        <p className={label}>engine settings</p>
        <p className="mt-2 text-sm leading-relaxed text-zinc-400">
          AI strategy parameters — timeframes, EMA/RSI/ATR, risk % per trade,
          kill switches.
        </p>
        <button
          type="button"
          onClick={onOpenEngineSettings}
          disabled={!isAdmin}
          className={`${btn} mt-3 w-full ${
            isAdmin
              ? "border-zinc-700 bg-zinc-800/60 text-zinc-200 hover:border-gold/40 hover:text-gold"
              : "border-zinc-800 text-zinc-600"
          }`}
        >
          {isAdmin ? "Open engine settings" : "Admin only"}
        </button>
      </section>

      {/* practice trading */}
      <section className={card}>
        <p className={label}>practice trading (paper) · optional</p>
        <p className="mt-2 text-sm leading-relaxed text-zinc-400">
          Simulated trades on live prices — no real money. Useful for testing
          the AI signals before connecting a real account.
        </p>
        <button
          type="button"
          onClick={onOpenPractice}
          className={`${btn} mt-3 w-full border-zinc-700 bg-zinc-800/60 text-zinc-200 hover:border-gold/40 hover:text-gold`}
        >
          Open practice account
        </button>
      </section>

      {/* platform logs */}
      <section className={card}>
        <p className={label}>platform logs</p>
        <p className="mt-2 text-sm leading-relaxed text-zinc-400">
          Engine activity, signal traces, execution events — CSV export
          included.
        </p>
        <button
          type="button"
          onClick={onOpenLogs}
          className={`${btn} mt-3 w-full border-zinc-700 bg-zinc-800/60 text-zinc-200 hover:border-gold/40 hover:text-gold`}
        >
          Open log viewer
        </button>
      </section>

      {/* system status */}
      <section className={card}>
        <p className={label}>system status</p>
        <ul className="mt-2 space-y-1.5 text-xs text-zinc-400">
          <li className="flex items-center justify-between">
            data source
            <b className="font-mono text-zinc-200">
              {dataSource === "live" ? "real market · MT5 only" : dataSource || "—"}
            </b>
          </li>
          <li className="flex items-center justify-between">
            engine
            <b className={mt5?.engine_running ? "text-emerald-400" : "text-zinc-500"}>
              {mt5?.engine_running ? "running" : "stopped"}
            </b>
          </li>
          <li className="flex items-center justify-between">
            websocket
            <b className={wsState === "open" ? "text-emerald-400" : "text-amber-400"}>
              {wsState}
            </b>
          </li>
          <li className="flex items-center justify-between">
            bridge
            <b className="font-mono text-zinc-200">{mt5?.status ?? "—"}</b>
          </li>
        </ul>
      </section>

      {/* account */}
      <section className={card}>
        <p className={label}>account</p>
        <p className="mt-2 truncate text-sm text-zinc-300">{email}</p>
        <p className="mt-1 text-xs text-zinc-500">
          Role: {isAdmin ? "admin" : "viewer"}
        </p>
        <button
          type="button"
          onClick={() => void signOut()}
          className={`${btn} mt-3 w-full border-red-500/40 bg-red-500/10 text-red-300 hover:bg-red-500/20`}
        >
          Sign out
        </button>
      </section>
    </div>
  );
}
