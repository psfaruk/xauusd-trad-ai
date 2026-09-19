"""Signal persistence (SPEC §6 signals table) with in-memory fallback (C6)."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger("xauusd.repo")


class SignalRepo:
    """DB-backed signal store; falls back to an in-memory list without Postgres."""

    def __init__(self, db_engine: Any = None) -> None:
        self._db = db_engine
        self._mem: list[dict] = []
        self._lock = asyncio.Lock()

    async def insert(self, payload: dict, symbol: str, tf: str) -> str:
        sid = str(payload.get("id") or __import__("uuid").uuid4())
        row = {
            "id": sid,
            "ts": payload["bar_time"].isoformat()
            if isinstance(payload.get("bar_time"), datetime)
            else str(payload.get("bar_time")),
            "symbol": symbol,
            "tf": tf,
            "direction": payload["direction"],
            "entry": payload["entry"],
            "sl": payload["sl"],
            "tp": payload["tp"],
            "confidence": payload["confidence"],
            "trace": payload["trace"],
            "status": "active",
            "result_r": None,
            "closed_at": None,
        }
        async with self._lock:
            self._mem.append(row)
        if self._db is not None:
            try:
                from sqlalchemy import text

                async with self._db.begin() as conn:
                    await conn.execute(
                        text(
                            """
                            insert into signals (id, ts, symbol, tf, direction,
                                                 entry, sl, tp, confidence, trace)
                            values (:id, :ts, :symbol, :tf, :direction,
                                    :entry, :sl, :tp, :confidence, :trace)
                            """
                        ),
                        {
                            "id": sid,
                            "ts": payload["bar_time"]
                            if isinstance(payload.get("bar_time"), datetime)
                            else datetime.now(UTC),
                            "symbol": symbol,
                            "tf": tf,
                            "direction": payload["direction"],
                            "entry": payload["entry"],
                            "sl": payload["sl"],
                            "tp": payload["tp"],
                            "confidence": payload["confidence"],
                            "trace": json.dumps(payload["trace"]),
                        },
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("signal persist failed (kept in memory): %s", exc)
        return sid

    async def update_status(
        self, signal_id: str, status: str, result_r: float | None, closed_at: datetime | None
    ) -> None:
        async with self._lock:
            for row in self._mem:
                if row["id"] == signal_id:
                    row["status"] = status
                    row["result_r"] = result_r
                    row["closed_at"] = closed_at.isoformat() if closed_at else None
        if self._db is not None:
            try:
                from sqlalchemy import text

                async with self._db.begin() as conn:
                    await conn.execute(
                        text(
                            """
                            update signals set status = :status, result_r = :r,
                                   closed_at = :closed where id = :id
                            """
                        ),
                        {
                            "status": status,
                            "r": result_r,
                            "closed": closed_at,
                            "id": signal_id,
                        },
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("signal status persist failed: %s", exc)

    async def list(self, limit: int = 100, status: str | None = None) -> list[dict]:
        if self._db is not None:
            try:
                from sqlalchemy import text

                q = "select * from signals"
                params: dict[str, Any] = {"lim": limit}
                if status:
                    q += " where status = :status"
                    params["status"] = status
                q += " order by ts desc limit :lim"
                async with self._db.connect() as conn:
                    rows = (
                        await conn.execute(text(q), params)
                    ).mappings().all()
                out = []
                for r in rows:
                    d = dict(r)
                    d["trace"] = (
                        json.loads(d["trace"])
                        if isinstance(d["trace"], str)
                        else d["trace"]
                    )
                    for k in ("ts", "created_at", "closed_at"):
                        if d.get(k) is not None:
                            d[k] = d[k].isoformat()
                    out.append(d)
                return out
            except Exception as exc:  # noqa: BLE001
                logger.warning("signal list fell back to memory: %s", exc)
        async with self._lock:
            rows = [r for r in self._mem if status is None or r["status"] == status]
            return list(reversed(rows[-limit:]))

    async def get(self, signal_id: str) -> dict | None:
        rows = await self.list(limit=1000)
        for r in rows:
            if r["id"] == signal_id:
                return r
        return None
