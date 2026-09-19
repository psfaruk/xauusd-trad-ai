"""routes_signals — SPEC §7.1 signal endpoints (Phase 3).

GET /api/signals?limit=100&status=  (auth) list with traces
GET /api/signals/{id}               (auth) single signal detail
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from app.auth import CurrentUser

router = APIRouter(prefix="/api/signals", tags=["signals"])


@router.get("")
async def list_signals(
    request: Request,
    user: CurrentUser,
    limit: int = Query(default=100, ge=1, le=500),
    status: str | None = Query(default=None, pattern="^(active|won|lost|expired|cancelled)$"),
) -> dict:
    repo = request.app.state.signals
    rows = await repo.list(limit=limit, status=status)
    return {"count": len(rows), "signals": rows}


@router.get("/{signal_id}")
async def get_signal(signal_id: str, request: Request, user: CurrentUser) -> dict:
    repo = request.app.state.signals
    row = await repo.get(signal_id)
    if row is None:
        raise HTTPException(status_code=404, detail="signal not found")
    return row
