"""Phase 4 tests — multi-user trading planes: isolation, masking, relay."""

from __future__ import annotations

import pytest

from app.config import Settings
from app.engine.config import DEFAULT_CONFIG, ConfigRepo
from app.mt5.mock_source import MockDataSource
from app.services.trading import UserTradingManager, _mask_login
from tests.conftest import make_mock


class _Hub:
    def __init__(self) -> None:
        self.user_events: list[tuple[str, str, dict]] = []

    async def broadcast_all(self, event: str, payload: dict) -> None:
        pass

    async def broadcast_user(self, user_id: str, event: str, payload: dict) -> None:
        self.user_events.append((user_id, event, payload))


def _manager(public: MockDataSource | None = None) -> tuple[UserTradingManager, _Hub]:
    hub = _Hub()
    settings = Settings(database_url=None, data_source="mock")
    mgr = UserTradingManager(
        settings=settings,
        public_source=public or make_mock(),
        hub=hub,
        db_engine=None,
        config_repo=ConfigRepo(),
    )
    return mgr, hub


USER_A = "11111111-1111-1111-1111-111111111111"
USER_B = "22222222-2222-2222-2222-222222222222"


def test_mask_login() -> None:
    assert _mask_login("12345678") == "12••••78"
    assert _mask_login("1") == "••"
    # the raw login must never appear in the masked form
    assert "12345678" not in _mask_login("12345678")


async def test_user_plane_connect_and_isolation() -> None:
    """Two users -> two isolated planes; positions never cross."""
    mgr, hub = _manager()
    st_a = await mgr.connect(USER_A, {"server": "Exness-MT5", "login": "10001", "password": "pwA"})
    st_b = await mgr.connect(USER_B, {"server": "Exness-MT5", "login": "10002", "password": "pwB"})
    assert st_a["connected"] and st_a["mode"] == "demo"
    assert st_b["connected"]
    # masked logins only — full numbers never echoed back (SPEC §13)
    assert st_a["login_masked"] == "10••01"
    assert "10001" not in str(st_a)

    # A trades; B must see nothing
    order_a = await mgr.place_manual_order(USER_A, "BUY", 0.10)
    assert order_a["ok"]
    pos_a = await mgr.positions(USER_A)
    pos_b = await mgr.positions(USER_B)
    assert len(pos_a) == 1
    assert pos_b == []

    # B closes A's ticket -> not found in B's plane (isolation)
    ticket_a = pos_a[0]["ticket"]
    res = await mgr.close_position(USER_B, ticket_a)
    assert not res["ok"]

    # A closes own ticket -> fine
    res = await mgr.close_position(USER_A, ticket_a)
    assert res["ok"]
    assert await mgr.positions(USER_A) == []


async def test_user_plane_auto_trade_requires_connection() -> None:
    mgr, _ = _manager()
    with pytest.raises(ValueError):
        await mgr.set_auto_trade(USER_A, True)  # not connected yet
    await mgr.connect(USER_A, {"server": "s", "login": "1", "password": "x"})
    out = await mgr.set_auto_trade(USER_A, True)
    assert out["auto_trade"] is True
    assert mgr.plane(USER_A).executor.auto_trade


async def test_signal_relay_only_armed_planes() -> None:
    """Engine signal -> executed on ARMED planes only; risk math per user."""
    mgr, hub = _manager()
    await mgr.connect(USER_A, {"server": "s", "login": "1", "password": "x"})
    await mgr.connect(USER_B, {"server": "s", "login": "2", "password": "y"})
    await mgr.set_auto_trade(USER_A, True)  # A armed, B not

    signal = {
        "id": "sig-relay-1", "direction": "BUY", "entry": 2744.0,
        "sl": 2742.0, "tp": 2748.0, "spread_points": 20.0, "confidence": 0.7,
    }
    await mgr.relay_signal(signal, "XAUUSDm", 0.01)

    pos_a = await mgr.positions(USER_A)
    pos_b = await mgr.positions(USER_B)
    assert len(pos_a) == 1  # armed plane executed
    assert pos_b == []  # unarmed plane skipped
    assert pos_a[0].get("side") == "BUY" if isinstance(pos_a[0], dict) else True
    # SL/TP attached (SPEC §9)
    assert pos_a[0]["sl"] == 2742.0
    assert pos_a[0]["tp"] == 2748.0

    # duplicate relay -> idempotent (zero duplicates per signal)
    await mgr.relay_signal(signal, "XAUUSDm", 0.01)
    assert len(await mgr.positions(USER_A)) == 1


