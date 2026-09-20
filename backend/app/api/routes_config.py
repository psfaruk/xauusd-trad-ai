"""routes_config — SPEC §7.1 engine config endpoints (Phase 3).

GET  /api/config              (auth)  engine_config + auto_trade flag
PUT  /api/config              (admin) validated config update (live engine)
POST /api/config/auto-trade   (admin) {enabled, confirm:"ENABLE"} typed confirm
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.auth import CurrentUser, require_admin
from app.engine.config import AUTO_TRADE_CONFIRM, EngineConfig

router = APIRouter(prefix="/api/config", tags=["config"])


@router.get("")
async def get_config(request: Request, user: CurrentUser) -> dict:
    app = request.app
    cfg, auto_trade = await app.state.config_repo.load(app.state.db_engine)
    return {"config": cfg.model_dump(), "auto_trade": auto_trade}


@router.put("", dependencies=[Depends(require_admin)])
async def put_config(body: EngineConfig, request: Request) -> dict:
    app = request.app
    _, auto_trade = await app.state.config_repo.load(app.state.db_engine)
    await app.state.config_repo.save(app.state.db_engine, body, auto_trade)
    # live-apply to EVERY running engine (gold + extra pairs, D-035) and the
    # trading planes (incl. the D-036 live auto-trader) — no restart needed
    if app.state.mt5.runtime is not None:
        await app.state.mt5.runtime.apply_config(body)
    for rt in list(getattr(app.state.mt5, "runtimes", {}).values()):
        await rt.apply_config(body)
    return {"config": body.model_dump(), "auto_trade": auto_trade}


class AutoTradeBody(BaseModel):
    enabled: bool
    confirm: str | None = Field(default=None, max_length=16)


@router.post("/auto-trade", dependencies=[Depends(require_admin)])
async def set_auto_trade(body: AutoTradeBody, request: Request) -> dict:
    app = request.app
    if body.enabled and body.confirm != AUTO_TRADE_CONFIRM:
        raise HTTPException(
            status_code=400,
            detail=(
                'typed confirmation required: '
                '{"enabled": true, "confirm": "ENABLE"}'
            ),
        )
    cfg, _ = await app.state.config_repo.load(app.state.db_engine)
    await app.state.config_repo.save(app.state.db_engine, cfg, body.enabled)
    # Phase 4 — arm/disarm the admin's own platform executor (SPEC §9:
    # auto_trade gates real order execution on the platform account).
    trading = getattr(app.state, "trading", None)
    executor = getattr(trading, "platform_executor", None) if trading else None
    if executor is not None:
        executor.arm(body.enabled)
    return {"auto_trade": body.enabled}
