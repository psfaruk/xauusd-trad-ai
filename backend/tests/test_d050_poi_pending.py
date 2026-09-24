"""D-050 — POI pending (limit) entries + signals-always-flow lifecycle.

User directives (Bengali, D-050):
- "পেন্ডিং অর্ডার creat করতে হবে, এন্ট্রি প্রাইস [market] বা তার আসে পাশে
  বসাতে হবে ... এতে করে স্টপ লস হিট কম হবে" — every signal becomes a
  PENDING LIMIT order placed at a structural level BEYOND the market.
- "মার্কেট এর সাপোর্ট জোন এর নিচ থেকে buy order বসাবেন, আর মার্কেট এর
  রেসিসটেন্স এর উপর থেকে sell order বসাবেন, এটাই হলো POI ZONE এর এন্ট্রি"
  — BUY limits below the demand (support) zone, SELL limits above the
  supply (resistance) zone.
- "অটো ট্রেড ওপেন থাকুক বা না থাকুক সিগন্যাল আসবে" — signals flow
  regardless of broker / auto-trade state; a failed boot connect must
  recover through the heartbeat, not stay dead.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from app.engine.config import (
    DEFAULT_CONFIG,
    EngineConfig,
    upgrade_legacy_d050,
    upgrade_legacy_d051,
)
from app.engine.engine import evaluate
from app.engine.executor import OrderExecutor, TradeRepo
from app.engine.tracker import SignalTracker, make_tracked
from app.engine.zones import poi_pending_entry
from tests.test_d048_poi_zones import (
    _bar,
    _uptrend_htf,
)

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------- poi_pending_entry

CFG = EngineConfig()


def test_pending_buy_anchors_below_demand_zone() -> None:
    """BUY limit anchored at the demand zone's NEAR edge (D-056).

    D-056 adverse-selection fix: the old code anchored at the zone's FAR
    edge (lo) — an order that only fills when the zone BREAKS. The near
    edge (hi) is the first-retest level where an intact zone rejects;
    the entry still clears the market by >= entry_min_usd and stays
    inside the user's USD window."""
    zones = [{"side": "demand", "source": "sd", "lo": 4508.0, "hi": 4510.0,
              "quality": 0.8, "t": None}]
    entry, note = poi_pending_entry("BUY", 4513.0, zones, atr=1.0, cfg=CFG)
    assert entry == 4510.0  # zone.hi — the NEAR edge (D-056)
    assert 1.0 <= 4513.0 - entry <= CFG.pending_max_usd  # the USD window
    assert "demand" in note
    assert "near edge" in note


def test_pending_sell_anchors_above_supply_zone() -> None:
    """SELL limit anchored at the supply zone's NEAR edge (D-056) —
    the first-retest level, inside the 4-6 USD window."""
    zones = [{"side": "supply", "source": "sd", "lo": 4516.0, "hi": 4518.5,
              "quality": 0.8, "t": None}]
    entry, note = poi_pending_entry("SELL", 4513.0, zones, atr=1.0, cfg=CFG)
    assert entry == 4516.0  # zone.lo — the NEAR edge (D-056)
    assert 1.0 <= entry - 4513.0 <= CFG.pending_max_usd
    assert "supply" in note
    assert "near edge" in note


def test_pending_nearest_zone_wins() -> None:
    zones = [
        {"side": "demand", "source": "sd", "lo": 4500.0, "hi": 4502.0,
         "quality": 0.9, "t": None},   # 11 USD away — deeper, better q
        {"side": "demand", "source": "ob", "lo": 4507.5, "hi": 4509.0,
         "quality": 0.6, "t": None},   # 5.5 USD away — nearest usable
    ]
    entry, _ = poi_pending_entry("BUY", 4513.0, zones, atr=2.0, cfg=CFG)
    assert entry == 4509.0  # nearest usable zone's NEAR edge (D-056),
    #                          not the best quality zone's


