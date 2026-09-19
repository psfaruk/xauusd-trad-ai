/**
 * WebSocket helper (SPEC §7.2). The full client — auto-reconnect with
 * exponential backoff (1s -> 30s cap), re-subscribe + REST gap-heal on
 * reconnect — arrives in Phase 2.
 */

export const WS_BASE =
  import.meta.env.VITE_WS_URL ??
  (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws";

export function wsUrl(token: string): string {
  return `${WS_BASE}?token=${encodeURIComponent(token)}`;
}

// TODO(phase-2): connectWs(symbol, tf) with backoff + heartbeat (15s).
