"""Database bootstrap: idempotent schema apply on startup (SPEC §6).

Degraded mode: without DATABASE_URL (or with an unreachable database) the app
still boots and serves /api/health — see DECISIONS.md D-002.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

logger = logging.getLogger("xauusd.db")

SCHEMA_PATH = Path(__file__).parent / "schema.sql"

# On plain Postgres (Railway, tests) `auth.users` does not exist; create a
# compatible stub first so the `profiles` FK, signup trigger and admin
# promotion all work outside Supabase. On Supabase the statement is a no-op —
# the table already exists (DECISIONS.md D-017).
AUTH_STUB_STATEMENTS = (
    "create schema if not exists auth",
    "create table if not exists auth.users ("
    "id uuid primary key, email text, raw_user_meta_data jsonb)",
)


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
    """Engine for the asyncpg driver. `?sslmode=require` URLs (Railway public
    endpoint, Supabase pooler) are handled: the query param is moved into
    asyncpg's connect_args (D-010)."""
    norm = normalize_url(url)
    connect_args: dict = {"timeout": connect_timeout_s}
    if "sslmode=require" in norm:
        norm = re.sub(r"[?&]sslmode=require", "", norm)
        connect_args["ssl"] = "require"
    return create_async_engine(norm, connect_args=connect_args)


async def promote_admins(engine: AsyncEngine, emails: list[str]) -> int:
    """ADMIN_EMAILS -> role='admin' at startup (SPEC §12 Phase 1).

    Needs `auth.users.email`, i.e. a real Supabase database. On the plain
    Postgres stub the column is absent -> the statement fails -> warn + 0
    (never crashes startup). Returns the number of promoted rows.
    """
    if not emails:
        return 0
    try:
        async with engine.begin() as conn:
            res = await conn.execute(
                text(
                    "update profiles set role = 'admin' "
                    "where id in (select id from auth.users where lower(email) = any(:emails)) "
                    "and role <> 'admin'"
                ),
                {"emails": emails},
            )
            return res.rowcount or 0
    except Exception as exc:  # noqa: BLE001 — promotion is best-effort
        logger.warning("ADMIN_EMAILS promotion skipped: %s", exc)
        return 0


async def apply_schema(engine: AsyncEngine) -> bool:
    """Apply schema.sql idempotently. Individual failing statements are logged
    and skipped (e.g. the auth.users trigger on plain Postgres) so the run never
    aborts on Supabase-only objects (D-009). Returns True on success."""
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    statements = [*AUTH_STUB_STATEMENTS, *split_sql(sql)]
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


def is_postgres_url(database_url: str | None) -> bool:
    """True when DATABASE_URL points at a Postgres server we can connect to."""
    return bool(
        database_url and database_url.startswith(("postgres://", "postgresql://"))
    )