def test_pending_fallback_offset_without_zone() -> None:
    """D-051 — no usable zone: the user's preferred 4-6 USD offset."""
    entry, note = poi_pending_entry("BUY", 4513.0, [], atr=1.0, cfg=CFG)
    assert entry == pytest.approx(4513.0 - CFG.pending_target_usd)
    entry_s, _ = poi_pending_entry("SELL", 4513.0, [], atr=1.0, cfg=CFG)
    assert entry_s == pytest.approx(4513.0 + CFG.pending_target_usd)
    assert "no demand POI" in note


def test_pending_min_offset_floor_enforced() -> None:
    """A zone hugging the market still yields a real margin (USD floor)."""
    zones = [{"side": "demand", "source": "sd", "lo": 4512.8, "hi": 4513.0,
              "quality": 0.8, "t": None}]  # only 0.2 below the market
    entry, _ = poi_pending_entry("BUY", 4513.0, zones, atr=1.0, cfg=CFG)
    assert entry <= 4513.0 - CFG.entry_min_usd  # USD floor wins


def test_pending_deep_zone_clamps() -> None:
    zones = [{"side": "demand", "source": "sd", "lo": 4400.0, "hi": 4410.0,
              "quality": 0.9, "t": None}]  # 113 below — way beyond the cap
    entry, note = poi_pending_entry("BUY", 4513.0, zones, atr=1.0, cfg=CFG)
    # D-051 — the USD cap binds: never further than pending_max_usd
    assert entry == pytest.approx(4513.0 - CFG.pending_max_usd)
    assert 4513.0 - entry < 113.0  # clamped, not the full distance
    assert "clamped" in note


def test_pending_ignores_weak_and_opposing_zones() -> None:
    zones = [
        {"side": "supply", "source": "sd", "lo": 4490.0, "hi": 4495.0,
         "quality": 0.9, "t": None},  # WRONG side for a BUY
        {"side": "demand", "source": "sd", "lo": 4500.0, "hi": 4502.0,
         "quality": 0.10, "t": None},  # too weak to anchor to
    ]
    entry, _ = poi_pending_entry("BUY", 4513.0, zones, atr=1.0, cfg=CFG)
    # D-051 fallback = the USD target offset
    assert entry == pytest.approx(4513.0 - CFG.pending_target_usd)


# ------------------------------------------------------------- evaluate path

def _deep_demand_frame() -> pd.DataFrame:
    """D-050 fixture: the demand zone sits DEEP below the tape (quiet tape
    at 4300, zone at ~4296) so the pending limit anchored at its lower edge
    has structural room for SL/TP — the geometry gates pass from the deeper
    entry exactly like the user's market-4513/BUY-4503 example."""
    t0 = datetime(2026, 9, 22, 9, 0, tzinfo=UTC)
    rows = []
    # 1) quiet tape at 4300 (ATR ~0.35)
    for i in range(50):
        c = 4300.0 + 0.05 * (i % 3)
        rows.append(_bar(t0 + timedelta(minutes=i), c - 0.05, c + 0.15, c - 0.20, c))
    # 2) fall from the tape (creates clean space above the zone)
    for k in range(51, 54):
        v = 4300.0 - (k - 50) * 1.2
        rows.append(_bar(t0 + timedelta(minutes=k), v, v + 0.2, v - 1.0, v - 0.9))
    # 3) base bar ~4296 (the future demand zone)
    rows.append(_bar(t0 + timedelta(minutes=54), 4296.9, 4296.95, 4296.0, 4296.4))
    # 4) up impulse (body ~3.5 = 3x ATR)
    rows.append(_bar(t0 + timedelta(minutes=55), 4296.4, 4300.1, 4296.35, 4299.9, v=300))
    # 5) rally to ~4302.6
    p = 4299.9
    for k in range(56, 62):
        rows.append(_bar(t0 + timedelta(minutes=k), p, p + 0.6, p - 0.1, p + 0.45, v=120))
        p += 0.45
    # 6) overlapping descent back to ~4297
    for k in range(62, 72):
        rows.append(_bar(t0 + timedelta(minutes=k), p, p + 0.10, p - 1.02, p - 0.55, v=90))
        p -= 0.55
    # 7) trigger: dips INTO the zone and rejects with a lower wick
    rows.append(_bar(t0 + timedelta(minutes=72), 4297.1, 4297.3, 4296.2, 4297.15, v=250))
    return pd.DataFrame(rows, columns=["time_utc", "o", "h", "l", "c", "v"])


