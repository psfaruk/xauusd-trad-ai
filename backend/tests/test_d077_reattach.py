"""D-077 tests — the lazy MT5 bridge re-attach (the permanent-offline fix).

User report (Bengali): "Live data আসছে না, অফলাইন দেখাচ্ছে ডেটা কানেকশন" —
live data never arrived and the header showed offline. Root cause: the
terminal overlay (McpMarketFeed) was attached ONLY at MarketFeed
construction; a bridge that was unreachable at that instant (deploy
restart while the terminal/tunnel was down) left mcp=None FOREVER — the
D-050 heartbeat retried connect() every 5s, but an MT5_ONLY feed without
the overlay can never produce a tick, so the platform stayed offline
permanently even after the terminal came back.

D-077: the overlay attaches lazily — a bounded first attempt in start()
plus a background watch loop that re-probes until the bridge answers.
These tests prove: no boot probe (construction never blocks), honest
not-attached status, the late attach wiring every symbol core, and the
idempotence/stop semantics.
"""

from __future__ import annotations

import asyncio
import time as time_mod

from app.mt5.live_source import LiveDataSource, MarketFeed

# ------------------------------------------------------------------ helpers


class _FakeTermClient:
    """Duck-typed MT5TerminalClient for the market-data surface."""

    def __init__(self, alive: bool = True) -> None:
        self.alive = alive
        self.tick_rows: list[dict] = []
        self.marketwatch = [
            {"symbol": "XAUUSDm"}, {"symbol": "BTCUSDm"},
            {"symbol": "USOIL"}, {"symbol": "USTEC"},
        ]

    def symbols(self):
        return self.marketwatch

    def ticks(self, symbol, dt_from, dt_to):
        return [r for r in self.tick_rows if r["_sym"] == symbol]

    def bars(self, symbol, period, dt_from, dt_to, limit=1000):
        return []


# ---------------------------------------------------------------- no boot probe


async def test_construction_never_probes_the_bridge(monkeypatch):
    """D-077 — __init__ must NOT touch the network (the old synchronous
    probe blocked the event loop and pinned the overlay state at boot)."""
    calls = {"probe": 0}

    def fake_available() -> bool:
        calls["probe"] += 1
        raise AssertionError("boot probe must never happen")

    monkeypatch.setattr(
        "app.mt5.mcp_market.mcp_market_available", fake_available
    )
    feed = MarketFeed(enable_ws=False)
    assert feed.mcp is None
    assert calls["probe"] == 0
    # and the honest not-attached note is visible through feed_status
    src = LiveDataSource(enable_ws=False, market=feed)
    st = src.feed_status()
    assert st["mt5"] == {"bridge": "not_attached"}
    assert "not attached" in st["note"]
    await src.disconnect()


async def test_start_with_dead_bridge_stays_degraded_and_spawns_watch(
    monkeypatch,
):
    """A dead bridge at start(): bounded attempt, watch loop spawned, feeds
    still degraded (offline-honest, never demo) — the pre-fix behavior was
    to silently pin mcp=None forever with NO retry path at all."""
    monkeypatch.setattr(
        "app.mt5.mcp_market.mcp_market_available", lambda: False
    )
    feed = MarketFeed(enable_ws=False)
    monkeypatch.setattr(MarketFeed, "MCP_REATTACH_S", 0.05)
    await feed.start()
    try:
        assert feed.mcp is None
        assert feed._mcp_watch_task is not None
        assert not feed._mcp_watch_task.done()
        assert await feed.poll_once() is False  # MT5_ONLY + no overlay
    finally:
        await feed.stop()
    assert feed._mcp_watch_task is None  # stop() cancels the watcher


# ------------------------------------------------------------------ late attach


async def test_late_bridge_attach_wires_every_symbol_core(monkeypatch):
    """THE fix: the bridge coming back AFTER boot attaches the overlay and
    re-wires every symbol core — no restart, feeds go broker-authoritative."""
    client = _FakeTermClient()
    monkeypatch.setattr("app.mt5.mcp.terminal_client", lambda: client)

    state = {"alive": False}

    def fake_available() -> bool:
        return state["alive"]

    monkeypatch.setattr(
        "app.mt5.mcp_market.mcp_market_available", fake_available
    )
    feed = MarketFeed(enable_ws=False)
    monkeypatch.setattr(MarketFeed, "MCP_REATTACH_S", 0.05)
    await feed.start()
    assert feed.mcp is None

    # the terminal bridge answers NOW (tunnel up / terminal started)
    state["alive"] = True
    attached = await feed._try_attach_mcp()
    assert attached is True
    assert feed.mcp is not None
    # every symbol core (incl. the D-076 terminal-only markets) is rewired
    for key, core in feed.feeds.items():
        assert core._mt5 is feed.mcp, key
        assert core._mt5_fresh() is False  # no ticks yet — honest
    # a routed broker tick becomes the authoritative quote
    now = time_mod.time()
    feed._route_mt5_tick("USOIL", 52.30, 52.36, now)
    core = feed.feed_for("USOIL")
    assert core.tick is not None
    assert core.provider == "mt5"
    # gold carries the poll verdict (D-035 semantics): route gold too
    feed._route_mt5_tick("XAUUSD", 4300.10, 4300.46, now)
    assert await feed.poll_once() is True
    # the watcher exited (attached — overlay self-heals from here)
    await asyncio.sleep(0.12)
    assert feed._mcp_watch_task is None or feed._mcp_watch_task.done()
    await feed.stop()


async def test_attach_is_idempotent_once_attached(monkeypatch):
    """Concurrent watch/loop probes after a successful attach are no-ops."""
    client = _FakeTermClient()
    monkeypatch.setattr("app.mt5.mcp.terminal_client", lambda: client)
    monkeypatch.setattr(
        "app.mt5.mcp_market.mcp_market_available", lambda: True
    )
    feed = MarketFeed(enable_ws=False)
    assert await feed._try_attach_mcp() is True
    first = feed.mcp
    assert await feed._try_attach_mcp() is True  # no-op path
    assert feed.mcp is first
    await feed.stop()


async def test_reattach_watch_loop_attaches_autonomously(monkeypatch):
    """The watch loop alone recovers a bridge that appears AFTER start()
    (no external caller) — this is the exact 'offline forever' production
    scenario: deploy restart while the terminal tunnel was down."""
    client = _FakeTermClient()
    monkeypatch.setattr("app.mt5.mcp.terminal_client", lambda: client)
    state = {"alive": False}
    monkeypatch.setattr(
        "app.mt5.mcp_market.mcp_market_available", lambda: state["alive"]
    )
    feed = MarketFeed(enable_ws=False)
    monkeypatch.setattr(MarketFeed, "MCP_REATTACH_S", 0.05)
    await feed.start()
    try:
        assert feed.mcp is None
        state["alive"] = True  # the terminal comes back seconds later
        for _ in range(100):  # ≤ ~5s of watch beats
            if feed.mcp is not None:
                break
            await asyncio.sleep(0.05)
        assert feed.mcp is not None, "watch loop failed to re-attach"
        assert feed.feeds["XAUUSD"]._mt5 is feed.mcp
    finally:
        await feed.stop()
