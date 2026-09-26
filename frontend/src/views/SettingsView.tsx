/**
 * SettingsView (D-041/D-044) — every former 3-dot-menu function lives in a
 * tab; this one hosts: YOUR trading account (auto-provisioned practice
 * plane), broker link (per-user, encrypted), engine settings (admin), data &
 * privacy, logs and about.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  getConfig, getLogs, getTradingStatus, postMt5Connect, postMt5Disconnect,
  postTradingReset, putConfig, getBridge, putBridge, resetBridge, diagnoseBridge,
} from "../lib/api";
import type {
  BrokerConnection, EngineConfig, LogEntry, Mt5Status, TradingStatus,
  BridgeDiagnosis, BridgeInfo,
} from "../types";
import { Badge, Btn, Card, Dot, EmptyState, Field, NumberField, SectionTitle, Stat, inputCls } from "../components/ui";
import { useAuth } from "../lib/auth";

interface Props {
  token: string;
  mt5: Mt5Status | null;
  broker: BrokerConnection | null;
  isAdmin: boolean;
  dataSource: string;
  engineLogs: { level: string; message: string }[];
  onBrokerConnected: () => void;
}

/* -------------------------------------------------------- broker connect */

function BrokerCard({
  token,
  broker,
  onConnected,
}: {
  token: string;
  broker: BrokerConnection | null;
  onConnected: () => void;
}) {
  const linked = broker?.status === "connected" || broker?.status === "linked";
  const [server, setServer] = useState("");
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
        title="Broker Link"
        right={
          linked ? (
            <span className="flex items-center gap-1.5 text-[10px] font-semibold text-emerald-400">
              <Dot tone="green" /> {broker?.status === "linked" ? "linked" : "connected"}
            </span>
          ) : broker?.status === "reconnecting" ? (
            <span className="flex items-center gap-1.5 text-[10px] font-semibold text-amber-400">
              <Dot tone="amber" /> reconnecting…
            </span>
          ) : (
            <span className="flex items-center gap-1.5 text-[10px] font-semibold text-zinc-500">
              <Dot tone="zinc" /> not linked
            </span>
          )
        }
      />
      {linked ? (
        <>
          <p className="text-[11px] leading-relaxed text-zinc-400">
            {broker?.login_masked ?? broker?.login ?? "—"} @ {broker?.server ?? "—"} —
            credentials are encrypted and stored for your account only.
          </p>
          <div className="mt-3">
            <Btn variant="danger" onClick={() => void disconnect()} disabled={busy} className="w-full">
              {busy ? "Unlinking…" : "Unlink Broker"}
            </Btn>
          </div>
        </>
      ) : (
        <>
          <p className="mb-3 text-[11px] leading-relaxed text-zinc-400">
            Link your broker account to activate live routing for your trades.
            Your credentials are encrypted per-user and never shared.
          </p>
          <div className="flex flex-col gap-2">
            <Field label="Broker server">
              <input className={inputCls} value={server} onChange={(e) => setServer(e.target.value)} placeholder="e.g. YourBroker-Real" />
            </Field>
            <div className="grid grid-cols-2 gap-2">
              <Field label="Login">
                <input className={inputCls} value={login} onChange={(e) => setLogin(e.target.value)} inputMode="numeric" placeholder="—" />
              </Field>
              <Field label="Password">
                <input className={inputCls} type="password" value={password} onChange={(e) => setPassword(e.target.value)} placeholder="••••••••" />
              </Field>
            </div>
            <Btn variant="gold" onClick={() => void connect()} disabled={busy || !login || !password || !server} className="mt-1 w-full">
              {busy ? "Linking…" : "Link Broker"}
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

/* ------------------------------------------------- D-078 terminal bridge */

/** Admin control room for the MT5 terminal bridge endpoint — the in-app
 * fix for the permanent "MT5 MCP HTTP 502" offline report. Free tunnels
 * rotate their URL on every restart; before D-078 that meant a Railway
 * env change + redeploy. Here the admin pastes the new tunnel URL, the
 * platform hot-swaps every live client, persists it, re-attaches the
 * broker feed immediately, and the staged diagnosis says exactly where
 * any remaining break is (dns / tcp / tls / http 502 / mcp). */
function BridgeCard({ token }: { token: string }) {
  const [info, setInfo] = useState<BridgeInfo | null>(null);
  const [url, setUrl] = useState("");
  const [busy, setBusy] = useState<"" | "save" | "diag" | "reset">("");
  const [diag, setDiag] = useState<BridgeDiagnosis | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [savedAt, setSavedAt] = useState(0);

  const reload = useCallback(() => {
    getBridge(token)
      .then((r) => {
        setInfo(r);
        setUrl((cur) => cur || r.url);
      })
      .catch(() => setInfo(null));
  }, [token]);
  useEffect(reload, [reload]);
  useEffect(() => {
    const t = window.setInterval(reload, 15_000);
    return () => window.clearInterval(t);
  }, [reload]);

  const save = async () => {
    setBusy("save");
    setError(null);
    try {
      const r = await putBridge(token, url.trim());
      setDiag(r.diagnosis ?? null);
      setSavedAt(Date.now());
      window.setTimeout(() => setSavedAt(0), 2500);
      reload();
    } catch (e) {
      setError(e instanceof Error ? e.message : "save failed");
    } finally {
      setBusy("");
    }
  };

  const runDiag = async () => {
    setBusy("diag");
    setError(null);
    try {
      setDiag(await diagnoseBridge(token));
    } catch (e) {
      setError(e instanceof Error ? e.message : "diagnose failed");
    } finally {
      setBusy("");
    }
  };

  const reset = async () => {
    setBusy("reset");
    setError(null);
    try {
      await resetBridge(token);
      setDiag(null);
      reload();
    } catch (e) {
      setError(e instanceof Error ? e.message : "reset failed");
    } finally {
      setBusy("");
    }
  };

  const attached = info?.attached ?? false;
  const probe = info?.last_probe ?? null;

  return (
    <Card>
      <SectionTitle
        title="Terminal Bridge"
        right={
          attached ? (
            <span className="flex items-center gap-1.5 text-[10px] font-semibold text-emerald-400">
              <Dot tone="green" /> attached
            </span>
          ) : (
            <span className="flex items-center gap-1.5 text-[10px] font-semibold text-amber-400">
              <Dot tone="amber" /> not attached
            </span>
          )
        }
      />
      <p className="mb-3 text-[11px] leading-relaxed text-zinc-400">
        The link between this app and the MetaTrader 5 terminal on your PC
        (through your tunnel). When the tunnel restarts its URL changes — paste
        the new one here; the platform reconnects without a redeploy.
      </p>

      <div className="mb-2 flex min-w-0 flex-wrap items-center gap-1.5">
        <Badge tone={info?.source === "runtime" ? "gold" : "zinc"}>
          {info?.source === "runtime" ? "URL: saved in-app" : "URL: deploy env"}
        </Badge>
        {probe && !probe.ok && (
          <Badge tone="red">last probe: {probe.stage}</Badge>
        )}
      </div>
      {probe && !probe.ok && (
        <p className="mb-3 break-words rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-[10px] leading-relaxed text-amber-300">
          {probe.detail}
        </p>
      )}

      <div className="flex flex-col gap-2">
        <Field label="Bridge URL (terminal MCP endpoint)">
          <input
            className={inputCls}
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="https://your-tunnel.example.com/mcp"
            spellCheck={false}
          />
        </Field>
        <div className="flex flex-wrap gap-2">
          <Btn
            variant="gold"
            onClick={() => void save()}
            disabled={busy !== "" || url.trim().length < 8}
          >
            {busy === "save" ? "Saving…" : savedAt ? "Saved ✓" : "Save & reconnect"}
          </Btn>
          <Btn variant="default" onClick={() => void runDiag()} disabled={busy !== ""}>
            {busy === "diag" ? "Diagnosing…" : "Diagnose"}
          </Btn>
          {info?.source === "runtime" && (
            <Btn variant="ghost" onClick={() => void reset()} disabled={busy !== ""}>
              {busy === "reset" ? "Resetting…" : "Reset to env"}
            </Btn>
          )}
        </div>
      </div>

      {diag && (
        <div className="mt-3 rounded-xl border border-zinc-800/70 bg-zinc-900/40 p-3">
          <p className={`text-[11px] font-semibold ${diag.ok ? "text-emerald-400" : "text-amber-400"}`}>
            {diag.ok ? "✓ " : "⚠ "}{diag.verdict}
          </p>
          <div className="mt-2 flex flex-col gap-1">
            {diag.stages.map((s) => (
              <p key={s.stage} className="flex min-w-0 items-start gap-2 text-[10px] leading-relaxed">
                <span className={s.ok ? "text-emerald-400" : "text-red-400"}>
                  {s.ok ? "✓" : "✗"}
                </span>
                <span className="min-w-0 break-words text-zinc-400">
                  <b className="text-zinc-300">{s.stage}</b> · {s.detail}
                  {s.hint && <span className="block text-amber-400/90">→ {s.hint}</span>}
                </span>
              </p>
            ))}
          </div>
        </div>
      )}

      {error && (
        <p className="mt-2.5 break-words rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-[11px] text-red-300">
          {error}
        </p>
      )}
    </Card>
  );
}

/* -------------------------------------------------------- engine settings */

/** D-052 — STRATEGY-ONLY engine fields, full-word labels.
 *
 * D-052 follow-up (user feedback): the Risk:Reward ratio, minimum stop
 * distance and maximum spread were dropped too aggressively — they are
 * STRATEGY parameters, not money management, so they are RESTORED here
 * with full-word labels. Money-management criteria (balance, stop loss
 * USD, target profit USD, lot size, signals per day, concurrent trades)
 * live ONLY in the AI tab's money-management window — never duplicated
 * here (user directive: "একই বিষয় দুই জায়গায় থাকবে না"). Trade-stopping
 * instructions are likewise absent from this section.
 */
const ENGINE_NUM_FIELDS: { key: keyof EngineConfig; label: string; hint?: string; step?: string }[] = [
  { key: "rr", label: "Risk : Reward Ratio", hint: "take-profit distance = this many times the stop-loss distance (fallback when no zone target is near)", step: "0.1" },
  { key: "min_sl_atr", label: "Minimum Stop Distance (× ATR)", hint: "stop loss never closer than this — keeps it clear of spread noise", step: "0.1" },
  { key: "max_spread_points", label: "Maximum Spread (points)", hint: "signals are skipped while the spread is wider than this", step: "1" },
  { key: "trusted_min_votes", label: "Trusted Strategy Votes", hint: "how many trusted strategies must vote together — whale flow counts double", step: "0.5" },
  { key: "pending_target_usd", label: "Preferred Pending Distance (USD)", hint: "how far from the market pending orders are placed", step: "0.5" },
  { key: "pending_max_usd", label: "Maximum Pending Distance (USD)", hint: "hard cap — orders further than this never fill", step: "0.5" },
  { key: "min_atr", label: "Minimum Volatility (ATR)", hint: "skip dead markets — signals need movement", step: "0.05" },
  { key: "cooldown_bars", label: "Cooldown After Each Signal (bars)", hint: "quiet period after every signal before the next one can fire", step: "1" },
  { key: "expiry_bars", label: "Pending Order Expiry (bars)", hint: "unfilled orders cancel after this many bars", step: "1" },
];

/** D-051/D-076 — the markets on offer (broker suffixes normalize
 * server-side). Kept in sync with lib/markets.ts (the single home is the
 * backend's MARKETS registry; the Auto Trade tab is the primary UI now). */
const MARKETS = ["XAUUSD", "BTCUSD", "USOIL", "USTEC"];

function MarketToggles({
  label, hint, options, value, onChange, disabled,
}: {
  label: string;
  hint: string;
  options: string[];
  value: string[];
  onChange: (next: string[]) => void;
  disabled?: boolean;
}) {
  const toggle = (m: string) => {
    const has = value.includes(m);
    if (has && value.length === 1) return; // never empty
    onChange(has ? value.filter((v) => v !== m) : [...value, m]);
  };
  return (
    <div className="min-w-0 rounded-xl border border-zinc-800/70 bg-zinc-900/40 px-3 py-2.5">
      <p className="text-[10px] font-semibold uppercase tracking-wider text-zinc-500">{label}</p>
      <p className="mb-2 mt-0.5 text-[10px] leading-relaxed text-zinc-500">{hint}</p>
      <div className="flex flex-wrap gap-1.5">
        {options.map((m) => {
          const on = value.includes(m);
          return (
            <button
              key={m}
              type="button"
              disabled={disabled}
              onClick={() => toggle(m)}
              className={`rounded-full border px-3 py-1 text-[11px] font-bold tracking-wide transition-colors disabled:opacity-50 ${
                on
                  ? "border-gold/50 bg-gold/15 text-gold"
                  : "border-zinc-700 bg-zinc-800/60 text-zinc-400 hover:text-zinc-200"
              }`}
            >
              {on ? "✓ " : ""}{m}
            </button>
          );
        })}
      </div>
    </div>
  );
}

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
        <SectionTitle title="AI Engine" />
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
        title="AI Engine"
        right={
          <Badge tone="blue">
            {cfg.timeframe} · {(cfg.confirm_tfs ?? []).join(" + ")} confirm · {cfg.trend_tf} trend
          </Badge>
        }
      />
      <p className="mb-3 text-[11px] leading-relaxed text-zinc-400">
        Strategy engine — trades the <b className="text-zinc-200">{cfg.timeframe}</b>{" "}
        timeframe: {cfg.trend_tf} sets the trend,{" "}
        {(cfg.confirm_tfs ?? []).join(" + ") || "higher timeframes"} confirm, then an
        M1 pattern (liquidity sweep or trend pullback) triggers the entry.
        Money-management limits live in the AI tab's money-management window —
        not here.
      </p>

      {/* D-051 — per-market controls: which markets SIGNAL, which EXECUTE */}
      <div className="mb-3 grid grid-cols-1 gap-2 sm:grid-cols-2">
        <MarketToggles
          label="Signal Markets"
          hint="One engine per market — signals always generate, auto-trade or not."
          options={MARKETS}
          value={cfg.signal_symbols ?? ["XAUUSD"]}
          disabled={!isAdmin}
          onChange={(next) => {
            // execution markets must stay a subset of the signal set
            const exec = (cfg.auto_trade_symbols ?? []).filter((m) => next.includes(m));
            patch({ signal_symbols: next, auto_trade_symbols: exec.length ? exec : next.slice(0, 1) });
          }}
        />
        <MarketToggles
          label="Auto-Trade Markets"
          hint="Orders execute ONLY on these markets (you choose which ones)."
          options={cfg.signal_symbols ?? MARKETS}
          value={cfg.auto_trade_symbols ?? []}
          disabled={!isAdmin}
          onChange={(next) => patch({ auto_trade_symbols: next })}
        />
      </div>

      <div className="grid grid-cols-2 gap-2">
        {ENGINE_NUM_FIELDS.map((f) => (
          <Field key={String(f.key)} label={f.label} hint={f.hint}>
            {/* D-054 — NumberField: select-all on focus + commit on blur.
            The old controlled input parsed on every keystroke, so the old
            number could not be cleared or edited in place (Bengali bug
            report: "পাশে আরেক টা লিখে তারপর মুছতে হচ্ছে"). */}
            <NumberField
              value={cfg[f.key] as number | undefined}
              onCommit={(v) => patch({ [f.key]: v } as Partial<EngineConfig>)}
              disabled={!isAdmin}
              placeholder={f.step}
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
          Pullback Trigger: {cfg.pullback_enabled ? "ON" : "OFF"}
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

/* ------------------------------------------------- D-044 your account */

function YourAccountCard({ token }: { token: string }) {
  const [status, setStatus] = useState<TradingStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [reset, setReset] = useState(false);

  const reload = useCallback(() => {
    getTradingStatus(token).then(setStatus).catch(() => setStatus(null));
  }, [token]);
  useEffect(reload, [reload]);
  useEffect(() => {
    const t = window.setInterval(reload, 10_000);
    return () => window.clearInterval(t);
  }, [reload]);

  const doReset = async () => {
    if (!reset) {
      setReset(true); // two-tap confirm
      window.setTimeout(() => setReset(false), 3000);
      return;
    }
    setBusy(true);
    setReset(false);
    try {
      await postTradingReset(token);
      reload();
    } finally {
      setBusy(false);
    }
  };

  const account = status?.account ?? null;

  return (
    <Card>
      <SectionTitle
        title="Your Trading Account"
        right={
          status?.connected ? (
            <span className="flex items-center gap-1.5 text-[10px] font-semibold text-emerald-400">
              <Dot tone="green" /> {status?.mode ?? "practice"}
            </span>
          ) : (
            <Badge tone="amber">starting…</Badge>
          )
        }
      />
      <p className="mb-3 text-[11px] leading-relaxed text-zinc-400">
        Your personal account on the institutional market feed — balance,
        positions, risk settings and trade history are yours alone. Orders
        fill at live market prices with SL/TP managed automatically.
      </p>
      <div className="grid grid-cols-2 gap-2">
        <Stat label="Balance" value={account?.balance?.toFixed(2) ?? "—"} hint={account?.currency ?? "USD"} loading={status === null} />
        <Stat
          label="Equity"
          value={account?.equity?.toFixed(2) ?? "—"}
          loading={status === null}
          tone={
            account?.balance != null && account?.equity != null
              ? account.equity >= account.balance ? "up" : "down"
              : "default"
          }
        />
      </div>
      <div className="mt-3">
        <Btn variant={reset ? "danger" : "default"} onClick={() => void doReset()} disabled={busy} className="w-full">
          {busy ? "Resetting…" : reset ? "Tap again to confirm reset" : "Reset account (flat, $10,000)"}
        </Btn>
      </div>
    </Card>
  );
}

/* ---------------------------------------------- D-044 data & privacy */

function DataPrivacyCard() {
  return (
    <Card>
      <SectionTitle title="Data & Privacy" />
      <div className="flex flex-col gap-2.5 text-[11px] leading-relaxed text-zinc-400">
        <p>
          <span className="font-semibold text-zinc-200">Market data.</span> Real-time
          prices, volume and history are licensed and aggregated by the operating
          institution from institutional market-data providers. Every user sees
          the same public market data.
        </p>
        <p>
          <span className="font-semibold text-zinc-200">Your account.</span> Your
          balance, positions, risk settings and trade history are stored per-user
          and are never visible to any other user.
        </p>
        <p>
          <span className="font-semibold text-zinc-200">Credentials.</span> Linked
          broker credentials are encrypted (at rest and in transit) and never
          displayed again after saving or shared with third parties.
        </p>
        <p>
          <span className="font-semibold text-zinc-200">Analytics.</span> Economic
          calendars and positioning reports are sourced from public official
          publications (e.g. CFTC) and refresh automatically.
        </p>
      </div>
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
      <YourAccountCard token={token} />
      <BrokerCard token={token} broker={broker} onConnected={onBrokerConnected} />
      {isAdmin && <BridgeCard token={token} />}
      <EngineCard token={token} isAdmin={isAdmin} />
      <DataPrivacyCard />
      <LogsCard token={token} engineLogs={engineLogs} />

      <Card>
        <SectionTitle title="Account" />
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <Badge tone="zinc">{email}</Badge>
          <Badge tone={isAdmin ? "gold" : "blue"}>{isAdmin ? "admin" : "member"}</Badge>
          <Badge tone="green">data: {dataSource || mt5?.status || "live"}</Badge>
          <Btn variant="default" onClick={() => void signOut()} className="ml-auto">
            Sign out
          </Btn>
        </div>
      </Card>

      <Card>
        <SectionTitle title="About" />
        <p className="text-[11px] leading-relaxed text-zinc-400">
          Gold AI Trading — institutional-grade market data only. Engine: M1
          entries with H1 trend + M5/M15 multi-timeframe confirmation, order-flow
          and news-window filtering. Every account is isolated per user.
        </p>
      </Card>
    </div>
  );
}