async def test_evaluate_emits_pending_limit_signal() -> None:
    """The full pipeline: a zone-retest signal becomes a PENDING limit with
    entry below the market (BUY), market_ref recorded, SL/TP re-derived
    from the deeper entry."""
    df = _deep_demand_frame()
    htf = _uptrend_htf()
    close_time = df["time_utc"].iloc[-1] + timedelta(minutes=5)
    cfg = EngineConfig()  # defaults: entry_mode poi_limit, M5
    ev = evaluate(df, htf, close_time, cfg, spread_points=20)
    assert ev.signal is not None, ev.trace["checks"]
    sig = ev.signal
    assert sig["entry_type"] == "limit"
    assert sig["market_ref"] == pytest.approx(float(df["c"].iloc[-1]), abs=0.5)
    assert sig["entry"] < sig["market_ref"]  # BUY limit BELOW the market
    assert sig["sl"] < sig["entry"] < sig["tp"]  # sane geometry
    names = {c["name"]: c for c in sig["trace"]["checks"]}
    assert names["entry_mode"]["pass"] is True
    assert "pending" in names["entry_mode"]["value"]


async def test_evaluate_market_mode_keeps_legacy_entry() -> None:
    df = _deep_demand_frame()
    htf = _uptrend_htf()
    close_time = df["time_utc"].iloc[-1] + timedelta(minutes=1)
    cfg = EngineConfig(entry_mode="market", regime_guard=False)  # D-068 isolated
    ev = evaluate(df, htf, close_time, cfg, spread_points=20)
    assert ev.signal is not None, ev.trace["checks"]
    assert ev.signal["entry_type"] == "market"
    assert ev.signal["entry"] == pytest.approx(
        float(df["c"].iloc[-1]), abs=0.5
    )


# ------------------------------------------------------------------- tracker

def _pending_sig(direction="BUY", entry=100.0, sl=98.0, tp=104.0):
    return make_tracked(
        direction=direction, entry=entry, sl=sl, tp=tp,
        confidence=0.7, trace={},
        bar_time=datetime(2025, 1, 6, 10, tzinfo=UTC),
        entry_type="limit", market_ref=101.0,
    )


