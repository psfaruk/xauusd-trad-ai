"""Shared fixtures."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

# Hermetic tests (D-030): DATA_SOURCE defaults to "live" (free real-time
# APIs) since the real-data switch — force mock for the whole suite so unit
# tests NEVER touch the network. Must run before any app import that loads
# Settings (test modules import app.main at collection time).
os.environ.setdefault("DATA_SOURCE", "mock")

from app.mt5.base import TIMEFRAME_MINUTES  # noqa: E402
from app.mt5.mock_source import MockDataSource  # noqa: E402

# Fixed anchor so every test is fully deterministic (Monday 2025-01-06 00:00 UTC).
START = datetime(2025, 1, 6, tzinfo=UTC)

BACKEND_DIR = Path(__file__).parent.parent
PGSERVER_DATA_DIR = BACKEND_DIR / ".pgdata-test"


def make_mock(seed: int = 7, **kwargs) -> MockDataSource:
    """Frozen-clock mock (time_scale=0) driven manually via advance_minutes()."""
    kwargs.setdefault("time_scale", 0.0)
    return MockDataSource(seed=seed, start_time=START, **kwargs)


def atr14(df) -> float:
    h, low, c = df["h"], df["l"], df["c"]
    prev_c = c.shift(1)
    tr = (h - low).combine((h - prev_c).abs(), max).combine((low - prev_c).abs(), max)
    return float(tr.tail(14).mean())


def _reachable(url: str) -> bool:
    from sqlalchemy import text

    from app.db import make_engine

    async def probe() -> None:
        engine = make_engine(url, connect_timeout_s=3)
        try:
            async with engine.connect() as conn:
                await conn.execute(text("select 1"))
        finally:
            await engine.dispose()

    try:
        asyncio.run(probe())
        return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def db_url():
    """Reachable Postgres URL for schema tests, else None (test skips).

    Priority: TEST_DATABASE_URL / DATABASE_URL (postgres:// only), then an
    embedded Postgres via `pgserver` when installed (dev sandbox — no root
    needed). See DECISIONS.md D-002/D-014.
    """
    server = None
    for var in ("TEST_DATABASE_URL", "DATABASE_URL"):
        url = os.environ.get(var)
        if url and url.startswith(("postgres://", "postgresql://")) and _reachable(url):
            yield url
            return
    try:
        import pgserver
    except ImportError:
        yield None
        return
    server = pgserver.get_server(str(PGSERVER_DATA_DIR))
    url = f"postgresql+asyncpg://postgres@/postgres?host={PGSERVER_DATA_DIR}"
    try:
        yield url if _reachable(url) else None
    finally:
        if server is not None:
            server.cleanup()


__all__ = ["START", "TIMEFRAME_MINUTES", "make_mock", "atr14"]
