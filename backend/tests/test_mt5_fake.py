"""MT5DataSource tests against a FAKE MetaTrader5 module (no Windows needed).

The fake module mimics the real package's API surface (numpy structured
rates, account/symbol info objects, constants) so the wrapper's logic —
UTC offset conversion (C4), closed-bars-only (D-003), symbol discovery (C3),
filling-mode mapping (C5), executor serialization — is fully exercised here.
"""

from __future__ import annotations

import sys
import time
import types
from datetime import UTC, datetime

import numpy as np
import pytest

pytest.importorskip("numpy")


def make_fake_mt5(server_offset_min=120):
    """Build an importable fake MetaTrader5 module."""
    mod = types.ModuleType("MetaTrader5")

    mod.TIMEFRAME_M1 = 1
    mod.TIMEFRAME_M5 = 5
    mod.TIMEFRAME_M15 = 15
    mod.TIMEFRAME_M30 = 30
    mod.TIMEFRAME_H1 = 16385
    mod.TIMEFRAME_H4 = 16388
    mod.TIMEFRAME_D1 = 16408
    mod.TRADE_ACTION_DEAL = 1
    mod.ORDER_TYPE_BUY = 0
    mod.ORDER_TYPE_SELL = 1
    mod.ORDER_TIME_GTC = 0
    mod.ORDER_FILLING_FOK = 0
    mod.ORDER_FILLING_IOC = 1
    mod.ORDER_FILLING_RETURN = 2
    mod.SYMBOL_FILLING_FOK = 1
    mod.SYMBOL_FILLING_IOC = 2
    mod.POSITION_TYPE_BUY = 0
    mod.TRADE_RETCODE_DONE = 10009

    state = {
        "initialized": False,
        "offset_min": server_offset_min,
        "orders": [],
        "filling_flags": mod.SYMBOL_FILLING_FOK,
    }

    class _NS:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    def initialize(path=None, login=0, password="", server=""):
        state["initialized"] = True
        return True

    def shutdown():
        state["initialized"] = False

    def last_error():
        return (0, "ok")

    def terminal_info():
        return _NS(connected=True, trade_allowed=True)

    def account_info():
        return _NS(
            login=12345678, server="Exness-MT5Trial", balance=10_000.0,
            equity=10_050.0, currency="USD", leverage=500,
        )

    def symbols_get(pattern="*"):
        if "XAUUSD" in (pattern or ""):
            return [
                _NS(name="XAUUSDm", point=0.01, filling_mode=state["filling_flags"]),
                _NS(name="XAUUSDz.a", point=0.01, filling_mode=state["filling_flags"]),
            ]
        return []

    def symbol_info(name):
        for s in symbols_get("*XAUUSD*"):
            if s.name == name:
                return s
        return None

    def symbol_info_tick(name):
        server_now = time.time() + state["offset_min"] * 60
        return _NS(bid=2695.10, ask=2695.30, time=int(server_now), last=0.0)

    _RATES_DTYPE = np.dtype(
        [
            ("time", "i8"), ("open", "f8"), ("high", "f8"), ("low", "f8"),
            ("close", "f8"), ("tick_volume", "f8"), ("spread", "i4"),
            ("real_volume", "f8"),
        ]
    )

    def _mk_rates(n, start_epoch, step_min=15):
        arr = np.zeros(n, dtype=_RATES_DTYPE)
        price = 2650.0
        for i in range(n):
            t = start_epoch + i * step_min * 60
            o = price
            c = price + 1.5
            arr[i] = (t, o, max(o, c) + 0.8, min(o, c) - 0.8, c, 100 + i, 20, 0)
            price = c
        return arr

    def copy_rates_from_pos(symbol, timeframe, start_pos, count):
        if not state["initialized"]:
            return None
        # forming bar at pos 0 with the CURRENT server time bucket
        server_now = int(time.time()) + state["offset_min"] * 60
        bucket = server_now // (15 * 60) * 15 * 60
        hist_start = bucket - (count + 1) * 15 * 60  # enough closed bars back
        arr = _mk_rates(count + 1, hist_start)
        # arr[0] is the oldest; last element = forming bar (pos 0)
        # for pos=1 (closed bars) drop nothing — caller slices; emulate:
        if start_pos == 0:
            return arr  # includes forming
        return arr[:-1]  # closed bars only

    def order_send(request):
        state["orders"].append(dict(request))
        return _NS(retcode=10009, order=987654, price=request.get("price", 0.0), comment="ok")

    def positions_get():
        return [
            _NS(
                ticket=111, symbol="XAUUSDm", type=mod.POSITION_TYPE_BUY,
                volume=0.02, price_open=2690.0, sl=2685.0, tp=2700.0,
                profit=1.2, time=time.time() + state["offset_min"] * 60,
            )
        ]

    mod.initialize = initialize
    mod.shutdown = shutdown
    mod.last_error = last_error
    mod.terminal_info = terminal_info
    mod.account_info = account_info
    mod.symbols_get = symbols_get
    mod.symbol_info = symbol_info
    mod.symbol_info_tick = symbol_info_tick
    mod.copy_rates_from_pos = copy_rates_from_pos
    mod.order_send = order_send
    mod.positions_get = positions_get
    mod._state = state
    return mod


