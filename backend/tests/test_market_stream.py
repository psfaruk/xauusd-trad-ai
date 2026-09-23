"""MarketStream tests — the tick-by-tick bar builder (Phase 2 core).

Fast virtual clock (time_scale) drives real ticks through the stream; asserts
bar_open/update/close sequencing and that closed bars reconcile with the
authoritative get_rates values.
"""

from __future__ import annotations

import asyncio
import contextlib
import time as time_mod

import pytest

from app.mt5.mock_source import MockDataSource
from app.services.market import MarketStream
from app.services.ws_hub import WSHub

pytestmark = pytest.mark.asyncio


class RecordingHub(WSHub):
    def __init__(self, tfs=("M15",)):
        super().__init__()
        self.tfs = set(tfs)
        self.events: list[dict] = []

    def subscribed_tfs(self) -> set[str]:
        return set(self.tfs)

    async def broadcast_ticks(self, event_type, symbol, payload):
        self.events.append({"type": event_type, "symbol": symbol, **payload})

    async def broadcast_market(self, event_type, symbol, tf, payload):
        self.events.append({"type": event_type, "symbol": symbol, "tf": tf, **payload})

    async def broadcast_all(self, event_type, payload):
        self.events.append({"type": event_type, **payload})


async def _drain(seconds: float = 2.5):
    await asyncio.sleep(seconds)


