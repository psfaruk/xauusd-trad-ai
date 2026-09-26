"""routes_trading — per-user trading plane endpoints (Phase 4 + D-044).

Every authenticated user gets an auto-provisioned PRACTICE plane (own
balance, positions, trades, risk settings) priced off the institutional
market feed — isolation is total: a user can only ever touch their own
plane. Broker links (mt5_connections) stay per-user and encrypted.

D-044 additions:
GET  /api/trading/settings  (auth)  the user's money-management settings
PUT  /api/trading/settings  (auth)  validated per-user update (live-apply)
POST /api/trading/reset     (auth)  reset the practice account (flat, $10k)
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.auth import CurrentUser

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
    confirm: str | None = None  # legacy field, ignored (D-042 switch button)


def _manager(request: Request):
    return request.app.state.trading


async def _budget(request: Request, bucket: str, per_minute: int) -> bool:
    """Tiny in-process token bucket on top of the global slowapi limit —
    money-moving endpoints get a tight per-client budget (SPEC §13 spirit)."""
    import time

    key = getattr(request.app.state, "_trading_budgets", None)
    if key is None:
        key = request.app.state._trading_budgets = {}
    now = time.monotonic()
    window_start, count = key.get((bucket, _client_key(request)), (0.0, 0))
    if now - window_start >= 60.0:
        window_start, count = now, 0
    key[(bucket, _client_key(request))] = (window_start, count + 1)
    return count < per_minute


def _client_key(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


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
    """Open positions + D-054 the plane's WAITING pending limit orders
    (so the UI can prove a pending order WAS created and sits at its fill
    price)."""
    manager = _manager(request)
    return {
        "positions": await manager.positions(user["id"]),
        "pending": await manager.pending_orders(user["id"]),
    }


@router.post("/order")
async def trading_order(body: ManualOrderBody, request: Request, user: CurrentUser) -> dict:
    if not await _budget(request, "order", 30):
        raise HTTPException(status_code=429, detail="too many order requests — slow down")
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
    if not await _budget(request, "close", 30):
        raise HTTPException(status_code=429, detail="too many close requests — slow down")
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


@router.get("/mt5-history")
async def trading_mt5_history(
    request: Request,
    user: CurrentUser,
    days: int = Query(default=90, ge=1, le=365),
) -> dict:
    """Real MT5 account deal history (D-034) — live planes only."""
    plane = _manager(request).plane(user["id"])
    if plane is None or plane.mode != "live":
        return {"deals": []}
    history_deals = getattr(plane.source, "history_deals", None)
    if not callable(history_deals):
        return {"deals": []}
    try:
        return {"deals": await history_deals(days)}
    except Exception as exc:  # noqa: BLE001 — history must never 500 the panel
        raise HTTPException(status_code=502, detail=f"mt5 history failed: {exc}") from exc


@router.post("/auto-trade")
async def trading_auto_trade(body: AutoTradeBody, request: Request, user: CurrentUser) -> dict:
    """Arm/disarm the user's OWN plane (D-044: no typed confirmation — the
    app's switch button + money-management window own this flow)."""
    if not await _budget(request, "arm", 10):
        raise HTTPException(status_code=429, detail="too many arm/disarm requests")
    try:
        return await _manager(request).set_auto_trade(user["id"], body.enabled)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ------------------------------------------------------ D-044 settings/reset

class SettingsBody(BaseModel):
    risk_mode: str | None = Field(default=None, pattern="^(percent|fixed)$")
    risk_percent: float | None = Field(default=None, ge=0.01, le=10.0)
    fixed_lot: float | None = Field(default=None, ge=0.01, le=100.0)
    max_positions: int | None = Field(default=None, ge=1, le=50)
    daily_max_loss_pct: float | None = Field(default=None, ge=0.5, le=100.0)
    max_trades_per_day: int | None = Field(default=None, ge=1, le=100)
    rr: float | None = Field(default=None, ge=0.5, le=10.0)
    min_sl_atr: float | None = Field(default=None, ge=0.3, le=6.0)
    max_spread_points: int | None = Field(default=None, ge=5, le=500)
    # ------------------------------------------------ D-052 money window
    daily_loss_usd: float | None = Field(
        default=None, ge=0.0,
        description="daily stop loss in USD (0 = off)",
    )
    daily_profit_usd: float | None = Field(
        default=None, ge=0.0,
        description="daily target profit in USD (0 = off)",
    )
    day_start_balance: float | None = Field(
        default=None, ge=0.0,
        description="today's trading balance — the USD-window anchor",
    )
    # ------------------------------------------------ D-076 per-pair lots
    symbol_lots: dict[str, float] | None = Field(
        default=None,
        description="per-market lot sizes (fixed-lot mode) — "
                    "{'XAUUSD': 0.02, 'USOIL': 0.1, 'USTEC': 0.05, ...}; "
                    "markets without an entry fall back to fixed_lot",
    )


@router.get("/settings")
async def trading_settings(request: Request, user: CurrentUser) -> dict:
    """The user's money-management settings + practice account balance."""
    try:
        return await _manager(request).user_settings(user["id"])
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"settings load failed: {exc}") from exc


@router.put("/settings")
async def trading_put_settings(
    body: SettingsBody, request: Request, user: CurrentUser
) -> dict:
    """Validate + persist the user's settings; live-applies to their plane."""
    if not await _budget(request, "settings", 30):
        raise HTTPException(status_code=429, detail="too many settings updates")
    try:
        clean = await _manager(request).set_user_settings(
            user["id"], body.model_dump(exclude_none=True)
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"settings save failed: {exc}") from exc
    return {"settings": clean}


@router.post("/reset")
async def trading_reset(request: Request, user: CurrentUser) -> dict:
    """Reset the practice account: flat, back to the starting balance."""
    if not await _budget(request, "reset", 5):
        raise HTTPException(status_code=429, detail="too many resets — take a breath")
    try:
        return await _manager(request).reset_practice_account(user["id"])
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"reset failed: {exc}") from exc
