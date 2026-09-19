import type { HealthInfo, MeInfo, CandlesResponse, Signal, Mt5Status, Position, StatsResponse, ConfigResponse, EngineConfig } from "../types";

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
