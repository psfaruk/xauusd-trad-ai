"""routes_market — SPEC §7.1 market endpoints (Phase 2 + Phase 4 + D-035).

GET /api/candles?tf=M15&limit=500&symbol=BTCUSD  (auth) closed-bars backfill
GET /api/positions                                (auth) open MT5 positions
GET /api/market/external                          (auth) external references
GET /api/analysis?symbol=XAUUSD                   (auth) D-042 ICT/SMC
        multi-TF snapshot: structure, order blocks, FVG, liquidity,
        supply/demand zones, whale events + classic indicators — feeds
        the chart overlays and the live analysis strip.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from app.auth import CurrentUser
from app.mt5.base import validate_tf

router = APIRouter(prefix="/api", tags=["market"])


@router.get("/analysis")
async def analysis(
    request: Request,
    user: CurrentUser,
    symbol: str = Query(default="XAUUSD", max_length=16),
) -> dict:
    """D-042 — cached ICT/SMC analysis snapshot for one symbol."""
    mgr = request.app.state.mt5
    available = getattr(mgr.source, "platform_symbols", None) or []
    if available and symbol not in available:
        raise HTTPException(status_code=404, detail=f"unknown symbol {symbol}")
    service = getattr(request.app.state, "analysis", None)
    if service is None:
        raise HTTPException(status_code=503, detail="analysis service unavailable")
    # D-043 — recent signals let the drawings engine mark a forming setup
    # as TRIGGERED (entry already taken) instead of drawing it again.
    try:
        recent = await request.app.state.signals.list(limit=25)
    except Exception:  # noqa: BLE001 — drawings must never fail on repo errors
        recent = []
    return await service.get(mgr.source, symbol, recent_signals=recent)


@router.get("/candles")
async def candles(
    request: Request,
    user: CurrentUser,
    tf: str = Query(default="M15", max_length=4),
    limit: int = Query(default=500, ge=10, le=3000),
    symbol: str | None = Query(default=None, max_length=16),
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
    # D-035: any symbol the source streams is chartable (XAUUSD + BTCUSD)
    sym = symbol or mgr.symbol
    available = getattr(mgr.source, "platform_symbols", None) or []
    # D-076 — the frontend sends MARKET KEYS while sources may list broker
    # spellings (XAUUSDm/BTCUSDm); compare on market_key so every spelling
    # resolves (USOIL/USTEC whose broker names equal their keys always did).
    from app.mt5.base import market_key

    if available and sym not in available and sym != mgr.symbol \
            and market_key(sym) not in {market_key(s) for s in available}:
        raise HTTPException(
            status_code=404,
            detail=f"symbol {sym} not served (available: {available})",
        )
    try:
        df = await mgr.source.get_rates(sym, tf, limit)
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
    return {"symbol": sym, "tf": tf, "count": len(bars), "candles": bars}


@router.get("/positions")
async def positions(request: Request, user: CurrentUser) -> dict:
    """D-044 — the USER's own practice-plane positions (isolated per user;
    institution-terminal positions are admin-only via /api/mt5/positions)."""
    trading = getattr(request.app.state, "trading", None)
    if trading is None:
        return {"positions": []}
    try:
        rows = await trading.positions(user["id"])
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"positions failed: {exc}") from exc
    return {"positions": rows}


@router.get("/market/external")
async def market_external(request: Request, user: CurrentUser) -> dict:
    """Free external reference data (user req #6) — every field fails soft."""
    service = getattr(request.app.state, "external", None)
    if service is None:
        return {"ok": False, "detail": "external data not configured"}
    return await service.snapshot()
