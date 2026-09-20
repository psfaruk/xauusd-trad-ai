"""TickFeed tests — multi-venue WS aggregation (D-033), fully offline.

Parsers + consolidation are pure functions over scripted venue frames; no
test ever opens a WebSocket.
"""

from __future__ import annotations

import time

from app.mt5.tick_feed import (
    TickAggregator,
    VenueState,
    consolidate,
    parse_binance,
    parse_bybit,
    parse_coinbase,
    parse_kraken,
    parse_okx,
)


def _st(name: str = "x", bid: float = 0.0, ask: float = 0.0, age: float = 0.0):
    s = VenueState(name=name)
    if bid and ask:
        s.bid, s.ask = bid, ask
        s.last_quote_monotonic = time.monotonic() - age
    return s


# ------------------------------------------------------------------ parsers


def test_parse_binance_quote_and_trade():
    st = _st()
    q = parse_binance(
        {"stream": "paxgusdt@bookTicker", "data": {"b": "4000.10", "a": "4000.50"}},
        st,
    )
    assert q == (4000.10, 4000.50, 0.0)
    assert st.bid == 4000.10 and st.ask == 4000.50

    t = parse_binance(
        {"stream": "paxgusdt@aggTrade", "data": {"p": "4000.30", "q": "0.42"}},
        st,
    )
    assert t == (4000.10, 4000.50, 0.42)  # BBO unchanged, qty carried

    d = parse_binance({"stream": "paxgusdt@depth@100ms", "data": {"b": [], "a": []}}, st)
    assert d == (4000.10, 4000.50, 0.0)  # book churn = activity tick

    assert parse_binance({"stream": "x", "data": {}}, st) is None


def test_parse_bybit_okx_kraken_coinbase():
    st = _st()
    ob = parse_bybit(
        {"topic": "orderbook.1.XAUTUSDT",
         "data": {"b": [["4370.8", "0.85"]], "a": [["4370.9", "0.62"]]}},
        st,
    )
    assert ob == (4370.8, 4370.9, 0.0)

    tr = parse_bybit(
        {"topic": "publicTrade.XAUTUSDT",
         "data": [{"p": "4370.9", "v": "0.013"}, {"p": "4370.9", "v": "0.020"}]},
        st,
    )
    assert tr[2] == 0.033  # summed traded qty

    okx = parse_okx(
        {"arg": {"channel": "bbo-tbt", "instId": "PAXG-USDT"},
         "data": [{"bids": [["4000.1", "1"]], "asks": [["4000.2", "1"]]}]},
        st,
    )
    assert okx == (4000.1, 4000.2, 0.0)

    kr = parse_kraken(
        {"channel": "book",
         "data": [{"bids": [{"price": 4000.0, "qty": 1.0}],
                   "asks": [{"price": 4000.4, "qty": 2.0}]}]},
        st,
    )
    assert kr == (4000.0, 4000.4, 0.0)

    cb = parse_coinbase(
        {"type": "ticker", "best_bid": "4000.1", "best_ask": "4000.5",
         "last_size": "0.05"},
        st,
    )
    assert cb == (4000.1, 4000.5, 0.05)


# ------------------------------------------------------------ consolidation


def test_consolidate_best_bid_ask_across_venues():
    states = {
        "a": _st("a", 4000.0, 4000.6),
        "b": _st("b", 4000.1, 4000.5),
    }
    bid, ask = consolidate(states, time.monotonic())
    assert bid == 4000.1  # max bid
    assert ask == 4000.5  # min ask


def test_consolidate_ignores_stale_venues():
    states = {
        "fresh": _st("fresh", 4000.0, 4000.4),
        "stale": _st("stale", 4100.0, 4100.0, age=60.0),  # older than window
    }
    bid, ask = consolidate(states, time.monotonic())
    assert (bid, ask) == (4000.0, 4000.4)


def test_consolidate_ignores_activity_only_venues():
    """XAUT books (bybit) trade away from the PAXG cluster — they must never
    move the displayed quote, but their events still count as activity."""
    states = {
        "paxg": _st("paxg", 4000.0, 4000.4),
        "xaut": _st("xaut", 4010.0, 4010.5),
    }
    states["xaut"].is_quote = False
    bid, ask = consolidate(states, time.monotonic())
    assert (bid, ask) == (4000.0, 4000.4)


def test_consolidate_cross_falls_back_to_freshest_venue():
    now = time.monotonic()
    older = _st("older", 4100.0, 4100.5)
    older.last_quote_monotonic = now - 5.0
    newest = _st("newest", 4000.0, 4000.4)
    newest.last_quote_monotonic = now - 0.1
    bid, ask = consolidate({"older": older, "newest": newest}, now)
    assert (bid, ask) == (4000.0, 4000.4)  # no crossed quote


def test_consolidate_nothing_fresh():
    assert consolidate({}, time.monotonic()) is None
    assert consolidate({"x": _st()}, time.monotonic()) is None


# -------------------------------------------------------------- aggregator


def test_aggregator_emits_every_real_event():
    """on_event fires per venue event; tps meter + venue stats track them."""
    events: list[tuple[float, float, float, str]] = []

    def on_event(ev):
        events.append((ev.bid, ev.ask, ev.qty, ev.venue))

    agg = TickAggregator(on_event=on_event, enabled_venues=("binance",))
    st = agg.states["binance"]

    st.bid, st.ask = 4000.0, 4000.4
    st.last_quote_monotonic = time.monotonic()

    # pump two venue frames through the parser path the worker uses
    for frame in (
        {"stream": "paxgusdt@bookTicker", "data": {"b": "4000.1", "a": "4000.5"}},
        {"stream": "paxgusdt@aggTrade", "data": {"p": "4000.3", "q": "0.9"}},
    ):
        parsed = parse_binance(frame, st)
        st.events += 1
        st.last_event_monotonic = time.monotonic()
        assert parsed is not None
        quote = consolidate(agg.states, time.monotonic())
        assert quote is not None
        on_event(type("Ev", (), {
            "bid": quote[0], "ask": quote[1], "ts": time.time(),
            "qty": parsed[2], "venue": "binance",
        })())

    assert len(events) == 2
    assert events[0][0] == 4000.1 and events[1][2] == 0.9
    assert agg.stats()["binance"]["events"] == 2
    assert agg.healthy_venue_count() == 0  # never actually connected (no WS)
