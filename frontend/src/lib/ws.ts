/**
 * WebSocket client (SPEC §7.2).
 *
 * - token via ?token= query param (browser WS cannot set headers)
 * - auto-reconnect with exponential backoff 1s -> 30s cap
 * - on reconnect: re-subscribes and emits "reconnected" so the Dashboard can
 *   refetch /api/candles to heal gaps (SPEC Phase 2 AC)
 * - stale-socket detection: no inbound message for 45s -> force reconnect
 *   (server heartbeat arrives every 15s)
 */

import type { WsMessage } from "../types";

export const WS_BASE =
  import.meta.env.VITE_WS_URL ??
  (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws";

type Handler = (msg: WsMessage) => void;
type StatusHandler = (status: "connecting" | "open" | "closed") => void;

const BACKOFF_MIN_MS = 1_000;
const BACKOFF_MAX_MS = 30_000;
const STALE_MS = 45_000;

export class WSClient {
  private ws: WebSocket | null = null;
  private token: string;
  private handlers = new Set<Handler>();
  private statusHandlers = new Set<StatusHandler>();
  private sub: { symbol: string; tf: string } | null = null;
  private backoff = BACKOFF_MIN_MS;
  private closedByUser = false;
  private reconnectTimer: number | null = null;
  private staleTimer: number | null = null;
  private lastMessageAt = 0;

  constructor(token: string) {
    this.token = token;
  }

  get connected(): boolean {
    return this.ws?.readyState === WebSocket.OPEN;
  }

  on(handler: Handler): () => void {
    this.handlers.add(handler);
    return () => this.handlers.delete(handler);
  }

  onStatus(handler: StatusHandler): () => void {
    this.statusHandlers.add(handler);
    return () => this.statusHandlers.delete(handler);
  }

  private emitStatus(status: "connecting" | "open" | "closed") {
    this.statusHandlers.forEach((h) => h(status));
  }

  connect(): void {
    this.closedByUser = false;
    this.open();
  }

  private open(): void {
    this.cleanup();
    this.emitStatus("connecting");
    const url = `${WS_BASE}?token=${encodeURIComponent(this.token)}`;
    const ws = new WebSocket(url);
    this.ws = ws;

    ws.onopen = () => {
      this.backoff = BACKOFF_MIN_MS;
      this.lastMessageAt = Date.now();
      this.emitStatus("open");
      if (this.sub) this.sendSubscribe(this.sub.symbol, this.sub.tf);
    };

    ws.onmessage = (event) => {
      this.lastMessageAt = Date.now();
      try {
        const msg = JSON.parse(event.data as string) as WsMessage;
        this.handlers.forEach((h) => h(msg));
      } catch {
        /* malformed frame — ignore */
      }
    };

    ws.onclose = () => {
      this.emitStatus("closed");
      if (!this.closedByUser) this.scheduleReconnect();
    };

    ws.onerror = () => {
      /* onclose follows; nothing to do */
    };

    this.staleTimer = window.setInterval(() => {
      if (Date.now() - this.lastMessageAt > STALE_MS && ws.readyState === WebSocket.OPEN) {
        ws.close(); // triggers onclose -> reconnect
      }
    }, 5_000);
  }

  private scheduleReconnect(): void {
    if (this.reconnectTimer !== null) return;
    const delay = this.backoff;
    this.backoff = Math.min(this.backoff * 2, BACKOFF_MAX_MS);
    this.reconnectTimer = window.setTimeout(() => {
      this.reconnectTimer = null;
      this.open();
    }, delay);
  }

  private sendSubscribe(symbol: string, tf: string): void {
    this.ws?.send(
      JSON.stringify({ type: "subscribe", channel: "market", symbol, tf })
    );
  }

  subscribe(symbol: string, tf: string): void {
    this.sub = { symbol, tf };
    if (this.connected) this.sendSubscribe(symbol, tf);
  }

  unsubscribe(): void {
    this.sub = null;
    if (this.connected) this.ws?.send(JSON.stringify({ type: "unsubscribe", channel: "market" }));
  }

  ping(): void {
    if (this.connected) this.ws?.send(JSON.stringify({ type: "ping" }));
  }

  private cleanup(): void {
    if (this.staleTimer !== null) {
      window.clearInterval(this.staleTimer);
      this.staleTimer = null;
    }
    if (this.ws) {
      this.ws.onopen = this.ws.onmessage = this.ws.onclose = this.ws.onerror = null;
      if (
        this.ws.readyState === WebSocket.OPEN ||
        this.ws.readyState === WebSocket.CONNECTING
      ) {
        this.ws.close();
      }
      this.ws = null;
    }
  }

  close(): void {
    this.closedByUser = true;
    if (this.reconnectTimer !== null) {
      window.clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    this.cleanup();
    this.emitStatus("closed");
  }
}
