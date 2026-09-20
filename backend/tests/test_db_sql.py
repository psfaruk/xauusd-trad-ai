"""Pure unit tests for db helpers (no database needed)."""

from app.db import normalize_url, split_sql


def test_split_sql_respects_dollar_quoted_blocks() -> None:
    sql = """
create table if not exists a (id int);
create or replace function f()
returns trigger language plpgsql as $$
begin
  insert into b values (1);   -- semicolon inside body must NOT split
  return new;
end $$;
create index if not exists idx on a (id);
"""
    stmts = split_sql(sql)
    assert len(stmts) == 3
    assert stmts[1].startswith("create or replace function")
    assert "$$" in stmts[1]
    assert "return new" in stmts[1]


def test_split_sql_handles_the_real_schema() -> None:
    from pathlib import Path

    schema = (Path(__file__).parent.parent / "app" / "schema.sql").read_text()
    stmts = split_sql(schema)
    # 6 create tables + 2 create index + 1 function + 1 drop trigger + 1 trigger
    # + 1 seed insert + 2 Phase-4 alter statements (mt5_connections columns)
    # = 14 statements.
    assert len(stmts) == 14, stmts
    assert all(s for s in stmts)
    # The trigger statement survives as one piece.
    trigger = [s for s in stmts if s.startswith("create trigger")]
    assert len(trigger) == 1
    assert "execute function public.handle_new_user" in trigger[0]


def test_normalize_url() -> None:
    assert normalize_url("postgres://u:p@h:5432/db") == "postgresql+asyncpg://u:p@h:5432/db"
    assert (
        normalize_url("postgresql://u:p@h:5432/db") == "postgresql+asyncpg://u:p@h:5432/db"
    )
    assert normalize_url("postgresql+asyncpg://u:p@h/db") == "postgresql+asyncpg://u:p@h/db"
    assert normalize_url("sqlite://x") == "sqlite://x"
