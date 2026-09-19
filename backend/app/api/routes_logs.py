"""routes_logs — SPEC §7.1 log endpoints.

GET /api/logs?limit=200&level=     (auth) recent logs
GET /api/logs/export.csv           (auth) CSV download

Logs come from the `logs` table (populated by the DB log handler). When the
DB is down the endpoints degrade gracefully (C6).
"""

from __future__ import annotations

import csv
import io
import json
import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

from app.auth import CurrentUser

router = APIRouter(prefix="/api/logs", tags=["logs"])


async def _fetch(request: Request, limit: int, level: str | None):
    engine = request.app.state.db_engine
    if engine is None:
        return []
    try:
        from sqlalchemy import text

        q = "select ts, level, source, message, meta from logs"
        params: dict = {"lim": limit}
        if level:
            q += " where level = :level"
            params["level"] = level
        q += " order by ts desc limit :lim"
        async with engine.connect() as conn:
            rows = (await conn.execute(text(q), params)).mappings().all()
        out = []
        for r in rows:
            d = dict(r)
            d["ts"] = d["ts"].isoformat() if d.get("ts") else None
            d["meta"] = d.get("meta") if isinstance(d.get("meta"), (dict, list)) else (
                json.loads(d["meta"]) if isinstance(d.get("meta"), str) and d["meta"] else None
            )
            out.append(d)
        return out
    except Exception:  # noqa: BLE001 — degrade
        return []


@router.get("")
async def list_logs(
    request: Request,
    user: CurrentUser,
    limit: int = Query(default=200, ge=1, le=1000),
    level: str | None = Query(default=None, pattern="^(DEBUG|INFO|WARNING|ERROR)$"),
) -> dict:
    rows = await _fetch(request, limit, level)
    return {"count": len(rows), "logs": rows}


@router.get("/export.csv")
async def export_logs(
    request: Request,
    user: CurrentUser,
    limit: int = Query(default=1000, ge=1, le=5000),
) -> StreamingResponse:
    rows = await _fetch(request, limit, None)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["ts", "level", "source", "message", "meta"])
    for r in rows:
        writer.writerow(
            [r.get("ts"), r.get("level"), r.get("source"), r.get("message"),
             json.dumps(r.get("meta")) if r.get("meta") else ""]
        )
    buf.seek(0)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="logs-{stamp}.csv"'},
    )


class DBLogHandler(logging.Handler):
    """Best-effort structured logging into the `logs` table (never blocks)."""

    def __init__(self, engine_getter) -> None:
        super().__init__(level=logging.INFO)
        self._engine_getter = engine_getter

    def emit(self, record: logging.LogRecord) -> None:
        try:
            import asyncio

            engine = self._engine_getter()
            if engine is None:
                return
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
            loop.create_task(self._write(engine, record))
        except Exception:  # noqa: BLE001 — logging must never raise
            pass

    async def _write(self, engine, record: logging.LogRecord) -> None:
        try:
            from sqlalchemy import text

            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "insert into logs (ts, level, source, message) values (now(), :l, :s, :m)"
                    ),
                    {
                        "l": record.levelname,
                        "s": record.name,
                        "m": record.getMessage()[:4000],
                    },
                )
        except Exception:  # noqa: BLE001
            pass
