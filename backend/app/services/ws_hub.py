"""WebSocket hub (SPEC §7.2) — connection registry + event broadcast.

Market events (tick / bar_*) go only to clients subscribed to that symbol+tf.
Global events (signal / signal_update / account / mt5_status / engine_log)
broadcast to every authenticated client. Heartbeat every 15s.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("xauusd.ws")

HEARTBEAT_S = 15.0


@dataclass(eq=False)  # identity hash — ws object + dict fields are unhashable
class Client:
    ws: Any
    user: dict
    symbol: str | None = None
    tf: str | None = None
    last_seen: float = field(default_factory=time.monotonic)


class WSHub:
    def __init__(self) -> None:
        self._clients: set[Client] = set()
        self._lock = asyncio.Lock()

    @property
    def client_count(self) -> int:
        return len(self._clients)

    def subscribed_tfs(self) -> set[str]:
        """Timeframes at least one client watches (bar streaming optimization)."""
        return {c.tf for c in self._clients if c.tf}

    async def register(self, client: Client) -> None:
        async with self._lock:
            self._clients.add(client)

    async def unregister(self, client: Client) -> None:
        async with self._lock:
            self._clients.discard(client)

    async def subscribe(self, client: Client, symbol: str, tf: str) -> None:
        async with self._lock:
            client.symbol, client.tf = symbol, tf

    async def unsubscribe(self, client: Client) -> None:
        async with self._lock:
            client.symbol = client.tf = None

    async def _send(self, client: Client, payload: dict) -> None:
        try:
            await client.ws.send_text(json.dumps(payload, default=str))
        except Exception:  # noqa: BLE001 — dead connection drops out on next sweep
            await self.unregister(client)

    async def broadcast_market(self, event_type: str, symbol: str, tf: str, payload: dict) -> None:
        """tick / bar_open / bar_update / bar_close -> subscribers of symbol+tf."""
        msg = {"type": event_type, "symbol": symbol, "tf": tf, **payload}
        for client in list(self._clients):
            if client.symbol == symbol and client.tf == tf:
                await self._send(client, msg)

    async def broadcast_ticks(self, event_type: str, symbol: str, payload: dict) -> None:
        """tick -> any client subscribed to the symbol (any tf)."""
        msg = {"type": event_type, "symbol": symbol, **payload}
        for client in list(self._clients):
            if client.symbol == symbol:
                await self._send(client, msg)

    async def broadcast_all(self, event_type: str, payload: dict) -> None:
        """signal / signal_update / account / mt5_status / engine_log -> everyone."""
        msg = {"type": event_type, **payload}
        for client in list(self._clients):
            await self._send(client, msg)

    async def broadcast_user(self, user_id: str, event_type: str, payload: dict) -> None:
        """Per-user trading-plane events -> only that user's sockets (Phase 4).

        Event names carry a `trading_` prefix upstream so the frontend can
        distinguish plane events (positions/equity/logs) from the public feed.
        """
        msg = {"type": event_type, **payload}
        for client in list(self._clients):
            if client.user.get("id") == user_id:
                await self._send(client, msg)

    async def heartbeat_loop(self, client: Client) -> None:
        """Per-client heartbeat — exits when the socket dies."""
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_S)
                await self._send(client, {"type": "heartbeat", "ts": int(time.time() * 1000)})
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            return

    async def sweep_stale(self, timeout_s: float = 90.0) -> None:
        """Drop clients whose socket produced no traffic (handled by receive loop)."""
        now = time.monotonic()
        stale = [c for c in self._clients if now - c.last_seen > timeout_s]
        for c in stale:
            with contextlib.suppress(Exception):
                await c.ws.close()
            await self.unregister(c)
