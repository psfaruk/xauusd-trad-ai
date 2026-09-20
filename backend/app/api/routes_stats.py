"""routes_stats — SPEC §7.1 performance stats (Phase 4 scope, minimal now).

GET /api/stats?days=30 — win rate, expectancy, profit factor, max DD (R),
by-session breakdown, computed from the signals table.
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request

from app.auth import CurrentUser

router = APIRouter(prefix="/api/stats", tags=["stats"])


@router.get("")
async def stats(
    request: Request,
    user: CurrentUser,
    days: int = Query(default=30, ge=1, le=365),
) -> dict:
    repo = request.app.state.signals
    rows = await repo.list(limit=1000)
    closed = [r for r in rows if r.get("status") in ("won", "lost", "expired")]
    wins = [r for r in closed if r["status"] == "won"]
    losses = [r for r in closed if r["status"] == "lost"]
    rs = [float(r["result_r"]) for r in closed if r.get("result_r") is not None]

    win_rate = len(wins) / (len(wins) + len(losses)) if (wins or losses) else None
    expectancy = sum(rs) / len(rs) if rs else None
    gains = sum(r for r in rs if r > 0)
    pains = abs(sum(r for r in rs if r < 0))
    pf = gains / pains if pains > 0 else None

    # max drawdown over the cumulative R curve (chronological)
    max_dd = 0.0
    if rs:
        chron = sorted(
            [r for r in closed if r.get("result_r") is not None],
            key=lambda r: r.get("closed_at") or r.get("ts") or "",
        )
        curve = peak = 0.0
        for r in chron:
            curve += float(r["result_r"])
            peak = max(peak, curve)
            max_dd = max(max_dd, peak - curve)

    by_session: dict[str, dict] = {}
    for r in closed:
        s_name = (r.get("trace") or {}).get("session") if isinstance(r.get("trace"), dict) else None
        if not s_name:
            # fall back to the hour of the signal timestamp
            ts = str(r.get("ts", ""))
            try:
                hour = int(ts[11:13])
                from app.engine.config import DEFAULT_CONFIG

                s_name = DEFAULT_CONFIG.session_for(hour) or "off"
            except Exception:  # noqa: BLE001
                s_name = "unknown"
        slot = by_session.setdefault(s_name, {"signals": 0, "won": 0, "lost": 0, "expired": 0})
        slot["signals"] += 1
        if r["status"] in slot:
            slot[r["status"]] += 1

    return {
        "days": days,
        "total_signals": len(rows),
        "closed_signals": len(closed),
        "won": len(wins),
        "lost": len(losses),
        "expired": len(closed) - len(wins) - len(losses),
        "win_rate": round(win_rate, 4) if win_rate is not None else None,
        "avg_r": round(sum(rs) / len(rs), 4) if rs else None,
        "expectancy": round(expectancy, 4) if expectancy is not None else None,
        "profit_factor": round(pf, 4) if pf is not None else None,
        "max_drawdown_r": round(max_dd, 4),
        "total_r": round(sum(rs), 4) if rs else None,
        "by_session": by_session,
    }
