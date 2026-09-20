import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import TopBar from "../components/TopBar";
import Chart from "../components/Chart";
import TimeframeSwitcher from "../components/TimeframeSwitcher";
import SymbolSwitcher from "../components/SymbolSwitcher";
import SignalPanel, { type SignalPanelTab } from "../components/SignalPanel";
import Mt5ConnectDialog from "../components/Mt5ConnectDialog";
import SettingsDialog from "../components/SettingsDialog";
import TradingDialog from "../components/TradingDialog";
import TradePanel from "../components/TradePanel";
import Mt5AccountPanel from "../components/Mt5AccountPanel";
import LogViewer from "../components/LogViewer";
import { useAuth } from "../lib/auth";
import {
  getCandles, getMt5Status, getSignals, getStats, getHealth, getMe,
  getTradingStatus, getTradingPositions, getMt5Account, getMt5Positions,
} from "../lib/api";
import { WSClient } from "../lib/ws";
import type {
  Candle, Mt5Account, Mt5OpenPosition, Mt5Status, Signal, StatsResponse,
  Timeframe, TradingPosition, TradingStatus, WsMessage, WsMt5StatusMsg,
} from "../types";

/**
 * Dashboard (SPEC §10 + Phase 4): live chart + TF switcher + tabbed signal
 * panel (signals / market data / performance) + account strip with the user's
 * own trading plane. All dialogs open from the 3-dot menu (user req #3).
 */
