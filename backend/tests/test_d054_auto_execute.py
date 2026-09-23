"""D-054 tests — auto-trade actually executes, pending orders are visible,
and the money window anchors on REAL equity.

User report (Bengali): "অটো ট্রেড এক্সিকিউট হয় না / পেন্ডিং অর্ডার creat হয় না".

Root causes fixed here (each with a regression test):
- A. set_auto_trade anchored the USD window on the TYPED day_start_balance;
     any drift vs plane equity made the FIRST signal instantly hit the
     profit/loss verdict and disarm before any order was placed.
     -> test_arm_anchors_on_real_equity_no_instant_disarm
- B. the admin money window never reached the institution executor.
     -> test_apply_money_window_merges_and_survives_apply_config
     -> test_arm_anchors_institution_window_on_terminal_equity
- C. UserTradingManager.apply_config wiped per-user money settings.
     -> test_apply_config_preserves_user_money_settings
- E. a practice plane that lost its source NEVER reconnected; every order
     failed "live: not connected" while status said connected=True.
     -> test_reconnect_plane_heals_disconnected_source
     -> test_plane_status_reports_honest_connection
- F. pending limit orders were invisible (repo rows + plane book).
     -> test_repo_persists_pending_contract
     -> test_pending_orders_visible_from_plane
     -> test_relay_creates_and_exposes_pending_limit_order
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.config import Settings
from app.engine.config import DEFAULT_CONFIG, ConfigRepo, EngineConfig
from app.engine.repo import SignalRepo
from app.mt5.base import Order
from app.mt5.mock_source import MockDataSource
from app.services.trading import UserTradingManager, _source_connected
from tests.conftest import make_mock

# fake infra reused from the auto-live suite --------------------------------


class FakeTerminal:
    """Scriptable stand-in for MT5TerminalClient (all calls sync)."""

    def __init__(self, *, equity: float = 500.0, trade_allowed: bool = True) -> None:
        self.equity = equity
        self.trade_allowed = trade_allowed
        self.orders: list[dict] = []

    def available(self) -> bool:
        return True

    def account(self) -> dict:
        return {
            "account": {
                "login": 414350770, "server": "Exness-MT5Trial6",
                "balance": self.equity, "equity": self.equity,
                "profit": 0.0, "currency": "USD", "leverage": 200,
            },
            "terminal": {
                "server_connected": True,
                "mcp_trade_allowed": self.trade_allowed,
                "experts_trade_allowed": self.trade_allowed,
                "build": 6204,
            },
        }

    def positions(self, include_orders: bool = True) -> dict:  # noqa: ARG002
        return {"positions": [], "orders": []}

    def symbols(self) -> list[dict]:
        return [
            {"symbol": "XAUUSDm", "point": 0.001, "contract_size": 100.0,
             "volume_min": 0.01, "volume_max": 200.0, "volume_step": 0.01,
             "description": "Gold", "digits": 3},
        ]

    def market_order(self, symbol, side, volume, sl=None, tp=None, comment=""):
        self.orders.append({"symbol": symbol, "side": side, "volume": volume})
        return {"retcode": 10009, "retcode_details": "done",
                "deal": 4425000001, "order": 991000001,
                "price": 2900.0, "volume": volume, "symbol": symbol}


class FakeHub:
    def __init__(self) -> None:
        self.user_events: list[tuple[str, str, dict]] = []
        self.events: list[tuple[str, dict]] = []

    async def broadcast_all(self, event: str, payload: dict) -> None:
        self.events.append((event, payload))

    async def broadcast_user(self, user_id: str, event: str, payload: dict) -> None:
        self.user_events.append((user_id, event, payload))


USER_A = "11111111-1111-1111-1111-111111111111"

MONEY_SETTINGS = {
    "risk_mode": "fixed",
    "day_start_balance": 10_000.0,   # TYPED (stale) balance
    "daily_loss_usd": 50.0,
    "daily_profit_usd": 100.0,       # would instantly "complete" vs 10 150
    "fixed_lot": 0.02,
    "max_trades_per_day": 6,
    "max_positions": 3,
}


def _manager(public: MockDataSource | None = None,
             settings: dict | None = None) -> tuple[UserTradingManager, FakeHub]:
    hub = FakeHub()
    mgr = UserTradingManager(
        settings=Settings(database_url=None, data_source="mock"),
        public_source=public or make_mock(),
        hub=hub,
        db_engine=None,
        config_repo=ConfigRepo(),
    )
    if settings is not None:
        async def _load(owner, _settings=None):
            return {"balance": 10_000.0, "currency": "USD",
                    "auto_trade": False, "settings": settings}
        mgr._load_account = _load  # type: ignore[method-assign]
    return mgr, hub


SIG = {"id": "d054-sig-1", "direction": "BUY", "entry": 100.0,
       "sl": 99.0, "tp": 101.0, "spread_points": 10, "symbol": "XAUUSDm"}


# ------------------------------------------------ A. real-equity anchor

@pytest.mark.asyncio
async def test_arm_anchors_on_real_equity_no_instant_disarm() -> None:
    """Typed balance 10 000 vs live equity 10 150, target +100 USD.

    OLD behavior: anchor = 10 000 -> pnl +150 >= 100 -> profit lock on the
    FIRST signal -> disarm + zero orders (the reported bug). NEW: the day
    anchors on the plane's REAL equity, pnl = 0, the order is placed and
    auto-trade stays armed.
    """
    mgr, _ = _manager(settings=dict(MONEY_SETTINGS))
    plane = await mgr.ensure_plane(USER_A)
    # plane equity drifted to 10 150 while the typed balance stayed 10 000
    plane.source.account_info = (  # type: ignore[method-assign]
        lambda: {"equity": 10_150.0, "balance": 10_150.0, "currency": "USD"}
    )
    await mgr.set_auto_trade(USER_A, True)
    assert plane.executor._day_start_equity == pytest.approx(10_150.0)

    result = await plane.executor.execute_signal(SIG, "XAUUSDm", 0.001)
    assert result is not None and result.ok
    assert plane.executor.auto_trade is True  # NOT disarmed by the window


# ------------------------------------------------ B. institution window

@pytest.mark.asyncio
async def test_apply_money_window_merges_and_survives_apply_config() -> None:
    from app.mt5.auto_trader import McpAutoTrader
    from app.mt5.mcp_source import McpTradingSource

    trader = McpAutoTrader(
        source=McpTradingSource(client=FakeTerminal()),  # type: ignore[arg-type]
        cfg=DEFAULT_CONFIG.model_copy(update={"auto_trade_symbols": ["XAUUSD"]}),
        repo=MagicMock(), hub=FakeHub(), config_repo=None,
    )
    await trader.apply_money_window(MONEY_SETTINGS)
    assert trader._executor._cfg.daily_loss_usd == 50.0
    assert trader._executor._cfg.fixed_lot == 0.02

    # a global config save must NOT wipe the admin's window
    await trader.apply_config(EngineConfig())
    assert trader._executor._cfg.daily_loss_usd == 50.0
    assert trader._executor._cfg.daily_profit_usd == 100.0


@pytest.mark.asyncio
async def test_arm_anchors_institution_window_on_terminal_equity() -> None:
    from app.mt5.auto_trader import McpAutoTrader
    from app.mt5.mcp_source import McpTradingSource

    trader = McpAutoTrader(
        source=McpTradingSource(client=FakeTerminal(equity=8_250.0)),  # type: ignore[arg-type]
        cfg=DEFAULT_CONFIG.model_copy(update={"auto_trade_symbols": ["XAUUSD"]}),
        repo=MagicMock(), hub=FakeHub(), config_repo=None,
    )
    await trader.apply_money_window(MONEY_SETTINGS)
    await trader.arm(True, owner="admin-1")
    assert trader.armed is True
    assert trader._executor._day_start_equity == pytest.approx(8_250.0)


# ------------------------------------------------ C. apply_config keeps user cfg

@pytest.mark.asyncio
async def test_apply_config_preserves_user_money_settings() -> None:
    mgr, _ = _manager(settings=dict(MONEY_SETTINGS))
    plane = await mgr.ensure_plane(USER_A)
    assert plane.executor._cfg.daily_loss_usd == 50.0

    # PUT /api/config lands the GLOBAL config (money fields at defaults)
    await mgr.apply_config(EngineConfig())
    assert plane.executor._cfg.daily_loss_usd == 50.0  # user window kept
    assert plane.executor._cfg.fixed_lot == 0.02


# ------------------------------------------------ E. plane reconnect

@pytest.mark.asyncio
async def test_reconnect_plane_heals_disconnected_source() -> None:
    mgr, _ = _manager()
    plane = await mgr.ensure_plane(USER_A)
    assert await _source_connected(plane.source) is True
    await plane.source.disconnect()
    assert await _source_connected(plane.source) is False

    ok = await mgr._reconnect_plane(plane)
    assert ok is True
    assert await _source_connected(plane.source) is True


@pytest.mark.asyncio
async def test_plane_status_reports_honest_connection() -> None:
    mgr, _ = _manager()
    plane = await mgr.ensure_plane(USER_A)
    assert (await plane.status_async())["connected"] is True
    await plane.source.disconnect()
    assert (await plane.status_async())["connected"] is False


# ------------------------------------------------ F. pending visibility

@pytest.mark.asyncio
async def test_repo_persists_pending_contract() -> None:
    repo = SignalRepo(None)
    payload = {
        "id": "sig-pending-1", "direction": "SELL", "entry": 4520.0,
        "sl": 4525.0, "tp": 4510.0, "confidence": 0.66, "trace": {},
        "bar_time": None, "entry_type": "limit", "market_ref": 4513.4,
        "entry_note": "sell limit at supply zone HI edge",
    }
    sid = await repo.insert(payload, "XAUUSDm", "M1")
    row = next(r for r in await repo.list() if r["id"] == sid)
    assert row["entry_type"] == "limit"
    assert row["market_ref"] == pytest.approx(4513.4)
    assert row["entry_note"].startswith("sell limit")
    # default stays "market" for plain signals
    sid2 = await repo.insert(
        {"id": "sig-market-1", "direction": "BUY", "entry": 100.0,
         "sl": 99.0, "tp": 101.0, "confidence": 0.7, "trace": {},
         "bar_time": None},
        "XAUUSDm", "M1",
    )
    row2 = next(r for r in await repo.list() if r["id"] == sid2)
    assert row2["entry_type"] == "market"


@pytest.mark.asyncio
async def test_pending_orders_visible_from_plane() -> None:
    mgr, _ = _manager()
    plane = await mgr.ensure_plane(USER_A)
    tick = await plane.source.get_tick("XAUUSDm")
    res = await plane.source.place_order(
        Order(symbol="XAUUSDm", side="BUY", volume=0.05,
              sl=tick.ask - 30.0, tp=tick.ask - 10.0,
              order_type="buy_limit", price=tick.ask - 20.0),
    )
    assert res.ok  # placed into the pending book

    pendings = await mgr.pending_orders(USER_A)
    assert len(pendings) == 1
    p = pendings[0]
    assert p["order_type"] == "buy_limit"
    assert p["price"] == pytest.approx(tick.ask - 20.0)
    assert p["sl"] is not None and p["tp"] is not None
    # still NOT a position — it waits at its fill price
    assert await mgr.positions(USER_A) == []


@pytest.mark.asyncio
async def test_degraded_mode_settings_still_reach_plane() -> None:
    """db=None (no DATABASE_URL): PUT /api/trading/settings must still
    govern the plane executor instead of being silently dropped.

    The old set_user_settings returned early when db was None — the money
    window never reached the executor on dev stacks / any no-DB deploy, so
    sizing stayed risk-percent and the USD limits never guarded anything.
    """
    mgr, _ = _manager()  # db None, no settings fixture
    plane = await mgr.ensure_plane(USER_A)
    assert plane.executor._cfg.fixed_lot != 0.02  # global default first

    await mgr.set_user_settings(USER_A, dict(MONEY_SETTINGS))
    # live-applied immediately...
    assert plane.executor._cfg.fixed_lot == 0.02
    assert plane.executor._cfg.daily_loss_usd == 50.0
    # ...and reloadable (the arm path re-reads _user_cfg)
    cfg = await mgr._user_cfg(USER_A)
    assert cfg.fixed_lot == 0.02
    assert cfg.daily_profit_usd == 100.0


@pytest.mark.asyncio
async def test_relay_creates_and_exposes_pending_limit_order() -> None:
    """The end-to-end complaint: an armed plane + a limit signal ->
    the pending order IS created and IS visible."""
    mgr, _ = _manager(settings=dict(MONEY_SETTINGS))
    await mgr.set_auto_trade(USER_A, True)
    plane = mgr.plane(USER_A)
    tick = await plane.source.get_tick("XAUUSDm")

    sig = {
        "id": "d054-limit-1", "direction": "BUY",
        "entry": round(tick.ask - 4.5, 2),   # inside the 1..6 USD window
        "sl": round(tick.ask - 4.5 - 20.0, 2), "tp": round(tick.ask - 4.5 + 30.0, 2),
        "confidence": 0.7, "spread_points": 5.0, "symbol": "XAUUSDm",
        "entry_type": "limit", "market_ref": round(tick.ask, 2),
        "entry_note": "buy limit at demand zone LO edge",
    }
    await mgr.relay_signal(sig, "XAUUSDm", 0.001)

    pendings = await mgr.pending_orders(USER_A)
    assert len(pendings) == 1, "pending limit order must exist after relay"
    assert pendings[0]["side"] == "BUY"
    assert plane.executor.auto_trade is True  # still armed — window intact
