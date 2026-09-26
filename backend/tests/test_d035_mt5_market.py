"""D-035 tests — MT5-first market data, BTCUSD pair, weekend fallback.

Covers:
- terminal timestamp parsing + McpMarketFeed tick polling (dedupe, freshness)
- chart-history bars split into closed/forming + cache
- SymbolFeedCore authority policy: MT5 fresh -> crypto events never move the
  quote; MT5 stale (weekend) -> crypto composite takes over automatically
- LiveDataSource multi-symbol routing (XAUUSD + BTCUSD) + contract sizes
"""

from __future__ import annotations

import datetime as dt_mod
import time as time_mod
from datetime import UTC

import pytest

from app.mt5.live_source import BTC_SYMBOL_INFO, GOLD_SYMBOL_INFO, LiveDataSource, MarketFeed
from app.mt5.mcp_market import McpMarketFeed, parse_terminal_time

# ------------------------------------------------------------------ helpers

class FakeTermClient:
    """Duck-typed MT5TerminalClient for the market-data surface."""

    def __init__(self) -> None:
        self.tick_rows: list[dict] = []
        self.bar_rows: list[dict] = []
        self.marketwatch = [{"symbol": "XAUUSDm"}, {"symbol": "BTCUSDm"}]

    def symbols(self):
        return self.marketwatch

    def ticks(self, symbol, dt_from, dt_to):
        return [r for r in self.tick_rows if r["_sym"] == symbol]

    def bars(self, symbol, period, dt_from, dt_to, limit=1000):
        return [r for r in self.bar_rows if r["_sym"] == symbol]


def _t(ts: float) -> str:
    return dt_mod.datetime.fromtimestamp(ts, tz=UTC).strftime("%Y.%m.%d %H:%M:%S.%f")[:-3]


class FakeMcp:
    """Duck-typed McpMarketFeed for SymbolFeedCore tests."""

    def __init__(self, fresh_keys=("XAUUSD",)):
        self.fresh_keys = set(fresh_keys)
        self.rows: dict[str, list[dict]] = {}
        self.forming_rows: dict[tuple[str, str], dict | None] = {}
        self.tps_map: dict[str, float] = {}
        self.status_val: dict = {}

    def fresh(self, key: str) -> bool:
        return key in self.fresh_keys

    async def bars(self, key: str, tf: str, count: int):
        return self.rows.get(key, []), self.forming_rows.get((key, tf))

    def forming(self, key: str, tf: str):
        return self.forming_rows.get((key, tf))

    def tps(self, key: str):
        return self.tps_map.get(key)

    def status(self):
        return self.status_val


# ------------------------------------------------------------- mcp_market

def test_parse_terminal_time_utc():
    ts = parse_terminal_time("2026.09.20 17:11:30.713")
    assert ts == pytest.approx(
        dt_mod.datetime(2026, 9, 20, 17, 11, 30, 713000, tzinfo=UTC).timestamp()
    )


def test_parse_terminal_time_with_shift():
    ts = parse_terminal_time("2026.09.20 17:11:30.713", shift_s=3600)
    assert ts == pytest.approx(
        dt_mod.datetime(2026, 9, 20, 18, 11, 30, 713000, tzinfo=UTC).timestamp()
    )


async def test_mcp_market_polls_dedupes_and_flags_fresh():
    c = FakeTermClient()
    now = time_mod.time()
    c.tick_rows = [
        {"_sym": "BTCUSDm", "time_ms": _t(now - 2), "bid": 81000.0, "ask": 81010.0},
        {"_sym": "BTCUSDm", "time_ms": _t(now - 1), "bid": 81001.0, "ask": 81011.0},
    ]
    seen: list[tuple[str, float, float, float]] = []

    feed = McpMarketFeed(client=c, watch=["BTCUSD"], on_tick=lambda *a: seen.append(a))
    await feed._discover_symbols()
    assert feed.symbol_map["BTCUSD"] == "BTCUSDm"

    got = await feed._poll_ticks("BTCUSD")
    assert got == 2
    assert feed.fresh("BTCUSD") is True
    assert len(seen) == 2
    assert seen[-1][1] == 81001.0  # bid

    # second poll overlaps the window -> the old tick is deduped away
    c.tick_rows.append(
        {"_sym": "BTCUSDm", "time_ms": _t(now), "bid": 81002.0, "ask": 81012.0}
    )
    got = await feed._poll_ticks("BTCUSD")
    assert got == 1
    assert seen[-1][1] == 81002.0


def test_parse_terminal_time_second_precision():
    """Chart-history bars have NO milliseconds — must parse too (D-035 bug)."""
    ts = parse_terminal_time("2026.09.20 17:32:00")
    assert ts == pytest.approx(
        dt_mod.datetime(2026, 9, 20, 17, 32, 0, tzinfo=UTC).timestamp()
    )


