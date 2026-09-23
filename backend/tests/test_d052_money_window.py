"""D-052 tests — the money-management window's USD limits and auto-off.

User directive (Bengali): the money-management window carries today's
balance, stop loss USD, target profit USD, lot size, signals per day and
concurrent trades. When ANY of them completes, the AI button turns itself
OFF automatically ("এর যেকোনো একটি যদি সম্পুর্ণ হয়ে যায় বাটন টি অটো অফ
হয়ে যাবে").

Covers:
- evaluate_kill_switches: USD stop loss breaches -> kill_daily_loss
- evaluate_kill_switches: USD target profit reached -> kill_profit
- execute_signal: profit lock closes every position + disarms
- execute_signal: USD stop loss closes every position + disarms
- arm(day_start_equity=...): the user's entered balance anchors the window
- EngineConfig: the three new D-052 fields exist and default off
"""

from __future__ import annotations

import pytest

from app.engine.config import EngineConfig
from app.engine.executor import OrderExecutor, TradeRepo, evaluate_kill_switches


class _Pos:
    def __init__(self, ticket: int) -> None:
        self.ticket = ticket


class _Src:
    """Fake plane source: N open positions, configurable equity."""

    def __init__(self, equity: float = 1000.0, positions: int = 0) -> None:
        self._equity = equity
        self._positions = [_Pos(i + 1) for i in range(positions)]
        self.closed: list[int] = []

    async def get_positions(self):
        return list(self._positions)

    def account_info(self):
        return {"equity": self._equity, "balance": self._equity}

    async def symbol_info(self, symbol):
        return None

    async def place_order(self, order):
        from app.mt5.base import OrderResult

        return OrderResult(ok=True, ticket=9, price=100.0, retcode=0,
                           comment="filled")

    async def close_position(self, ticket: int):
        self.closed.append(ticket)
        from app.mt5.base import OrderResult

        return OrderResult(ok=True, ticket=ticket, price=100.0, retcode=0,
                           comment="closed")


SIG = {"id": "s1", "direction": "BUY", "entry": 100.0, "sl": 99.0,
       "tp": 101.0, "spread_points": 10}


# ------------------------------------------------------- config surface


def test_engine_config_has_d052_money_window_fields():
    cfg = EngineConfig()
    assert cfg.daily_loss_usd == 0.0   # off by default
    assert cfg.daily_profit_usd == 0.0  # off by default
    assert cfg.day_start_balance == 0.0  # anchor at arm time
    cfg2 = EngineConfig(daily_loss_usd=50.0, daily_profit_usd=120.0,
                        day_start_balance=1000.0)
    assert cfg2.daily_loss_usd == 50.0
    assert cfg2.daily_profit_usd == 120.0
    assert cfg2.day_start_balance == 1000.0


# ---------------------------------------------------- kill-switch checks


@pytest.mark.asyncio
async def test_usd_stop_loss_triggers_daily_loss_kill():
    cfg = EngineConfig(daily_loss_usd=50.0)
    src = _Src(equity=940.0)  # -60 from a 1000 anchor
    verdict = await evaluate_kill_switches(
        cfg, src, "XAUUSD", 0.01, spread_points=5,
        day_start_equity=1000.0, positions=[],
    )
    assert not verdict.allowed
    assert verdict.kill_daily_loss is True
    assert "stop loss" in verdict.reason


@pytest.mark.asyncio
async def test_usd_target_profit_triggers_profit_lock():
    cfg = EngineConfig(daily_profit_usd=100.0)
    src = _Src(equity=1120.0)  # +120 from a 1000 anchor
    verdict = await evaluate_kill_switches(
        cfg, src, "XAUUSD", 0.01, spread_points=5,
        day_start_equity=1000.0, positions=[],
    )
    assert not verdict.allowed
    assert verdict.kill_profit is True
    assert "target" in verdict.reason


@pytest.mark.asyncio
async def test_inside_money_window_stays_allowed():
    cfg = EngineConfig(daily_loss_usd=50.0, daily_profit_usd=100.0)
    src = _Src(equity=1030.0)  # +30 — inside both bounds
    verdict = await evaluate_kill_switches(
        cfg, src, "XAUUSD", 0.01, spread_points=5,
        day_start_equity=1000.0, positions=[],
    )
    assert verdict.allowed


# ------------------------------------------------- execute_signal auto-off


@pytest.mark.asyncio
async def test_profit_target_auto_off_closes_everything():
    cfg = EngineConfig(daily_profit_usd=100.0)
    src = _Src(equity=1150.0, positions=3)
    ex = OrderExecutor(src, cfg, TradeRepo(None))
    ex.arm(True, day_start_equity=1000.0)
    assert ex.auto_trade is True
    res = await ex.execute_signal(SIG, "XAUUSD", 0.01)
    assert res is None
    # the button turned itself OFF + every position was closed (locked)
    assert ex.auto_trade is False
    assert len(src.closed) == 3
    assert "target" in (ex.last_skip_reason or "")


@pytest.mark.asyncio
async def test_usd_stop_loss_auto_off_closes_everything():
    cfg = EngineConfig(daily_loss_usd=50.0)
    src = _Src(equity=930.0, positions=2)
    ex = OrderExecutor(src, cfg, TradeRepo(None))
    ex.arm(True, day_start_equity=1000.0)
    res = await ex.execute_signal(SIG, "XAUUSD", 0.01)
    assert res is None
    assert ex.auto_trade is False
    assert len(src.closed) == 2
    assert "stop loss" in (ex.last_skip_reason or "")


# --------------------------------------------------------- day anchor


def test_arm_with_user_balance_anchors_the_day():
    cfg = EngineConfig(day_start_balance=2500.0)
    src = _Src(equity=1000.0)  # equity differs — the USER's number wins
    ex = OrderExecutor(src, cfg, TradeRepo(None))
    ex.arm(True, day_start_equity=2500.0)
    assert ex._day_start_equity == 2500.0  # noqa: SLF001 — anchor asserted
    # re-arming resets the counters (a new day's window)
    ex._day_trades = 5  # noqa: SLF001
    ex.arm(True, day_start_equity=2500.0)
    assert ex._day_trades == 0  # noqa: SLF001
