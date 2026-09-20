"""Schema apply against a real Postgres (Supabase or local).

Auto-skips when no database is reachable — Phase 0 AC only requires pytest
green; this test becomes fully active once DATABASE_URL points at Supabase
(DECISIONS.md D-002/D-009).
"""

from pathlib import Path

import pytest
from sqlalchemy import text

from app.db import apply_schema, make_engine

SCHEMA = (Path(__file__).parent.parent / "app" / "schema.sql").read_text()

EXPECTED_TABLES = {
    "profiles",
    "mt5_connections",
    "engine_config",
    "signals",
    "trades",
    "logs",
}


@pytest.mark.skipif(
    not pytest.importorskip("sqlalchemy"), reason="sqlalchemy missing"
)
class TestSchemaApply:
    async def test_schema_applies_idempotently(self, db_url: str | None) -> None:
        if not db_url:
            pytest.skip("no reachable Postgres — schema test skipped (D-002)")
        engine = make_engine(db_url)
        try:
            # Plain (non-Supabase) Postgres: stub auth.users first (D-009).
            async with engine.connect() as conn:
                conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
                await conn.execute(text("create schema if not exists auth"))
                await conn.execute(
                    text("create table if not exists auth.users (id uuid primary key)")
                )

            # Apply twice — must be idempotent.
            assert await apply_schema(engine)
            assert await apply_schema(engine)

            async with engine.connect() as conn:
                rows = await conn.execute(
                    text("select tablename from pg_tables where schemaname = 'public'")
                )
                tables = {r[0] for r in rows}
                assert EXPECTED_TABLES <= tables, tables

                seed = await conn.execute(
                    text("select config->>'timeframe', auto_trade from engine_config where id = 1")
                )
                row = seed.first()
                assert row is not None
                assert row[0] == "M15"  # SPEC §8.6 default
                assert row[1] is False  # auto_trade OFF by default (§0.6)
        finally:
            await engine.dispose()
