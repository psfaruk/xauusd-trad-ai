"""routes_mt5 — SPEC §7.1 mt5 endpoints (Phase 2) + D-034 real-account panel.

POST /api/mt5/connect     (admin) connect + discover symbol + start engine
POST /api/mt5/disconnect  (admin) clean shutdown
GET  /api/mt5/status      (auth)  {status, symbol, account, broker offset}

D-034 — real MT5 terminal bridge (MCP):
GET  /api/mt5/account     (auth)  live account: balance/equity/margin/connected
GET  /api/mt5/positions   (auth)  open positions + pending orders
GET  /api/mt5/history     (auth)  closed-position history (days=1..365)
GET  /api/mt5/symbols     (auth)  Market Watch symbols (Exness set)
POST /api/mt5/order       (auth)  market order on the REAL account (MCP)
POST /api/mt5/close       (auth)  close a position by ticket (MCP)
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.auth import CurrentUser, require_admin
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
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="admin role required")
    mgr = _manager(request)
    creds = body.model_dump()
    creds["mode"] = "platform"
    try:
        return await mgr.connect(creds, owner=user["id"])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — DataSourceError etc -> 502
        raise HTTPException(status_code=502, detail=f"MT5 connect failed: {exc}") from exc


@router.post("/disconnect", dependencies=[Depends(require_admin)])
async def mt5_disconnect(request: Request) -> dict:
    return await _manager(request).disconnect()


@router.get("/status")
async def mt5_status(request: Request, user: CurrentUser) -> dict:
    return await _manager(request).status()


# ------------------------------------------------------------------ D-034
@router.get("/account")
async def mt5_account(user: CurrentUser) -> dict:
    """Live Exness account through the real MetaTrader 5 terminal (MCP)."""
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
async def mt5_positions(user: CurrentUser) -> dict:
    res = await _mcp_call(terminal_client().positions)
    return {"positions": res.get("positions", []), "orders": res.get("orders", [])}


@router.get("/history")
async def mt5_history(
    user: CurrentUser,
    days: int = Query(default=30, ge=1, le=365),
    symbol: str | None = Query(default=None, max_length=32),
) -> dict:
    res = await _mcp_call(terminal_client().history, days=days, symbol=symbol)
    return {"positions": res.get("positions", [])}


@router.get("/symbols")
async def mt5_symbols(user: CurrentUser) -> dict:
    res = await _mcp_call(terminal_client().symbols)
    return {"symbols": res}


@router.post("/order")
async def mt5_order(body: OrderBody, user: CurrentUser) -> dict:
    """Market order on the REAL account — executed by MetaTrader 5 (MCP)."""
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
async def mt5_close(body: CloseBody, user: CurrentUser) -> dict:
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