class TestPendingTracker:
    async def test_pending_fills_when_ask_reaches_entry(self):
        tr = SignalTracker()
        sig = _pending_sig()  # BUY limit @100
        await tr.register(sig)
        await tr.on_tick(bid=100.2, ask=100.3)  # above the limit — no fill
        assert tr.active[0].status == "pending"
        await tr.on_tick(bid=99.8, ask=100.0)  # ask trades down to it
        assert tr.active[0].status == "active"
        assert tr.active[0].filled_at is not None

    async def test_pending_sell_fills_when_bid_rises(self):
        tr = SignalTracker()
        sig = _pending_sig(direction="SELL", entry=102.0, sl=104.0, tp=98.0)
        await tr.register(sig)
        await tr.on_tick(bid=101.5, ask=101.6)
        assert tr.active[0].status == "pending"
        await tr.on_tick(bid=102.2, ask=102.3)
        assert tr.active[0].status == "active"

    async def test_pending_never_resolved_by_tp_without_fill(self):
        """A pending BUY whose TP level trades has NO position — no
        phantom win (and no phantom loss) until the entry actually fills."""
        tr = SignalTracker()
        sig = _pending_sig()  # BUY limit @100, tp 104
        await tr.register(sig)
        await tr.on_tick(bid=104.5, ask=104.6)  # TP trades — but unfilled
        assert tr.active[0].status == "pending"  # no phantom win

    async def test_pending_expires_unfilled_with_no_r(self):
        tr = SignalTracker()
        sig = _pending_sig()
        await tr.register(sig)
        t0 = datetime(2025, 1, 6, 10, 5, tzinfo=UTC)
        for i in range(24):  # pending_expiry_bars default
            await tr.on_bar_close(36, t0 + timedelta(minutes=5 * i), 100.5,
                                  pending_expiry_bars=24)
        assert tr.active == []
        closed = sig
        assert closed.status == "expired"
        assert closed.result_r is None  # missed trade — never a loss

    async def test_bar_low_fills_pending_buy(self):
        """D-050 — the bar-close fill fallback (missed ticks)."""
        tr = SignalTracker()
        sig = _pending_sig()
        await tr.register(sig)
        await tr.on_bar_close(36, datetime(2025, 1, 6, 10, 5, tzinfo=UTC),
                              100.4, bar_low=99.9, bar_high=100.6)
        assert sig.status == "active"

    async def test_to_signal_dict_carries_pending_fields(self):
        sig = _pending_sig()
        d = sig.to_signal_dict("XAUUSD", "M5")
        assert d["entry_type"] == "limit"
        assert d["market_ref"] == 101.0
        assert d["status"] == "pending"


# ------------------------------------------------------------------ executor

class _ExecSource:
    """Minimal duck-typed source for OrderExecutor (market+limit capture)."""

    def __init__(self):
        self.orders: list = []
        self.account_info = lambda: {"equity": 10_000.0}

    async def get_positions(self):
        return []

    def symbol_info(self, symbol):
        return None  # §9 defaults (XAUUSD 100oz contract)

    async def place_order(self, order):
        self.orders.append(order)
        from app.mt5.base import OrderResult

        return OrderResult(ok=True, ticket=1, price=order.price, retcode=10009)


class TestPendingExecutor:
    async def test_limit_signal_places_limit_order(self):
        src = _ExecSource()
        repo = TradeRepo(None)
        ex = OrderExecutor(source=src, cfg=EngineConfig(), repo=repo)
        ex.arm(True)
        sig = {
            "id": "d050-1", "direction": "BUY", "entry": 4503.0,
            "sl": 4500.0, "tp": 4510.0, "entry_type": "limit",
            "market_ref": 4513.0, "spread_points": 20,
        }
        res = await ex.execute_signal(sig, "XAUUSD", 0.01)
        assert res is not None and res.ok
        assert src.orders[0].order_type == "buy_limit"
        assert src.orders[0].price == 4503.0
        assert src.orders[0].sl == 4500.0 and src.orders[0].tp == 4510.0

    async def test_sell_limit_signal_places_sell_limit(self):
        src = _ExecSource()
        ex = OrderExecutor(source=src, cfg=EngineConfig(), repo=TradeRepo(None))
        ex.arm(True)
        sig = {
            "id": "d050-2", "direction": "SELL", "entry": 4520.0,
            "sl": 4524.0, "tp": 4510.0, "entry_type": "limit",
            "market_ref": 4513.0, "spread_points": 20,
        }
        res = await ex.execute_signal(sig, "XAUUSD", 0.01)
        assert res is not None and res.ok
        assert src.orders[0].order_type == "sell_limit"
        assert src.orders[0].price == 4520.0

    async def test_market_signal_still_places_market_order(self):
        src = _ExecSource()
        ex = OrderExecutor(source=src, cfg=EngineConfig(), repo=TradeRepo(None))
        ex.arm(True)
        sig = {
            "id": "d050-3", "direction": "BUY", "entry": 4513.0,
            "sl": 4510.0, "tp": 4518.0, "spread_points": 20,
        }  # no entry_type -> legacy market behaviour
        await ex.execute_signal(sig, "XAUUSD", 0.01)
        assert src.orders[0].order_type == "market"
        assert src.orders[0].price is None


