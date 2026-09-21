"""routes_mt5 — MT5 endpoints.

Phase 2 (SPEC §7.1):
POST /api/mt5/connect     (auth)  per-user broker connect (D-037)
POST /api/mt5/disconnect  (auth)  drop the user's broker connection
GET  /api/mt5/status      (auth)  per-user connection state + account

D-034 — real MT5 terminal bridge (MCP):
GET  /api/mt5/account     (auth + connection) live account snapshot
GET  /api/mt5/positions   (auth + connection) open positions + orders
GET  /api/mt5/history     (auth + connection) closed-position history
GET  /api/mt5/symbols     (auth)  Market Watch symbols (read-only)
POST /api/mt5/order       (auth + connection) market order (REAL account)
POST /api/mt5/close       (auth + connection) close a position by ticket

D-036 — AI signal -> auto-order on the REAL account:
GET  /api/mt5/auto-trade  (auth)  arm state + readiness + risk summary
POST /api/mt5/auto-trade  (admin OR connected user) {enabled, confirm}

D-037 — every trading route is scoped to the requesting user's OWN broker
connection: users only ever see/trade the account they connected.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.auth import CurrentUser
from app.mt5.auto_trader import ArmError
from app.mt5.broker_connect import (
    AccountMismatchError,
    BrokerUnavailableError,
    NotConnectedError,
)
from app.mt5.mcp import MCPError, terminal_client

router = APIRouter(prefix="/api/mt5", tags=["mt5"])


class ConnectBody(BaseModel):
    server: str = Field(min_length=1, max_length=200)
    login: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=1, max_length=200)
    terminal_path: str | None = Field(default=None, max_length=500)


class OrderBody(BaseModel):
    symbol: str = Field(min_length=1, max_length=32)
    side: str = Field(pattern="^(buy|sell|BUY|SELL)$")
    volume: float = Field(gt=0, le=1000)
    sl: float | None = Field(default=None, gt=0)
    tp: float | None = Field(default=None, gt=0)
    comment: str = Field(default="", max_length=31)


class CloseBody(BaseModel):
    symbol: str = Field(min_length=1, max_length=32)
    ticket: int = Field(gt=0)


def _manager(request: Request):
    return request.app.state.mt5


def _broker(request: Request):
    svc = getattr(request.app.state, "broker_connect", None)
    if svc is None:
        raise HTTPException(
            status_code=503,
            detail="broker connection service not initialized on this server",
        )
    return svc


def _is_demo(request: Request) -> bool:
    return getattr(request.app.state, "data_source", "") == "mock"


def _require_connection(request: Request, user: CurrentUser) -> None:
    """D-037: trading routes only for users with an active broker connection."""
    try:
        _broker(request).get(user["id"])
    except NotConnectedError as exc:
        raise HTTPException(status_code=428, detail=str(exc)) from exc


async def _mcp_call(fn, *args, **kwargs):
    """Run a blocking MCP call in a worker thread; map errors to 502."""
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except MCPError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"MT5 terminal bridge unavailable: {exc}",
        ) from exc


@router.post("/connect")
async def mt5_connect(body: ConnectBody, request: Request, user: CurrentUser) -> dict:
    """D-037: connect THIS user's broker account through the REAL terminal.

    The entered (server, login) must match the live terminal session; the
    password is Fernet-encrypted at rest. On success the user is bound to
    the account and every /api/mt5/* route serves THEIR connection.
    """
    svc = _broker(request)
    try:
        result = await svc.connect(
            user["id"], body.server, body.login, body.password
        )
    except AccountMismatchError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except BrokerUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    # ensure the platform data plane (engines + feed) is running too
    mgr = _manager(request)
    try:
        if mgr.state.status != "connected":
            creds = body.model_dump()
            creds["mode"] = "platform"
            platform = await mgr.connect(creds, owner=user["id"])
            # surface the platform plane (symbol discovery, feed info) too
            for k, v in platform.items():
                if k != "status":
                    result.setdefault(k, v)
    except Exception as exc:  # noqa: BLE001 — data plane best-effort
        # the broker connection itself is fine; the feed keeps retrying
        result["note"] = f"market feed starting in background ({type(exc).__name__})"
    result["engine_running"] = mgr.state.status == "connected"
    return result


@router.post("/disconnect")
async def mt5_disconnect(request: Request, user: CurrentUser) -> dict:
    """Drop the requesting user's broker connection (never anyone else's)."""
    result = await _broker(request).disconnect(user["id"])
    # D-037: tell every WS client about the (per-user) state change — the
    # platform market plane keeps running for everyone else.
    hub = getattr(request.app.state, "hub", None)
    if hub is not None:
        try:
            st = await _manager(request).status()
            st["broker"] = result
            await hub.broadcast_all("mt5_status", st)
        except Exception:  # noqa: BLE001 — broadcast is best-effort
            pass
    return result


@router.get("/status")
async def mt5_status(request: Request, user: CurrentUser) -> dict:
    """Platform market state (symbol/feed/engine) + the USER's broker
    connection (D-037: per-user, `broker` block)."""
    st = await _manager(request).status()
    st["broker"] = await _broker(request).status(user["id"])
    return st


# ------------------------------------------------------------------ D-034
@router.get("/account")
async def mt5_account(request: Request, user: CurrentUser) -> dict:
    """Live account through the real MetaTrader 5 terminal (user-scoped)."""
    _require_connection(request, user)
    res = await _mcp_call(terminal_client().account)
    acct = res.get("account", {})
    term = res.get("terminal", {})
    return {
        "connected": term.get("server_connected") is True,
        "bridge": "mt5-terminal-mcp",
        "login": acct.get("login"),
        "name": acct.get("name"),
        "server": acct.get("server"),
        "broker": acct.get("broker"),
        "type": acct.get("type"),
        "currency": acct.get("currency"),
        "leverage": acct.get("leverage"),
        "margin_mode": acct.get("margin_mode"),
        "balance": acct.get("balance"),
        "equity": acct.get("equity"),
        "margin": acct.get("margin"),
        "margin_free": acct.get("margin_free"),
        "profit": acct.get("profit"),
        "mcp_trade_allowed": term.get("mcp_trade_allowed"),
        "build": term.get("build"),
    }


@router.get("/positions")
async def mt5_positions(request: Request, user: CurrentUser) -> dict:
    _require_connection(request, user)
    res = await _mcp_call(terminal_client().positions)
    return {"positions": res.get("positions", []), "orders": res.get("orders", [])}


@router.get("/history")
async def mt5_history(
    request: Request,
    user: CurrentUser,
    days: int = Query(default=30, ge=1, le=365),
    symbol: str | None = Query(default=None, max_length=32),
) -> dict:
    _require_connection(request, user)
    res = await _mcp_call(terminal_client().history, days=days, symbol=symbol)
    return {"positions": res.get("positions", [])}


@router.get("/symbols")
async def mt5_symbols(user: CurrentUser) -> dict:
    """Market Watch symbols (read-only market data — no connection needed)."""
    res = await _mcp_call(terminal_client().symbols)
    return {"symbols": res}


@router.post("/order")
async def mt5_order(body: OrderBody, request: Request, user: CurrentUser) -> dict:
    """Market order on the REAL account — executed by MetaTrader 5 (MCP)."""
    _require_connection(request, user)
    res = await _mcp_call(
        terminal_client().market_order,
        symbol=body.symbol,
        side=body.side.lower(),
        volume=body.volume,
        sl=body.sl,
        tp=body.tp,
        comment=body.comment or "platform",
    )
    ok = res.get("retcode") == 10009
    return {
        "ok": ok,
        "retcode": res.get("retcode"),
        "detail": res.get("retcode_details"),
        "deal": res.get("deal"),
        "order": res.get("order"),
        "price": res.get("price"),
        "volume": res.get("volume"),
        "symbol": res.get("symbol"),
    }


@router.post("/close")
async def mt5_close(body: CloseBody, request: Request, user: CurrentUser) -> dict:
    _require_connection(request, user)
    res = await _mcp_call(
        terminal_client().close_position, symbol=body.symbol, ticket=body.ticket
    )
    ok = res.get("retcode") == 10009
    return {
        "ok": ok,
        "retcode": res.get("retcode"),
        "detail": res.get("retcode_details"),
        "price": res.get("price"),
        "profit": None,
    }


# ------------------------------------------------------------------ D-036
def _auto_trader(request: Request):
    trader = getattr(request.app.state, "mt5_auto", None)
    if trader is None:
        raise HTTPException(
            status_code=503,
            detail="live auto-trade module not initialized on this server",
        )
    return trader


@router.get("/auto-trade")
async def mt5_auto_trade_status(request: Request, user: CurrentUser) -> dict:
    """AI-signal -> auto-order state on the REAL MT5 terminal."""
    return await _auto_trader(request).status()


class AutoTradeLiveBody(BaseModel):
    enabled: bool
    confirm: str | None = Field(default=None, max_length=16)


@router.post("/auto-trade")
async def mt5_auto_trade_arm(
    body: AutoTradeLiveBody, request: Request, user: CurrentUser
) -> dict:
    """Arm/disarm REAL auto-execution (typed confirmation "ENABLE").

    D-037: any user with an ACTIVE broker connection may arm auto-trade —
    orders execute on the account THEY connected (verified against the
    terminal session). Admins may arm without a connection (platform plane).
    """
    if body.enabled and body.confirm != "ENABLE":
        raise HTTPException(
            status_code=400,
            detail=(
                'typed confirmation required: {"enabled": true, "confirm": "ENABLE"}'
            ),
        )
    if user.get("role") != "admin":
        try:
            _broker(request).get(user["id"])
        except NotConnectedError as exc:
            raise HTTPException(
                status_code=428,
                detail="connect your broker account before arming auto-trade",
            ) from exc
    trader = _auto_trader(request)
    try:
        await trader.arm(body.enabled, owner=user["id"])
    except ArmError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    st = await trader.status()
    return {"armed": st["armed"], "terminal": st["terminal"]}
