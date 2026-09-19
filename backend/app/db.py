"""Database bootstrap: idempotent schema apply on startup (SPEC §6).

Degraded mode: without DATABASE_URL (or with an unreachable database) the app
still boots and serves /api/health — see DECISIONS.md D-002.
"""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

logger = logging.getLogger("xauusd.db")

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def split_sql(sql: str) -> list[str]:
    """Split a .sql file into statements, respecting $$ dollar-quoted blocks.

    The schema contains a plpgsql function body (`$$ ... $$`) whose body includes
    semicolons — a naive split on ';' would break it (DECISIONS.md D-009).
    """
    statements: list[str] = []
    buf: list[str] = []
    in_dollar = False
    i = 0
    while i < len(sql):
        if sql.startswith("$$", i):
            in_dollar = not in_dollar
            buf.append("$$")
            i += 2
            continue
        if not in_dollar and sql.startswith("--", i):
            # Line comment outside dollar-quoted bodies: skip to end of line so
            # a ';' inside a comment never splits statements (D-009).
            j = sql.find("\n", i)
            i = len(sql) if j == -1 else j
            continue
        ch = sql[i]
        if ch == ";" and not in_dollar:
            stmt = "".join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
        else:
            buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


def normalize_url(url: str) -> str:
    """Normalize postgres:// / postgresql:// to the asyncpg driver scheme (D-010)."""
    if url.startswith("postgres://"):
        return "postgresql+asyncpg://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url[len("postgresql://") :]
    return url


def make_engine(url: str, connect_timeout_s: int = 5) -> AsyncEngine:
    return create_async_engine(
        normalize_url(url),
        connect_args={"timeout": connect_timeout_s},
    )


async def apply_schema(engine: AsyncEngine) -> bool:
    """Apply schema.sql idempotently. Individual failing statements are logged
    and skipped (e.g. the auth.users trigger on plain Postgres) so the run never
    aborts on Supabase-only objects (D-009). Returns True on success."""
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    statements = split_sql(sql)
    applied = 0
    # AUTOCOMMIT: a failed statement must not poison the remaining ones.
    async with engine.connect() as conn:
        conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
        for stmt in statements:
            head = stmt[:70].replace("\n", " ")
            try:
                await conn.execute(text(stmt))
                applied += 1
            except Exception as exc:  # noqa: BLE001 — intentionally per-statement
                logger.warning("schema statement skipped [%s ...]: %s", head, exc)
    logger.info("schema applied: %d/%d statements OK", applied, len(statements))
    return True


async def init_db(database_url: str | None) -> bool:
    """Startup entrypoint. Returns True when the DB is reachable and schema applied."""
    if not database_url:
        logger.warning("DATABASE_URL not set — running without persistence (degraded mode)")
        return False
    if not database_url.startswith(("postgres://", "postgresql://")):
        logger.warning(
            "DATABASE_URL is not a Postgres URL — running without persistence (degraded mode)"
        )
        return False
    engine = make_engine(database_url)
    try:
        return await apply_schema(engine)
    except Exception as exc:  # noqa: BLE001 — boot must never crash (SPEC C6 spirit)
        logger.warning("DB init failed — running in degraded mode: %s", exc)
        return False
    finally:
        await engine.dispose()