# ------------------------------------------------------- mock pending orders

class TestMockPendingOrders:
    async def test_mock_pending_fills_when_price_crosses(self):
        from app.mt5.base import Order, OrderResult  # noqa: F401
        from tests.conftest import make_mock

        src = make_mock(seed=11)
        await src.connect({"server": "s", "login": "1", "password": "x"})
        tick = await src.get_tick(src.SYMBOL)
        # a BUY limit far BELOW the current ask -> goes to the pending book
        deep = round(tick.ask - 5.0, 2)
        order = Order(symbol=src.SYMBOL, side="BUY", volume=0.1,
                      order_type="buy_limit", price=deep)
        res = await src.place_order(order)
        assert res.ok
        assert src._positions == []  # not filled yet — still pending
        # force the market down through the limit (mock price path)
        src.set_scenario_volume_boost = None  # noqa: B018 — no-op guard
        for _ in range(60):
            src.advance_minutes(1)
            if (await src.get_tick(src.SYMBOL)).ask <= deep:
                break
        await src.get_positions()  # promotion pass
        tickets = [p.ticket for p in src._positions]
        assert res.ticket in tickets  # filled at the limit price
        filled = next(p for p in src._positions if p.ticket == res.ticket)
        assert filled.price_open == deep

    async def test_mock_pending_fills_immediately_when_reachable(self):
        from app.mt5.base import Order
        from tests.conftest import make_mock

        src = make_mock(seed=11)
        await src.connect({"server": "s", "login": "1", "password": "x"})
        tick = await src.get_tick(src.SYMBOL)
        # a BUY limit ABOVE the current ask: reachable now -> instant fill
        order = Order(symbol=src.SYMBOL, side="BUY", volume=0.1,
                      order_type="buy_limit", price=round(tick.ask + 1.0, 2))
        res = await src.place_order(order)
        assert res.ok
        assert len(src._positions) == 1
        assert src._positions[0].price_open == round(tick.ask + 1.0, 2)


# ------------------------------------------------------------------- config

class TestD050Config:
    def test_m1_profile_defaults(self):
        """D-051 — the engine is BACK on M1 (the user's signal-flow TF),
        with the 4-6 USD pending window and multi-market defaults."""
        assert DEFAULT_CONFIG.timeframe == "M1"
        assert DEFAULT_CONFIG.confirm_tfs == ["M5", "M15"]
        assert DEFAULT_CONFIG.trend_tf == "H1"
        assert DEFAULT_CONFIG.entry_mode == "poi_limit"
        assert DEFAULT_CONFIG.entry_min_usd == 1.0
        assert DEFAULT_CONFIG.pending_target_usd == 4.5
        assert DEFAULT_CONFIG.pending_max_usd == 6.0
        assert DEFAULT_CONFIG.pending_expiry_bars == 60
        assert DEFAULT_CONFIG.signal_symbols == ["XAUUSD", "BTCUSD"]
        assert DEFAULT_CONFIG.auto_trade_symbols == ["XAUUSD"]
        assert DEFAULT_CONFIG.trusted_min_votes == 2.0

    def test_legacy_upgrade_moves_untouched_m1_rows(self):
        raw = {"timeframe": "M1", "confirm_tfs": ["M5", "M15"],
               "min_atr": 0.15, "expiry_bars": 45, "min_confluence": 3}
        out, moved = upgrade_legacy_d050(raw)
        assert moved
        assert out["timeframe"] == "M5"
        assert out["confirm_tfs"] == ["M15"]
        assert out["min_atr"] == 0.25
        assert out["expiry_bars"] == 36

    def test_legacy_upgrade_preserves_custom_rows(self):
        raw = {"timeframe": "M1", "confirm_tfs": ["M15"],  # user choice
               "min_atr": 0.15, "expiry_bars": 45}
        out, moved = upgrade_legacy_d050(raw)
        assert not moved
        assert out is raw

    def test_legacy_upgrade_skips_d050_rows(self):
        raw = {"timeframe": "M5", "confirm_tfs": ["M15"],
               "entry_mode": "market"}
        out, moved = upgrade_legacy_d050(raw)
        assert not moved

    def test_legacy_d051_moves_untouched_d050_rows_back_to_m1(self):
        """D-051 — a row still on the D-050 auto-set M5 profile returns to
        the M1 profile the user's signal flow came from."""
        raw = {"entry_mode": "poi_limit", "timeframe": "M5",
               "confirm_tfs": ["M15"], "min_atr": 0.25, "expiry_bars": 36,
               "pending_expiry_bars": 24, "pending_max_atr": 10.0}
        out, moved = upgrade_legacy_d051(raw)
        assert moved
        assert out["timeframe"] == "M1"
        assert out["confirm_tfs"] == ["M5", "M15"]
        assert out["min_atr"] == 0.15
        assert out["expiry_bars"] == 45
        assert out["pending_expiry_bars"] == 60

    def test_legacy_d051_preserves_custom_rows(self):
        raw = {"entry_mode": "poi_limit", "timeframe": "M15",  # user's own TF
               "confirm_tfs": ["M15"], "min_atr": 0.25, "expiry_bars": 36,
               "pending_expiry_bars": 24, "pending_max_atr": 10.0}
        out, moved = upgrade_legacy_d051(raw)
        assert not moved
        assert out is raw

    def test_legacy_d051_skips_d051_rows(self):
        raw = {"signal_symbols": ["XAUUSD"], "timeframe": "M1"}
        out, moved = upgrade_legacy_d051(raw)
        assert not moved
        assert out is raw

