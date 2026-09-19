/**
 * Shared frontend types. Candle/Tick/Signal shapes finalize in Phase 2/3 when
 * the WS events (SPEC §7.2) and REST payloads are wired.
 */

export type Timeframe = "M1" | "M5" | "M15" | "M30" | "H1" | "H4" | "D1";

export type SignalDirection = "BUY" | "SELL";

export type SignalStatus = "active" | "won" | "lost" | "expired" | "cancelled";

export interface HealthInfo {
  status: string;
  version: string;
  data_source: "mock" | "mt5";
  db: boolean;
}

/** GET /api/me — profile + role (SPEC §7.1). */
export interface MeInfo {
  id: string;
  email: string | null;
  display_name: string | null;
  role: "admin" | "viewer" | string;
}
