"""routes_trading — per-user trading plane endpoints (Phase 4 agent flow).

Any authenticated user can connect their OWN MT5/Exness account (demo mode
works everywhere; live needs the Windows bridge — D-024), place manual
orders, close positions, review their trade history and arm per-account
auto-trade with a typed confirmation. Every route is owner-scoped: users
can only ever touch their own plane.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.auth import CurrentUser
from app.engine.config import AUTO_TRADE_CONFIRM

router = APIRouter(prefix="/api/trading", tags=["trading"])


class TradingConnectBody(BaseModel):
    server: str = Field(min_length=1, max_length=200)
    login: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=1, max_length=200)
    mode: str = Field(default="demo", pattern="^(demo|live)$")


class ManualOrderBody(BaseModel):
    side: str = Field(pattern="^(BUY|SELL)$")
    volume: float = Field(gt=0, le=100)
    sl: float | None = Field(default=None, gt=0)
    tp: float | None = Field(default=None, gt=0)


class AutoTradeBody(BaseModel):
    enabled: bool
    confirm: str | None = None


def _manager(request: Request):
    return request.app.state.trading


@router.post("/connect")
async def trading_connect(body: TradingConnectBody, request: Request, user: CurrentUser) -> dict:
    try:
        return await _manager(request).connect(user["id"], body.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"connect failed: {exc}") from exc


@router.post("/disconnect")
async def trading_disconnect(request: Request, user: CurrentUser) -> dict:
    return await _manager(request).disconnect(user["id"])


@router.get("/status")
async def trading_status(request: Request, user: CurrentUser) -> dict:
    return await _manager(request).status(user["id"])


@router.get("/positions")
async def trading_positions(request: Request, user: CurrentUser) -> dict:
    return {"positions": await _manager(request).positions(user["id"])}


@router.post("/order")
async def trading_order(body: ManualOrderBody, request: Request, user: CurrentUser) -> dict:
    try:
        return await _manager(request).place_manual_order(
            user["id"], body.side, body.volume, body.sl, body.tp
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"order failed: {exc}") from exc


@router.post("/positions/{ticket}/close")
async def trading_close(ticket: int, request: Request, user: CurrentUser) -> dict:
    try:
        return await _manager(request).close_position(user["id"], ticket)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"close failed: {exc}") from exc


@router.get("/trades")
async def trading_history(
    request: Request,
    user: CurrentUser,
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    return {"trades": await _manager(request).trade_history(user["id"], limit)}


@router.post("/auto-trade")
async def trading_auto_trade(body: AutoTradeBody, request: Request, user: CurrentUser) -> dict:
    if body.enabled and body.confirm != AUTO_TRADE_CONFIRM:
        raise HTTPException(
            status_code=400,
            detail=f'typed confirmation required: {{"confirm": "{AUTO_TRADE_CONFIRM}"}}',
        )
    try:
        return await _manager(request).set_auto_trade(user["id"], body.enabled)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
