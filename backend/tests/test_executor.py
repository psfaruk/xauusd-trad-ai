"""Phase 4 tests — executor math, kill switches, idempotency (SPEC §9)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from app.engine.config import DEFAULT_CONFIG
from app.engine.executor import (
    OrderExecutor,
    TradeRepo,
    evaluate_kill_switches,
    size_lot,
)
from app.mt5.base import Order, OrderResult, Position, SymbolInfo
from app.mt5.mock_source import MockDataSource
from tests.conftest import make_mock

# --------------------------------------------------------------------- sizing


def test_lot_sizing_percent_mode() -> None:
    """SPEC §9 formula: lots = risk_amount / (sl_distance x contract)."""
    cfg = DEFAULT_CONFIG.model_copy(update={"risk_percent": 1.0})
    # equity 10_000, risk 1% = 100 USD; SL distance 2.0 USD; contract 100
    # -> 100 / (2 * 100) = 0.5 lots
    lot = size_lot(cfg, equity=10_000.0, sl_distance=2.0)
    assert lot.lots == pytest.approx(0.5)
    assert lot.risk_amount == pytest.approx(100.0)
    assert not lot.clamped


def test_lot_sizing_rounds_down_to_step() -> None:
    cfg = DEFAULT_CONFIG.model_copy(update={"risk_percent": 0.5})
    # 10_000 x 0.5% = 50; 50 / (1.4 * 100) = 0.35714 -> floor to 0.35
    lot = size_lot(cfg, equity=10_000.0, sl_distance=1.4)
    assert lot.lots == pytest.approx(0.35)


def test_lot_sizing_clamps_to_broker_limits() -> None:
    cfg = DEFAULT_CONFIG.model_copy(update={"risk_percent": 10.0})
    info = SymbolInfo(name="X", volume_min=0.05, volume_max=1.0, volume_step=0.01)
    # huge risk -> raw 4.7 -> clamp to max 1.0
    lot = size_lot(cfg, equity=10_000.0, sl_distance=2.123, info=info)
    assert lot.lots == 1.0
    assert lot.clamped
    # tiny risk -> raw 0.002 -> clamp up to min 0.05
    cfg2 = DEFAULT_CONFIG.model_copy(update={"risk_percent": 0.01})
    lot2 = size_lot(cfg2, equity=1_000.0, sl_distance=5.0, info=info)
    assert lot2.lots == 0.05
    assert lot2.clamped


def test_lot_sizing_fixed_mode() -> None:
    cfg = DEFAULT_CONFIG.model_copy(update={"risk_mode": "fixed", "fixed_lot": 0.02})
    lot = size_lot(cfg, equity=10_000.0, sl_distance=3.0)
    assert lot.lots == pytest.approx(0.02)


def test_lot_sizing_degenerate_sl() -> None:
    lot = size_lot(DEFAULT_CONFIG, equity=10_000.0, sl_distance=0.0)
    assert lot.lots == 0.01  # volume_min


# --------------------------------------------------------------- kill switches


async def test_kill_switch_max_positions() -> None:
    mock = make_mock()
    await mock.connect({"login": "1", "password": "x", "server": "s"})
    positions = [
        Position(1, "XAUUSDm", "BUY", 0.1, 2650.0, None, None, 0.0, datetime.now(UTC))
    ]
    verdict = await evaluate_kill_switches(
        DEFAULT_CONFIG.model_copy(update={"max_positions": 1}),
        mock, "XAUUSDm", 0.01,
        spread_points=10.0, positions=positions,
    )
    assert not verdict.allowed
    assert "max_positions" in verdict.reason


async def test_kill_switch_spread() -> None:
    mock = make_mock()
    await mock.connect({"login": "1", "password": "x", "server": "s"})
    verdict = await evaluate_kill_switches(
        DEFAULT_CONFIG, mock, "XAUUSDm", 0.01,
        spread_points=50.0, day_start_equity=10_000.0, positions=[],
    )
    assert not verdict.allowed
    assert "spread" in verdict.reason


async def test_kill_switch_daily_loss() -> None:
    """Daily loss >= daily_max_loss_pct -> kill flag raised (SPEC §12 AC)."""
    mock = make_mock()
    await mock.connect({"login": "1", "password": "x", "server": "s"})
    mock.set_starting_balance(10_000.0)
    # open a BUY, then crash the market in the minute the clock lands on
    res = await mock.place_order(Order(symbol="XAUUSDm", side="BUY", volume=1.0))
    assert res.ok
    crash_minute = int((mock._vnow() - mock._origin).total_seconds() // 60) + 2
    mock._m1_overrides[crash_minute] = (2650.0, 2650.0, 2400.0, 2410.0, 100)
    mock.advance_minutes(2.5)  # land mid-crash-minute -> price ~2530
    verdict = await evaluate_kill_switches(
        DEFAULT_CONFIG, mock, "XAUUSDm", 0.01,
        spread_points=10.0, day_start_equity=10_000.0, positions=None,
    )
    assert not verdict.allowed
    assert verdict.kill_daily_loss


# ----------------------------------------------------------- executor behavior


class _Hub:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def broadcast_all(self, event: str, payload: dict) -> None:
        self.events.append((event, payload))


def _signal(direction="BUY", entry=2650.0, sl=2648.0, tp=2654.0, sid="sig-1") -> dict:
    return {
        "id": sid, "direction": direction, "entry": entry, "sl": sl, "tp": tp,
        "spread_points": 20.0, "confidence": 0.7,
    }


async def test_executor_noop_when_auto_trade_off() -> None:
    """SPEC §0/§9: auto_trade false -> NEVER send a real order."""
    mock = make_mock()
    await mock.connect({"login": "1", "password": "x", "server": "s"})
    repo = TradeRepo(None)
    ex = OrderExecutor(mock, DEFAULT_CONFIG, repo, hub=_Hub())
    result = await ex.execute_signal(_signal(), "XAUUSDm", 0.01)
    assert result is None
    assert await mock.get_positions() == []


async def test_executor_executes_when_armed() -> None:
    mock = make_mock()
    await mock.connect({"login": "1", "password": "x", "server": "s"})
    mock.set_starting_balance(10_000.0)
    repo = TradeRepo(None)
    hub = _Hub()
    ex = OrderExecutor(mock, DEFAULT_CONFIG, repo, hub=hub)
    ex.arm(True)
    result = await ex.execute_signal(_signal(), "XAUUSDm", 0.01)
    assert result is not None and result.ok
    positions = await mock.get_positions()
    assert len(positions) == 1
    # SL/TP attached (SPEC §12 Phase 4 AC)
    assert positions[0].sl == 2648.0
    assert positions[0].tp == 2654.0
    # lot per formula: 10_000 x 0.5% = 50 risk; |2650-2648| x 100 = 200 -> 0.25
    assert positions[0].volume == pytest.approx(0.25)


async def test_executor_idempotent_per_signal() -> None:
    """Zero duplicates per signal (SPEC §12 Phase 4 AC)."""
    mock = make_mock()
    await mock.connect({"login": "1", "password": "x", "server": "s"})
    mock.set_starting_balance(10_000.0)
    repo = TradeRepo(None)
    ex = OrderExecutor(mock, DEFAULT_CONFIG, repo, hub=_Hub())
    ex.arm(True)
    first = await ex.execute_signal(_signal(sid="same-id"), "XAUUSDm", 0.01)
    second = await ex.execute_signal(_signal(sid="same-id"), "XAUUSDm", 0.01)
    assert first is not None and first.ok
    assert second is None  # duplicate skipped
    assert len(await mock.get_positions()) == 1


async def test_executor_respects_max_positions() -> None:
    mock = make_mock()
    await mock.connect({"login": "1", "password": "x", "server": "s"})
    mock.set_starting_balance(10_000.0)
    repo = TradeRepo(None)
    cfg = DEFAULT_CONFIG.model_copy(update={"max_positions": 1})
    ex = OrderExecutor(mock, cfg, repo, hub=_Hub())
    ex.arm(True)
    assert (await ex.execute_signal(_signal(sid="a"), "XAUUSDm", 0.01)).ok
    # one position open now (max_positions=1) -> second signal skipped
    result = await ex.execute_signal(_signal(sid="b"), "XAUUSDm", 0.01)
    assert result is None
    assert len(await mock.get_positions()) == 1


async def test_executor_daily_loss_emergency_stop() -> None:
    """Kill switch: close all + disarm + CRITICAL log (SPEC §12 Phase 4 AC)."""
    mock = make_mock()
    await mock.connect({"login": "1", "password": "x", "server": "s"})
    mock.set_starting_balance(10_000.0)
    repo = TradeRepo(None)
    hub = _Hub()
    ex = OrderExecutor(mock, DEFAULT_CONFIG, repo, hub=hub)
    ex.arm(True)
    await ex.execute_signal(_signal(sid="first"), "XAUUSDm", 0.01)
    assert ex.auto_trade
    assert len(await mock.get_positions()) == 1

    # crash the market in the minute the clock will land on, then advance
    crash_minute = int((mock._vnow() - mock._origin).total_seconds() // 60) + 2
    mock._m1_overrides[crash_minute] = (2650.0, 2650.0, 2400.0, 2410.0, 100)
    mock.advance_minutes(2.5)

    # even though max_positions=1 would skip, the daily-loss emergency fires
    result = await ex.execute_signal(_signal(sid="second"), "XAUUSDm", 0.01)
    assert result is None
    assert not ex.auto_trade  # disarmed
    levels = [e[1].get("level") for e in hub.events if e[0] == "engine_log"]
    assert "critical" in levels
    assert await mock.get_positions() == []  # everything closed


async def test_executor_retry_once_on_failure() -> None:
    class FlakySource(MockDataSource):
        def __init__(self) -> None:
            super().__init__(time_scale=0)
            self._fails = 0

        async def place_order(self, order):  # type: ignore[override]
            if self._fails < 1:
                self._fails += 1
                return OrderResult(ok=False, retcode=10004, comment="requote")
            return await super().place_order(order)

    src = FlakySource()
    await src.connect({"login": "1", "password": "x", "server": "s"})
    src.set_starting_balance(10_000.0)
    ex = OrderExecutor(src, DEFAULT_CONFIG, TradeRepo(None), hub=_Hub())
    ex.arm(True)
    result = await ex.execute_signal(_signal(), "XAUUSDm", 0.01)
    assert result is not None and result.ok  # succeeded on retry #1


def test_mock_floating_pl_and_close() -> None:
    """Mock paper-trading: floating P/L priced off the market + close realizes."""
    async def run() -> None:
        mock = make_mock()
        await mock.connect({"login": "1", "password": "x", "server": "s"})
        mock.set_starting_balance(10_000.0)
        res = await mock.place_order(
            Order(symbol="XAUUSDm", side="BUY", volume=0.5)
        )
        assert res.ok
        positions = await mock.get_positions()
        assert len(positions) == 1
        # account equity reflects floating P/L
        info = mock.account_info()
        assert info is not None
        assert info["equity"] != 10_000.0 or positions[0].profit == 0.0
        closed = await mock.close_position(res.ticket)
        assert closed.ok
        assert await mock.get_positions() == []
        info2 = mock.account_info()
        assert info2 is not None
        assert info2["balance"] == pytest.approx(10_000.0 + positions[0].profit)

    asyncio.run(run())


def test_mock_sibling_shares_public_market() -> None:
    """Per-user demo planes see the SAME prices as the public chart (lockstep)."""
    async def run() -> None:
        public = make_mock()
        await public.connect({"login": "1", "password": "x", "server": "s"})
        user_plane = MockDataSource.sibling(public)
        await user_plane.connect({"login": "2", "password": "y", "server": "s"})
        t1 = await public.get_tick("XAUUSDm")
        t2 = await user_plane.get_tick("XAUUSDm")
        assert t1.bid == pytest.approx(t2.bid, abs=1e-6)
        assert t1.ask == pytest.approx(t2.ask, abs=1e-6)
        # and after advancing virtual time, they stay aligned
        public.advance_minutes(3)
        user_plane.advance_minutes(3)
        t3 = await public.get_tick("XAUUSDm")
        t4 = await user_plane.get_tick("XAUUSDm")
        assert t3.bid == pytest.approx(t4.bid, abs=1e-6)

    asyncio.run(run())