async def test_mcp_market_bars_split_closed_and_forming():
    c = FakeTermClient()
    now = time_mod.time()
    m1 = 60
    b_now = int(now // m1) * m1
    c.bar_rows = [
        {"_sym": "BTCUSDm", "time": _t(b_now - 3 * m1), "open": 1.0, "high": 2.0,
         "low": 0.5, "close": 1.5, "tick_volume": 10},
        {"_sym": "BTCUSDm", "time": _t(b_now - 2 * m1), "open": 1.5, "high": 2.5,
         "low": 1.0, "close": 2.0, "tick_volume": 12},
        {"_sym": "BTCUSDm", "time": _t(b_now), "open": 2.0, "high": 2.2,
         "low": 1.9, "close": 2.1, "tick_volume": 3},  # forming
    ]
    feed = McpMarketFeed(client=c, watch=["BTCUSD"])
    closed, forming = await feed.bars("BTCUSD", "M1", 10)
    assert [r["t"] for r in closed] == [b_now - 3 * m1, b_now - 2 * m1]
    assert forming is not None and forming["t"] == b_now
    # cache hit without refetch
    closed2, forming2 = await feed.bars("BTCUSD", "M1", 10)
    assert forming2 == forming and len(closed2) == len(closed)


# ---------------------------------------------------- SymbolFeedCore policy

def _core(mcp: FakeMcp | None, **kw):
    from app.mt5.live_source import GOLD_SPEC, SymbolFeedCore

    return SymbolFeedCore(spec=GOLD_SPEC, enable_ws=False, mt5=mcp, **kw)


async def test_d037_mt5_only_crypto_events_never_move_quote():
    """D-037 (default): with MT5_ONLY the broker feed is the single authority
    — crypto composite events are ignored even while the broker is idle."""
    mcp = FakeMcp(fresh_keys=("XAUUSD",))
    core = _core(mcp)  # mt5_only defaults to True (D-037)
    mcp.fresh_keys.clear()  # weekend / broker idle
    core._on_ws_event(_Ev(bid=4300.0, ask=4301.0))
    assert core.tick is None  # composite ignored entirely

    # a REAL broker tick takes over once the market reopens
    core.on_mt5_tick(4378.10, 4378.40, time_mod.time())
    assert core.provider == "mt5"
    assert core.tick.bid == 4378.10
    # and crypto events still cannot move it
    core._on_ws_event(_Ev(bid=1.0, ask=2.0))
    assert core.tick.bid == 4378.10


async def test_mt5_fresh_crypto_events_never_move_quote():
    """D-035 composite semantics — exercised explicitly with mt5_only=False."""
    mcp = FakeMcp(fresh_keys=("XAUUSD",))
    core = _core(mcp, mt5_only=False)
    mcp.fresh_keys.clear()
    core._on_ws_event(_Ev(bid=4300.0, ask=4301.0))
    assert core.tick is not None and core.provider == "aggregate"

    # MT5 becomes fresh -> the next crypto event is ignored
    mcp.fresh_keys.add("XAUUSD")
    core._on_ws_event(_Ev(bid=9999.0, ask=9999.5))
    assert core.tick.bid == 4300.0  # unchanged

    # a REAL broker tick takes over
    core.on_mt5_tick(4378.10, 4378.40, time_mod.time())
    assert core.provider == "mt5"
    assert core.tick.bid == 4378.10
    # and crypto events still cannot move it
    core._on_ws_event(_Ev(bid=1.0, ask=2.0))
    assert core.tick.bid == 4378.10


async def test_weekend_fallback_crypto_composite_resumes():
    """D-035 composite mode (mt5_only=False): MT5 goes stale (weekend) ->
    the crypto composite becomes the quote."""
    mcp = FakeMcp(fresh_keys=("XAUUSD",))
    core = _core(mcp, mt5_only=False)
    core.on_mt5_tick(4378.10, 4378.40, time_mod.time())
    assert core.provider == "mt5"

    mcp.fresh_keys.clear()  # weekend / terminal down
    core._on_ws_event(_Ev(bid=4368.0, ask=4369.0))
    assert core.provider == "aggregate"
    assert core.tick.bid == 4368.0  # composite took over — chart keeps moving


async def test_d037_weekend_stays_broker_only():
    """D-037 default: MT5 goes stale (weekend) -> NO composite takeover; the
    last real broker price stays and the state is reported honestly."""
    mcp = FakeMcp(fresh_keys=("XAUUSD",))
    core = _core(mcp)  # MT5_ONLY default
    core.on_mt5_tick(4378.10, 4378.40, time_mod.time())
    mcp.fresh_keys.clear()  # weekend
    core._on_ws_event(_Ev(bid=4368.0, ask=4369.0))
    assert core.tick is not None and core.tick.bid == 4378.10  # broker price
    assert core.provider == "mt5"


async def test_ensure_tf_prefers_mt5_bars_when_fresh():
    mcp = FakeMcp(fresh_keys=("XAUUSD",))
    now = time_mod.time()
    m15 = 900
    b = int(now // m15) * m15
    mcp.rows["XAUUSD"] = [
        {"t": b - 2 * m15, "o": 4370.0, "h": 4380.0, "l": 4360.0, "c": 4375.0, "v": 50},
        {"t": b - m15, "o": 4375.0, "h": 4385.0, "l": 4365.0, "c": 4378.0, "v": 60},
    ]
    core = _core(mcp)
    closed, _ = await core.ensure_tf("M15", 2)
    assert closed[-1]["t"] == b - m15
    assert closed[-1]["o"] == 4375.0  # MT5 bars, not binance (no http factory)


class _Ev:
    def __init__(self, bid, ask, qty=0.0):
        self.bid = bid
        self.ask = ask
        self.qty = qty
        self.ts = time_mod.time()
        self.venue = "binance"


# ------------------------------------------------- LiveDataSource multi-symbol

async def test_platform_symbols_and_contract_sizes():
    src = LiveDataSource(enable_ws=False)
    assert src.platform_symbols == ["XAUUSD", "BTCUSD", "USOIL", "USTEC"]
    assert src.symbol_info("XAUUSD").contract_size == 100.0
    assert src.symbol_info("BTCUSDm").contract_size == 1.0  # broker suffix routed
    assert src.symbol_info("USOIL").contract_size == 1000.0  # D-076 oil
    assert src.symbol_info("USTECm").point == 0.1  # D-076 index
    assert src.point_size("BTCUSD") == 0.01
    assert src.point_size("USTEC") == 0.1
    assert GOLD_SYMBOL_INFO.name == "XAUUSD"
    assert BTC_SYMBOL_INFO.name == "BTCUSD"


async def test_btc_market_feed_created_without_mt5():
    feed = MarketFeed(enable_ws=False)
    assert set(feed.feeds.keys()) == {"XAUUSD", "BTCUSD", "USOIL", "USTEC"}
    assert feed.feeds["BTCUSD"].spec.binance_symbol == "BTCUSDT"
    # D-076 — terminal-only markets: no composite providers, ever
    assert feed.feeds["USOIL"].spec.binance_symbol == ""
    assert feed.feeds["USTEC"].spec.ws_venues == "NONE"
    assert feed.feeds["USOIL"]._resolve_venues() == ()  # type: ignore[attr-defined]
    assert feed.feed_for("XAUUSDm").spec.key == "XAUUSD"
    assert feed.feed_for("BTCUSDm").spec.key == "BTCUSD"
    assert feed.feed_for("USOILm").spec.key == "USOIL"  # 5-char broker suffix
    assert feed.feed_for("USTECmicro").spec.key == "USTEC"
    assert feed.feed_for(None).spec.key == "XAUUSD"


async def test_feed_status_reports_symbols_and_weekend_note():
    mcp = FakeMcp(fresh_keys=())
    mcp.status_val = {"XAUUSD": {"ok": False}, "BTCUSD": {"ok": False}}
    src = LiveDataSource(enable_ws=False, mcp_market=mcp)
    st = src.feed_status()
    assert set(st["symbols"].keys()) == {"XAUUSD", "BTCUSD", "USOIL", "USTEC"}
    assert st["symbols"]["XAUUSD"]["mt5"] is False
    assert "note" in st  # honest weekend/terminal-down badge text


async def test_mcp_market_tick_volume_builds_m1():
    """MT5 ticks fold into the tick-built M1 exactly like crypto events."""
    mcp = FakeMcp(fresh_keys=("XAUUSD",))
    core = _core(mcp)
    # FIXED epoch (a safe 5s inside a synthetic minute): wall-clock `now`
    # occasionally lands at second 58+ and the now+2 tick spills into the
    # NEXT minute bucket — a 1-in-30 latent flake, not a code path
    now = 1_700_000_000 * 60 + 5.0
    core.on_mt5_tick(4378.10, 4378.40, now)
    core.on_mt5_tick(4378.50, 4378.80, now + 1)
    core.on_mt5_tick(4379.00, 4379.30, now + 2)
    bucket = int(now // 60) * 60
    bar = core._m1_tick[bucket]
    assert bar["v"] == 3
    assert bar["h"] == pytest.approx((4379.00 + 4379.30) / 2)
    assert bar["l"] == pytest.approx((4378.10 + 4378.40) / 2)
