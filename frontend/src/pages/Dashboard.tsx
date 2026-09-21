import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import BottomNav from "../components/BottomNav";
import ErrorBoundary from "../components/ErrorBoundary";
import HomeView from "../views/HomeView";
import ChartsView from "../views/ChartsView";
import AiView from "../views/AiView";
import SettingsView from "../views/SettingsView";
import { useAuth } from "../lib/auth";
import {
  getCandles, getMt5Status, getSignals, getStats, getHealth, getMe,
  getMt5Account, getMt5AutoTrade,
} from "../lib/api";
import { WSClient } from "../lib/ws";
import { feed } from "../state/feed";
import type {
  AppTab, Mt5Account, Mt5AutoTradeStatus, Mt5Status, Timeframe, WsMessage,
  WsMt5AutoMsg,
} from "../types";

/**
 * Dashboard (D-041 rewrite) — a mobile-app-style shell: 4 tabs + bottom nav
 * on EVERY form factor (no 3-dot menu; all its functions moved into the
 * tabs). Data architecture:
 *
 *  - ONE WebSocket for the whole session (created once per token). The
 *    message handler is STABLE — it reads symbol/tf through a ref — so
 *    switching pair/timeframe NEVER tears the socket down again (that was
 *    the "chart breaks / disconnects" bug). Subscribing is just a send().
 *  - FAST frames (ticks, forming bars) go into the feed store; leaf
 *    components subscribe themselves — the app tree does NOT re-render at
 *    20fps (the "app is slow" bug).
 *  - Slow state (mt5 status, engine logs, AI events) lives here, capped.
 */

const LOG_CAP = 60;
const EVENT_CAP = 60;

