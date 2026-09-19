"""Phase 1 auth — Supabase JWT verification, roles, guards (SPEC §12 Phase 1).

Supabase is faked with httpx.MockTransport; the token cache is cleared per
test. ACs covered: `/api/me` -> 401 without token; admin vs viewer enforced.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import text

from app import auth as auth_mod
from app.auth import clear_auth_cache, require_admin, resolve_role
from app.config import Settings
from app.db import make_engine, promote_admins
from app.main import app

USER_ID = "11111111-1111-1111-1111-111111111111"
ADMIN_ID = "22222222-2222-2222-2222-222222222222"
VIEWER = {
    "id": USER_ID,
    "email": "trader@example.com",
    "user_metadata": {"full_name": "Test Trader"},
}
ADMIN_USER = {
    "id": ADMIN_ID,
    "email": "boss@example.com",
    "user_metadata": {},
}

GOOD = "Bearer good-token"
ADMIN_TOKEN = "Bearer admin-token"


def make_settings(**overrides) -> Settings:
    base = dict(
        supabase_url="https://supabase.test",
        supabase_anon_key="anon-key-test",
        admin_emails="boss@example.com",
    )
    base.update(overrides)
    return Settings(**base)


def supabase_mock(calls: dict, fail: bool = False):
    """MockTransport that accepts `good-token` / `admin-token` only."""

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        calls.setdefault("paths", set()).add(str(request.url))
        if fail:
            raise httpx.ConnectError("supabase down")
        authz = request.headers.get("authorization", "")
        if authz == GOOD:
            return httpx.Response(200, json=VIEWER)
        if authz == ADMIN_TOKEN:
            return httpx.Response(200, json=ADMIN_USER)
        return httpx.Response(401, json={"message": "Invalid API key"})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture()
def client(monkeypatch):
    """TestClient with Supabase faked; settings + http client swapped in/out."""
    clear_auth_cache()
    calls = {"n": 0}
    mock = supabase_mock(calls)
    settings = make_settings()
    monkeypatch.setattr(auth_mod, "get_settings", lambda: settings)

    with TestClient(app) as c:
        orig_http, orig_settings = app.state.http, app.state.settings
        app.state.http, app.state.settings = mock, settings
        yield c, calls
        app.state.http, app.state.settings = orig_http, orig_settings
    clear_auth_cache()


# ---------------------------------------------------------------- basic ACs


def test_me_requires_token(client) -> None:
    c, _ = client
    resp = c.get("/api/me")
    assert resp.status_code == 401  # SPEC §12 Phase 1 AC


def test_me_rejects_bad_token(client) -> None:
    c, calls = client
    resp = c.get("/api/me", headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 401
    assert calls["n"] == 1  # negative verdicts hit Supabase too


def test_me_valid_token_viewer(client) -> None:
    c, _ = client
    resp = c.get("/api/me", headers={"Authorization": GOOD})
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == USER_ID
    assert body["email"] == "trader@example.com"
    assert body["display_name"] == "Test Trader"
    assert body["role"] == "viewer"


def test_me_admin_via_admin_emails(client) -> None:
    c, _ = client
    resp = c.get("/api/me", headers={"Authorization": ADMIN_TOKEN})
    assert resp.status_code == 200
    assert resp.json()["role"] == "admin"


def test_token_cache_60s(client) -> None:
    c, calls = client
    for _ in range(3):
        assert c.get("/api/me", headers={"Authorization": GOOD}).status_code == 200
    assert calls["n"] == 1  # cached per token-hash (SPEC §12 Phase 1)
    # A different token must not reuse the verdict.
    assert c.get("/api/me", headers={"Authorization": ADMIN_TOKEN}).status_code == 200
    assert calls["n"] == 2


def test_health_stays_public(client) -> None:
    c, _ = client
    assert c.get("/api/health", headers={}).status_code == 200


# ------------------------------------------------------------- failure modes


def test_supabase_unreachable_503(client, monkeypatch) -> None:
    c, _ = client
    calls = {"n": 0}
    app.state.http = supabase_mock(calls, fail=True)
    resp = c.get("/api/me", headers={"Authorization": GOOD})
    assert resp.status_code == 503  # fail closed


def test_supabase_not_configured_503(client, monkeypatch) -> None:
    c, _ = client
    monkeypatch.setattr(auth_mod, "get_settings", lambda: make_settings(supabase_url=None))
    resp = c.get("/api/me", headers={"Authorization": GOOD})
    assert resp.status_code == 503


async def test_require_admin_guards() -> None:
    with pytest.raises(HTTPException) as exc:
        await require_admin({"role": "viewer"})
    assert exc.value.status_code == 403  # SPEC §12 Phase 1 AC: admin enforced
    ok = await require_admin({"role": "admin"})
    assert ok["role"] == "admin"


# --------------------------------------------------- DB-backed role handling


async def test_roles_persist_in_profiles(db_url) -> None:
    if db_url is None:
        pytest.skip("no reachable Postgres — role persistence test skipped (D-002)")
    engine = make_engine(db_url)
    try:
        # auth.users stub has only `id` (D-017) — widen it so the signup
        # trigger (handle_new_user) can run, mirroring real Supabase.
        async with engine.begin() as conn:
            await conn.execute(text("alter table auth.users add column if not exists email text"))
            await conn.execute(
                text("alter table auth.users add column if not exists raw_user_meta_data jsonb")
            )
            for uid, mail in (
                (USER_ID, "trader@example.com"),
                (ADMIN_ID, "boss@example.com"),
            ):
                await conn.execute(
                    text("insert into auth.users (id, email) values (:id, :email)"),
                    {"id": uid, "email": mail},
                )
    finally:
        await engine.dispose()

    class _State:
        settings = make_settings()

    request = type("R", (), {"app": type("A", (), {"state": _State})()})()
    request.app.state.db_engine = engine

    try:
        # First lookup creates the viewer profile row.
        assert await resolve_role(request, USER_ID, "trader@example.com") == "viewer"
        # ADMIN_EMAILS member is promoted (and never demoted afterwards).
        assert await resolve_role(request, ADMIN_ID, "boss@example.com") == "admin"
        assert await resolve_role(request, ADMIN_ID, "other@x.test") == "admin"

        async with engine.connect() as conn:
            roles = dict(
                (await conn.execute(text("select id::text, role from profiles"))).fetchall()
            )
        assert roles[USER_ID] == "viewer"
        assert roles[ADMIN_ID] == "admin"

        # Startup promotion: existing viewer with a matching auth.users.email.
        async with engine.begin() as conn:
            await conn.execute(
                text("update profiles set role = 'viewer' where id = :id"), {"id": ADMIN_ID}
            )
        assert await promote_admins(engine, ["boss@example.com"]) == 1
        assert await promote_admins(engine, ["boss@example.com"]) == 0  # idempotent
        assert await promote_admins(engine, []) == 0
    finally:
        async with engine.begin() as conn:  # keep the shared test DB clean
            await conn.execute(text("delete from auth.users"))  # cascades profiles
        await engine.dispose()