export default function Dashboard() {
  const { session } = useAuth();
  const token = session?.access_token ?? null;
  const queryClient = useQueryClient();

  const [tf, setTf] = useState<Timeframe>("M15");
  const [symbol, setSymbol] = useState("XAUUSD"); // D-035: selected instrument
  const [mt5, setMt5] = useState<Mt5Status | null>(null);
  const [dataSource, setDataSource] = useState<"mock" | "mt5" | "live" | "">("");
  const [liveBar, setLiveBar] = useState<Candle | null>(null);
  const [lastPrice, setLastPrice] = useState<{ bid: number; ask: number } | null>(null);
  const [lastTickAt, setLastTickAt] = useState<number | null>(null);
  const [tps, setTps] = useState<number | null>(null); // D-033: real ticks/sec
  const [account, setAccount] = useState<{
    balance: number; equity: number; currency: string;
    positions: { ticket: number; symbol: string; side: string; volume: number; profit: number }[];
  } | null>(null);
  const [engineLogs, setEngineLogs] = useState<{ level: string; message: string }[]>([]);
  const [wsState, setWsState] = useState<"connecting" | "open" | "closed">("connecting");

  /* Phase 4: own trading plane state */
  const [trading, setTrading] = useState<TradingStatus | null>(null);
  const [tradingPositions, setTradingPositions] = useState<TradingPosition[]>([]);

  /* dialog + panel state */
  const [connectOpen, setConnectOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [tradingOpen, setTradingOpen] = useState(false);
  const [tradePanelOpen, setTradePanelOpen] = useState(false);
  const [tradePanelTab, setTradePanelTab] = useState<"trade" | "history">("trade");
  const [mt5AccountOpen, setMt5AccountOpen] = useState(false); // D-034 real account
  const [logsOpen, setLogsOpen] = useState(false);
  const [panelTab, setPanelTab] = useState<SignalPanelTab>("signals");

  /* D-034/D-035: the REAL MT5 account (balance/equity/positions) shown right
   * on the dashboard footer — polled every 10s while the bridge answers. */
  const [mt5Acct, setMt5Acct] = useState<Mt5Account | null>(null);
  const [mt5Pos, setMt5Pos] = useState<Mt5OpenPosition[]>([]);

  const connectedSymbol = mt5?.symbol ?? null;
  const symbols = useMemo(() => {
    const list = mt5?.symbols?.length ? mt5.symbols : [connectedSymbol ?? "XAUUSD"];
    return list.filter((s, i) => list.indexOf(s) === i);
  }, [mt5?.symbols, connectedSymbol]);
  const [roleState, setRoleState] = useState<string>("viewer");
  const isAdmin = useMemo(() => roleState === "admin", [roleState]);
  const tradingConnected = trading?.connected === true;

  /* ---------------------------------------------------------------- queries */

  const candlesQuery = useQuery({
    queryKey: ["candles", tf, symbol],
    enabled: !!token && !!symbol && mt5?.status === "connected",
    queryFn: () => getCandles(token!, tf, 500, symbol),
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

  const refreshTrading = useCallback(() => {
    if (!token) return;
    void getTradingStatus(token).then(setTrading).catch(() => undefined);
    void getTradingPositions(token).then((r) => setTradingPositions(r.positions)).catch(() => undefined);
  }, [token]);

  /* ------------------------------------------------------------ ws lifecycle */

  const handleWsMessage = useCallback(
    (msg: WsMessage) => {
      switch (msg.type) {
        case "tick":
          if (msg.symbol !== symbol) return; // D-035: per-symbol frames
          setLastPrice({ bid: msg.bid, ask: msg.ask });
          setLastTickAt(Date.now());
          if (msg.tps != null) setTps(msg.tps);
          break;
        case "bar_open":
        case "bar_update":
        case "bar_close":
          if (msg.tf === tf && msg.symbol === symbol) setLiveBar(msg.candle);
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
              ? {
                  ...prev,
                  status: msg.status,
                  symbol: msg.symbol ?? prev.symbol,
                  symbols: (msg as WsMt5StatusMsg).symbols ?? prev.symbols,
                  feed: (msg as WsMt5StatusMsg).feed ?? prev.feed,
                }
              : prev
          );
          {
            const feedTps = (msg as WsMt5StatusMsg).feed?.symbols?.[symbol]?.tps
              ?? (msg as WsMt5StatusMsg).feed?.tps;
            if (feedTps != null) setTps(feedTps);
          }
          break;
        case "engine_log":
          setEngineLogs((prev) => [...prev.slice(-7), { level: msg.level, message: msg.message }]);
          break;
        case "trading_account":
          setTrading((prev) => ({
            ...prev,
            connected: true,
            mode: msg.mode === "live" ? "live" : "demo",
            auto_trade: msg.auto_trade,
            account: {
              balance: msg.balance,
              equity: msg.equity,
              currency: msg.currency,
            },
          }));
          setTradingPositions(msg.positions ?? []);
          break;
        case "trading_log":
          setEngineLogs((prev) => [
            ...prev.slice(-7),
            { level: msg.level, message: `[my account] ${msg.message}` },
          ]);
          break;
        default:
          break;
      }
    },
    [tf, symbol, queryClient]
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

  // D-035: switching instruments resets the per-symbol live state (price,
  // forming bar, tick rate) so the chart starts clean for the new symbol.
  useEffect(() => {
    setLiveBar(null);
    setLastPrice(null);
    setLastTickAt(null);
    setTps(null);
  }, [symbol]);

  // D-035: refetch candles ONLY when the quote SOURCE for the selected
  // symbol actually switches (MT5 terminal <-> crypto composite, e.g. the
  // Monday open) — the 15s status heartbeat alone no longer thrashes the
  // chart with full setData resets (the old "chart breaking" symptom).
  const feedSourceRef = useRef<string>("");
  useEffect(() => {
    const fs = mt5?.feed?.symbols?.[symbol];
    if (!fs) return;
    const src = `${fs.provider}:${fs.mt5 ? "mt5" : "composite"}`;
    if (feedSourceRef.current && feedSourceRef.current !== src) {
      void queryClient.invalidateQueries({ queryKey: ["candles"] });
    }
    feedSourceRef.current = src;
  }, [mt5?.feed, symbol, queryClient]);

  // D-034/D-035: the REAL MT5 account (balance/equity/positions) — 10s poll,
  // hidden honestly when the terminal bridge is offline.
  useEffect(() => {
    if (!token) return;
    let alive = true;
    const poll = () => {
      getMt5Account(token)
        .then((a) => {
          if (!alive) return;
          if (a.connected) setMt5Acct(a);
          else setMt5Acct(null);
        })
        .catch(() => alive && setMt5Acct(null));
      getMt5Positions(token)
        .then((r) => alive && setMt5Pos(r.positions))
        .catch(() => alive && setMt5Pos([]));
    };
    poll();
    const timer = window.setInterval(poll, 10_000);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [token]);

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

  // initial status + role + data source + trading plane
  useEffect(() => {
    if (!token) return;
    getMt5Status(token).then(setMt5).catch(() => undefined);
    getHealth()
      .then((h) => setDataSource(h.data_source))
      .catch(() => undefined);
    getMe(token).then((me) => setRoleState(me.role)).catch(() => undefined);
    refreshTrading();
  }, [token, refreshTrading]);

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
  const activeSignal =
    signals.find((s) => s.status === "active" && s.symbol === symbol) ?? null;

  const menuActions = {
    onOpenTrading: () => setTradingOpen(true),
    onOpenTradePanel: () => {
      setTradePanelTab("trade");
      setTradePanelOpen(true);
    },
    onOpenTradeHistory: () => {
      setTradePanelTab("history");
      setTradePanelOpen(true);
    },
    onOpenMt5Account: () => setMt5AccountOpen(true),
    onOpenSignals: () => setPanelTab("signals"),
    onOpenMarketData: () => setPanelTab("market"),
    onOpenLogs: () => setLogsOpen(true),
    onOpenSettings: () => setSettingsOpen(true),
    onOpenPlatformMt5: () => setConnectOpen(true),
  };

  const onChartDesync = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: ["candles"] });
  }, [queryClient]);

  return (
    <div className="flex min-h-screen flex-col bg-zinc-950">
      <TopBar
        mt5={mt5}
        dataSource={dataSource}
        tradingConnected={tradingConnected}
        menuActions={menuActions}
        isAdmin={isAdmin}
        onOpenTrade={() => (tradingConnected ? setTradePanelOpen(true) : setTradingOpen(true))}
        lastPrice={lastPrice}
        lastTickAt={lastTickAt}
        tps={tps}
        symbol={symbol}
      />

      <main className="mx-auto flex w-full max-w-[1600px] flex-1 flex-col gap-4 p-4 xl:flex-row">
        {/* CHART area */}
        <section className="flex min-w-0 flex-1 flex-col gap-3" aria-label="Chart">
          <div className="flex flex-wrap items-center gap-3">
            <SymbolSwitcher symbols={symbols} value={symbol} onChange={setSymbol} mt5={mt5} />
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
              onDesync={onChartDesync}
            />
          </div>

          {engineLogs.length > 0 && (
            <div className="max-h-24 overflow-y-auto rounded-xl border border-zinc-800 bg-zinc-900/40 p-2.5">
              {engineLogs.slice(-4).map((log, i) => (
                <p key={i} className="truncate font-mono text-[11px] text-zinc-500">
                  <span className={log.level === "info" ? "text-gold/80" : log.level === "critical" ? "text-red-400" : "text-zinc-400"}>
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
          <SignalPanel
            signals={signals}
            stats={stats}
            token={token ?? ""}
            tab={panelTab}
            onTabChange={setPanelTab}
          />
        </section>
      </main>

      {/* AccountStrip per SPEC §10 + Phase 4 own-plane linkage */}
      <footer className="border-t border-zinc-800 bg-zinc-900/60">
        <div className="mx-auto flex w-full max-w-[1600px] flex-wrap items-center gap-x-6 gap-y-1 px-4 py-2.5 text-xs text-zinc-400">
          {/* platform feed account (admin plane) */}
          <span className="flex items-center gap-1">
            <span className="rounded bg-zinc-800 px-1.5 py-0.5 text-[10px] text-zinc-500">platform</span>
            balance{" "}
            <span className="font-semibold text-zinc-200">
              {account ? `${account.balance.toFixed(2)} ${account.currency}` : "—"}
            </span>
            <span className="mx-1 text-zinc-600">·</span>
            equity{" "}
            <span className="font-semibold text-zinc-200">
              {account ? account.equity.toFixed(2) : "—"}
            </span>
          </span>

          {/* D-034/D-035: the REAL MT5 account — balance/equity/positions from
           * the MetaTrader 5 terminal, visible right here after connect. */}
          {mt5Acct && (
            <button
              type="button"
              onClick={() => setMt5AccountOpen(true)}
              className="flex items-center gap-1 rounded px-1 py-0.5 hover:bg-zinc-800/60"
              title={`${mt5Acct.server ?? ""} · login ${mt5Acct.login ?? ""} — click for positions & trade history`}
            >
              <span
                className={`rounded px-1.5 py-0.5 text-[10px] ${
                  mt5Acct.connected
                    ? "bg-emerald-500/15 text-emerald-400"
                    : "bg-red-500/15 text-red-400"
                }`}
              >
                MT5{mt5Acct.type ? ` · ${mt5Acct.type}` : ""}
              </span>
              <span className="font-semibold text-zinc-100">
                {mt5Acct.balance?.toFixed(2) ?? "—"} {mt5Acct.currency ?? "USD"}
              </span>
              <span className="text-zinc-600">·</span>
              <span>
                eq{" "}
                <span className="font-semibold text-zinc-200">
                  {mt5Acct.equity?.toFixed(2) ?? "—"}
                </span>
              </span>
              <span className="text-zinc-600">·</span>
              <span
                className={
                  (mt5Acct.profit ?? 0) >= 0 ? "text-emerald-400" : "text-red-400"
                }
              >
                {(mt5Acct.profit ?? 0) >= 0 ? "+" : ""}
                {mt5Acct.profit?.toFixed(2) ?? "0.00"}
              </span>
              <span className="text-zinc-600">·</span>
              <span>
                {mt5Pos.length} open{mt5Pos.length > 0 && (
                  <span className="ml-1 text-zinc-500">
                    ({mt5Pos.map((p) => `${p.symbol} ${p.action} ${p.volume}`).join(", ")})
                  </span>
                )}
              </span>
            </button>
          )}

          {/* own trading plane */}
          <span className="flex items-center gap-1">
            <span
              className={`rounded px-1.5 py-0.5 text-[10px] ${
                tradingConnected ? "bg-emerald-500/15 text-emerald-400" : "bg-zinc-800 text-zinc-500"
              }`}
            >
              my account{trading?.mode ? ` · ${trading.mode}` : ""}
            </span>
            {tradingConnected && trading.account ? (
              <>
                <span className="font-semibold text-zinc-200">
                  {trading.account.equity.toFixed(2)} {trading.account.currency}
                </span>
                <span className="text-zinc-600">·</span>
                <span>
                  {tradingPositions.length} open
                  {tradingPositions.length > 0 && (
                    <span className="ml-1 text-zinc-500">
                      ({tradingPositions.map((p) => `${p.side} ${p.volume}`).join(", ")})
                    </span>
                  )}
                </span>
              </>
            ) : (
              <button
                type="button"
                onClick={() => setTradingOpen(true)}
                className="underline decoration-dotted hover:text-gold"
              >
                connect your account
              </button>
            )}
          </span>

          <span className="ml-auto flex items-center gap-3">
            <span className="flex items-center gap-1.5">
              <span
                aria-hidden
                className={`inline-block h-2 w-2 rounded-full ${
                  mt5?.engine_running ? "bg-emerald-500" : "bg-zinc-600"
                }`}
              />
              engine <span className="font-semibold text-zinc-300">{mt5?.engine_running ? "ON" : "OFF"}</span>
            </span>
            <span className="flex items-center gap-1.5">
              <span
                aria-hidden
                className={`inline-block h-2 w-2 rounded-full ${
                  trading?.auto_trade ? "bg-amber-400" : "bg-zinc-600"
                }`}
              />
              my auto-trade{" "}
              <span className={`font-semibold ${trading?.auto_trade ? "text-amber-400" : "text-zinc-500"}`}>
                {trading?.auto_trade ? "ARMED" : "OFF"}
              </span>
            </span>
          </span>
        </div>
      </footer>

      {/* ------------------------------------------------------------- dialogs */}

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

      <TradingDialog
        open={tradingOpen}
        onClose={() => setTradingOpen(false)}
        token={token ?? ""}
        status={trading}
        onStatusChange={(st) => {
          setTrading(st);
          refreshTrading();
        }}
      />

      <TradePanel
        open={tradePanelOpen}
        onClose={() => setTradePanelOpen(false)}
        token={token ?? ""}
        connected={tradingConnected}
        positions={tradingPositions}
        lastPrice={lastPrice}
        onPositionsChanged={refreshTrading}
        initialTab={tradePanelTab}
      />

      <Mt5AccountPanel
        open={mt5AccountOpen}
        onClose={() => setMt5AccountOpen(false)}
        token={token ?? ""}
        defaultSymbol={symbol}
      />

      <LogViewer
        open={logsOpen}
        onClose={() => setLogsOpen(false)}
        token={token ?? ""}
      />
    </div>
  );
}
