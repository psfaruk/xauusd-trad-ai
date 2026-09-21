"""WebSocket endpoint /ws (SPEC §7.2).

Auth: Supabase JWT via query param `?token=...` (browsers cannot set headers
on WebSocket). Protocol:

  client -> server: {"type":"subscribe","channel":"market","symbol":..,"tf":..}
                    {"type":"unsubscribe","channel":"market"}
  server -> client: tick / bar_open / bar_update / bar_close (market channel)
                    signal / signal_update / account / mt5_status /
                    engine_log / heartbeat (global)

Heartbeat every 15s; dead sockets are dropped after 90s of silence.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.auth import resolve_role, verify_supabase_token

logger = logging.getLogger("xauusd.ws")

router = APIRouter()


async def _ws_user(ws: WebSocket) -> dict | None:
    """Validate the query-param token against Supabase (same rule as REST)."""
    token = ws.query_params.get("token", "")
    if not token:
        return None
    http = getattr(ws.app.state, "http", None)
    if http is None:
        return None
    user = await verify_supabase_token(http, token)
    if user is None:
        return None
    try:
        role = await resolve_role(ws, str(user["id"]), user.get("email"))
    except Exception:  # noqa: BLE001 — role fallback to viewer
        role = "viewer"
    return {"id": str(user["id"]), "email": user.get("email"), "role": role}


@router.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    user = await _ws_user(ws)
    if user is None:
        await ws.close(code=4401, reason="invalid or missing token")
        return
    await ws.accept()

    from app.services.ws_hub import Client

    client = Client(ws=ws, user=user)
    hub = ws.app.state.hub
    await hub.register(client)
    hb_task = asyncio.create_task(hub.heartbeat_loop(client))
    # D-044 — auto-provision the user's practice plane so their account
    # events (balance/positions) start streaming immediately (best-effort;
    # the REST routes ensure it too on first touch).
    trading = getattr(ws.app.state, "trading", None)

    async def _ensure_plane() -> None:
        try:
            await trading.ensure_plane(user["id"])  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 — plane provisions on next API touch
            pass

    if trading is not None:
        asyncio.create_task(_ensure_plane())
    try:
        while True:
            raw = await ws.receive_text()
            client.last_seen = time.monotonic()
            try:
                msg = json.loads(raw)
            except ValueError:
                await ws.send_text(json.dumps({"type": "error", "detail": "bad json"}))
                continue
            mtype = msg.get("type")
            if mtype == "subscribe" and msg.get("channel") == "market":
                symbol = str(msg.get("symbol") or "")[:32]
                tf = str(msg.get("tf") or "M15")[:4]
                if symbol and tf:
                    await hub.subscribe(client, symbol, tf)
                    await ws.send_text(
                        json.dumps(
                            {"type": "subscribed", "symbol": symbol, "tf": tf}
                        )
                    )
            elif mtype == "unsubscribe" and msg.get("channel") == "market":
                await hub.unsubscribe(client)
                await ws.send_text(json.dumps({"type": "unsubscribed"}))
            elif mtype == "ping":
                await ws.send_text(
                    json.dumps({"type": "heartbeat", "ts": int(time.time() * 1000)})
                )
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001 — never crash the endpoint
        logger.exception("ws loop error")
    finally:
        hb_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await hb_task
        await hub.unregister(client)
        with contextlib.suppress(Exception):
            await ws.close()
