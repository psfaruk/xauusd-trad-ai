import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import TopBar from "../components/TopBar";
import Chart from "../components/Chart";
import TimeframeSwitcher from "../components/TimeframeSwitcher";
import SignalPanel from "../components/SignalPanel";
import Mt5ConnectDialog from "../components/Mt5ConnectDialog";
import SettingsDialog from "../components/SettingsDialog";
import { useAuth } from "../lib/auth";
import { getCandles, getMt5Status, getSignals, getStats, getHealth, getMe } from "../lib/api";
import { WSClient } from "../lib/ws";
import type {
  Candle, Mt5Status, Signal, StatsResponse, Timeframe, WsMessage,
} from "../types";

/**
 * Dashboard (SPEC §10): live chart + TF switcher + signal panel + account
 * strip. Data flow: REST backfill -> WS subscribe -> bar events stream into
 * the chart; on WS reconnect the candles query is invalidated to heal gaps
 * (Phase 2 AC).
 */
export default function Dashboard() {
  const { session } = useAuth();
  const token = session?.access_token ?? null;
  const queryClient = useQueryClient();

  const [tf, setTf] = useState<Timeframe>("M15");
  const [mt5, setMt5] = useState<Mt5Status | null>(null);
  const [dataSource, setDataSource] = useState<"mock" | "mt5" | "">("");
  const [liveBar, setLiveBar] = useState<Candle | null>(null);
  const [lastPrice, setLastPrice] = useState<{ bid: number; ask: number } | null>(null);
  const [account, setAccount] = useState<{
    balance: number; equity: number; currency: string;
    positions: { ticket: number; symbol: string; side: string; volume: number; profit: number }[];
  } | null>(null);
  const [engineLogs, setEngineLogs] = useState<{ level: string; message: string }[]>([]);
  const [wsState, setWsState] = useState<"connecting" | "open" | "closed">("connecting");
  const [connectOpen, setConnectOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);

  const symbol = mt5?.symbol ?? null;
  const [roleState, setRoleState] = useState<string>("viewer");
  const isAdmin = useMemo(() => roleState === "admin", [roleState]);

  /* ---------------------------------------------------------------- queries */

  const candlesQuery = useQuery({
    queryKey: ["candles", tf, symbol],
    enabled: !!token && !!symbol && mt5?.status === "connected",
    queryFn: () => getCandles(token!, tf, 500),
    refetchOnWindowFocus: false,
    staleTime: Infinity,
  });

  const signalsQuery = useQuery({
    queryKey: ["signals"],
    enabled: !!token,
    queryFn: () => getSignals(token!, 100),
    refetchOnWindowFocus: false,
  });

  const statsQuery = useQuery({
    queryKey: ["stats"],
    enabled: !!token,
    queryFn: () => getStats(token!, 30),
    refetchOnWindowFocus: false,
  });

  const wsRef = useRef<WSClient | null>(null);

  /* ------------------------------------------------------------ ws lifecycle */

  const handleWsMessage = useCallback(
    (msg: WsMessage) => {
      switch (msg.type) {
        case "tick":
          setLastPrice({ bid: msg.bid, ask: msg.ask });
          break;
        case "bar_open":
        case "bar_update":
        case "bar_close":
          if (msg.tf === tf) setLiveBar(msg.candle);
          break;
        case "signal":
        case "signal_update":
          void queryClient.invalidateQueries({ queryKey: ["signals"] });
          void queryClient.invalidateQueries({ queryKey: ["stats"] });
          break;
        case "account":
          setAccount({
            balance: msg.balance,
            equity: msg.equity,
            currency: msg.currency,
            positions: msg.positions ?? [],
          });
          break;
        case "mt5_status":
          setMt5((prev) =>
            prev
              ? { ...prev, status: msg.status, symbol: msg.symbol ?? prev.symbol }
              : prev
          );
          if (msg.symbol) {
            void queryClient.invalidateQueries({ queryKey: ["candles"] });
          }
          break;
        case "engine_log":
          setEngineLogs((prev) => [...prev.slice(-7), { level: msg.level, message: msg.message }]);
          break;
        default:
          break;
      }
    },
    [tf, queryClient]
  );

  useEffect(() => {
    if (!token) return;
    const ws = new WSClient(token);
    wsRef.current = ws;
    const offMsg = ws.on(handleWsMessage);
    const offStatus = ws.onStatus(setWsState);
    ws.connect();
    return () => {
      offMsg();
      offStatus();
      ws.close();
      wsRef.current = null;
    };
  }, [token, handleWsMessage]);

  // subscribe whenever symbol/tf becomes available or changes
  useEffect(() => {
    const ws = wsRef.current;
    if (ws && symbol && ws.connected) ws.subscribe(symbol, tf);
  }, [symbol, tf, wsState]);

  // gap healing: on reconnect refetch candles + status (Phase 2 AC)
  useEffect(() => {
    if (wsState === "open") {
      void queryClient.invalidateQueries({ queryKey: ["candles"] });
      if (token) getMt5Status(token).then(setMt5).catch(() => undefined);
      const ws = wsRef.current;
      if (ws && symbol) ws.subscribe(symbol, tf);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wsState]);

  // initial status + role + data source
  useEffect(() => {
    if (!token) return;
    getMt5Status(token).then(setMt5).catch(() => undefined);
    getHealth()
      .then((h) => setDataSource(h.data_source))
      .catch(() => undefined);
    getMe(token).then((me) => setRoleState(me.role)).catch(() => undefined);
  }, [token]);

  // poll status while WS is down (fallback path)
  useEffect(() => {
    if (!token || wsState === "open") return;
    const timer = window.setInterval(() => {
      getMt5Status(token).then(setMt5).catch(() => undefined);
    }, 10_000);
    return () => window.clearInterval(timer);
  }, [token, wsState]);

  /* ------------------------------------------------------------------ render */

  const signals: Signal[] = signalsQuery.data?.signals ?? [];
  const stats: StatsResponse | null = statsQuery.data ?? null;
  const activeSignal = signals.find((s) => s.status === "active") ?? null;

  return (
    <div className="flex min-h-screen flex-col bg-zinc-950">
      <TopBar
        mt5={mt5}
        dataSource={dataSource}
        onOpenConnect={() => setConnectOpen(true)}
        onOpenSettings={() => setSettingsOpen(true)}
      />

      <main className="mx-auto flex w-full max-w-[1600px] flex-1 flex-col gap-4 p-4 xl:flex-row">
        {/* CHART area */}
        <section className="flex min-w-0 flex-1 flex-col gap-3" aria-label="Chart">
          <div className="flex flex-wrap items-center gap-3">
            <TimeframeSwitcher tf={tf} onChange={setTf} />
            <div className="ml-auto flex items-center gap-3 text-xs">
              {lastPrice && (
                <span className="font-mono">
                  <span className="text-zinc-500">bid </span>
                  <span className="text-zinc-200">{lastPrice.bid.toFixed(2)}</span>
                  <span className="mx-1 text-zinc-600">/</span>
                  <span className="text-zinc-500">ask </span>
                  <span className="text-zinc-200">{lastPrice.ask.toFixed(2)}</span>
                </span>
              )}
              <span
                className={`rounded-full border px-2 py-0.5 text-[11px] ${
                  wsState === "open"
                    ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-400"
                    : wsState === "connecting"
                      ? "border-amber-500/30 bg-amber-500/10 text-amber-400"
                      : "border-red-500/30 bg-red-500/10 text-red-400"
                }`}
              >
                ws {wsState}
              </span>
            </div>
          </div>

          <div className="h-[52vh] min-h-[380px] overflow-hidden rounded-xl border border-zinc-800 bg-[#0c0e14] xl:h-[calc(100vh-220px)]">
            <Chart
              candles={candlesQuery.data?.candles ?? []}
              liveBar={liveBar}
              activeSignal={activeSignal}
              tf={tf}
            />
          </div>

          {engineLogs.length > 0 && (
            <div className="max-h-24 overflow-y-auto rounded-xl border border-zinc-800 bg-zinc-900/40 p-2.5">
              {engineLogs.slice(-4).map((log, i) => (
                <p key={i} className="truncate font-mono text-[11px] text-zinc-500">
                  <span className={log.level === "info" ? "text-gold/80" : "text-zinc-400"}>
                    [{log.level}]
                  </span>{" "}
                  {log.message}
                </p>
              ))}
            </div>
          )}
        </section>

        {/* SIGNAL PANEL */}
        <section className="w-full min-w-0 xl:w-96" aria-label="Signals">
          <SignalPanel signals={signals} stats={stats} />
        </section>
      </main>

      {/* AccountStrip per SPEC §10 */}
      <footer className="border-t border-zinc-800 bg-zinc-900/60">
        <div className="mx-auto flex w-full max-w-[1600px] flex-wrap items-center gap-x-6 gap-y-1 px-4 py-2.5 text-xs text-zinc-400">
          <span>
            balance{" "}
            <span className="font-semibold text-zinc-200">
              {account ? `${account.balance.toFixed(2)} ${account.currency}` : "—"}
            </span>
          </span>
          <span>
            equity{" "}
            <span className="font-semibold text-zinc-200">
              {account ? account.equity.toFixed(2) : "—"}
            </span>
          </span>
          <span>
            open positions{" "}
            <span className="font-semibold text-zinc-200">{account?.positions.length ?? 0}</span>
            {account && account.positions.length > 0 && (
              <span className="ml-1 text-zinc-500">
                ({account.positions.map((p) => `${p.side} ${p.volume}`).join(", ")})
              </span>
            )}
          </span>
          <span className="ml-auto flex items-center gap-1.5">
            <span
              aria-hidden
              className={`inline-block h-2 w-2 rounded-full ${
                mt5?.engine_running ? "bg-emerald-500" : "bg-zinc-600"
              }`}
            />
            engine <span className="font-semibold text-zinc-300">{mt5?.engine_running ? "ON" : "OFF"}</span>
            <span className="text-zinc-600">· auto-trade OFF (default, Phase 4)</span>
          </span>
        </div>
      </footer>

      <Mt5ConnectDialog
        open={connectOpen}
        onClose={() => setConnectOpen(false)}
        token={token ?? ""}
        isAdmin={isAdmin}
        status={mt5}
        dataSource={dataSource}
        onConnected={(st) => {
          setMt5(st);
          void queryClient.invalidateQueries({ queryKey: ["candles"] });
        }}
        onDisconnected={(st) => {
          setMt5(st);
          setLiveBar(null);
        }}
      />

      <SettingsDialog
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
        token={token ?? ""}
        isAdmin={isAdmin}
      />
    </div>
  );
}
