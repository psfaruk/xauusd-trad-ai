"""LiveDataSource tests — free real-time market data (DATA_SOURCE=live, D-030).

All network access is faked via FakeClient (scripted URL routing): no test
ever touches Binance/gold-api/Yahoo. Fixtures are aligned to the REAL clock
(the source bucket-aligns with time.time()), so bars roll like production.
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from app.mt5.base import DataSourceError, Order
from app.mt5.live_source import LiveDataSource, MarketFeed, _parse_binance_klines

# --------------------------------------------------------------------- fakes


class FakeResponse:
    def __init__(self, data, status: int = 200) -> None:
        self._data = data
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=None, response=None
            )

    def json(self):
        return self._data


class FakeClient:
    """GET router keyed by URL substring; handlers read mutable state."""

    def __init__(self) -> None:
        self.routes: dict[str, object] = {}
        self.fail: set[str] = set()
        self.calls: list[str] = []

    def on(self, frag: str, handler) -> None:
        self.routes[frag] = handler

    async def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append(url)
        for frag in self.fail:
            if frag in url:
                raise httpx.ConnectError(f"scripted fail: {frag}")
        for frag, handler in self.routes.items():
            if frag in url:
                result = handler(params or {})
                if isinstance(result, Exception):
                    raise result
                return FakeResponse(result)
        raise AssertionError(f"unexpected url: {url}")

    def count(self, frag: str) -> int:
        return sum(1 for u in self.calls if frag in u)


def binance_klines(tf_sec: int, n: int, end_bucket: int | None = None,
                   base: float = 4000.0) -> list[list]:
    """n rows ending at the CURRENT forming bucket (or `end_bucket`)."""
    now = int(time.time())
    end = end_bucket if end_bucket is not None else (now // tf_sec) * tf_sec
    rows = []
    for i in range(n):
        t = end - (n - 1 - i) * tf_sec
        o = base + i * 0.5
        rows.append([
            t * 1000, str(o), str(o + 1.0), str(o - 1.0), str(o + 0.5),
            str(10 + i), (t + tf_sec - 1) * 1000, "0", 0, "0", "0", "0",
        ])
    return rows


def yahoo_chart(tf_sec: int, n: int, base: float = 4000.0) -> dict:
    now = int(time.time())
    end = (now // tf_sec) * tf_sec
    ts, o, h, lo, c, v = [], [], [], [], [], []
    for i in range(n):
        t = end - (n - 1 - i) * tf_sec
        ts.append(t)
        o.append(base + i * 0.5)
        h.append(base + i * 0.5 + 1.0)
        lo.append(base + i * 0.5 - 1.0)
        c.append(base + i * 0.5 + 0.5)
        v.append(100 + i)
    return {
        "chart": {
            "result": [{
                "timestamp": ts,
                "indicators": {"quote": [{
                    "open": o, "high": h, "low": lo, "close": c, "volume": v,
                }]},
            }],
            "error": None,
        }
    }


def make_source(client: FakeClient, **kw) -> LiveDataSource:
    kw.setdefault("poll_seconds", 0.05)
    kw.setdefault("connect_timeout_s", 0.4)
    return LiveDataSource(http_factory=lambda: client, **kw)


def binance_quotes(client: FakeClient, bid: float = 4000.0, ask: float = 4000.2,
                   m1: list[list] | None = None) -> dict:
    state = {"bid": bid, "ask": ask, "m1": m1 if m1 is not None else binance_klines(60, 3)}
    client.on("bookTicker", lambda p: {
        "symbol": "PAXGUSDT", "bidPrice": str(state["bid"]),
        "askPrice": str(state["ask"]),
    })
    client.on("/api/v3/klines", lambda p: state["m1"])
    return state


# ------------------------------------------------------------ kline parsing


def test_parse_binance_klines_splits_closed_and_forming():
    now = int(time.time())
    tf = 60
    forming_t = (now // tf) * tf
    rows = [
        # closed two buckets ago
        [(forming_t - 2 * tf) * 1000, "1", "2", "0.5", "1.5", "10",
         (forming_t - tf - 1) * 1000, "0", 0, "0", "0", "0"],
        # closed one bucket ago
        [(forming_t - tf) * 1000, "1.5", "2.5", "1", "2", "11",
         (forming_t - 1) * 1000, "0", 0, "0", "0", "0"],
        # currently forming (close time in the future)
        [forming_t * 1000, "2", "3", "1.5", "2.5", "12",
         (forming_t + tf - 1) * 1000, "0", 0, "0", "0", "0"],
    ]
    closed, forming = _parse_binance_klines(rows)
    assert [r["t"] for r in closed] == [forming_t - 2 * tf, forming_t - tf]
    assert closed[0]["o"] == 1.0 and closed[0]["v"] == 10
    assert forming is not None and forming["t"] == forming_t
    assert forming["c"] == 2.5


async def test_get_rates_returns_closed_bars_only_ascending():
    client = FakeClient()
    client.on("/api/v3/klines", lambda p: binance_klines(900, 8, base=4300.0))
    src = make_source(client)
    df = await src.get_rates("XAUUSD", "M15", 5)
    assert len(df) == 5
    ts = list(df["time_utc"])
    assert ts == sorted(ts)
    assert str(ts[0].tzinfo) == "UTC"
    now = int(time.time())
    last_closed = (now // 900 - 1) * 900
    assert int(ts[-1].timestamp()) == last_closed
    assert float(df["o"].iloc[0]) == pytest.approx(4300.0 + 2 * 0.5)  # row 2 of 8


async def test_forming_bar_authoritative_m1_from_binance():
    client = FakeClient()
    binance_quotes(client, m1=binance_klines(60, 2, base=4361.0))
    src = make_source(client)
    await src.market.poll_once()
    bar = await src.get_forming_bar("XAUUSD", "M1")
    assert bar is not None
    now = int(time.time())
    assert bar["t"] == (now // 60) * 60
    assert bar["o"] == pytest.approx(4361.5)  # forming row open = base + 0.5


async def test_forming_bar_tick_built_when_klines_unavailable():
    client = FakeClient()
    state = binance_quotes(client)
    client.fail.add("/api/v3/klines")  # quotes live, history/klines dead
    src = make_source(client)
    await src.market.poll_once()  # mid 4000.1
    state["bid"], state["ask"] = 4002.0, 4002.2
    await src.market.poll_once()  # mid 4002.1
    bar = await src.get_forming_bar("XAUUSD", "M5")
    assert bar is not None
    assert bar["t"] % 300 == 0
    assert bar["o"] == pytest.approx(4000.1)  # first tick's mid
    assert bar["h"] == pytest.approx(4002.1)
    assert bar["c"] == pytest.approx(4002.1)


async def test_cache_refetch_on_missing_closed_bucket():
    client = FakeClient()
    now = int(time.time())
    b0 = (now // 900) * 900
    state = {"rows": binance_klines(900, 6, end_bucket=b0 - 2 * 900)}
    client.on("/api/v3/klines", lambda p: state["rows"])
    feed = MarketFeed(http_factory=lambda: client, poll_seconds=0.05)
    closed, _ = await feed.ensure_tf("M15", 3)
    assert closed[-1]["t"] == b0 - 2 * 900  # stale by one bucket
    # the just-closed bucket is missing -> refetch (after the rate cap)
    feed._tf_refetch_ts["M15"] = 0.0
    state["rows"] = binance_klines(900, 7, end_bucket=b0 - 900)
    closed, _ = await feed.ensure_tf("M15", 3)
    assert closed[-1]["t"] == b0 - 900  # == want_t: cache now complete
    # fresh + complete -> no further refetch
    calls = client.count("/api/v3/klines")
    await feed.ensure_tf("M15", 3)
    assert client.count("/api/v3/klines") == calls


# ------------------------------------------------------------- failover


async def test_quote_fallback_to_goldapi():
    client = FakeClient()
    client.fail.add("binance")
    client.on("gold-api", lambda p: {"price": 4379.0})
    src = make_source(client)
    ok = await src.market.poll_once()
    assert ok is True
    assert src.market.provider == "goldapi"
    assert src.market.tick is not None
    assert src.market.tick.bid == pytest.approx(4379.0 - 0.175)
    assert src.market.tick.ask == pytest.approx(4379.0 + 0.175)


async def test_all_providers_down_degrades():
    client = FakeClient()
    client.fail.add("binance")
    client.fail.add("gold-api")
    src = make_source(client)
    assert await src.market.poll_once() is False
    assert src.market.provider == "degraded"


async def test_history_fallback_to_yahoo():
    client = FakeClient()
    binance_quotes(client)
    client.fail.add("/api/v3/klines")  # binance history dead
    client.on("yahoo", lambda p: yahoo_chart(3600, 10, base=4100.0))
    src = make_source(client)
    df = await src.get_rates("XAUUSD", "H1", 5)
    assert len(df) == 5
    now = int(time.time())
    assert int(df["time_utc"].iloc[-1].timestamp()) == (now // 3600 - 1) * 3600
    assert float(df["o"].iloc[0]) == pytest.approx(4100.0 + 4 * 0.5)  # row 4 of 10


async def test_yahoo_h4_aggregates_hourly_bars():
    client = FakeClient()
    binance_quotes(client)
    client.fail.add("/api/v3/klines")
    client.on("yahoo", lambda p: yahoo_chart(3600, 8, base=4200.0))
    src = make_source(client)
    df = await src.get_rates("XAUUSD", "H4", 10)
    assert len(df) >= 1
    for t in df["time_utc"]:
        assert int(t.timestamp()) % 14400 == 0  # aligned to H4 grid
    # hourly closes within one bucket sum into the H4 bar
    assert (df["v"] > 0).all()


async def test_tick_built_history_when_every_provider_is_down():
    client = FakeClient()
    client.fail.add("binance")
    client.on("gold-api", lambda p: {"price": 4400.0})
    feed = MarketFeed(http_factory=lambda: client, poll_seconds=0.05)
    # quotes alive via gold-api (feeds the live tick)
    assert await feed.poll_once() is True
    # seed the tick-built M1 series as if a few minutes already streamed
    base = int(time.time()) // 60
    feed._m1_tick_history = [
        {"t": (base - 2) * 60, "o": 4400.0, "h": 4401.0, "l": 4399.0, "c": 4400.5, "v": 30},
        {"t": (base - 1) * 60, "o": 4400.5, "h": 4402.0, "l": 4400.0, "c": 4401.0, "v": 25},
    ]
    feed._m1_tick = {
        base * 60: {"t": base * 60, "o": 4401.0, "h": 4403.0, "l": 4400.5, "c": 4402.0, "v": 12}
    }
    closed, forming = await feed.ensure_tf("M1", 10)
    assert [r["t"] for r in closed] == [(base - 2) * 60, (base - 1) * 60]
    assert forming is not None and forming["t"] == base * 60


# ------------------------------------------------------------- lifecycle


async def test_connect_raises_when_no_provider_reachable():
    client = FakeClient()
    client.fail.add("binance")
    client.fail.add("gold-api")
    src = make_source(client, connect_timeout_s=0.3)
    with pytest.raises(DataSourceError):
        await src.connect({"server": "LiveMarket"})


async def test_connect_and_is_connected():
    client = FakeClient()
    binance_quotes(client, bid=4361.5, ask=4361.7)
    src = make_source(client, connect_timeout_s=2.0)
    info = await src.connect({"server": "LiveMarket"})
    assert info["server"].startswith("LiveMarket • binance")
    assert await src.is_connected() is True
    # stale data (> 90s) -> disconnected so the heartbeat can heal it
    src.market.last_data_monotonic -= 999.0
    assert await src.is_connected() is False


async def test_disconnect_stops_owned_feed_only():
    client = FakeClient()
    binance_quotes(client)
    src = make_source(client)
    await src.connect({})
    assert src.market.running is True
    await src.disconnect()
    assert src.market.running is False
    # a sibling sharing the feed must NOT stop it
    src2 = make_source(client)
    await src2.connect({})
    sib = LiveDataSource.sibling(src2)
    await sib.connect({})
    await sib.disconnect()
    assert src2.market.running is True


# ------------------------------------------------------------- paper trading


async def test_paper_order_floating_pl_and_close():
    client = FakeClient()
    state = binance_quotes(client, bid=4000.0, ask=4000.2)
    src = make_source(client, connect_timeout_s=2.0)
    await src.connect({"server": "LiveMarket"})

    res = await src.place_order(Order(symbol="XAUUSD", side="BUY", volume=0.5))
    assert res.ok is True
    assert res.price == pytest.approx(4000.2)  # BUY fills at ask

    # price moves +10 -> floating P/L = (4010.0 - 4000.2) * 100 * 0.5
    state["bid"], state["ask"] = 4010.0, 4010.2
    await src.market.poll_once()
    positions = await src.get_positions()
    assert len(positions) == 1
    assert positions[0].profit == pytest.approx((4010.0 - 4000.2) * 100 * 0.5)

    acct = src.account_info()
    assert acct is not None
    assert acct["equity"] == pytest.approx(10_000.0 + 490.0)

    closed = await src.close_position(res.ticket)
    assert closed.ok is True
    assert closed.price == pytest.approx(4010.0)  # BUY closes at bid
    acct2 = src.account_info()
    assert acct2["balance"] == pytest.approx(10_490.0)
    assert await src.get_positions() == []

    # order rejection before connect
    src2 = make_source(client)
    res2 = await src2.place_order(Order(symbol="XAUUSD", side="SELL", volume=0.1))
    assert res2.ok is False


async def test_sibling_shares_market_but_not_balance():
    client = FakeClient()
    binance_quotes(client, bid=4300.0, ask=4300.2)
    src = make_source(client, connect_timeout_s=2.0)
    await src.connect({})

    sib = LiveDataSource.sibling(src)
    sib.set_starting_balance(5_000.0)
    await sib.connect({})

    # same live tick object -> identical pricing
    assert sib.market.tick is src.market.tick
    r1 = await src.place_order(Order(symbol="XAUUSD", side="BUY", volume=1.0))
    r2 = await sib.place_order(Order(symbol="XAUUSD", side="BUY", volume=1.0))
    assert r1.ok and r2.ok
    assert r1.price == pytest.approx(r2.price)

    a1, a2 = src.account_info(), sib.account_info()
    assert a1["balance"] == pytest.approx(10_000.0)
    assert a2["balance"] == pytest.approx(5_000.0)
    assert len(await src.get_positions()) == 1
    assert len(await sib.get_positions()) == 1  # isolated planes


async def test_feed_status_transparency():
    client = FakeClient()
    binance_quotes(client, bid=4361.62, ask=4361.63)
    src = make_source(client)
    await src.connect({})
    st = src.feed_status()
    assert st["provider"] == "binance"
    assert st["last_price"] == pytest.approx(4361.62, abs=0.01)
    assert st["spread"] == pytest.approx(0.01, abs=0.011)
    assert st["last_tick_age_s"] is not None and st["last_tick_age_s"] < 5


async def test_subscribe_ticks_yields_at_poll_cadence():
    client = FakeClient()
    binance_quotes(client)
    src = make_source(client)
    await src.connect({})
    seen: list[float] = []
    gen = src.subscribe_ticks("XAUUSD").__aiter__()
    try:
        for _ in range(3):
            tick = await asyncio.wait_for(gen.__anext__(), timeout=2.0)
            seen.append(tick.bid)
    finally:
        await gen.aclose()
    assert len(seen) == 3


# ------------------------------------------------------- boot resolution


async def test_resolve_data_source_live_and_fallback(monkeypatch):
    from app.config import Settings
    from app.mt5.connection import resolve_data_source
    from app.mt5.mock_source import MockDataSource

    # providers reachable -> live
    client = FakeClient()
    binance_quotes(client)
    settings = Settings(data_source="live")
    res = await resolve_data_source(settings, lambda: client)
    assert res.effective == "live" and isinstance(res.source, LiveDataSource)
    assert res.requested == "live" and res.degraded is False
    await res.source.market.stop()

    # providers dead -> honest mock fallback (dashboard still streams)
    dead = FakeClient()
    dead.fail.add("binance")
    dead.fail.add("gold-api")
    settings2 = Settings(data_source="live")
    res2 = await resolve_data_source(settings2, lambda: dead)
    assert res2.effective == "mock" and isinstance(res2.source, MockDataSource)
    assert res2.requested == "live" and res2.degraded is True
    assert res2.reason  # diagnosable via /api/health

    # explicit non-live mode untouched (not degraded — user asked for it)
    settings3 = Settings(data_source="mt5")
    res3 = await resolve_data_source(settings3, lambda: dead)
    assert res3.effective == "mt5" and res3.requested == "mt5"
    assert res3.degraded is False

    # explicit mock: requested mock (health surfaces it; UI shows DEMO banner)
    settings4 = Settings(data_source="mock")
    res4 = await resolve_data_source(settings4, lambda: dead)
    assert res4.effective == "mock" and res4.requested == "mock"
    assert res4.degraded is False


async def test_binance_mirror_failover_451():
    """D-032: api.binance.com 451-blocked (datacenter) -> data-api.binance.vision.

    Mirror #1 (vision) is default-primary; here we force the classic domain
    first via binance_bases order and script a 451 for it — the feed must
    rotate to the vision mirror, remember it (sticky), and keep streaming.
    """
    client = FakeClient()
    state = {"bid": 4000.0, "ask": 4000.2}

    def classic_451(p):
        return httpx.HTTPStatusError(
            "HTTP 451", request=None,
            response=httpx.Response(451, request=httpx.Request("GET", "https://api.binance.com")),
        )

    client.on("api.binance.com", classic_451)
    client.on("bookTicker", lambda p: {
        "symbol": "PAXGUSDT", "bidPrice": str(state["bid"]),
        "askPrice": str(state["ask"]),
    })
    client.on("/api/v3/klines", lambda p: binance_klines(60, 3))
    feed = MarketFeed(
        http_factory=lambda: client,
        poll_seconds=0.05,
        binance_bases=["https://api.binance.com", "https://data-api.binance.vision"],
    )
    assert await feed.poll_once() is True
    assert feed.provider == "binance"
    # the classic domain was tried and failed; the vision mirror answered
    assert client.count("api.binance.com") >= 1
    assert client.count("data-api.binance.vision") >= 1
    # sticky: the next poll goes straight to the working mirror
    calls_before = len(client.calls)
    await feed.poll_once()
    new_calls = client.calls[calls_before:]
    assert all("data-api.binance.vision" in u for u in new_calls)
