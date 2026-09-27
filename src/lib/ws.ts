'use client';

/**
 * WSClient (port of the Vite app's lib/ws.ts) — the SAME public interface
 * (on / onStatus / connect / subscribe / close / connected), now over
 * socket.io through the gateway: io("/?XTransformPort=3003").
 *
 * - token travels in the socket handshake `auth` (bound to the user's
 *   practice plane on the server)
 * - socket.io's built-in reconnection replaces the manual backoff
 * - the server emits every frame on the "msg" event with a `type` field,
 *   exactly like the original WebSocket JSON protocol
 * - cloud deployments can point the socket at an externally tunnelled
 *   market-service via NEXT_PUBLIC_MARKET_WS_URL; otherwise the sandbox
 *   gateway path (/?XTransformPort=3003) is used
 */

import { io, type Socket } from "socket.io-client";
import type { WsMessage } from "../types";

const SERVICE_PORT = 3003;

/** Optional external market-service origin (e.g. https://mt5.example.com) —
 *  inlined at build time by Next.js (NEXT_PUBLIC_*). */
const MARKET_WS_URL = process.env.NEXT_PUBLIC_MARKET_WS_URL;

type Handler = (msg: WsMessage) => void;
type StatusHandler = (status: "connecting" | "open" | "closed") => void;

export class WSClient {
  private socket: Socket | null = null;
  private token: string;
  private handlers = new Set<Handler>();
  private statusHandlers = new Set<StatusHandler>();
  private sub: { symbol: string; tf: string } | null = null;
  private closedByUser = false;

  constructor(token: string) {
    this.token = token;
  }

  get connected(): boolean {
    return this.socket?.connected ?? false;
  }

  on(handler: Handler): () => void {
    this.handlers.add(handler);
    return () => {
      this.handlers.delete(handler);
    };
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
    this.emitStatus("connecting");
    const socket = io(MARKET_WS_URL ?? `/?XTransformPort=${SERVICE_PORT}`, {
      auth: { token: this.token },
      transports: ["websocket", "polling"],
      reconnection: true,
      reconnectionAttempts: Infinity,
      reconnectionDelay: 1000,
      reconnectionDelayMax: 5000,
      timeout: 10000,
    });
    this.socket = socket;

    socket.on("connect", () => {
      this.emitStatus("open");
      if (this.sub) this.sendSubscribe(this.sub.symbol, this.sub.tf);
    });

    socket.on("disconnect", () => {
      this.emitStatus("closed");
    });

    socket.on("connect_error", () => {
      this.emitStatus("closed");
    });

    // route every "msg" frame to the registered handlers
    socket.on("msg", (payload: unknown) => {
      try {
        const msg = payload as WsMessage;
        if (!msg || typeof msg !== "object" || !("type" in msg)) return;
        this.handlers.forEach((h) => h(msg));
      } catch {
        /* malformed frame — ignore */
      }
    });
  }

  private sendSubscribe(symbol: string, tf: string): void {
    this.socket?.emit("subscribe", { type: "subscribe", channel: "market", symbol, tf });
  }

  subscribe(symbol: string, tf: string): void {
    this.sub = { symbol, tf };
    if (this.connected) this.sendSubscribe(symbol, tf);
  }

  unsubscribe(): void {
    this.sub = null;
    if (this.connected) this.socket?.emit("unsubscribe", { type: "unsubscribe", channel: "market" });
  }

  ping(): void {
    if (this.connected) this.socket?.emit("ping", { type: "ping" });
  }

  close(): void {
    this.closedByUser = true;
    this.socket?.removeAllListeners();
    this.socket?.disconnect();
    this.socket = null;
    this.emitStatus("closed");
  }
}