# ------------------------------------------------------ signals-always-flow

class _FlakySource:
    """Fails connect() the first N attempts, then succeeds — models a live
    feed that answers late (the D-050 boot-connect hole)."""

    def __init__(self, fail_times: int = 2):
        self._fail_left = fail_times
        self._connected = False
        self.discover_symbols = lambda p="*XAUUSD*": ["XAUUSD"]

    async def connect(self, creds):
        if self._fail_left > 0:
            self._fail_left -= 1
            raise ConnectionError("provider not answering yet")
        self._connected = True
        return {"login": "REALTIME", "server": "LiveMarket",
                "balance": 0.0, "equity": 0.0, "currency": "USD"}

    async def disconnect(self):
        self._connected = False

    async def is_connected(self):
        return self._connected


class TestSignalsAlwaysFlow:
    async def test_failed_boot_connect_recovers_via_heartbeat(self):
        """THE D-050 headline: a boot-time connect failure no longer kills
        the engine — creds stored pre-attempt + heartbeat retry loop means
        the runtime (and signals) start once the provider answers."""
        import asyncio

        import app.mt5.connection as connection_mod
        from app.mt5.connection import ConnectionManager

        src = _FlakySource(fail_times=2)
        mgr = ConnectionManager(source=src, settings=None, hub=None,
                                db_engine=None, repo=None)
        creds = {"server": "LiveMarket", "login": "REALTIME",
                 "password": "none"}
        # boot attempt — fails (provider not answering yet)
        with pytest.raises(ConnectionError):
            await mgr.connect(creds)
        assert mgr.state.status == "disconnected"
        assert mgr._creds == creds  # D-050: stored BEFORE the attempt
        # heartbeat starts despite the failed boot
        old = connection_mod.HEARTBEAT_S
        connection_mod.HEARTBEAT_S = 0.02
        try:
            mgr.ensure_heartbeat()
            for _ in range(200):
                await asyncio.sleep(0.02)
                if mgr.state.status == "connected":
                    break
            assert mgr.state.status == "connected"
            assert mgr.state.symbol == "XAUUSD"
        finally:
            connection_mod.HEARTBEAT_S = old
            mgr._stop_heartbeat()
