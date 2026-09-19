import type { HealthInfo, MeInfo } from "../types";

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

// TODO(phase-2+): /candles, /signals, /config, /stats, /logs with the token.
