"""routes_mt5 — SPEC §7.1 mt5 endpoints (Phase 2).

POST /api/mt5/connect     (admin) connect + discover symbol + start engine
POST /api/mt5/disconnect  (admin) clean shutdown
GET  /api/mt5/status      (auth)  {status, symbol, account, broker offset}
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.auth import CurrentUser, require_admin

router = APIRouter(prefix="/api/mt5", tags=["mt5"])


class ConnectBody(BaseModel):
    server: str = Field(min_length=1, max_length=200)
    login: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=1, max_length=200)
    terminal_path: str | None = Field(default=None, max_length=500)


def _manager(request: Request):
    return request.app.state.mt5


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
