/**
 * SettingsView (D-041) — every former 3-dot-menu function lives in a tab now;
 * this one hosts: account, broker connection (the ONE connect flow), engine
 * settings (admin), practice trading (paper, optional), logs and about.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  getConfig, getLogs, getTradingStatus, getTradingTrades, postMt5Connect,
  postMt5Disconnect, postTradingAutoTrade, postTradingConnect,
  postTradingDisconnect, putConfig,
} from "../lib/api";
import type {
  BrokerConnection, EngineConfig, LogEntry, Mt5Account, Mt5Status,
  TradeRecord, TradingStatus,
} from "../types";
import { Badge, Btn, Card, Dot, EmptyState, Field, SectionTitle, Stat, inputCls } from "../components/ui";
import { useAuth } from "../lib/auth";

interface Props {
  token: string;
  mt5: Mt5Status | null;
  broker: BrokerConnection | null;
  brokerAccount: Mt5Account | null;
  isAdmin: boolean;
  dataSource: string;
  engineLogs: { level: string; message: string }[];
  onBrokerConnected: () => void;
}

/* -------------------------------------------------------- broker connect */

function BrokerCard({
  token,
  broker,
  account,
  onConnected,
}: {
  token: string;
  broker: BrokerConnection | null;
  account: Mt5Account | null;
  onConnected: () => void;
}) {
  const connected = broker?.status === "connected" || account?.connected === true;
  const [server, setServer] = useState("Exness-MT5Trial6");
  const [login, setLogin] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const connect = async () => {
    setBusy(true);
    setError(null);
    try {
      await postMt5Connect(token, { server, login, password });
      onConnected();
    } catch (e) {
      setError(e instanceof Error ? e.message : "connect failed");
    } finally {
      setBusy(false);
    }
  };

  const disconnect = async () => {
    setBusy(true);
    setError(null);
    try {
      await postMt5Disconnect(token);
      onConnected();
    } catch (e) {
      setError(e instanceof Error ? e.message : "disconnect failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card>
      <SectionTitle
        title="Broker Account"
        right={
          connected ? (
            <span className="flex items-center gap-1.5 text-[10px] font-semibold text-emerald-400">
              <Dot tone="green" /> connected
            </span>
          ) : broker?.status === "reconnecting" ? (
            <span className="flex items-center gap-1.5 text-[10px] font-semibold text-amber-400">
              <Dot tone="amber" /> reconnecting…
            </span>
          ) : (
            <span className="flex items-center gap-1.5 text-[10px] font-semibold text-zinc-500">
              <Dot tone="zinc" /> not connected
            </span>
          )
        }
      />
      {connected ? (
        <>
          <div className="grid grid-cols-2 gap-2">
            <Stat label="Balance" value={account?.balance?.toFixed(2) ?? "—"} hint={account?.currency ?? undefined} />
            <Stat label="Equity" value={account?.equity?.toFixed(2) ?? "—"} />
            <Stat label="Leverage" value={account?.leverage ? `1:${account.leverage}` : "—"} />
            <Stat label="Type" value={account?.type ?? "—"} />
          </div>
          <p className="mt-2.5 truncate text-[10px] text-zinc-500">
            {account?.broker ?? broker?.server ?? "—"}
            {account?.login != null && ` · #${account.login}`}
            {account?.server != null && ` · ${account.server}`}
          </p>
          <div className="mt-3">
            <Btn variant="danger" onClick={() => void disconnect()} disabled={busy} className="w-full">
              {busy ? "Disconnecting…" : "Disconnect"}
            </Btn>
          </div>
        </>
      ) : (
        <>
          <p className="mb-3 text-[11px] leading-relaxed text-zinc-400">
            Connect your MetaTrader 5 broker account (e.g. Exness). The terminal login
            happens on the server — you only connect once here.
          </p>
          <div className="flex flex-col gap-2">
            <Field label="Server">
              <input className={inputCls} value={server} onChange={(e) => setServer(e.target.value)} placeholder="Exness-MT5Trial6" />
            </Field>
            <div className="grid grid-cols-2 gap-2">
              <Field label="Login">
                <input className={inputCls} value={login} onChange={(e) => setLogin(e.target.value)} inputMode="numeric" placeholder="414350770" />
              </Field>
              <Field label="Password">
                <input className={inputCls} type="password" value={password} onChange={(e) => setPassword(e.target.value)} placeholder="••••••••" />
              </Field>
            </div>
            <Btn variant="gold" onClick={() => void connect()} disabled={busy || !login || !password || !server} className="mt-1 w-full">
              {busy ? "Connecting…" : "Connect Broker"}
            </Btn>
          </div>
          {error && (
            <p className="mt-2.5 break-words rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-[11px] leading-relaxed text-red-300">
              {error}
            </p>
          )}
        </>
      )}
    </Card>
  );
}

/* -------------------------------------------------------- engine settings */

const ENGINE_NUM_FIELDS: { key: keyof EngineConfig; label: string; hint?: string; step?: string }[] = [
  { key: "rr", label: "Risk:Reward", hint: "TP distance = RR × SL distance", step: "0.1" },
  { key: "min_sl_atr", label: "Min SL (×ATR)", hint: "stop never closer than this — spread floor", step: "0.1" },
  { key: "min_atr", label: "Min ATR", hint: "skip dead markets", step: "0.05" },
  { key: "risk_percent", label: "Risk %", step: "0.1" },
  { key: "fixed_lot", label: "Fixed lot", step: "0.01" },
  { key: "max_spread_points", label: "Max spread (points)" },
  { key: "cooldown_bars", label: "Cooldown (bars)" },
  { key: "expiry_bars", label: "Expiry (bars)" },
];

function EngineCard({ token, isAdmin }: { token: string; isAdmin: boolean }) {
  const [cfg, setCfg] = useState<EngineConfig | null>(null);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getConfig(token)
      .then((r) => setCfg(r.config))
      .catch(() => setCfg(null));
  }, [token]);

  const patch = (patchObj: Partial<EngineConfig>) =>
    setCfg((c) => (c ? { ...c, ...patchObj } : c));

  const save = async () => {
    if (!cfg) return;
    setBusy(true);
    setError(null);
    setSaved(false);
    try {
      await putConfig(token, cfg);
      setSaved(true);
      window.setTimeout(() => setSaved(false), 2500);
    } catch (e) {
      setError(e instanceof Error ? e.message : "save failed");
    } finally {
      setBusy(false);
    }
  };

  if (!cfg) {
    return (
      <Card>
        <SectionTitle title="AI Engine Settings" />
        <div className="flex flex-col gap-2">
          <div className="h-4 w-2/3 animate-pulse rounded bg-zinc-800" />
          <div className="h-4 w-1/2 animate-pulse rounded bg-zinc-800" />
        </div>
      </Card>
    );
  }

  return (
    <Card>
      <SectionTitle
        title="AI Engine Settings"
        right={
          <Badge tone="blue">
            {cfg.timeframe} · {(cfg.confirm_tfs ?? []).join("+")} confirm · {cfg.trend_tf} trend
          </Badge>
        }
      />
      <p className="mb-3 text-[11px] leading-relaxed text-zinc-400">
        The engine trades the <b className="text-zinc-200">{cfg.timeframe}</b> timeframe:
        {" "}{cfg.trend_tf} sets the trend, {(cfg.confirm_tfs ?? []).join(" + ") || "higher TFs"} must
        confirm, then an M1 pattern (liquidity sweep or trend pullback) triggers the entry.
      </p>

      <div className="grid grid-cols-2 gap-2">
        {ENGINE_NUM_FIELDS.map((f) => (
          <Field key={String(f.key)} label={f.label} hint={f.hint}>
            <input
              className={inputCls}
              value={String(cfg[f.key] ?? "")}
              inputMode="decimal"
              disabled={!isAdmin}
              onChange={(e) => {
                const v = parseFloat(e.target.value);
                if (!Number.isNaN(v)) patch({ [f.key]: v } as Partial<EngineConfig>);
              }}
            />
          </Field>
        ))}
      </div>

      <div className="mt-3 flex flex-wrap gap-2">
        <Btn
          variant={cfg.pullback_enabled ? "gold" : "default"}
          disabled={!isAdmin}
          onClick={() => patch({ pullback_enabled: !cfg.pullback_enabled })}
        >
          Pullback trigger: {cfg.pullback_enabled ? "ON" : "OFF"}
        </Btn>
        <Btn
          variant={cfg.risk_mode === "percent" ? "gold" : "default"}
          disabled={!isAdmin}
          onClick={() => patch({ risk_mode: cfg.risk_mode === "percent" ? "fixed" : "percent" })}
        >
          Risk mode: {cfg.risk_mode}
        </Btn>
      </div>

      {!isAdmin && (
        <p className="mt-3 text-[10px] text-zinc-500">Read-only — admin access required to change engine settings.</p>
      )}
      {isAdmin && (
        <div className="mt-3.5">
          <Btn variant="gold" onClick={() => void save()} disabled={busy} className="w-full">
            {busy ? "Saving…" : saved ? "Saved ✓" : "Save settings"}
          </Btn>
        </div>
      )}
      {error && (
        <p className="mt-2.5 break-words rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-[11px] text-red-300">{error}</p>
      )}
    </Card>
  );
}

