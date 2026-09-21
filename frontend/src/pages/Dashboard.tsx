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
import LogViewer from "../components/LogViewer";
import TabBar from "../components/TabBar";
import HomeView from "../components/HomeView";
import AiTradingView from "../components/AiTradingView";
import SettingsView from "../components/SettingsView";
import { useAuth } from "../lib/auth";
import {
  getCandles, getMt5Status, getSignals, getStats, getHealth, getMe,
  getTradingStatus, getTradingPositions, getMt5Account, getMt5Positions,
  getMt5AutoTrade,
} from "../lib/api";import { WSClient } from "../lib/ws";
import type {
  AppTab, BrokerConnection, Candle, Mt5Account, Mt5OpenPosition, Mt5Status,
  Signal, StatsResponse, Timeframe, TradingPosition, TradingStatus, WsMessage,
  WsMt5AutoMsg, WsMt5StatusMsg,
} from "../types";

/**
 * Dashboard (D-039 tab architecture): Home / Chart & Signals / AI Trading /
 * Settings. Mobile navigates via the bottom TabBar, desktop via the 3-dot
 * menu — every function lives under one of the tabs (user req). All data
 * flows (WS, queries, polls) stay owned here and are passed down as props.
 */
export default function Dashboard() {
  const { session } = useAuth();
  const token = session?.access_token ?? null;
  const queryClient = useQueryClient();

  /* D-039: active app tab */
  const [activeTab, setActiveTab] = useState<AppTab>("home");

  const [tf, setTf] = useState<Timeframe>("M15");
  const [symbol, setSymbol] = useState("XAUUSD");
  const [mt5, setMt5] = useState<Mt5Status | null>(null);
  const [dataSource, setDataSource] = useState<"mock" | "mt5" | "live" | "">("");
  const [liveBar, setLiveBar] = useState<Candle | null>(null);
  const [lastPrice, setLastPrice] = useState<{ bid: number; ask: number } | null>(null);
  const [lastTickAt, setLastTickAt] = useState<number | null>(null);
  const [tps, setTps] = useState<number | null>(null);
  const [engineLogs, setEngineLogs] = useState<{ level: string; message: string }[]>([]);
  const [wsState, setWsState] = useState<"connecting" | "open" | "closed">("connecting");

  /* practice plane state (Settings → practice, D-039: clearly optional) */
  const [trading, setTrading] = useState<TradingStatus | null>(null);
  const [tradingPositions, setTradingPositions] = useState<TradingPosition[]>([]);

  /* dialog state */
  const [connectOpen, setConnectOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [tradingOpen, setTradingOpen] = useState(false);
  const [tradePanelOpen, setTradePanelOpen] = useState(false);
  const [tradePanelTab, setTradePanelTab] = useState<"trade" | "history">("trade");
  const [logsOpen, setLogsOpen] = useState(false);
  const [panelTab, setPanelTab] = useState<SignalPanelTab>("signals");

  /* the USER's broker connection + real account (per-user, D-037) */
  const [mt5Acct, setMt5Acct] = useState<Mt5Account | null>(null);
  const [mt5Pos, setMt5Pos] = useState<Mt5OpenPosition[]>([]);
  /* D-036/D-039: AI auto-trade state + live event feed + refresh trigger */
  const [mt5AutoArmed, setMt5AutoArmed] = useState(false);
  const [mt5AutoWhy, setMt5AutoWhy] = useState<{ code: string; text: string } | null>(null);
  const [mt5AutoEvents, setMt5AutoEvents] = useState<WsMt5AutoMsg[]>([]);
  const [aiRefreshKey, setAiRefreshKey] = useState(0);

  const connectedSymbol = mt5?.symbol ?? null;
  const broker: BrokerConnection | null = mt5?.broker ?? null;
  const symbols = useMemo(() => {
    const list = mt5?.symbols?.length ? mt5.symbols : [connectedSymbol ?? "XAUUSD"];
    return list.filter((s, i) => list.indexOf(s) === i);
  }, [mt5?.symbols, connectedSymbol]);
  const [roleState, setRoleState] = useState<string>("viewer");
  const isAdmin = useMemo(() => roleState === "admin", [roleState]);

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
          if (msg.symbol !== symbol) return;
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
        case "mt5_auto": {
          const ev = msg as WsMt5AutoMsg;
          setMt5AutoEvents((prev) => [...prev.slice(-29), ev]);
          if (ev.event === "armed" || ev.event === "disarmed") {
            setMt5AutoArmed(ev.event === "armed");
          }
          // D-039: any execution event refreshes the AI tab data
          if (ev.event === "order" || ev.event === "close") {
            setAiRefreshKey((k) => k + 1);
          }
          break;
        }
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
            { level: msg.level, message: `[practice] ${msg.message}` },
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

  useEffect(() => {
    const ws = wsRef.current;
    if (ws && symbol && ws.connected) ws.subscribe(symbol, tf);
  }, [symbol, tf, wsState]);

  useEffect(() => {
    setLiveBar(null);
    setLastPrice(null);
    setLastTickAt(null);
    setTps(null);
  }, [symbol]);

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

  /* the USER's broker account + AI auto state — 10s poll */
  const refreshMt5Account = useCallback(() => {
    if (!token) return;
    getMt5Account(token)
      .then((a) => {
        if (a.connected) setMt5Acct(a);
        else setMt5Acct(null);
      })
      .catch(() => setMt5Acct(null));
    getMt5Positions(token)
      .then((r) => setMt5Pos(r.positions))
      .catch(() => setMt5Pos([]));
    getMt5AutoTrade(token)
      .then((s) => {
        setMt5AutoArmed(s.armed);
        setMt5AutoWhy(s.why ?? null);
      })
      .catch(() => undefined);
    getMt5Status(token)
      .then((st) =>
        setMt5((prev) => (prev ? { ...prev, broker: st.broker } : st))
      )
      .catch(() => undefined);
  }, [token]);

  useEffect(() => {
    refreshMt5Account();
    const timer = window.setInterval(refreshMt5Account, 10_000);
    return () => window.clearInterval(timer);
  }, [refreshMt5Account]);

  useEffect(() => {
    if (wsState === "open") {
      void queryClient.invalidateQueries({ queryKey: ["candles"] });
      if (token) getMt5Status(token).then(setMt5).catch(() => undefined);
      const ws = wsRef.current;
      if (ws && symbol) ws.subscribe(symbol, tf);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wsState]);

  useEffect(() => {
    if (!token) return;
    getMt5Status(token).then(setMt5).catch(() => undefined);
    getHealth()
      .then((h) => setDataSource(h.data_source))
      .catch(() => undefined);
    getMe(token).then((me) => setRoleState(me.role)).catch(() => undefined);
    refreshTrading();
  }, [token, refreshTrading]);

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
  const tradingConnected = trading?.connected === true;

  const navigate = useCallback((tab: AppTab) => setActiveTab(tab), []);
  const openConnectBroker = useCallback(() => setConnectOpen(true), []);

  const menuActions = {
    onNavigate: navigate,
    onOpenConnectBroker: openConnectBroker,
    onOpenPractice: () => setTradingOpen(true),
    onOpenTradePanel: () => {
      setTradePanelTab("trade");
      setTradePanelOpen(true);
    },
    onOpenTradeHistory: () => {
      setTradePanelTab("history");
      setTradePanelOpen(true);
    },
    onOpenLogs: () => setLogsOpen(true),
    onOpenEngineSettings: () => setSettingsOpen(true),
    onOpenSignals: () => {
      setActiveTab("charts");
      setPanelTab("signals");
    },
    onOpenMarketData: () => {
      setActiveTab("charts");
      setPanelTab("market");
    },
  };

  const onChartDesync = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: ["candles"] });
  }, [queryClient]);

  return (
    <div className="flex min-h-screen flex-col bg-zinc-950">
      <TopBar
        mt5={mt5}
        dataSource={dataSource}
        menuActions={menuActions}
        isAdmin={isAdmin}
        onOpenAi={() => setActiveTab("ai")}
        lastPrice={lastPrice}
        lastTickAt={lastTickAt}
        tps={tps}
        symbol={symbol}
        activeTab={activeTab}
      />

      <main className="mx-auto w-full max-w-[1600px] flex-1 p-4 pb-24 lg:pb-6">
        {activeTab === "home" && (
          <HomeView
            symbol={symbol}
            symbols={symbols}
            onSymbolChange={setSymbol}
            lastPrice={lastPrice}
            lastTickAt={lastTickAt}
            tps={tps}
            mt5={mt5}
            broker={broker}
            brokerAccount={mt5Acct}
            brokerPositions={mt5Pos}
            autoArmed={mt5AutoArmed}
            autoWhy={mt5AutoWhy}
            signals={signals}
            stats={stats}
            engineRunning={!!mt5?.engine_running}
            onConnectBroker={openConnectBroker}
            onNavigate={navigate}
          />
        )}

        {activeTab === "charts" && (
          <div className="flex flex-col gap-4 xl:flex-row">
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

              <div className="h-[48vh] min-h-[320px] overflow-hidden rounded-xl border border-zinc-800 bg-[#0c0e14] sm:h-[52vh] sm:min-h-[380px] xl:h-[calc(100vh-220px)]">
                <Chart
                  candles={candlesQuery.data?.candles ?? []}
                  liveBar={liveBar}
                  liveTick={lastPrice}
                  market={mt5?.feed?.symbols?.[symbol]?.market}
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
          </div>
        )}

        {activeTab === "ai" && (
          <div className="mx-auto max-w-4xl">
            <AiTradingView
              token={token ?? ""}
              onConnectBroker={openConnectBroker}
              autoEvents={mt5AutoEvents}
              refreshKey={aiRefreshKey}
            />
          </div>
        )}

        {activeTab === "settings" && (
          <div className="mx-auto max-w-4xl">
            <SettingsView
              token={token ?? ""}
              broker={broker}
              mt5={mt5}
              isAdmin={isAdmin}
              dataSource={dataSource}
              wsState={wsState}
              onConnectBroker={openConnectBroker}
              onOpenEngineSettings={() => setSettingsOpen(true)}
              onOpenPractice={() => setTradingOpen(true)}
              onOpenLogs={() => setLogsOpen(true)}
            />
          </div>
        )}
      </main>

      {/* mobile bottom navigation (D-039) */}
      <TabBar active={activeTab} onChange={setActiveTab} />

      {/* ------------------------------------------------------------- dialogs */}

      <Mt5ConnectDialog
        open={connectOpen}
        onClose={() => setConnectOpen(false)}
        token={token ?? ""}
        broker={broker}
        onConnected={(st) => {
          setMt5(st);
          void queryClient.invalidateQueries({ queryKey: ["candles"] });
          refreshMt5Account();
          setAiRefreshKey((k) => k + 1);
        }}
        onDisconnected={(st) => {
          setMt5(st);
          setLiveBar(null);
          refreshMt5Account();
          setAiRefreshKey((k) => k + 1);
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

      <LogViewer
        open={logsOpen}
        onClose={() => setLogsOpen(false)}
        token={token ?? ""}
      />
    </div>
  );
}
