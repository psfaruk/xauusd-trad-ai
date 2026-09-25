"""D-037 — per-user broker connections (BrokerConnectionService).

Unit tests for the three honesty paths of the live connect flow:
- demo plane (DATA_SOURCE=mock): entered credentials bind without a terminal
- live + terminal session matches  -> connected, user-bound
- live + different account entered -> AccountMismatchError (422 at the route)
- live + terminal down             -> BrokerUnavailableError (503 at the route)
- per-user isolation: one user's connection never leaks to another
"""

from __future__ import annotations

import pytest

from app.mt5.broker_connect import (
    AccountMismatchError,
    BrokerConnectionService,
    BrokerUnavailableError,
    NotConnectedError,
)


def _svc(**kw) -> BrokerConnectionService:
    return BrokerConnectionService(demo_mode=kw.pop("demo_mode", False), **kw)


class _FakeMcp:
    def __init__(self, account: dict | None = None, error: Exception | None = None):
        self.account_result = account
        self.error = error

    def account(self) -> dict:
        if self.error:
            raise self.error
        return self.account_result or {}


def _terminal_ok() -> dict:
    return {
        "account": {
            "login": 414350770,
            "server": "Exness-MT5Trial6",
            "balance": 500.02,
            "equity": 501.10,
            "currency": "USD",
            "name": "Test Trader",
            "type": "demo",
            "leverage": 200,
        },
        "terminal": {
            "server_connected": True,
            "mcp_trade_allowed": True,
            "build": 6204,
        },
    }


async def test_demo_mode_binds_without_terminal():
    svc = _svc(demo_mode=True)
    res = await svc.connect("user-a", "Exness-MT5Trial6", "123", "pw")
    # D-044 — non-admin/demo links return "linked" (per-user broker
    # profile; no institution-terminal data exposed)
    assert res["status"] == "linked"
    assert res["mode"] == "broker-link"
    st = await svc.status("user-a")
    assert st["status"] == "linked"
    assert "•" in st["login_masked"]


async def test_live_connect_binds_terminal_session(monkeypatch):
    mcp = _FakeMcp(_terminal_ok())
    monkeypatch.setattr("app.mt5.mcp.terminal_client", lambda: mcp)
    svc = _svc()
    res = await svc.connect(
        "user-a", "Exness-MT5Trial6", "414350770", "pw"
    )
    assert res["status"] == "connected"
    assert str(res["login"]) == "414350770"
    assert res["account"]["balance"] == 500.02
    assert res["account"]["mcp_trade_allowed"] is True


async def test_live_connect_mismatch_rejected(monkeypatch):
    """The entered account is NOT the terminal session -> honest 422."""
    mcp = _FakeMcp(_terminal_ok())
    monkeypatch.setattr("app.mt5.mcp.terminal_client", lambda: mcp)
    svc = _svc()
    with pytest.raises(AccountMismatchError):
        await svc.connect("user-a", "Exness-MT5Trial8", "999999999", "pw")
    # nothing was bound
    assert await svc.status("user-a") == {"status": "disconnected"}


async def test_live_connect_server_name_mismatch_rejected(monkeypatch):
    mcp = _FakeMcp(_terminal_ok())
    monkeypatch.setattr("app.mt5.mcp.terminal_client", lambda: mcp)
    svc = _svc()
    with pytest.raises(AccountMismatchError):
        # right login, wrong server
        await svc.connect("user-a", "Exness-MT5Real8", "414350770", "pw")


async def test_live_connect_terminal_down(monkeypatch):
    from app.mt5.mcp import MCPError

    mcp = _FakeMcp(error=MCPError("bridge down"))
    monkeypatch.setattr("app.mt5.mcp.terminal_client", lambda: mcp)
    svc = _svc()
    with pytest.raises(BrokerUnavailableError):
        await svc.connect("user-a", "Exness-MT5Trial6", "414350770", "pw")


async def test_live_connect_no_broker_session(monkeypatch):
    """Terminal answers but has no active broker session -> unavailable."""
    acct = _terminal_ok()
    acct["terminal"]["server_connected"] = False
    mcp = _FakeMcp(acct)
    monkeypatch.setattr("app.mt5.mcp.terminal_client", lambda: mcp)
    svc = _svc()
    with pytest.raises(BrokerUnavailableError):
        await svc.connect("user-a", "Exness-MT5Trial6", "414350770", "pw")


async def test_per_user_isolation_and_disconnect(monkeypatch):
    mcp = _FakeMcp(_terminal_ok())
    monkeypatch.setattr("app.mt5.mcp.terminal_client", lambda: mcp)
    svc = _svc()
    await svc.connect("user-a", "Exness-MT5Trial6", "414350770", "pw")
    # user-b is NOT connected
    assert await svc.status("user-b") == {"status": "disconnected"}
    with pytest.raises(NotConnectedError):
        svc.get("user-b")
    assert svc.connected_users() == 1
    # disconnect only drops user-a
    res = await svc.disconnect("user-a")
    assert res["status"] == "disconnected"
    with pytest.raises(NotConnectedError):
        svc.get("user-a")


async def test_status_reconnecting_when_terminal_dies(monkeypatch):
    mcp = _FakeMcp(_terminal_ok())
    monkeypatch.setattr("app.mt5.mcp.terminal_client", lambda: mcp)
    svc = _svc()
    await svc.connect("user-a", "Exness-MT5Trial6", "414350770", "pw")
    mcp.error = Exception("gone")  # next snapshot fails -> reconnecting
    mcp.account_result = None
    st = await svc.status("user-a")
    assert st["status"] == "reconnecting"
    # the binding survives (last snapshot kept)
    assert str(st["login"]) == "414350770"
    conn = svc.get("user-a")  # still bound
    assert conn.server == "Exness-MT5Trial6"


