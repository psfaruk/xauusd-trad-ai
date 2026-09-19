import type { HealthInfo } from "../types";

/**
 * REST helper. Empty base -> same-origin (dev proxy, D-005); set VITE_API_URL
 * for production (Vercel -> backend host, SPEC §11).
 */
export const API_BASE = import.meta.env.VITE_API_URL ?? "";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!response.ok) {
    throw new Error(`GET ${path} failed: ${response.status} ${response.statusText}`);
  }
  return (await response.json()) as T;
}

export function getHealth(): Promise<HealthInfo> {
  return request<HealthInfo>("/api/health");
}

// TODO(phase-1+): authenticated calls with the Supabase access token
// (Authorization: Bearer), /me, /candles, /signals, /config, /stats, /logs.
export const api = { getHealth };