async def test_platform_executor_relay() -> None:
    """The admin's platform executor joins the relay when armed."""
    from app.engine.executor import OrderExecutor, TradeRepo

    mgr, hub = _manager()
    public = mgr._public
    await public.connect({"login": "9", "password": "z", "server": "sys"})
    platform_ex = OrderExecutor(public, DEFAULT_CONFIG, TradeRepo(None), hub=hub)
    mgr.attach_platform_executor(platform_ex)

    signal = {
        "id": "sig-plat-1", "direction": "SELL", "entry": 2744.0,
        "sl": 2746.0, "tp": 2740.0, "spread_points": 20.0, "confidence": 0.7,
    }
    await mgr.relay_signal(signal, "XAUUSDm", 0.01)
    assert await public.get_positions() == []  # disarmed -> no order

    platform_ex.arm(True)
    signal2 = dict(signal, id="sig-plat-2")
    await mgr.relay_signal(signal2, "XAUUSDm", 0.01)
    positions = await public.get_positions()
    assert len(positions) == 1
    assert positions[0].side == "SELL"


async def test_demo_plane_prices_match_public_market() -> None:
    """User demo plane prices off the SAME market as the public source."""
    public = make_mock()
    await public.connect({"login": "1", "password": "x", "server": "s"})
    mgr, _ = _manager(public=public)
    await mgr.connect(USER_A, {"server": "s", "login": "5", "password": "p"})
    await mgr.place_manual_order(USER_A, "BUY", 0.1)

    public_tick = await public.get_tick("XAUUSDm")
    positions = await mgr.positions(USER_A)
    # position entry must be within the public tick's bid/ask spread window
    entry = positions[0]["price_open"]
    assert public_tick.bid - 1.0 <= entry <= public_tick.ask + 1.0


async def test_live_mode_bridge_required_on_linux() -> None:
    """Live mode on DATA_SOURCE=mock: honest bridge_required, never faked."""
    mgr, _ = _manager()
    st = await mgr.connect(
        USER_A,
        {"server": "Exness-MT5Trial", "login": "10001", "password": "pw",
         "mode": "live"},
    )
    assert st["connected"] is False
    assert st["status"] == "bridge_required"
    assert "bridge" in st["detail"].lower()
    assert mgr.plane(USER_A) is None  # no simulated live plane


async def test_disconnect_clears_plane() -> None:
    mgr, _ = _manager()
    await mgr.connect(USER_A, {"server": "s", "login": "1", "password": "x"})
    await mgr.place_manual_order(USER_A, "BUY", 0.1)
    out = await mgr.disconnect(USER_A)
    assert out["connected"] is False
    assert mgr.plane(USER_A) is None
    assert await mgr.positions(USER_A) == []
    # reconnect works cleanly afterwards
    st = await mgr.connect(USER_A, {"server": "s", "login": "1", "password": "x"})
    assert st["connected"]


async def test_trading_events_targeted_to_owner() -> None:
    """broadcast_user routing — plane logs only reach the owner's socket."""
    mgr, hub = _manager()
    await mgr.connect(USER_A, {"server": "s", "login": "1", "password": "x"})
    await mgr.set_auto_trade(USER_A, True)
    user_ids = {uid for uid, _evt, _p in hub.user_events}
    assert user_ids == {USER_A}