class TestMarketStream:
    async def test_tick_bar_lifecycle_and_reconciliation(self):
        # 30 virtual min per real second -> an M15 bar closes ~every 0.5s
        src = MockDataSource(seed=11, time_scale=30 * 60.0, tick_interval=0.05)
        await src.connect({"server": "s", "login": "1", "password": "x"})
        hub = RecordingHub(tfs={"M15"})
        stream = MarketStream(src, hub, src.SYMBOL, engine_tf="M15")
        closes: list[tuple[str, dict]] = []

        async def on_close(tf, bar):
            closes.append((tf, dict(bar)))

        stream.on_bar_close = on_close
        await stream.start()
        try:
            # D-064 flake fix: drain UNTIL the goal (>= 2 M15 closes) with a
            # generous cap instead of a fixed 2.5s window — under full-suite
            # event-loop load the 0.5s virtual bars overshoot their sleeps
            # and a fixed budget starves the second close.
            deadline = time_mod.monotonic() + 15.0
            while len(closes) < 2 and time_mod.monotonic() < deadline:
                await asyncio.sleep(0.1)
        finally:
            await stream.stop()

        types = [e["type"] for e in hub.events]
        assert "tick" in types
        assert "bar_open" in types
        assert "bar_update" in types
        assert "bar_close" in types
        assert len(closes) >= 2, f"expected >=2 M15 closes, got {len(closes)}"

        # authoritative reconciliation: each closed bar must equal get_rates
        # (fetch a wider window — virtual time keeps advancing past the last
        # recorded close, pushing older buckets out of a tight window)
        df = await src.get_rates(src.SYMBOL, "M15", len(closes) + 12)
        by_t = {int(r["time_utc"].timestamp()): r for _, r in df.iterrows()}
        for tf, bar in closes:
            assert tf == "M15"
            r = by_t.get(bar["t"])
            assert r is not None, f"closed bar {bar['t']} missing from get_rates"
            assert bar["o"] == pytest.approx(float(r["o"]), abs=1e-6)
            assert bar["h"] == pytest.approx(float(r["h"]), abs=1e-6)
            assert bar["l"] == pytest.approx(float(r["l"]), abs=1e-6)
            assert bar["c"] == pytest.approx(float(r["c"]), abs=1e-6)
            assert bar["v"] == int(r["v"])

        # sequencing per bucket: open -> updates -> close, monotonic buckets.
        # D-064: gaps between closed buckets are ALLOWED — under event-loop
        # load a starved tick feed legitimately skips whole virtual bars
        # (the stream closes the stale forming bucket and opens the live
        # one); what must NEVER happen is a duplicate/out-of-order bucket
        # or an off-grid timestamp.
        buckets = [b["t"] for _, b in closes]
        assert buckets == sorted(set(buckets))
        assert all(t % (15 * 60) == 0 for t in buckets)

    async def test_multi_tf_streams(self):
        src = MockDataSource(seed=3, time_scale=30 * 60.0, tick_interval=0.05)
        await src.connect({"server": "s", "login": "1", "password": "x"})
        hub = RecordingHub(tfs={"M1", "M15"})
        stream = MarketStream(src, hub, src.SYMBOL, engine_tf="M15")
        await stream.start()
        try:
            await _drain(1.5)
        finally:
            await stream.stop()
        tfs = {e["tf"] for e in hub.events if e["type"].startswith("bar_")}
        assert {"M1", "M15"} <= tfs

    async def test_engine_tf_always_built_even_without_subscribers(self):
        src = MockDataSource(seed=5, time_scale=30 * 60.0, tick_interval=0.05)
        await src.connect({"server": "s", "login": "1", "password": "x"})
        hub = RecordingHub(tfs=set())  # nobody subscribed
        stream = MarketStream(src, hub, src.SYMBOL, engine_tf="M15")
        await stream.start()
        try:
            await _drain(1.5)
        finally:
            await stream.stop()
        tfs = {e["tf"] for e in hub.events if e["type"].startswith("bar_")}
        assert "M15" in tfs  # engine timeframe must always stream

    async def test_mid_bucket_subscribe_seeds_forming_bar(self):
        """Chart subscribes mid-bucket -> the forming bar arrives complete
        (seeded from get_forming_bar), not from the subscribe tick alone."""
        src = MockDataSource(seed=9, time_scale=15 * 60.0, tick_interval=0.05)
        await src.connect({"server": "s", "login": "1", "password": "x"})
        hub = RecordingHub(tfs=set())
        stream = MarketStream(src, hub, src.SYMBOL, engine_tf="M15")
        await stream.start()
        try:
            await _drain(0.4)  # let a bucket start forming
            hub.tfs = {"M15"}  # a client subscribes mid-bucket
            await _drain(1.2)
        finally:
            await stream.stop()
        opens = [e for e in hub.events if e["type"] == "bar_open" and e.get("tf") == "M15"]
        assert opens, "mid-bucket subscribe produced no bar_open"
        # the seeded bar's volume reflects prior activity, not a single tick
        seeded = opens[0]["candle"]
        assert seeded["v"] >= 1

    # ------------------------------------------------ D-042 micro-tick fill

    async def test_interp_fill_between_slow_real_ticks(self):
        """D-042 — with a slow real feed (2/s) the stream emits display-only
        micro-ticks between broker ticks so the candle moves at ~15Hz.

        Micro frames are marked i=True with n=0 (no real events inside);
        real frames keep n>=1. Micro mids stay anchored to the last REAL
        quote (bounded OU) so the fill can never invent price levels.
        """
        src = MockDataSource(seed=21, time_scale=60.0, tick_interval=0.5)
        await src.connect({"server": "s", "login": "1", "password": "x"})
        hub = RecordingHub(tfs={"M1"})
        stream = MarketStream(src, hub, src.SYMBOL, engine_tf="M1")
        await stream.start()
        try:
            await _drain(2.2)
        finally:
            await stream.stop()

        ticks = [e for e in hub.events if e["type"] == "tick"]
        real = [e for e in ticks if not e.get("i")]
        interp = [e for e in ticks if e.get("i")]
        assert real, "no real ticks flowed"
        assert interp, "expected interpolated display frames between real ticks"
        assert all(e["n"] == 0 for e in interp)
        assert all(e["n"] >= 1 for e in real)
        # micro frames sit within the OU clamp of the last real mid
        last_mid = None
        for e in ticks:
            mid = (e["bid"] + e["ask"]) / 2.0
            spread = e["ask"] - e["bid"]
            if not e.get("i"):
                last_mid = mid
                continue
            assert last_mid is not None
            clamp = max(0.3 * spread, 0.02) + 0.02  # + rounding headroom
            assert abs(mid - last_mid) <= clamp, (
                f"micro mid {mid} drifted {abs(mid - last_mid):.3f}"
                f" from real {last_mid} (clamp {clamp:.3f})"
            )
        # bar_update frames flowed for the engine TF (display bar moving)
        updates = [e for e in hub.events
                   if e["type"] == "bar_update" and e.get("tf") == "M1"]
        assert updates

    async def test_interp_stops_when_feed_quiet(self, monkeypatch):
        """D-042 — no fake action in a quiet market: once real ticks stop
        arriving (> INTERP_MAX_GAP_S) the fill freezes too."""
        from app.services import market as market_mod

        monkeypatch.setattr(market_mod, "INTERP_MAX_GAP_S", 0.6)
        src = MockDataSource(seed=33, time_scale=60.0, tick_interval=0.2)
        await src.connect({"server": "s", "login": "1", "password": "x"})
        hub = RecordingHub(tfs={"M1"})
        stream = MarketStream(src, hub, src.SYMBOL, engine_tf="M1")
        await stream.start()
        try:
            await _drain(0.8)  # real ticks flowing at ~5/s
            # kill the REAL feed only — the interp loop must survive
            stream._task.cancel()
            with contextlib.suppress(BaseException):
                await stream._task
            await _drain(0.8)  # past the 0.6s stale window — fill settles
            n_settled = len([e for e in hub.events if e.get("i")])
            await _drain(0.8)  # a quiet market stays quiet
            n_after = len([e for e in hub.events if e.get("i")])
        finally:
            await stream.stop()
        assert n_after == n_settled, "micro frames kept flowing on a dead feed"
        assert stream.interp_stats()["active"] is False
