"""routes_mt5 — MT5 endpoints.

Phase 2 (SPEC §7.1):
POST /api/mt5/connect     (auth)  per-user broker link (D-037/D-044)
POST /api/mt5/disconnect  (auth)  drop the user's broker link
GET  /api/mt5/status      (auth)  per-user connection state + platform feed

D-034 — the REAL terminal bridge (INSTITUTION-OPERATED, D-044):
GET  /api/mt5/account     (admin) live institution account snapshot
GET  /api/mt5/positions   (admin) open positions + orders
GET  /api/mt5/history     (admin) closed-position history
GET  /api/mt5/symbols     (auth)  Market Watch symbols (read-only)
POST /api/mt5/order       (admin) market order (institution account)
POST /api/mt5/close       (admin) close a position by ticket

D-036/D-044 — AI signal -> auto-order:
GET  /api/mt5/auto-trade  (auth)  PER-USER arm state + the user's OWN
                                   account balance/risk (admins additionally
                                   get the institution terminal block)
POST /api/mt5/auto-trade  (auth)  arm/disarm — non-admins arm THEIR OWN
                                   practice plane; admins arm the institution
                                   terminal executor.

D-044 institutional isolation: the trading terminal is company infrastructure
— its account/balance/positions/history are visible to ADMINS ONLY. Every
regular user trades on their own auto-provisioned practice plane (own
balance, own positions, own risk settings) via /api/trading/* and the
per-user auto-trade endpoints below.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.auth import AdminUser, CurrentUser
from app.mt5.auto_trader import ArmError
from app.mt5.broker_connect import (
    AccountMismatchError,
    BrokerUnavailableError,
    NotConnectedError,
)
from app.mt5.mcp import (
    TRADING_NOT_PERMITTED_HINT,
    MCPError,
    terminal_client,
)

router = APIRouter(prefix="/api/mt5", tags=["mt5"])

logger = logging.getLogger("xauusd.routes.mt5")


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


def _trading(request: Request):
    svc = getattr(request.app.state, "trading", None)
    if svc is None:
        raise HTTPException(
            status_code=503,
            detail="trading service not initialized on this server",
        )
    return svc


def _require_connection(request: Request, user: CurrentUser) -> None:
    """D-037: broker-link routes only for users with an active link."""
    try:
        _broker(request).get(user["id"])
    except NotConnectedError as exc:
        raise HTTPException(status_code=428, detail=str(exc)) from exc


async def _mcp_call(fn, *args, **kwargs):
    """Run a blocking MCP call in a worker thread; map errors honestly."""
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except MCPError as exc:
        # D-040: a terminal-side trading refusal is NOT a bridge outage —
        # say exactly what to enable instead of "bridge unavailable".
        if "not permitted" in str(exc):
            raise HTTPException(
                status_code=409, detail=TRADING_NOT_PERMITTED_HINT
            ) from exc
        raise HTTPException(
            status_code=502,
            detail=f"MT5 terminal bridge unavailable: {exc}",
        ) from exc


@router.post("/connect")
async def mt5_connect(body: ConnectBody, request: Request, user: CurrentUser) -> dict:
    """D-037/D-044: link a broker account to THIS user.

    Admins bind the institution terminal session (verified, live data);
    regular users store their OWN broker profile encrypted per-user —
    execution stays on their practice plane, isolation is total.
    """
    svc = _broker(request)
    try:
        result = await svc.connect(
            user["id"], body.server, body.login, body.password,
            admin=user.get("role") == "admin",
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
# D-044: the terminal is INSTITUTION infrastructure — these routes are
# admin-only. Regular users trade their own practice planes (/api/trading).
@router.get("/account")
async def mt5_account(request: Request, user: AdminUser) -> dict:
    """Institution account through the real terminal (admin only)."""
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
async def mt5_positions(request: Request, user: AdminUser) -> dict:
    res = await _mcp_call(terminal_client().positions)
    return {"positions": res.get("positions", []), "orders": res.get("orders", [])}


@router.get("/history")
async def mt5_history(
    request: Request,
    user: AdminUser,
    days: int = Query(default=30, ge=1, le=365),
    symbol: str | None = Query(default=None, max_length=32),
) -> dict:
    res = await _mcp_call(terminal_client().history, days=days, symbol=symbol)
    return {"positions": res.get("positions", [])}


@router.get("/symbols")
async def mt5_symbols(user: CurrentUser) -> dict:
    """Market Watch symbols (read-only market data — no connection needed)."""
    res = await _mcp_call(terminal_client().symbols)
    return {"symbols": res}


@router.post("/order")
async def mt5_order(body: OrderBody, request: Request, user: AdminUser) -> dict:
    """Market order on the INSTITUTION account (admin only)."""
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
async def mt5_close(body: CloseBody, request: Request, user: AdminUser) -> dict:
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


def _markets_snapshot(request: Request) -> dict:
    """Per-symbol broker-market state from the institution feed (public
    info: market open/closed — no account details).

    D-076 — every configured signal market reports (oil/index join the
    forex session clock; BTC stays 24/7)."""
    trader = getattr(request.app.state, "mt5_auto", None)
    if trader is None:
        return {}
    try:
        state = trader._market_state  # noqa: SLF001 — route glue
    except AttributeError:
        return {}
    cfg = getattr(trader, "_cfg", None)
    watch = list(getattr(cfg, "signal_symbols", None) or (
        "XAUUSD", "BTCUSD", "USOIL", "USTEC"
    ))
    out: dict = {}
    for sym in watch:
        try:
            open_, detail = state(sym)
        except Exception:  # noqa: BLE001
            open_, detail = True, "market state unknown"
        from app.mt5.base import market_key

        out[market_key(sym)] = {"open": bool(open_), "detail": detail}
    return out


async def _user_auto_status(request: Request, user: CurrentUser) -> dict:
    """D-044 — the USER's own auto-trade state (practice plane).

    Mirrors the admin payload shape but every number is the user's OWN
    account: balance/equity of their plane, their risk settings, their
    skip reason. Institution-terminal details never appear here.
    """
    trading = getattr(request.app.state, "trading", None)
    owner = user["id"]
    if trading is None:
        # degraded server (no trading service): honest minimal state
        return {
            "armed": False,
            "scope": "account",
            "account": None,
            "markets": _markets_snapshot(request),
            "why": {
                "code": "not_armed",
                "text": "AI auto-trade is OFF for your account.",
            },
            "risk": {},
            "last_skip_reason": None,
        }
    try:
        plane = await trading.ensure_plane(owner)
        info = plane.source.account_info()
        if asyncio.iscoroutine(info):
            info = await info
        armed = plane.executor.auto_trade
        balance = float(info.get("balance")) if info else None
        skip = plane.executor.last_skip_reason
        from app.services.trading import _source_connected

        conn = await _source_connected(plane.source)
    except Exception:  # noqa: BLE001 — degraded feed/DB: fall back to stored
        account = await trading._load_account(owner)  # noqa: SLF001 — route glue
        armed = bool(account and account.get("auto_trade"))
        balance = float(account.get("balance")) if account else None
        info = None
        skip = None
        conn = None
    markets = _markets_snapshot(request)
    if not armed:
        why = {
            "code": "not_armed",
            "text": (
                "AI auto-trade is OFF for your account — turn the switch on"
                " in the AI Trading tab."
            ),
        }
    elif conn is False:
        why = {
            "code": "plane_disconnected",
            "text": (
                "Your trading plane lost its market feed — orders would fail"
                " right now; the app retries the connection every 30s and"
                " will resume automatically."
            ),
        }
    elif balance is not None and balance <= 0:
        why = {
            "code": "no_balance",
            "text": (
                "Your practice balance is exhausted — reset it in Settings"
                " to keep trading."
            ),
        }
    elif (
        markets
        and all(m.get("open") is False for m in markets.values())
    ):
        why = {
            "code": "market_closed",
            "text": (
                "Markets are closed (weekend/holiday) — signals stay, your"
                " orders resume at open."
            ),
        }
    else:
        why = {
            "code": "ready",
            "text": (
                "Armed and ready — every confirmed AI signal executes on"
                " your account automatically."
            ),
        }
    settings = await trading.user_settings(owner)
    return {
        "armed": armed,
        "scope": "account",
        "account": {
            "mode": "practice",
            "connected": True if conn is None else bool(conn),
            "balance": balance,
            "equity": float(info.get("equity")) if info else None,
            "currency": (info or {}).get("currency", "USD"),
        },
        "markets": markets,
        "why": why,
        "risk": {k: settings.get(k) for k in (
            "risk_mode", "risk_percent", "fixed_lot", "max_positions",
            "daily_max_loss_pct", "max_spread_points", "rr", "min_sl_atr",
        )},
        "last_skip_reason": skip,
    }


@router.get("/auto-trade")
async def mt5_auto_trade_status(request: Request, user: CurrentUser) -> dict:
    """AI auto-trade state. D-044: per-user — regular users see THEIR OWN
    account (practice plane); admins get the institution terminal block."""
    if user.get("role") != "admin":
        return await _user_auto_status(request, user)
    try:
        st = await _auto_trader(request).status()
    except HTTPException:
        return await _user_auto_status(request, user)
    st["scope"] = "institution"
    return st


class AutoTradeLiveBody(BaseModel):
    enabled: bool
    confirm: str | None = Field(default=None, max_length=16)  # legacy, ignored


@router.post("/auto-trade")
async def mt5_auto_trade_arm(
    body: AutoTradeLiveBody, request: Request, user: CurrentUser
) -> dict:
    """Arm/disarm auto-trading (D-042: simple switch toggle).

    D-044: non-admins arm THEIR OWN practice plane — orders execute on
    their isolated account, never on institution infrastructure. Admins
    arm the institution terminal executor (real orders).

    D-054: the ADMIN's money-management window (saved just before this
    call from the AI Trading tab modal) now governs the institution
    executor too — before, it saved into user settings while the executor
    kept the global defaults, so the admin's USD stop-loss / target never
    actually guarded real orders.
    """
    if user.get("role") != "admin":
        try:
            await _trading(request).set_auto_trade(user["id"], body.enabled)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        st = await _user_auto_status(request, user)
        return {"armed": st["armed"], "account": st["account"], "scope": "account"}
    trader = _auto_trader(request)
    if body.enabled:
        try:
            settings = await _trading(request).user_settings(user["id"])
            await trader.apply_money_window(settings)
        except Exception:  # noqa: BLE001 — window is best-effort; arm still proceeds
            logger.exception("admin money window apply failed (arm continues)")
    try:
        await trader.arm(body.enabled, owner=user["id"])
    except ArmError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    st = await trader.status()
    return {"armed": st["armed"], "terminal": st["terminal"], "scope": "institution"}
