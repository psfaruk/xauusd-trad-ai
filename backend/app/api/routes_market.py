"""routes_market — SPEC §7.1 market endpoints (Phase 2 + Phase 4 external).

GET /api/candles?tf=M15&limit=500  (auth) closed-bars OHLCV backfill
GET /api/positions                 (auth) open MT5 positions
GET /api/market/external           (auth) free external reference data
                                            (Binance PAXG gold, ECB FX)
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from app.auth import CurrentUser
from app.mt5.base import validate_tf

router = APIRouter(prefix="/api", tags=["market"])


@router.get("/candles")
async def candles(
    request: Request,
    user: CurrentUser,
    tf: str = Query(default="M15", max_length=4),
    limit: int = Query(default=500, ge=10, le=1500),
) -> dict:
    try:
        validate_tf(tf)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    mgr = request.app.state.mt5
    if mgr.state.status != "connected" or not mgr.symbol:
        raise HTTPException(
            status_code=409,
            detail="MT5 not connected — connect first (POST /api/mt5/connect)",
        )
    try:
        df = await mgr.source.get_rates(mgr.symbol, tf, limit)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"get_rates failed: {exc}") from exc
    bars = [
        {
            "t": int(ts.timestamp()),
            "o": float(o),
            "h": float(h),
            "l": float(low),
            "c": float(c),
            "v": int(v),
        }
        for ts, o, h, low, c, v in zip(
            df["time_utc"], df["o"], df["h"], df["l"], df["c"], df["v"], strict=True
        )
    ]
    return {"symbol": mgr.symbol, "tf": tf, "count": len(bars), "candles": bars}


@router.get("/positions")
async def positions(request: Request, user: CurrentUser) -> dict:
    mgr = request.app.state.mt5
    if mgr.state.status != "connected":
        return {"positions": []}
    try:
        rows = await mgr.get_positions()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"positions failed: {exc}") from exc
    return {
        "positions": [
            {
                "ticket": p.ticket,
                "symbol": p.symbol,
                "side": p.side,
                "volume": p.volume,
                "price_open": p.price_open,
                "sl": p.sl,
                "tp": p.tp,
                "profit": p.profit,
                "time": p.time.isoformat(),
            }
            for p in rows
        ]
    }


@router.get("/market/external")
async def market_external(request: Request, user: CurrentUser) -> dict:
    """Free external reference data (user req #6) — every field fails soft."""
    service = getattr(request.app.state, "external", None)
    if service is None:
        return {"ok": False, "detail": "external data not configured"}
    return await service.snapshot()