# ------------------------------------------------------------------- D-075
# "Fronted এ exness not connected দেখাচ্ছে, কিন্তু অটো সিগন্যাল এন্ট্রি হচ্ছে"
# — the terminal bridge + armed auto-trader survive restarts, but the
# per-user broker links lived ONLY in memory. restore() reads the
# persisted rows back at boot so the status is honest.

from typing import Any  # noqa: E402 — test-scope import


def _fernet():
    """A real cipher so _persist actually writes rows (no FERNET_KEY in
    the test env => persistence silently skips — the D-075 restore tests
    need the rows on disk)."""
    from cryptography.fernet import Fernet

    return Fernet(Fernet.generate_key())


def _sqlite_svc(**kw) -> tuple[BrokerConnectionService, Any]:
    import sqlalchemy as sa
    from sqlalchemy.pool import StaticPool

    eng = sa.create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    kw.setdefault("fernet", _fernet())
    with eng.begin() as cx:
        cx.execute(sa.text(
            "create table mt5_connections ("
            " owner text not null, server text not null, login text not null,"
            " enc_password text, terminal_path text, symbol text,"
            " status text not null default 'disconnected',"
            " last_heartbeat text, created_at text,"
            " mode text not null default 'demo',"
            " auto_trade boolean not null default 0,"
            " broker_admin boolean not null default 0)"
        ))
    return BrokerConnectionService(db_engine=eng, **kw), eng


async def test_restore_rebinds_admin_link_after_restart(monkeypatch):
    """A terminal-verified link persisted before a restart comes back at
    boot and status() probes the terminal again — orders flow, the badge
    says connected. THE regression test for the user's report."""
    mcp = _FakeMcp(_terminal_ok())
    monkeypatch.setattr("app.mt5.mcp.terminal_client", lambda: mcp)

    svc, eng = _sqlite_svc()
    await svc.connect("user-a", "Exness-MT5Trial6", "414350770", "pw")
    # simulate the restart: memory wiped, DB rows remain
    svc2 = BrokerConnectionService(db_engine=eng)
    assert await svc2.status("user-a") == {"status": "disconnected"}
    assert await svc2.restore() == 1
    st = await svc2.status("user-a")
    assert st["status"] == "connected"
    assert str(st["login"]) == "414350770"
    assert st["account"]["balance"] == 500.02  # re-probed from the terminal


async def test_restore_admin_link_terminal_down_is_reconnecting(monkeypatch):
    mcp = _FakeMcp(_terminal_ok())
    monkeypatch.setattr("app.mt5.mcp.terminal_client", lambda: mcp)
    svc, eng = _sqlite_svc()
    await svc.connect("user-a", "Exness-MT5Trial6", "414350770", "pw")
    # restart AND the terminal bridge blipped
    mcp_dead = _FakeMcp(error=Exception("bridge down"))
    monkeypatch.setattr("app.mt5.mcp.terminal_client", lambda: mcp_dead)
    svc2 = BrokerConnectionService(db_engine=eng)
    await svc2.restore()
    st = await svc2.status("user-a")
    assert st["status"] == "reconnecting"  # honest, not "disconnected"
    assert str(st["login"]) == "414350770"


class _ProbeSpy:
    """Counts terminal probes — the public-link test must record ZERO."""

    def __init__(self) -> None:
        self.calls = 0

    def account(self) -> dict:
        self.calls += 1
        return _terminal_ok()


async def test_restore_public_link_stays_linked_never_probes(monkeypatch):
    """D-044 isolation preserved: a public user's broker-link row never
    touches the institution terminal on restore."""
    spy = _ProbeSpy()
    monkeypatch.setattr("app.mt5.mcp.terminal_client", lambda: spy)

    svc, eng = _sqlite_svc()
    await svc.connect("user-pub", "Exness-MT5Trial6", "999", "pw", admin=False)
    svc2 = BrokerConnectionService(db_engine=eng)
    assert await svc2.restore() == 1
    st = await svc2.status("user-pub")
    assert st["status"] == "linked"
    assert st["login_masked"].startswith("99")
    assert spy.calls == 0  # the terminal was NEVER probed for the public link


async def test_restore_skips_explicitly_disconnected_rows():
    svc, eng = _sqlite_svc()
    await svc.connect("user-a", "Exness-MT5Trial6", "414350770", "pw", admin=False)
    await svc.disconnect("user-a")  # sets the row's status to disconnected
    svc2 = BrokerConnectionService(db_engine=eng)
    assert await svc2.restore() == 0
    assert await svc2.status("user-a") == {"status": "disconnected"}


async def test_restore_is_a_noop_without_db():
    svc = _svc()
    assert await svc.restore() == 0


async def test_live_connect_persists_the_admin_flag(monkeypatch):
    """broker_admin=true only on the terminal-verified bind (the public
    link persists false) — restore() reads it to decide who may probe."""
    import sqlalchemy as sa

    mcp = _FakeMcp(_terminal_ok())
    monkeypatch.setattr("app.mt5.mcp.terminal_client", lambda: mcp)

    svc, eng = _sqlite_svc()
    await svc.connect("admin", "Exness-MT5Trial6", "414350770", "pw", admin=True)
    await svc.connect("pub", "Exness-MT5Trial6", "999", "pw", admin=False)
    with eng.begin() as cx:
        rows = {
            r[0]: r[1] for r in cx.execute(sa.text(
                "select owner, broker_admin from mt5_connections"
            )).all()
        }
    assert rows["admin"] in (1, True)
    assert rows["pub"] in (0, False)
