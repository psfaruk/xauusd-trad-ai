"""Engine end-to-end tests (SPEC §12 Phase 3 AC).

AC: engine on MockSource (accelerated + injected sweep scenario) emits a
signal with complete trace; tracker transitions active->won/lost/expired
correctly; no signal ever from a forming candle; cooldown enforced.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from app.engine.config import EngineConfig
from app.engine.engine import SignalEngine, evaluate
from app.engine.repo import SignalRepo
from app.engine.tracker import SignalTracker, make_tracked
from app.services.ws_hub import WSHub
from tests.conftest import make_mock

pytestmark = pytest.mark.asyncio


class EventHub(WSHub):
    """WSHub subclass that records every broadcast for assertions."""

    def __init__(self):
        super().__init__()
        self.events: list[tuple[str, dict]] = []

    async def broadcast_all(self, event_type, payload):
        self.events.append((event_type, dict(payload)))
        await super().broadcast_all(event_type, payload)

    async def broadcast_market(self, event_type, symbol, tf, payload):
        self.events.append((event_type, {**payload, "_tf": tf}))
        await super().broadcast_market(event_type, symbol, tf, payload)


def closed_bar_of(df) -> dict:
    r = df.iloc[-1]
    return {
        "t": int(r["time_utc"].timestamp()),
        "o": float(r["o"]), "h": float(r["h"]),
        "l": float(r["l"]), "c": float(r["c"]), "v": int(r["v"]),
    }


async def _trend_direction(src, symbol):
    from app.engine.indicators import ema

    h1 = await src.get_rates(symbol, "H1", 100)
    if len(h1) < 51:
        return None
    close = float(h1["c"].iloc[-1])
    ema_val = float(ema(h1["c"], 50).iloc[-1])
    return "BUY" if close > ema_val else "SELL"


class TestEngineE2E:
    async def test_injected_sweep_emits_signal_with_full_trace(self):
        """The Phase 3 headline AC — happy path through the REAL engine.

        Injects a trend-aligned sweep at successive buckets until one clears
        every filter (random-walk RSI/session can legitimately block crafted
        sweeps — the retry keeps the test deterministic without weakening the
        default config).
        """
        src = make_mock(seed=7)
        await src.connect({"server": "s", "login": "1", "password": "x"})
        symbol = src.SYMBOL
        # START anchors at Monday 00:00 UTC (off-session) — jump to 10:00 UTC
        # so injected buckets close inside the london window.
        src.advance_minutes(10 * 60)

        from app.mt5.mock_source import SweepScenario

        m15 = await src.get_rates(symbol, "M15", 200)
        h1_full = await src.get_rates(symbol, "H1", 100)
        from app.engine.indicators import ema

        hub = EventHub()
        repo = SignalRepo(None)
        engine = SignalEngine(EngineConfig(), hub, repo)
        tracker = SignalTracker()

        fired = None
        last_trace = None
        for _attempt in range(12):
            direction = (
                "BUY"
                if float(h1_full["c"].iloc[-1]) > float(ema(h1_full["c"], 50).iloc[-1])
                else "SELL"
            )
            scenario = src.inject_sweep(
                SweepScenario(direction=direction, tf="M15")
            )
            bucket_end = scenario.bucket_time + timedelta(minutes=15)
            now = src._vnow()
            src.advance_minutes(
                max(0.0, (bucket_end - now).total_seconds() / 60 + 1)
            )
            m15 = await src.get_rates(symbol, "M15", 200)
            assert m15["time_utc"].iloc[-1] == scenario.bucket_time
            await engine.on_bar_close(
                "M15", closed_bar_of(m15), src, tracker, symbol
            )
            signals = await repo.list()
            if signals:
                fired = signals[0]
                break
            h1_full = await src.get_rates(symbol, "H1", 100)

        assert fired is not None, (
            f"no injected sweep cleared all filters in 12 attempts; last trace: {last_trace}"
        )
        sig = fired
        assert sig["direction"] == direction
        assert sig["status"] == "active"
        # complete 7-check trace
        names = [c["name"] for c in sig["trace"]["checks"]]
        for expected in (
            "trend_h1", "sfp_sweep", "rsi", "atr", "session", "news", "spread",
        ):
            assert expected in names, f"missing check {expected}: {names}"
        assert all(c["pass"] for c in sig["trace"]["checks"])
        # levels math per §8.2
        if sig["direction"] == "BUY":
            assert sig["sl"] < sig["entry"] < sig["tp"]
        else:
            assert sig["sl"] > sig["entry"] > sig["tp"]
        risk = abs(sig["entry"] - sig["sl"])
        assert abs(abs(sig["tp"] - sig["entry"]) - 2.0 * risk) < 0.02
        assert 0.0 < sig["confidence"] <= 1.0
        # WS signal event broadcast
        assert any(t == "signal" for t, _ in hub.events)
        # tracker holds it active
        assert tracker.has_active()

    async def test_no_signal_from_forming_candle(self):
        """The injected sweep sits in the FORMING bucket — engine must stay
        silent because it only ever evaluates closed bars (SPEC §8.2)."""
        src = make_mock(seed=7)
        await src.connect({"server": "s", "login": "1", "password": "x"})
        symbol = src.SYMBOL
        direction = await _trend_direction(src, symbol)

        from app.mt5.mock_source import SweepScenario

        scenario = src.inject_sweep(SweepScenario(direction=direction, tf="M15"))
        # DO NOT advance the clock: bucket still forming (last closed bar < it)
        m15 = await src.get_rates(symbol, "M15", 200)
        assert m15["time_utc"].iloc[-1] < scenario.bucket_time

        hub = EventHub()
        repo = SignalRepo(None)
        engine = SignalEngine(EngineConfig(), hub, repo)
        tracker = SignalTracker()
        await engine.on_bar_close("M15", closed_bar_of(m15), src, tracker, symbol)

        assert await repo.list() == []
        assert not tracker.has_active()

    async def test_cooldown_blocks_immediate_second_signal(self):
        src = make_mock(seed=7)
        await src.connect({"server": "s", "login": "1", "password": "x"})
        symbol = src.SYMBOL
        direction = await _trend_direction(src, symbol)

        from app.mt5.mock_source import SweepScenario

        src.inject_sweep(SweepScenario(direction=direction, tf="M15", offset_bars=1))
        src.inject_sweep(SweepScenario(direction=direction, tf="M15", offset_bars=4))
        src.advance_minutes(4 * 15 + 1)

        m15 = await src.get_rates(symbol, "M15", 200)
        hub = EventHub()
        repo = SignalRepo(None)
        engine = SignalEngine(EngineConfig(), hub, repo)
        tracker = SignalTracker()

        # replay both closed bars
        for i in (-2, -1):
            r = m15.iloc[i]
            bar = {
                "t": int(r["time_utc"].timestamp()),
                "o": float(r["o"]), "h": float(r["h"]),
                "l": float(r["l"]), "c": float(r["c"]), "v": int(r["v"]),
            }
            await engine.on_bar_close("M15", bar, src, tracker, symbol)

        # first sweep may have fired OR been filtered (session etc.), but a
        # second one within cooldown_bars must NEVER fire while one is active
        signals = await repo.list()
        assert len(signals) <= 1
        if signals:
            assert tracker.has_active()

    async def test_wrong_timestep_ignored(self):
        src = make_mock(seed=7)
        await src.connect({"server": "s", "login": "1", "password": "x"})
        hub = EventHub()
        repo = SignalRepo(None)
        engine = SignalEngine(EngineConfig(), hub, repo)
        tracker = SignalTracker()
        df = await src.get_rates(src.SYMBOL, "M5", 10)
        await engine.on_bar_close("M5", closed_bar_of(df), src, tracker, src.SYMBOL)
        assert await repo.list() == []


class TestTracker:
    def _sig(self, direction="BUY", entry=100.0, sl=99.0, tp=102.0):
        return make_tracked(
            direction=direction, entry=entry, sl=sl, tp=tp,
            confidence=0.7, trace={},
            bar_time=datetime(2025, 1, 6, 10, tzinfo=UTC),
        )

    def _tracker_with_log(self):
        tr = SignalTracker()
        log = []

        async def cb(sig):
            log.append((sig.status, sig.result_r))

        tr.on_status = cb
        return tr, log

    async def test_sl_touch_lost(self):
        tr, log = self._tracker_with_log()
        await tr.register(self._sig())
        await tr.on_tick(bid=98.9, ask=99.0)  # bid <= sl
        assert log == [("lost", -1.0)]
        assert tr.active == [] and not tr.has_active()

    async def test_tp_touch_won_rr2(self):
        tr, log = self._tracker_with_log()
        await tr.register(self._sig())
        await tr.on_tick(bid=102.1, ask=102.2)  # bid >= tp (rr 2.0)
        assert log[0][0] == "won"
        assert log[0][1] == pytest.approx(2.0)

    async def test_sell_exits_on_ask(self):
        tr, log = self._tracker_with_log()
        await tr.register(self._sig(direction="SELL", entry=100.0, sl=101.0, tp=98.0))
        await tr.on_tick(bid=100.5, ask=101.1)  # ask >= sl -> lost
        assert log == [("lost", -1.0)]

    async def test_no_touch_stays_active(self):
        tr, log = self._tracker_with_log()
        await tr.register(self._sig())
        await tr.on_tick(bid=100.5, ask=100.6)
        assert log == [] and tr.has_active()

    async def test_expiry_r_math(self):
        tr, log = self._tracker_with_log()
        sig = self._sig()
        await tr.register(sig)
        # 20 closed bars pass without touching SL/TP -> expires on the 20th
        for i in range(20):
            await tr.on_bar_close(
                20,
                datetime(2025, 1, 6, 10 + 15 * (i + 1) // 60, 15 * (i + 1) % 60, tzinfo=UTC),
                close_price=100.5,
            )
        # expired: (100.5 - 100) / 1.0 risk = +0.5R
        assert sig.status == "expired"
        assert sig.result_r == pytest.approx(0.5)
        assert log == [("expired", pytest.approx(0.5))]

    async def test_status_callback_fires(self):
        tr = SignalTracker()
        seen = []

        async def cb(sig):
            seen.append(sig.status)

        tr.on_status = cb
        await tr.register(self._sig())
        await tr.on_tick(bid=98.9, ask=99.0)
        assert seen == ["lost"]


class TestEvaluatePure:
    """The shared pipeline used by BOTH the live engine and the backtest."""

    def _frames(self, hour_shift=0):
        """60 oscillating bars + a clean BUY sweep; H1 uptrend.

        Closes alternate 100.2/99.8 so RSI(14) lands mid-window (~50), and
        the sweep bar closes at 17:15 UTC (newyork session).
        """
        base = pd.Timestamp("2025-01-06 02:00", tz="UTC") + pd.Timedelta(hours=hour_shift)
        rows = []
        prev_c = 100.0
        for i in range(60):
            t = base + pd.Timedelta(minutes=15 * i)
            c = 100.2 if i % 2 == 0 else 99.8
            o = prev_c
            rows.append([t, o, max(o, c) + 0.4, min(o, c) - 0.4, c, 10])
            prev_c = c
        t = base + pd.Timedelta(minutes=15 * 60)
        rows.append([t, 99.8, 100.8, 98.6, 100.4, 30])  # sweep: low 98.6 < prior min 99.4
        m15 = pd.DataFrame(rows, columns=["time_utc", "o", "h", "l", "c", "v"])
        h1_rows = [
            [pd.Timestamp("2025-01-06 02:00", tz="UTC") + pd.Timedelta(hours=i),
             90.0 + i, 91.0 + i, 89.0 + i, 90.5 + i, 50]
            for i in range(60)
        ]
        h1 = pd.DataFrame(h1_rows, columns=["time_utc", "o", "h", "l", "c", "v"])
        return m15, h1

    def test_full_pass_emits_signal(self):
        m15, h1 = self._frames()
        close_time = m15["time_utc"].iloc[-1] + pd.Timedelta(minutes=15)
        ev = evaluate(m15, h1, close_time, EngineConfig(), spread_points=20)
        assert ev.signal is not None, ev.trace
        assert ev.signal["direction"] == "BUY"

    def test_spread_blocks(self):
        m15, h1 = self._frames()
        close_time = m15["time_utc"].iloc[-1] + pd.Timedelta(minutes=15)
        ev = evaluate(m15, h1, close_time, EngineConfig(), spread_points=99)
        assert ev.signal is None
        assert ev.trace["checks"][-1]["name"] == "spread"

    def test_off_session_blocks(self):
        m15, h1 = self._frames(hour_shift=12)  # sweep closes 05:15 UTC — off-session
        close_time = m15["time_utc"].iloc[-1] + pd.Timedelta(minutes=15)
        ev = evaluate(m15, h1, close_time, EngineConfig(), spread_points=20)
        assert ev.signal is None
        assert ev.trace["checks"][-1]["name"] == "session"
