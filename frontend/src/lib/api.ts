import type {
  HealthInfo, MeInfo, CandlesResponse, Signal, Mt5Status, Position, StatsResponse,
  ConfigResponse, EngineConfig, TradingStatus, TradingPosition, TradeRecord,
  OrderResult, ExternalSnapshot, LogEntry, Mt5Account, Mt5OpenPosition,
  Mt5HistoryPosition, Mt5Symbol, Mt5OrderResult,
} from "../types";

/**
 * REST helper. Empty base -> same-origin (dev proxy, D-005; Railway
 * single-service D-016); set VITE_API_URL for a split deploy.
 */
export const API_BASE = import.meta.env.VITE_API_URL ?? "";

/** fetch() error carrying the HTTP status and backend `detail` message. */
export class ApiError extends Error {
  constructor(
    public status: number,
    message: string
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(path: string, init?: RequestInit, token?: string | null): Promise<T> {
  const headers: Record<string, string> = {
    ...(init?.headers as Record<string, string> | undefined),
  };
  if (token) headers.Authorization = `Bearer ${token}`;
  const response = await fetch(`${API_BASE}${path}`, { ...init, headers });
  if (!response.ok) {
    let detail = `HTTP ${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      if (body && typeof body.detail === "string") detail = body.detail;
    } catch {
      /* non-JSON error body — keep the status line */
    }
    throw new ApiError(response.status, detail);
  }
  return (await response.json()) as T;
}

export function getHealth(): Promise<HealthInfo> {
  return request<HealthInfo>("/api/health");
}

export function getMe(token: string): Promise<MeInfo> {
  return request<MeInfo>("/api/me", {}, token);
}

/* ------------------------------------------------------------- market data */

export function getCandles(token: string, tf: string, limit = 500): Promise<CandlesResponse> {
  return request<CandlesResponse>(`/api/candles?tf=${tf}&limit=${limit}`, {}, token);
}

export function getPositions(token: string): Promise<{ positions: Position[] }> {
  return request<{ positions: Position[] }>("/api/positions", {}, token);
}

/* ------------------------------------------------------------------ mt5 */

export function getMt5Status(token: string): Promise<Mt5Status> {
  return request<Mt5Status>("/api/mt5/status", {}, token);
}

export function postMt5Connect(
  token: string,
  body: { server: string; login: string; password: string; terminal_path?: string }
): Promise<Mt5Status> {
  return request<Mt5Status>("/api/mt5/connect", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }, token);
}

export function postMt5Disconnect(token: string): Promise<Mt5Status> {
  return request<Mt5Status>("/api/mt5/disconnect", { method: "POST" }, token);
}

/* --------------------------------------------- D-034 real MT5 account (MCP) */

export function getMt5Account(token: string): Promise<Mt5Account> {
  return request<Mt5Account>("/api/mt5/account", {}, token);
}

export function getMt5Positions(
  token: string
): Promise<{ positions: Mt5OpenPosition[]; orders: unknown[] }> {
  return request<{ positions: Mt5OpenPosition[]; orders: unknown[] }>(
    "/api/mt5/positions", {}, token
  );
}

export function getMt5History(
  token: string,
  days = 30
): Promise<{ positions: Mt5HistoryPosition[] }> {
  return request<{ positions: Mt5HistoryPosition[] }>(
    `/api/mt5/history?days=${days}`, {}, token
  );
}

export function getMt5Symbols(token: string): Promise<{ symbols: Mt5Symbol[] }> {
  return request<{ symbols: Mt5Symbol[] }>("/api/mt5/symbols", {}, token);
}

export function postMt5Order(
  token: string,
  body: { symbol: string; side: "buy" | "sell"; volume: number; sl?: number; tp?: number }
): Promise<Mt5OrderResult> {
  return request<Mt5OrderResult>("/api/mt5/order", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }, token);
}

export function postMt5Close(
  token: string,
  body: { symbol: string; ticket: number }
): Promise<Mt5OrderResult> {
  return request<Mt5OrderResult>("/api/mt5/close", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }, token);
}

/* --------------------------------------------------------------- signals */

export function getSignals(
  token: string,
  limit = 100,
  status?: string
): Promise<{ count: number; signals: Signal[] }> {
  const q = new URLSearchParams({ limit: String(limit) });
  if (status) q.set("status", status);
  return request<{ count: number; signals: Signal[] }>(`/api/signals?${q}`, {}, token);
}

export function getStats(token: string, days = 30): Promise<StatsResponse> {
  return request<StatsResponse>(`/api/stats?days=${days}`, {}, token);
}

/* ---------------------------------------------------------------- config */

export function getConfig(token: string): Promise<ConfigResponse> {
  return request<ConfigResponse>("/api/config", {}, token);
}

export function putConfig(token: string, config: EngineConfig): Promise<ConfigResponse> {
  return request<ConfigResponse>("/api/config", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(config),
  }, token);
}

export function postAutoTrade(
  token: string,
  enabled: boolean,
  confirm?: string
): Promise<{ auto_trade: boolean }> {
  return request<{ auto_trade: boolean }>("/api/config/auto-trade", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled, confirm }),
  }, token);
}

/* ------------------------------------------------------- trading plane */

export function getTradingStatus(token: string): Promise<TradingStatus> {
  return request<TradingStatus>("/api/trading/status", {}, token);
}

export function postTradingConnect(
  token: string,
  body: { server: string; login: string; password: string; mode: "demo" | "live" }
): Promise<TradingStatus> {
  return request<TradingStatus>("/api/trading/connect", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }, token);
}

export function postTradingDisconnect(token: string): Promise<{ connected: boolean }> {
  return request<{ connected: boolean }>("/api/trading/disconnect", {
    method: "POST",
  }, token);
}

export function getTradingPositions(token: string): Promise<{ positions: TradingPosition[] }> {
  return request<{ positions: TradingPosition[] }>("/api/trading/positions", {}, token);
}

export function postTradingOrder(
  token: string,
  body: { side: "BUY" | "SELL"; volume: number; sl?: number; tp?: number }
): Promise<OrderResult> {
  return request<OrderResult>("/api/trading/order", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }, token);
}

export function postTradingClose(token: string, ticket: number): Promise<OrderResult> {
  return request<OrderResult>(`/api/trading/positions/${ticket}/close`, {
    method: "POST",
  }, token);
}

export function getTradingTrades(token: string, limit = 100): Promise<{ trades: TradeRecord[] }> {
  return request<{ trades: TradeRecord[] }>(`/api/trading/trades?limit=${limit}`, {}, token);
}

export function postTradingAutoTrade(
  token: string,
  enabled: boolean,
  confirm?: string
): Promise<{ auto_trade: boolean }> {
  return request<{ auto_trade: boolean }>("/api/trading/auto-trade", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled, confirm }),
  }, token);
}

/* ---------------------------------------------------- external + logs */

export function getExternalSnapshot(token: string): Promise<ExternalSnapshot> {
  return request<ExternalSnapshot>("/api/market/external", {}, token);
}

export function getLogs(
  token: string,
  limit = 200,
  level?: string
): Promise<{ count: number; logs: LogEntry[] }> {
  const q = new URLSearchParams({ limit: String(limit) });
  if (level) q.set("level", level);
  return request<{ count: number; logs: LogEntry[] }>(`/api/logs?${q}`, {}, token);
}

export function logsExportUrl(): string {
  return `${API_BASE}/api/logs/export.csv`;
}