@pytest.fixture()
def fake_mt5(monkeypatch):
    mod = make_fake_mt5(server_offset_min=120)
    monkeypatch.setitem(sys.modules, "MetaTrader5", mod)
    yield mod
    sys.modules.pop("MetaTrader5", None)


@pytest.fixture()
async def source(fake_mt5):
    from app.mt5.mt5_source import MT5DataSource

    s = MT5DataSource()
    await s.connect({"server": "Exness-MT5Trial", "login": "12345678", "password": "pw"})
    yield s
    await s.disconnect()


@pytest.mark.asyncio
class TestMT5DataSourceFake:
    async def test_connect_returns_account(self, source):
        info = await source.account_info()
        assert info["login"] == "12345678"
        assert info["balance"] == 10_000.0

    async def test_broker_offset_detected(self, source, fake_mt5):
        # C4: Exness-style +2h server time
        assert source.broker_utc_offset_minutes == 120

    async def test_get_rates_converts_to_utc_closed_only(self, source, fake_mt5):
        import pandas as pd

        df = await source.get_rates("XAUUSDm", "M15", 50)
        assert len(df) <= 50
        assert list(df.columns) == ["time_utc", "o", "h", "l", "c", "v"]
        now = datetime.now(UTC)
        # all bars closed and in the past (UTC-converted, +2h offset applied)
        assert (df["time_utc"] + pd.Timedelta(minutes=1)).max() < now
        # closed only: the forming bucket is NOT included
        forming_bucket = int(datetime.now(UTC).timestamp() // 900) * 900
        assert int(df["time_utc"].iloc[-1].timestamp()) < forming_bucket
        # without the offset conversion the newest closed bar would sit
        # ~offset_min in the future
        assert (now - df["time_utc"].iloc[-1]).total_seconds() < 2 * 3600

    async def test_discover_symbols_c3(self, source):
        names = source.discover_symbols("*XAUUSD*")
        assert names == ["XAUUSDm", "XAUUSDz.a"]

    async def test_point_size(self, source):
        assert source.point_size("XAUUSDm") == 0.01

    async def test_tick_utc(self, source):
        t = await source.get_tick("XAUUSDm")
        delta = abs((t.time - datetime.now(UTC)).total_seconds())
        assert delta < 300  # converted to UTC (server stamp was +2h)

    async def test_place_order_fok_mapping(self, source, fake_mt5):
        from app.mt5.base import Order

        res = await source.place_order(
            Order(symbol="XAUUSDm", side="BUY", volume=0.01, sl=2690.0,
                  tp=2700.0, magic=234000, comment="sfp-test")
        )
        assert res.ok and res.retcode == 10009
        req = fake_mt5._state["orders"][-1]
        assert req["type"] == fake_mt5.ORDER_TYPE_BUY
        assert req["magic"] == 234000
        assert req["sl"] == 2690.0 and req["tp"] == 2700.0
        assert req["type_filling"] == fake_mt5.ORDER_FILLING_FOK

    async def test_place_order_ioc_fallback(self, source, fake_mt5):
        from app.mt5.base import Order

        fake_mt5._state["filling_flags"] = fake_mt5.SYMBOL_FILLING_IOC
        source._symbol_point.clear()
        await source.place_order(
            Order(symbol="XAUUSDm", side="SELL", volume=0.02)
        )
        req = fake_mt5._state["orders"][-1]
        assert req["type_filling"] == fake_mt5.ORDER_FILLING_IOC

    async def test_positions(self, source):
        rows = await source.get_positions()
        assert len(rows) == 1
        assert rows[0].side == "BUY" and rows[0].symbol == "XAUUSDm"

    async def test_is_connected(self, source):
        assert await source.is_connected()
        await source.disconnect()
        assert not await source.is_connected()
