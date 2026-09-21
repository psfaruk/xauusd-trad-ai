"""D-036 tests — AI signal -> auto-order on the REAL MT5 terminal.

Covers the full pipeline with a FAKE terminal client (the real terminal is
never touched by the unit suite — conftest points MT5_MCP_URL at a dead
endpoint):
- McpTradingSource: account/positions/symbol-spec mapping, order results,
  close-by-ticket symbol resolution
- McpAutoTrader: arming guards, weekend/market-closed honesty, §9 delegation
  (idempotency, kill switches), exit sync (expiry -> close)
- relay wiring: UserTradingManager fans signals out to the live plane
- ConfigRepo auto_live persistence round-trip
- REST routes: GET/POST /api/mt5/auto-trade (auth + typed confirm + 409)
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.engine.config import DEFAULT_CONFIG, ConfigRepo
from app.engine.executor import TradeRepo
from app.mt5.auto_trader import ArmError, McpAutoTrader
from app.mt5.base import DataSourceError
from app.mt5.mcp import MCPError
from app.mt5.mcp_source import McpTradingSource

# ---------------------------------------------------------------- fake infra


class FakeTerminal:
    """Scriptable stand-in for MT5TerminalClient (all calls sync)."""

    def __init__(
        self,
        *,
        available: bool = True,
        trade_allowed: bool = True,
        equity: float = 500.0,
        open_positions: list | None = None,
        retcode: int = 10009,
    ) -> None:
        self.available_flag = available
        self.trade_allowed = trade_allowed
        self.equity = equity
        self.open_positions = open_positions or []
        self.retcode = retcode
        self.orders: list[dict] = []
        self.closes: list[int] = []

    def available(self) -> bool:
        return self.available_flag

    def account(self) -> dict:
        if not self.available_flag:
            raise MCPError("terminal down")
        return {
            "account": {
                "login": 414350770,
                "server": "Exness-MT5Trial6",
                "broker": "Exness",
                "balance": self.equity,
                "equity": self.equity,
                "margin_free": self.equity,
                "profit": 0.0,
                "currency": "USD",
                "leverage": 200,
            },
            "terminal": {
                "server_connected": True,
                "mcp_trade_allowed": self.trade_allowed,
                "build": 6204,
            },
        }

    def positions(self, include_orders: bool = True) -> dict:  # noqa: ARG002
        return {"positions": list(self.open_positions), "orders": []}

    def symbols(self) -> list[dict]:
        return [
            {
                "symbol": "XAUUSDm", "point": 0.001, "contract_size": 100.0,
                "volume_min": 0.01, "volume_max": 200.0, "volume_step": 0.01,
                "description": "Gold", "digits": 3,
            },
            {
                "symbol": "BTCUSDm", "point": 0.01, "contract_size": 1.0,
                "volume_min": 0.01, "volume_max": 200.0, "volume_step": 0.01,
                "description": "Bitcoin", "digits": 2,
            },
        ]

    def market_order(self, symbol, side, volume, sl=None, tp=None, comment=""):
        self.orders.append(
            {"symbol": symbol, "side": side, "volume": volume,
             "sl": sl, "tp": tp, "comment": comment}
        )
        if self.retcode != 10009:
            return {"retcode": self.retcode, "retcode_details": "Market closed"}
        return {
            "retcode": 10009, "retcode_details": "done",
            "deal": 4425000001, "order": 991000001,
            "price": 81230.0, "volume": volume, "symbol": symbol,
        }

    def close_position(self, symbol, ticket):
        self.closes.append(ticket)
        self.open_positions = [
            p for p in self.open_positions if p["position_id"] != ticket
        ]
        return {"retcode": 10009, "retcode_details": "done", "price": 81240.0}


def make_source(client: FakeTerminal) -> McpTradingSource:
    return McpTradingSource(client=client)  # type: ignore[arg-type]


class FakeHub:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def broadcast_all(self, kind: str, payload: dict) -> None:
        self.events.append((kind, payload))


def make_trader(client: FakeTerminal, hub: FakeHub | None = None,
                cfg=None, market_state=None):
    hub = hub or FakeHub()
    repo = TradeRepo(None)
    trader = McpAutoTrader(
        source=make_source(client), cfg=cfg or DEFAULT_CONFIG, repo=repo,
        hub=hub, config_repo=None, market_state=market_state,
    )
    return trader, hub, repo


SIG = {
    "id": "0123456789abcdef",
    "direction": "BUY",
    "entry": 81230.0,
    "sl": 81130.0,
    "tp": 81430.0,
    "confidence": 0.72,
    "spread_points": 8.0,
    "symbol": "BTCUSD",
    "tf": "M15",
}


def _pos(ticket=991000001, symbol="BTCUSDm", action="buy"):
    return {
        "position_id": ticket, "action": action, "symbol": symbol,
        "volume": 0.01, "price_open": 81230.0, "price_last": 81231.0,
        "profit": 0.01, "create_time": "2026.09.20 18:00:00",
        "update_time": "2026.09.20 18:00:00",
        # real terminal field names (verified live, D-036)
        "stop_loss": 81130.0, "take_profit": 81430.0,
    }


# ------------------------------------------------------------ McpTradingSource


async def test_source_maps_account_and_positions() -> None:
    client = FakeTerminal(open_positions=[_pos()])
    src = make_source(client)
    info = await src.account_info()
    assert info is not None
    assert info["equity"] == pytest.approx(500.0)
    assert info["trade_allowed"] is True
    assert info["server"] == "Exness-MT5Trial6"

    positions = await src.get_positions()
    assert len(positions) == 1
    p = positions[0]
    assert p.ticket == 991000001
    assert p.symbol == "BTCUSDm"
    assert p.side == "BUY"
    assert p.volume == pytest.approx(0.01)
    # terminal stop_loss/take_profit fields map onto Position.sl/tp
    assert p.sl == pytest.approx(81130.0)
    assert p.tp == pytest.approx(81430.0)


async def test_source_symbol_discovery_and_specs() -> None:
    client = FakeTerminal()
    src = make_source(client)
    assert await src.abroker_symbol("XAUUSD") == "XAUUSDm"
    assert await src.abroker_symbol("BTCUSD") == "BTCUSDm"
    # real terminal spec flows into symbol_info (contract + volume limits)
    info = src.symbol_info("XAUUSDm")  # broker name -> platform lookup
    assert info is not None
    assert info.contract_size == pytest.approx(100.0)
    assert info.volume_max == pytest.approx(200.0)
    btc = src.symbol_info("BTCUSD")
    assert btc is not None
    assert btc.contract_size == pytest.approx(1.0)


async def test_source_market_data_is_execution_only() -> None:
    src = make_source(FakeTerminal())
    assert (await src.get_rates("XAUUSD", "M15", 10)).empty
    with pytest.raises(DataSourceError):
        await src.get_tick("XAUUSD")


async def test_source_place_order_maps_result() -> None:
    client = FakeTerminal()
    src = make_source(client)
    from app.mt5.base import Order

    res = await src.place_order(
        Order(symbol="BTCUSD", side="BUY", volume=0.01, sl=81130.0,
              tp=81430.0, comment="xauai-01234567")
    )
    assert res.ok
    assert res.retcode == 10009
    assert res.ticket == 991000001
    assert res.price == pytest.approx(81230.0)
    assert res.volume == pytest.approx(0.01)
    # platform symbol was mapped to the broker name
    assert client.orders[0]["symbol"] == "BTCUSDm"
    assert client.orders[0]["sl"] == 81130.0


async def test_source_close_resolves_symbol_and_tolerates_gone() -> None:
    client = FakeTerminal(open_positions=[_pos()])
    src = make_source(client)
    res = await src.close_position(991000001)
    assert res.ok
    assert client.closes == [991000001]
    # closing again (already gone broker-side, e.g. SL hit) is a clean ok
    res2 = await src.close_position(991000001)
    assert res2.ok


# --------------------------------------------------------------- McpAutoTrader


async def test_arm_requires_terminal_and_permission() -> None:
    trader, _, _ = make_trader(FakeTerminal(available=False))
    with pytest.raises(ArmError):
        await trader.arm(True, owner="admin-1")

    trader2, _, _ = make_trader(FakeTerminal(trade_allowed=False))
    with pytest.raises(ArmError):
        await trader2.arm(True, owner="admin-1")


async def test_disarmed_trader_ignores_signals() -> None:
    client = FakeTerminal()
    trader, hub, _ = make_trader(client)
    await trader.on_signal(dict(SIG), "BTCUSD", 0.01)
    assert client.orders == []  # never touched the terminal


async def test_armed_signal_places_real_order_with_sl_tp() -> None:
    client = FakeTerminal()
    trader, hub, repo = make_trader(client)
    await trader.arm(True, owner="admin-1")

    await trader.on_signal(dict(SIG), "BTCUSD", 0.01)
    assert len(client.orders) == 1
    order = client.orders[0]
    assert order["symbol"] == "BTCUSDm"
    assert order["side"] == "buy"
    assert order["sl"] == 81130.0  # §8 SL/TP ride with the order
    assert order["tp"] == 81430.0
    assert order["comment"].startswith("xauai-01234567")
    # sizing: equity 500, risk 0.5% = 2.50; SL dist 100; contract 1 -> 0.02
    assert order["volume"] == pytest.approx(0.02)
    # trade recorded with signal linkage + arming admin as owner
    trade = await repo.open_trade_for_signal(SIG["id"], "admin-1")
    assert trade is not None and trade["ticket"] == 991000001
    # structured WS event
    kinds = [k for k, _ in hub.events]
    assert "mt5_auto" in kinds
    order_ev = next(p for k, p in hub.events if k == "mt5_auto" and p["event"] == "order")
    assert order_ev["ok"] is True and order_ev["symbol"] == "BTCUSDm"


async def test_duplicate_signal_is_idempotent() -> None:
    client = FakeTerminal()
    trader, _, _ = make_trader(client)
    await trader.arm(True, owner="admin-1")
    await trader.on_signal(dict(SIG), "BTCUSD", 0.01)
    await trader.on_signal(dict(SIG), "BTCUSD", 0.01)
    assert len(client.orders) == 1


async def test_weekend_market_closed_skips_honestly() -> None:
    """The forex-weekend logic: gold closed Sat/Sun -> signal kept, NO order."""
    client = FakeTerminal()
    trader, hub, _ = make_trader(
        client, market_state=lambda plat: (plat == "BTCUSD", "broker ticking")
    )
    await trader.arm(True, owner="admin-1")
    await trader.on_signal(dict(SIG, symbol="XAUUSD"), "XAUUSD", 0.01)
    assert client.orders == []
    skip = next(p for k, p in hub.events if k == "mt5_auto" and p["event"] == "skip")
    assert "market closed" in skip["reason"]


async def test_broker_rejection_market_closed_is_honest() -> None:
    """No market_state injected -> the broker rejection (10018) is reported."""
    client = FakeTerminal(retcode=10018)
    trader, hub, _ = make_trader(client)
    await trader.arm(True, owner="admin-1")
    await trader.on_signal(dict(SIG), "BTCUSD", 0.01)
    assert client.orders  # attempted (retry included)
    ev = next(p for k, p in hub.events if k == "mt5_auto" and p["event"] == "order")
    assert ev["ok"] is False
    assert "market closed" in ev["detail"].lower()


async def test_terminal_down_at_signal_skips_not_queues() -> None:
    client = FakeTerminal()
    trader, hub, _ = make_trader(client)
    await trader.arm(True, owner="admin-1")
    client.available_flag = False  # terminal drops AFTER arming
    await trader.on_signal(dict(SIG), "BTCUSD", 0.01)
    assert client.orders == []
    skip = next(p for k, p in hub.events if k == "mt5_auto" and p["event"] == "skip")
    assert "unreachable" in skip["reason"]


async def test_daily_loss_kill_switch_closes_all_and_disarms() -> None:
    """§9 emergency: real equity drop past daily_max_loss -> close-all+disarm."""
    client = FakeTerminal()
    cfg = DEFAULT_CONFIG.model_copy(update={"daily_max_loss_pct": 3.0})
    trader, hub, _ = make_trader(client, cfg=cfg)
    await trader.arm(True, owner="admin-1")
    # first order fills; then the account bleeds below the -3% anchor
    await trader.on_signal(dict(SIG), "BTCUSD", 0.01)
    assert trader.armed
    client.equity = 400.0  # -20% from the 500 anchor
    await trader.on_signal(dict(SIG, id="ffffffffffffffff"), "BTCUSD", 0.01)
    assert not trader.armed  # disarmed by the kill switch


async def test_expiry_closes_linked_position() -> None:
    client = FakeTerminal(open_positions=[_pos()])
    trader, hub, repo = make_trader(client)
    await trader.arm(True, owner="admin-1")
    # simulate the earlier auto-order's trade record
    await repo.insert(
        {"signal_id": SIG["id"], "owner": "admin-1", "ticket": 991000001,
         "side": "BUY", "volume": 0.01, "price_open": 81230.0,
         "sl": 81130.0, "tp": 81430.0,
         "opened_at": datetime.now(tz=UTC).isoformat()}
    )
    await trader.notify_signal_status(SIG["id"], "expired")
    assert client.closes == [991000001]
    # record reconciled
    trade = await repo.open_trade_for_signal(SIG["id"], "admin-1")
    assert trade is None


async def test_won_lost_reconciles_without_close() -> None:
    """Broker SL/TP already exited — only the record is closed."""
    client = FakeTerminal(open_positions=[_pos()])
    trader, _, repo = make_trader(client)
    await trader.arm(True, owner="admin-1")
    await repo.insert(
        {"signal_id": SIG["id"], "owner": "admin-1", "ticket": 991000001,
         "side": "BUY", "volume": 0.01, "price_open": 81230.0,
         "opened_at": datetime.now(tz=UTC).isoformat()}
    )
    await trader.notify_signal_status(SIG["id"], "won")
    assert client.closes == []  # broker already closed it
    assert await repo.open_trade_for_signal(SIG["id"], "admin-1") is None


# ------------------------------------------------------------------- relay


async def test_relay_fans_out_to_live_plane() -> None:
    from app.config import Settings
    from app.services.trading import UserTradingManager

    client = FakeTerminal()
    hub = FakeHub()
    trader, _, _ = make_trader(client, hub=hub)
    await trader.arm(True, owner="admin-1")

    mgr = UserTradingManager(
        settings=Settings(database_url="", data_source="live", allow_demo=True),
        public_source=None, hub=hub, db_engine=None, config_repo=None,
    )
    mgr.attach_live_auto_trader(trader)
    await mgr.relay_signal(dict(SIG), "BTCUSD", 0.01)
    assert len(client.orders) == 1
    # exit sync routing
    await mgr.notify_signal_status(SIG["id"], "expired")
    # status routing
    st = await trader.status()
    assert st["armed"] is True
    assert st["risk"]["risk_mode"] == "percent"


# ------------------------------------------------------------ ConfigRepo arm


async def test_config_repo_auto_live_roundtrip_memory() -> None:
    repo = ConfigRepo()
    armed, by = await repo.load_auto_live(None)
    assert armed is False and by is None
    await repo.save_auto_live(None, True, "admin-1")
    armed, by = await repo.load_auto_live(None)
    assert armed is True and by == "admin-1"


# ------------------------------------------------------------------- routes


class _RouteApp:
    """Minimal app stand-in carrying state.mt5_auto + auth stubs."""

    def __init__(self, trader) -> None:
        from types import SimpleNamespace

        self.state = SimpleNamespace(mt5_auto=trader)


async def test_routes_auto_trade() -> None:
    from app.api import routes_mt5

    client = FakeTerminal()
    trader, _, _ = make_trader(client)
    app = _RouteApp(trader)
    from fastapi import HTTPException
    from starlette.requests import Request

    def make_req(app_obj) -> Request:
        return Request(
            {"type": "http", "method": "POST", "url": "/",
             "headers": [], "query_string": b"", "app": app_obj}
        )

    # GET status
    st = await routes_mt5.mt5_auto_trade_status(make_req(app), user={"id": "u1"})
    assert st["armed"] is False
    assert st["terminal"]["available"] is True

    # arm without typed confirm -> 400
    with pytest.raises(HTTPException) as ei:
        await routes_mt5.mt5_auto_trade_arm(
            routes_mt5.AutoTradeLiveBody(enabled=True), make_req(app),
            user={"id": "admin-1", "role": "admin"},
        )
    assert ei.value.status_code == 400

    # arm with confirm -> 200
    res = await routes_mt5.mt5_auto_trade_arm(
        routes_mt5.AutoTradeLiveBody(enabled=True, confirm="ENABLE"),
        make_req(app), user={"id": "admin-1", "role": "admin"},
    )
    assert res["armed"] is True
    assert trader.armed

    # arm while terminal down -> 409 honest refusal
    trader2, _, _ = make_trader(FakeTerminal(available=False))
    app2 = _RouteApp(trader2)
    with pytest.raises(HTTPException) as ei2:
        await routes_mt5.mt5_auto_trade_arm(
            routes_mt5.AutoTradeLiveBody(enabled=True, confirm="ENABLE"),
            make_req(app2), user={"id": "admin-1", "role": "admin"},
        )
    assert ei2.value.status_code == 409

    # disarm always works
    res2 = await routes_mt5.mt5_auto_trade_arm(
        routes_mt5.AutoTradeLiveBody(enabled=False), make_req(app),
        user={"id": "admin-1", "role": "admin"},
    )
    assert res2["armed"] is False
    assert not trader.armed
