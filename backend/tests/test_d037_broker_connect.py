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