/* ------------------------------------------------------- practice trading */

function PracticeCard({ token }: { token: string }) {
  const [status, setStatus] = useState<TradingStatus | null>(null);
  const [login, setLogin] = useState("");
  const [password, setPassword] = useState("");
  const [server, setServer] = useState("Exness-MT5Trial6");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [trades, setTrades] = useState<TradeRecord[] | null>(null);

  const reload = useCallback(() => {
    getTradingStatus(token).then(setStatus).catch(() => setStatus(null));
    getTradingTrades(token, 10).then((r) => setTrades(r.trades)).catch(() => setTrades([]));
  }, [token]);
  useEffect(reload, [reload]);

  const connected = status?.connected === true;

  const connect = async () => {
    setBusy(true);
    setError(null);
    try {
      await postTradingConnect(token, { server, login, password, mode: "demo" });
      reload();
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
      reload();
    } finally {
      setBusy(false);
    }
  };

  const toggleAuto = async () => {
    if (!status) return;
    setBusy(true);
    try {
      const enable = !status.auto_trade;
      await postTradingAutoTrade(token, enable, enable ? "ENABLE" : undefined);
      reload();
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card>
      <SectionTitle
        title="Practice Trading (Paper)"
        right={
          connected ? (
            <span className="flex items-center gap-1.5 text-[10px] font-semibold text-emerald-400">
              <Dot tone="green" /> active
            </span>
          ) : (
            <Badge tone="zinc">optional</Badge>
          )
        }
      />
      <p className="mb-3 text-[11px] leading-relaxed text-zinc-400">
        Simulated (paper) account for testing the AI signals without real money —
        completely optional, your broker account above is separate.
      </p>
      {connected ? (
        <>
          <div className="grid grid-cols-2 gap-2">
            <Stat label="Paper balance" value={status?.account?.balance?.toFixed(2) ?? "—"} hint={status?.account?.currency} />
            <Stat label="Paper equity" value={status?.account?.equity?.toFixed(2) ?? "—"} />
          </div>
          <div className="mt-3 grid grid-cols-2 gap-2">
            <Btn variant={status?.auto_trade ? "success" : "default"} onClick={() => void toggleAuto()} disabled={busy}>
              Copy signals: {status?.auto_trade ? "ON" : "OFF"}
            </Btn>
            <Btn variant="danger" onClick={() => void disconnect()} disabled={busy}>
              Disconnect
            </Btn>
          </div>
          {trades && trades.length > 0 && (
            <ul className="mt-3 flex max-h-40 min-w-0 flex-col gap-1.5 overflow-y-auto pr-1">
              {trades.map((t, i) => (
                <li key={i} className="flex items-center gap-2 rounded-lg border border-zinc-800/70 bg-zinc-900/40 px-3 py-1.5 text-[11px]">
                  <Badge tone={t.side.toLowerCase().includes("buy") ? "green" : "red"}>{t.side}</Badge>
                  <span className="min-w-0 flex-1 truncate font-mono text-zinc-400 tabular-nums">
                    {t.volume} @ {t.price_open?.toFixed(2)}
                    {t.price_close != null && ` → ${t.price_close.toFixed(2)}`}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </>
      ) : (
        <div className="flex flex-col gap-2">
          <Field label="Server">
            <input className={inputCls} value={server} onChange={(e) => setServer(e.target.value)} />
          </Field>
          <div className="grid grid-cols-2 gap-2">
            <Field label="Login">
              <input className={inputCls} value={login} onChange={(e) => setLogin(e.target.value)} inputMode="numeric" />
            </Field>
            <Field label="Password">
              <input className={inputCls} type="password" value={password} onChange={(e) => setPassword(e.target.value)} />
            </Field>
          </div>
          <Btn onClick={() => void connect()} disabled={busy || !login || !password} className="mt-1 w-full">
            {busy ? "Connecting…" : "Start practice account"}
          </Btn>
        </div>
      )}
      {error && <p className="mt-2.5 break-words text-[11px] text-red-300">{error}</p>}
    </Card>
  );
}

/* ------------------------------------------------------------------- logs */

function LogsCard({ token, engineLogs }: { token: string; engineLogs: { level: string; message: string }[] }) {
  const [sysLogs, setSysLogs] = useState<LogEntry[] | null>(null);
  const [showSys, setShowSys] = useState(false);

  useEffect(() => {
    if (!showSys) return;
    getLogs(token, 60)
      .then((r) => setSysLogs(r.logs))
      .catch(() => setSysLogs([]));
  }, [token, showSys]);

  return (
    <Card>
      <SectionTitle
        title="Activity Logs"
        right={
          <Btn variant="ghost" onClick={() => setShowSys((v) => !v)}>
            {showSys ? "Engine only" : "System logs"}
          </Btn>
        }
      />
      {engineLogs.length === 0 && !showSys && (
        <EmptyState title="No engine activity yet" hint="Engine decisions stream here live." />
      )}
      <div className="flex max-h-64 min-w-0 flex-col gap-1 overflow-y-auto pr-1 font-mono text-[10px] leading-relaxed">
        {engineLogs.slice(-40).map((l, i) => (
          <p key={`e${i}`} className="min-w-0 break-words text-zinc-400">
            <span className={l.level === "critical" ? "text-red-400" : l.level === "warning" ? "text-amber-400" : "text-gold/70"}>
              [{l.level}]
            </span>{" "}
            {l.message}
          </p>
        ))}
        {showSys &&
          (sysLogs ?? []).map((l, i) => (
            <p key={`s${i}`} className="min-w-0 break-words text-zinc-500">
              <span className="text-zinc-600">[{l.level}]</span> {l.source}: {l.message}
            </p>
          ))}
      </div>
    </Card>
  );
}

/* ------------------------------------------------------------------- view */

export default function SettingsView({
  token,
  mt5,
  broker,
  brokerAccount,
  isAdmin,
  dataSource,
  engineLogs,
  onBrokerConnected,
}: Props) {
  const { session, signOut } = useAuth();
  const email = useMemo(
    () => session?.user?.email ?? "—",
    [session?.user?.email],
  );

  return (
    <div className="flex min-w-0 flex-col gap-3">
      <BrokerCard token={token} broker={broker} account={brokerAccount} onConnected={onBrokerConnected} />
      <EngineCard token={token} isAdmin={isAdmin} />
      <PracticeCard token={token} />
      <LogsCard token={token} engineLogs={engineLogs} />

      <Card>
        <SectionTitle title="Account" />
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <Badge tone="zinc">{email}</Badge>
          <Badge tone={isAdmin ? "gold" : "blue"}>{isAdmin ? "admin" : "viewer"}</Badge>
          <Badge tone="green">data: {dataSource || mt5?.status || "live"}</Badge>
          <Btn variant="default" onClick={() => void signOut()} className="ml-auto">
            Sign out
          </Btn>
        </div>
      </Card>

      <Card>
        <SectionTitle title="About" />
        <p className="text-[11px] leading-relaxed text-zinc-400">
          XAUUSD AI Trading Platform — real MetaTrader 5 data only (no demo prices).
          Engine: M1 entries with H1 trend + M5/M15 multi-timeframe confirmation.
          Signals and orders execute through your connected broker terminal.
        </p>
      </Card>
    </div>
  );
}