export default function Dashboard() {
  const { session, signOut } = useAuth();
  const token = session?.access_token ?? null;
  const queryClient = useQueryClient();

  /* ------------------------------------------------------------- ui state */
  const [activeTab, setActiveTab] = useState<AppTab>("home");
  const [tf, setTf] = useState<Timeframe>("M1");
  const [symbol, setSymbol] = useState("XAUUSD");
  const [focusSignalId, setFocusSignalId] = useState<string | null>(null);

  /* ----------------------------------------------------------- slow state */
  const [mt5, setMt5] = useState<Mt5Status | null>(null);
  const [dataSource, setDataSource] = useState("");
  const [role, setRole] = useState("viewer");
  const [wsState, setWsState] = useState<"connecting" | "open" | "closed">("connecting");
  const [engineLogs, setEngineLogs] = useState<{ level: string; message: string }[]>([]);
  const [autoStatus, setAutoStatus] = useState<Mt5AutoTradeStatus | null>(null);
  const [autoEvents, setAutoEvents] = useState<WsMt5AutoMsg[]>([]);
  const [brokerAccount, setBrokerAccount] = useState<Mt5Account | null>(null);
  const [aiRefreshKey, setAiRefreshKey] = useState(0);

  const isAdmin = useMemo(() => role === "admin", [role]);

  /* --------------------------------------------------------------- queries */
  const candlesQuery = useQuery({
    queryKey: ["candles", tf, symbol],
    enabled: !!token,
    queryFn: () => getCandles(token!, tf, 400, symbol),
    refetchOnWindowFocus: false,
    staleTime: 60_000,
    retry: 2,
    retryDelay: 1_500,
  });

  const signalsQuery = useQuery({
    queryKey: ["signals"],
    enabled: !!token,
    queryFn: () => getSignals(token!, 150),
    refetchOnWindowFocus: false,
  });

  const statsQuery = useQuery({
    queryKey: ["stats"],
    enabled: !!token,
    queryFn: () => getStats(token!, 30),
    refetchOnWindowFocus: false,
  });

  const symbols = useMemo(() => {
    const list = mt5?.symbols?.length ? mt5.symbols : ["XAUUSD"];
    return [...new Set(list)];
  }, [mt5?.symbols]);

  /* --------------------------------------------------- stable ws handling */
  const wsRef = useRef<WSClient | null>(null);
  const subRef = useRef({ symbol, tf });
  subRef.current = { symbol, tf };

  const handleWsMessage = useCallback(
    (msg: WsMessage) => {
      switch (msg.type) {
        case "tick":
          feed.pushTick(msg);
          break;
        case "bar_open":
        case "bar_update":
        case "bar_close":
          feed.pushBar(msg);
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
                  symbols: msg.symbols ?? prev.symbols,
                  feed: msg.feed ?? prev.feed,
                }
              : prev,
          );
          break;
        case "engine_log":
          setEngineLogs((prev) => [
            ...prev.slice(-(LOG_CAP - 1)),
            { level: msg.level, message: msg.message },
          ]);
          break;
        case "mt5_auto": {
          const ev = msg as WsMt5AutoMsg;
          setAutoEvents((prev) => [...prev.slice(-(EVENT_CAP - 1)), ev]);
          if (ev.event === "order" || ev.event === "close") {
            setAiRefreshKey((k) => k + 1);
          }
          if (ev.event === "armed" || ev.event === "disarmed") {
            void getMt5AutoTrade(token ?? "")
              .then((s) => setAutoStatus(s))
              .catch(() => undefined);
          }
          break;
        }
        default:
          break;
      }
    },
    [queryClient, token],
  );

  // ONE WS per session — never rebuilt on symbol/tf changes
  useEffect(() => {
    if (!token) return;
    const ws = new WSClient(token);
    wsRef.current = ws;
    const offMsg = ws.on(handleWsMessage);
    const offStatus = (ws as WSClient).onStatus(setWsState);
    ws.connect();
    return () => {
      offMsg();
      offStatus();
      ws.close();
      wsRef.current = null;
      feed.clearAll();
    };
  }, [token, handleWsMessage]);

  // re-subscribe when the watched pair/tf changes (cheap send, no reconnect)
  useEffect(() => {
    const ws = wsRef.current;
    if (ws && ws.connected) ws.subscribe(symbol, tf);
  }, [symbol, tf, wsState]);

  // feed reset on symbol switch
  const onSymbolChange = useCallback((s: string) => {
    feed.clearSymbol(s === "XAUUSD" ? "BTCUSD" : "XAUUSD");
    setSymbol(s);
  }, []);

  const onDesync = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: ["candles"] });
  }, [queryClient]);

  /* ------------------------------------------------------------ polling */
  const refreshSlow = useCallback(() => {
    if (!token) return;
    getMt5Status(token).then(setMt5).catch(() => undefined);
    getMt5AutoTrade(token).then(setAutoStatus).catch(() => undefined);
    getMt5Account(token)
      .then((a) => setBrokerAccount(a.connected ? a : null))
      .catch(() => undefined); // transient errors keep the previous state —
      // only an explicit connected:false clears the account (honest UX)
  }, [token]);

  useEffect(() => {
    if (!token) return;
    refreshSlow();
    getHealth().then((h) => setDataSource(h.data_source)).catch(() => undefined);
    getMe(token).then((me) => setRole(me.role)).catch(() => undefined);
    const timer = window.setInterval(refreshSlow, 8_000);
    return () => window.clearInterval(timer);
  }, [token, refreshSlow, wsState]);

  // candle refetch on (re)connect
  useEffect(() => {
    if (wsState === "open") {
      void queryClient.invalidateQueries({ queryKey: ["candles"] });
    }
  }, [wsState, queryClient]);

  /* -------------------------------------------------------------- derived */
  const signals = signalsQuery.data?.signals ?? [];
  const stats = statsQuery.data ?? null;
  const broker = mt5?.broker ?? null;
  const autoArmed = autoStatus?.armed ?? false;
  const autoWhy = autoStatus?.why ?? null;
  const candles = candlesQuery.data?.candles ?? [];

  const navigate = useCallback((tab: AppTab) => {
    setActiveTab(tab);
    if (typeof window !== "undefined") window.scrollTo({ top: 0 });
  }, []);

  const openSignalAnalysis = useCallback(
    (id: string) => {
      setFocusSignalId(id);
      navigate("charts");
    },
    [navigate],
  );

  /* --------------------------------------------------------------- render */
  return (
    <div className="flex min-h-screen flex-col overflow-x-hidden bg-zinc-950">
      {/* slim app header — brand + live connection */}
      <header className="sticky top-0 z-30 border-b border-zinc-800/70 bg-zinc-950/90 backdrop-blur-md">
        <div className="mx-auto flex h-12 w-full max-w-3xl items-center gap-2 px-4">
          <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md bg-gold/15 text-[11px] font-black text-gold">
            Au
          </span>
          <span className="truncate text-sm font-bold tracking-tight text-zinc-100">
            Gold&nbsp;AI&nbsp;Trader
          </span>
          <span className="ml-auto flex shrink-0 items-center gap-2 text-[10px] font-semibold">
            {mt5?.status === "connected" && (
              <span className="flex items-center gap-1 text-emerald-400">
                <span className="relative flex h-1.5 w-1.5">
                  <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-400 opacity-75" />
                  <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-emerald-400" />
                </span>
                MT5
              </span>
            )}
            {mt5?.status === "reconnecting" && (
              <span className="flex items-center gap-1 text-amber-400">reconnecting…</span>
            )}
            {mt5?.status === "disconnected" && (
              <span className="flex items-center gap-1 text-red-400">offline</span>
            )}
            <button
              type="button"
              onClick={() => void signOut()}
              className="rounded-lg border border-zinc-800 px-2 py-1 text-zinc-500 hover:text-zinc-300"
              aria-label="Sign out"
            >
              exit
            </button>
          </span>
        </div>
      </header>

      <main className="mx-auto w-full max-w-3xl flex-1 px-3 pb-28 pt-3 sm:px-4">
        <ErrorBoundary label="App">
          {activeTab === "home" && (
            <HomeView
              symbol={symbol}
              symbols={symbols}
              onSymbolChange={onSymbolChange}
              mt5={mt5}
              broker={broker}
              brokerAccount={brokerAccount}
              autoArmed={autoArmed}
              autoWhy={autoWhy}
              signals={signals}
              stats={stats}
              onOpenAi={() => navigate("ai")}
              onOpenSettings={() => navigate("settings")}
              onOpenSignal={openSignalAnalysis}
            />
          )}
          {activeTab === "charts" && (
            <ChartsView
              symbol={symbol}
              symbols={symbols}
              onSymbolChange={onSymbolChange}
              tf={tf}
              onTfChange={setTf}
              candles={candles}
              candlesLoading={candlesQuery.isLoading}
              signals={signals}
              mt5={mt5}
              wsState={wsState}
              onDesync={onDesync}
              focusSignalId={focusSignalId}
              onFocusSignalConsumed={() => setFocusSignalId(null)}
            />
          )}
          {activeTab === "ai" && (
            <AiView
              token={token ?? ""}
              symbol={symbol}
              autoStatus={autoStatus}
              autoEvents={autoEvents}
              refreshKey={aiRefreshKey}
              signals={signals}
              onArmChanged={refreshSlow}
            />
          )}
          {activeTab === "settings" && (
            <SettingsView
              token={token ?? ""}
              mt5={mt5}
              broker={broker}
              brokerAccount={brokerAccount}
              isAdmin={isAdmin}
              dataSource={dataSource}
              engineLogs={engineLogs}
              onBrokerConnected={() => {
                refreshSlow();
                void queryClient.invalidateQueries({ queryKey: ["candles"] });
              }}
            />
          )}
        </ErrorBoundary>
      </main>

      <BottomNav active={activeTab} onChange={navigate} />
    </div>
  );
}
