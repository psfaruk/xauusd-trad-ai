"""Multi-venue WebSocket tick aggregator (D-033) — REAL market events only.

The user's directive: NO demo/synthetic prices anywhere — real market data,
real ticks, candles that update on every tick. REST polling (MarketFeed,
D-030) caps at one quote per poll cycle; this module pushes EVERY real
market event the instant it happens by subscribing to the public key-less
WebSocket feeds of five gold venues:

  venue     | instrument      | channels
  ----------+-----------------+-------------------------------------------
  Binance   | PAXG/USDT       | bookTicker + aggTrade + depth@100ms
           | PAXG/USDC       | bookTicker
  Bybit     | XAUT/USDT       | orderbook.1 (100ms) + publicTrade
  OKX       | PAXG/USDT       | bbo-tbt + trades
           | XAUT/USDT       | bbo-tbt
  Kraken    | PAXG/USD        | book (depth 10) + trade
  Coinbase  | PAXG-USD        | ticker

PAXG (Paxos) and XAUT (Tether) each redeem for exactly one fine troy ounce
of physical gold, so every venue prices the same underlying as spot XAUUSD
(within a fraction of a percent). Aggregating five venues multiplies the
real tick rate: active sessions stream tens of events per second; quiet
hours stream what the real market actually does — no synthetic filler.

Geo-robustness (Railway US egress): `data-stream.binance.vision` is
Binance's public data mirror (not the 451-geo-blocked domains), and
Kraken + Coinbase are US exchanges — at least three venues answer from any
region. Every venue worker auto-reconnects with backoff forever.

Consolidated quote: best bid = max(bid) across fresh QUOTE venues, best
ask = min(ask); on a cross-venue cross (arb flicker) the freshest venue's
own BBO is used. Quote sources are the PAXG venues only — XAUT (Tether
Gold) books on Bybit/OKX are thin and can trade several dollars away from
the PAXG cluster (a stale/premium artifact), so their events still count
as REAL activity ticks (rate + volume) but never move the displayed quote.
Every event is forwarded to the callback — the MarketFeed turns each one
into a tick, so forming candles absorb all 20-50+ real updates per second
even though the UI throttles its own re-renders.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time as time_mod
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import websockets

logger = logging.getLogger("xauusd.tickfeed")

QUOTE_FRESH_S = 20.0     # venue excluded from consolidation past this age
RECONNECT_MIN_S = 1.0
RECONNECT_MAX_S = 30.0
KEEPALIVE_S = 18.0       # text-level ping for venues that need it


@dataclass
class VenueState:
    name: str
    bid: float = 0.0
    ask: float = 0.0
    last_quote_monotonic: float = 0.0   # last BBO refresh
    last_event_monotonic: float = 0.0   # last message of any kind
    events: int = 0
    connected: bool = False
    last_error: str = ""
    is_quote: bool = True               # False = activity-only venue (D-033)


@dataclass
class VenueConfig:
    name: str
    urls: tuple[str, ...]               # rotated on reconnect (geo fallback)
    subscribe: tuple[str, ...]          # raw JSON frames sent in order after connect
    ping_text: str | None = None        # text-level keepalive (None = protocol pings suffice)
    quote: bool = True                  # False = events count as activity, not price


@dataclass
class TickEvent:
    bid: float
    ask: float
    ts: float                # epoch seconds (venue time or local clock)
    qty: float               # traded base qty (0.0 for pure book events)
    venue: str


# --------------------------------------------------------------------- configs

BINANCE_STREAMS = "/".join((
    "paxgusdt@bookTicker",
    "paxgusdt@aggTrade",
    "paxgusdt@depth@100ms",
    "paxgusdc@bookTicker",
))

VENUES: tuple[VenueConfig, ...] = (
    VenueConfig(
        name="binance",
        urls=(
            f"wss://data-stream.binance.vision/stream?streams={BINANCE_STREAMS}",
            f"wss://stream.binance.com:9443/stream?streams={BINANCE_STREAMS}",
        ),
        subscribe=(),  # streams are in the URL
    ),
    VenueConfig(
        name="bybit",
        urls=("wss://stream.bybit.com/v5/public/spot",),
        subscribe=(json.dumps({
            "op": "subscribe",
            "args": ["orderbook.1.XAUTUSDT", "publicTrade.XAUTUSDT"],
        }),),
        ping_text=json.dumps({"op": "ping"}),
        quote=False,  # XAUT books trade away from the PAXG cluster — activity only
    ),
    VenueConfig(
        name="okx",
        urls=("wss://ws.okx.com:8443/ws/v5/public",),
        subscribe=(json.dumps({"op": "subscribe", "args": [
            {"channel": "bbo-tbt", "instId": "PAXG-USDT"},
            {"channel": "trades", "instId": "PAXG-USDT"},
            {"channel": "bbo-tbt", "instId": "XAUT-USDT"},
        ]}),),
        ping_text="ping",  # OKX text ping -> expects "pong"
    ),
    VenueConfig(
        name="kraken",
        urls=("wss://ws.kraken.com/v2",),
        # one channel per message (Kraken v2 rejects combined params)
        subscribe=(
            json.dumps({"method": "subscribe", "params": {
                "channel": "book", "symbol": ["PAXG/USD"], "depth": 10}}),
            json.dumps({"method": "subscribe", "params": {
                "channel": "trade", "symbol": ["PAXG/USD"]}}),
        ),
    ),
    VenueConfig(
        name="coinbase",
        urls=("wss://ws-feed.exchange.coinbase.com",),
        subscribe=(json.dumps({
            "type": "subscribe", "product_ids": ["PAXG-USD"], "channels": ["ticker"],
        }),),
    ),
)


# ------------------------------------------------------------------- parsing

def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def parse_binance(msg: dict, st: VenueState) -> tuple[float, float, float] | None:
    """Combined-stream frames -> (bid, ask, qty) or None (book-diff events
    return the last quote unchanged so they still count as activity)."""
    stream = msg.get("stream") or ""
    data = msg.get("data")
    if not isinstance(data, dict):
        return None
    if stream.endswith("bookTicker"):
        b, a = _f(data.get("b")), _f(data.get("a"))
        if b > 0 and a > 0 and a >= b:
            st.bid, st.ask = b, a
            st.last_quote_monotonic = time_mod.monotonic()
            return b, a, 0.0
        return None
    if stream.endswith("aggTrade"):
        qty = _f(data.get("q"))
        return st.bid, st.ask, qty  # trades confirm activity; BBO unchanged
    if "@depth" in stream:
        # real book churn (100ms diff) — activity tick at the current quote
        return st.bid, st.ask, 0.0
    return None


def parse_bybit(msg: dict, st: VenueState) -> tuple[float, float, float] | None:
    topic = msg.get("topic") or ""
    data = msg.get("data")
    if not isinstance(data, (dict, list)):
        return None
    if topic.startswith("orderbook."):
        if isinstance(data, dict):
            b = _f((data.get("b") or [[0]])[0][0])
            a = _f((data.get("a") or [[0]])[0][0])
            if b > 0 and a > 0 and a >= b:
                st.bid, st.ask = b, a
                st.last_quote_monotonic = time_mod.monotonic()
                return b, a, 0.0
        return None
    if topic.startswith("publicTrade"):
        trades = data if isinstance(data, list) else [data]
        qty = sum(_f(t.get("v")) for t in trades if isinstance(t, dict))
        return st.bid, st.ask, qty
    return None


def parse_okx(msg: dict, st: VenueState) -> tuple[float, float, float] | None:
    arg = msg.get("arg") or {}
    channel = arg.get("channel") or ""
    inst = arg.get("instId") or ""
    data = msg.get("data")
    if not isinstance(data, list):
        return None
    if channel == "bbo-tbt":
        # XAUT books sit dollars away from the PAXG cluster — count their
        # updates as activity but never quote off them (D-033).
        if inst != "PAXG-USDT":
            return st.bid, st.ask, 0.0
        d = data[-1]
        bids, asks = d.get("bids") or [], d.get("asks") or []
        if bids and asks:
            b, a = _f(bids[0][0]), _f(asks[0][0])
            if b > 0 and a > 0 and a >= b:
                st.bid, st.ask = b, a
                st.last_quote_monotonic = time_mod.monotonic()
                return b, a, 0.0
        return None
    if channel == "trades":
        qty = sum(_f(t.get("sz")) for t in data if isinstance(t, dict))
        return st.bid, st.ask, qty
    return None


def parse_kraken(msg: dict, st: VenueState) -> tuple[float, float, float] | None:
    channel = msg.get("channel") or ""
    data = msg.get("data")
    if not isinstance(data, list):
        return None
    if channel == "book":
        for d in data:
            bids = d.get("bids") or []
            asks = d.get("asks") or []
            if bids and asks:
                b, a = _f(bids[0].get("price")), _f(asks[0].get("price"))
                if b > 0 and a > 0 and a >= b:
                    st.bid, st.ask = b, a
                    st.last_quote_monotonic = time_mod.monotonic()
                    return b, a, 0.0
        return None
    if channel == "trade":
        qty = sum(_f(t.get("qty")) for t in data if isinstance(t, dict))
        return st.bid, st.ask, qty
    return None


def parse_coinbase(msg: dict, st: VenueState) -> tuple[float, float, float] | None:
    if msg.get("type") != "ticker":
        return None
    b, a = _f(msg.get("best_bid")), _f(msg.get("best_ask"))
    qty = _f(msg.get("last_size"))
    if b > 0 and a > 0 and a >= b:
        st.bid, st.ask = b, a
        st.last_quote_monotonic = time_mod.monotonic()
        return b, a, qty
    return None


PARSERS = {
    "binance": parse_binance,
    "bybit": parse_bybit,
    "okx": parse_okx,
    "kraken": parse_kraken,
    "coinbase": parse_coinbase,
}


# ---------------------------------------------------------------- aggregator

def consolidate(states: dict[str, VenueState], now: float) -> tuple[float, float] | None:
    """Best bid = max, best ask = min across QUOTE venues with a FRESH quote.

    Activity-only venues (XAUT books, `is_quote=False`) never move the
    displayed price. Cross-venue crosses (bid_A >= ask_B — latency arb
    flicker) fall back to the freshest venue's own BBO so the consolidated
    quote is always sane.
    """
    fresh = [
        s for s in states.values()
        if s.is_quote
        and s.bid > 0 and s.ask > 0 and s.ask >= s.bid
        and now - s.last_quote_monotonic < QUOTE_FRESH_S
    ]
    if not fresh:
        return None
    best_bid = max(s.bid for s in fresh)
    best_ask = min(s.ask for s in fresh)
    if best_bid >= best_ask:
        newest = max(fresh, key=lambda s: s.last_quote_monotonic)
        return newest.bid, newest.ask
    return best_bid, best_ask


class TickAggregator:
    """Owns one WS worker task per venue; consolidates every event.

    `on_event` is a SYNC callback (fast, non-blocking) invoked for every
    real market event — the MarketFeed turns each into a tick.
    """

    def __init__(
        self,
        on_event: Callable[[TickEvent], None],
        venues: tuple[VenueConfig, ...] = VENUES,
        enabled_venues: tuple[str, ...] | None = None,
    ) -> None:
        self._on_event = on_event
        self._venues = (
            venues if enabled_venues is None
            else tuple(v for v in venues if v.name in enabled_venues)
        )
        self.states: dict[str, VenueState] = {
            v.name: VenueState(name=v.name, is_quote=v.quote) for v in self._venues
        }
        self._tasks: list[asyncio.Task] = []
        self._stopped = asyncio.Event()
        self._event_times: deque[float] = deque(maxlen=512)

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        if self.running:
            return
        self._stopped.clear()
        for cfg in self._venues:
            self._tasks.append(
                asyncio.create_task(
                    self._venue_loop(cfg), name=f"tickfeed-{cfg.name}"
                )
            )

    async def stop(self) -> None:
        self._stopped.set()
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(BaseException):
                await t
        self._tasks = []
        for st in self.states.values():
            st.connected = False

    @property
    def running(self) -> bool:
        return bool(self._tasks) and not self._stopped.is_set()

    # ------------------------------------------------------------- stats

    def tps(self, window_s: float = 5.0) -> float:
        """Real events per second over the trailing window."""
        now = time_mod.monotonic()
        while self._event_times and now - self._event_times[0] > window_s:
            self._event_times.popleft()
        return len(self._event_times) / max(window_s, 0.001)

    def healthy_venue_count(self) -> int:
        now = time_mod.monotonic()
        return sum(
            1 for s in self.states.values()
            if s.connected and now - s.last_event_monotonic < 60.0
        )

    def stats(self) -> dict[str, dict]:
        now = time_mod.monotonic()
        return {
            name: {
                "ok": st.connected and now - st.last_event_monotonic < 60.0,
                "events": st.events,
                "age_s": (
                    round(now - st.last_event_monotonic, 1)
                    if st.last_event_monotonic else None
                ),
                "err": st.last_error or None,
            }
            for name, st in self.states.items()
        }

    # -------------------------------------------------------------- workers

    async def _venue_loop(self, cfg: VenueConfig) -> None:
        backoff = RECONNECT_MIN_S
        url_idx = 0
        st = self.states[cfg.name]
        while not self._stopped.is_set():
            try:
                url = cfg.urls[url_idx % len(cfg.urls)]
                async with websockets.connect(
                    url, open_timeout=10.0, ping_interval=20.0, ping_timeout=20.0,
                    max_queue=4096,
                ) as ws:
                    st.connected = True
                    st.last_error = ""
                    backoff = RECONNECT_MIN_S
                    for sub_frame in cfg.subscribe:
                        await ws.send(sub_frame)
                    await self._pump(cfg, st, ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — reconnect forever
                st.connected = False
                st.last_error = f"{type(exc).__name__}: {exc}"[:160]
                logger.debug("venue %s disconnected (%s) — retrying", cfg.name, st.last_error)
            st.connected = False
            # rotate to the next mirror after a failure streak (geo fallback)
            url_idx += 1
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=backoff)
            except TimeoutError:
                pass
            backoff = min(backoff * 1.6, RECONNECT_MAX_S)

    async def _pump(self, cfg: VenueConfig, st: VenueState, ws: Any) -> None:
        parser = PARSERS[cfg.name]
        keepalive: asyncio.Task | None = None
        if cfg.ping_text:
            keepalive = asyncio.create_task(self._keepalive(ws, cfg.ping_text))
        try:
            async for raw in ws:
                if self._stopped.is_set():
                    return
                try:
                    msg = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if not isinstance(msg, dict):
                    continue
                if cfg.name == "okx" and msg.get("event") == "pong":
                    continue
                if cfg.name == "bybit" and msg.get("op") in ("pong", "subscribe"):
                    continue
                if cfg.name == "kraken" and (msg.get("method") or msg.get("channel") == "status"):
                    continue
                if cfg.name == "coinbase" and msg.get("type") in ("subscriptions",):
                    continue
                if cfg.name == "binance" and msg.get("id"):
                    continue
                try:
                    parsed = parser(msg, st)
                except Exception:  # noqa: BLE001 — one bad frame never kills the venue
                    logger.debug("venue %s parse error", cfg.name, exc_info=True)
                    continue
                st.events += 1
                st.last_event_monotonic = time_mod.monotonic()
                self._event_times.append(st.last_event_monotonic)
                if parsed is None:
                    continue
                bid, ask, qty = parsed
                quote = consolidate(self.states, time_mod.monotonic())
                if quote is None:
                    quote = (bid or st.bid, ask or st.ask)
                if quote[0] > 0 and quote[1] >= quote[0]:
                    with contextlib.suppress(Exception):
                        self._on_event(TickEvent(
                            bid=quote[0], ask=quote[1],
                            ts=time_mod.time(), qty=qty, venue=cfg.name,
                        ))
        finally:
            if keepalive is not None:
                keepalive.cancel()
                with contextlib.suppress(BaseException):
                    await keepalive

    @staticmethod
    async def _keepalive(ws: Any, text: str) -> None:
        try:
            while True:
                await asyncio.sleep(KEEPALIVE_S)
                await ws.send(text)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — socket death handled by the pump
            return


def utc_now() -> datetime:
    return datetime.now(tz=UTC)
