"""Real-time market data source from FREE public APIs (DATA_SOURCE=live, D-030).

Why: the MetaTrader5 package only runs on Windows (SPEC C1), so a Linux/Railway
deployment cannot read MT5 directly — it used to fall back to the synthetic
MockDataSource, which shows FAKE prices. This source streams REAL gold prices
24/7 from key-less public APIs instead:

Provider chain (ordered failover, C6 graceful degrade):
1. Binance PAXG/USDT — PRIMARY quotes + candles. PAXG is a regulated,
   physical-gold-backed token (1 PAXG = 1 fine troy ounce, Paxos), so
   PAXG/USDT tracks spot XAUUSD within a fraction of a percent and trades
   around the clock. `bookTicker` -> real bid/ask every poll; `klines` ->
   authoritative OHLCV for every timeframe (native Binance intervals map 1:1
   onto M1..D1).
2. gold-api.com — QUOTE fallback (spot XAU mid, synthetic 0.35 spread) when
   Binance is unreachable (e.g. regional 451 geo-blocks on datacenter IPs).
3. Yahoo Finance GC=F — HISTORY fallback (COMEX gold-futures candles).
4. Tick-built candles — last resort: M1+ bars aggregated live from whatever
   quote provider still answers (charts fill in over time).

Basis note (transparency, user req #5): candle/quote data is PAXG-based and
can differ from a specific broker's XAUUSD feed by a few tenths of a percent.
The active provider is surfaced everywhere (health, /api/mt5/status, WS
mt5_status, TopBar LIVE badge) so users always know what they are looking at.

Paper trading: the platform plane and per-user demo planes run paper accounts
(MockDataSource semantics) priced off the LIVE feed — real prices, simulated
fills. Real MT5 execution stays on the Windows bridge (D-024).
"""

from __future__ import annotations

import asyncio
import logging
import time as time_mod
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from dataclasses import replace as dataclasses_replace
from datetime import UTC, datetime

import httpx
import pandas as pd

from app.mt5.base import (
    RATES_COLUMNS,
    TIMEFRAME_MINUTES,
    DataSource,
    DataSourceError,
    Order,
    OrderResult,
    Position,
    SymbolInfo,
    Tick,
    empty_rates,
    validate_tf,
)

logger = logging.getLogger("xauusd.live")

TRADE_RETCODE_DONE = 10009

# Binance supports every platform timeframe natively (SPEC §7.1 / §8.2).
BINANCE_INTERVALS = {
    "M1": "1m", "M5": "5m", "M15": "15m", "M30": "30m",
    "H1": "1h", "H4": "4h", "D1": "1d",
}
# Yahoo chart API (interval, range) per timeframe — GC=F = COMEX gold futures.
YAHOO_SPECS = {
    "M1": ("1m", "5d"), "M5": ("5m", "1mo"), "M15": ("15m", "1mo"),
    "M30": ("30m", "1mo"), "H1": ("60m", "3mo"), "H4": ("60m", "3mo"),
    "D1": ("1d", "2y"),
}

GOLD_API_SPREAD = 0.35  # synthetic XAUUSD spread for the quote fallback ($)
STALE_DATA_S = 90.0     # is_connected -> False past this quote age
CACHE_TTL_S = 15.0      # TF kline cache freshness
MIN_REFETCH_S = 5.0     # forced-refetch rate cap (session gaps must not hammer)
M1_TICK_KEEP = 4320     # 3 days of tick-built M1 bars kept in memory

# Lot-sizing metadata mirrors a typical Exness XAUUSD standard account.
LIVE_SYMBOL_INFO = SymbolInfo(
    name="XAUUSD", point=0.01, contract_size=100.0,
    volume_min=0.01, volume_max=100.0, volume_step=0.01,
)

DEMO_START_BALANCE = 10_000.0

HttpFactory = Callable[[], Awaitable[httpx.AsyncClient]]

_shared_client: httpx.AsyncClient | None = None


async def _default_http_client() -> httpx.AsyncClient:
    global _shared_client  # noqa: PLW0603 — lazy shared client (closed by app)
    if _shared_client is None:
        _shared_client = httpx.AsyncClient(timeout=4.0)
    return _shared_client


def _parse_binance_klines(rows: list) -> tuple[list[dict], dict | None]:
    """Split Binance kline rows into (closed_bars, forming_bar|None).

    Binance returns the still-FORMING candle as the last row; a row is closed
    once its closeTime (ms, index 6) has passed.
    """
    now = time_mod.time()
    closed: list[dict] = []
    forming: dict | None = None
    for r in rows:
        if not isinstance(r, list) or len(r) < 7:
            continue
        try:
            t = int(r[0]) // 1000
            o, h, low, c, v = (
                float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])
            )
            close_s = int(r[6]) // 1000
        except (TypeError, ValueError):
            continue
        row = {"t": t, "o": o, "h": h, "l": low, "c": c, "v": max(1, int(round(v)))}
        if close_s > now:
            forming = row
        else:
            closed.append(row)
    return closed, forming


def _aggregate_rows(rows: list[dict], tf_min: int) -> list[dict]:
    """Aggregate lower-TF rows into `tf_min` buckets (Yahoo H4 = 2 x 60m)."""
    out: dict[int, dict] = {}
    for r in sorted(rows, key=lambda x: x["t"]):
        key = r["t"] // (tf_min * 60) * tf_min * 60
        cur = out.get(key)
        if cur is None:
            out[key] = {
                "t": key, "o": r["o"], "h": r["h"], "l": r["l"], "c": r["c"], "v": r["v"],
            }
        else:
            cur["h"] = max(cur["h"], r["h"])
            cur["l"] = min(cur["l"], r["l"])
            cur["c"] = r["c"]
            cur["v"] += r["v"]
    return [out[k] for k in sorted(out)]


def _merge_rows(old: list[dict], new: list[dict]) -> list[dict]:
    """Union two row lists by bucket open time; `new` wins on conflicts."""
    merged = {r["t"]: r for r in old}
    merged.update({r["t"]: r for r in new})
    return [merged[k] for k in sorted(merged)]


@dataclass
class _Account:
    balance: float = DEMO_START_BALANCE
    equity: float = DEMO_START_BALANCE
    currency: str = "USD"
    leverage: int = 100


# Binance public market-data mirrors (D-032). data-api.binance.vision is
# Binance's official key-less data endpoint — it serves the identical REST
# shape and is NOT subject to the 451 "Unsupported Security Policy" geo
# blocks that api.binance.com applies to many datacenter/AWS egress IPs
# (Railway, Fly, AWS Lambda...). The classic domain stays as mirror #2.
BINANCE_BASES_DEFAULT = (
    "https://data-api.binance.vision",
    "https://api.binance.com",
)


class MarketFeed:
    """Owns provider polling + candle caches.

    One feed per process (the public market). Sibling paper planes share it,
    so every user's demo orders are priced off the identical live market.
    """

    def __init__(
        self,
        http_factory: HttpFactory | None = None,
        poll_seconds: float = 2.0,
        binance_base: str = "",
        binance_bases: Sequence[str] | None = None,
    ) -> None:
        self._http_factory = http_factory or _default_http_client
        self.poll_seconds = float(poll_seconds)
        # Mirror chain with a sticky "currently working" index (D-032):
        # try mirrors in rotation, remember the first one that answers.
        if binance_bases:
            bases = [b.rstrip("/") for b in binance_bases]
        else:
            bases = list(BINANCE_BASES_DEFAULT)
        if binance_base and binance_base.rstrip("/") not in bases:
            bases.insert(0, binance_base.rstrip("/"))
        self._binance_bases = bases or list(BINANCE_BASES_DEFAULT)
        self._binance_idx = 0

        self.tick: Tick | None = None
        self.provider = "init"  # init | binance | goldapi | degraded
        self.provider_detail = "waiting for first poll"
        self.last_data_monotonic = 0.0

        # Authoritative M1 (from the 2-row kline poll)
        self._m1_recent_closed: list[dict] = []
        self._m1_forming: dict | None = None
        # Tick-built M1 (always maintained — powers forming bars + degrade)
        self._m1_tick: dict[int, dict] = {}
        self._m1_tick_history: list[dict] = []
        # Per-TF closed-row caches
        self._tf_cache: dict[str, list[dict]] = {}
        self._tf_cache_ts: dict[str, float] = {}
        self._tf_refetch_ts: dict[str, float] = {}

        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._new_data: asyncio.Event = asyncio.Event()

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._poll_loop(), name="live-feed")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def data_age_s(self) -> float:
        if self.last_data_monotonic == 0.0:
            return float("inf")
        return time_mod.monotonic() - self.last_data_monotonic

    async def _poll_loop(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    await self.poll_once()
                except Exception:  # noqa: BLE001 — one bad cycle never kills the feed
                    logger.debug("live poll cycle failed", exc_info=True)
                await asyncio.sleep(self.poll_seconds)
        except asyncio.CancelledError:
            raise

    def _wake_listeners(self) -> None:
        evt = self._new_data
        self._new_data = asyncio.Event()
        evt.set()

    async def next_data(self, timeout: float) -> bool:
        """Wait for the next fresh poll result (True) or timeout (False)."""
        evt = self._new_data
        if evt.is_set():
            return True
        try:
            await asyncio.wait_for(evt.wait(), timeout)
            return True
        except TimeoutError:
            return False

    # -------------------------------------------------------------- providers

    async def _client(self) -> httpx.AsyncClient:
        """Fetch the http client (factory may return it directly or awaited)."""
        c = self._http_factory()
        if asyncio.iscoroutine(c) or hasattr(c, "__await__"):
            c = await c
        return c  # type: ignore[return-value]

    async def _binance_get(
        self, client: httpx.AsyncClient, path: str, params: dict, timeout: float
    ) -> httpx.Response:
        """GET a Binance endpoint with mirror failover (D-032).

        Tries the sticky mirror first, then rotates through the rest (e.g.
        data-api.binance.vision when api.binance.com answers 451 from a
        datacenter IP). The first mirror that responds becomes sticky so
        healthy polls never pay the failover latency.
        """
        last_exc: Exception | None = None
        bases = self._binance_bases
        for offset in range(len(bases)):
            idx = (self._binance_idx + offset) % len(bases)
            try:
                r = await client.get(f"{bases[idx]}{path}", params=params, timeout=timeout)
                r.raise_for_status()
                if idx != self._binance_idx:
                    logger.info("binance mirror switched to %s", bases[idx])
                    self._binance_idx = idx
                return r
            except Exception as exc:  # noqa: BLE001 — try the next mirror
                last_exc = exc
                logger.debug("binance mirror %s failed: %s", bases[idx], exc)
        assert last_exc is not None
        raise last_exc

    async def poll_once(self) -> bool:
        """One provider cycle (binance -> gold-api). True when data arrived."""
        try:
            await self._poll_binance()
            return True
        except Exception as exc:  # noqa: BLE001 — try the fallback
            logger.debug("binance poll failed: %s", exc)
        try:
            await self._poll_goldapi()
            return True
        except Exception as exc:  # noqa: BLE001
            logger.debug("gold-api poll failed: %s", exc)
        self.provider = "degraded"
        self.provider_detail = "all free providers unreachable — retrying"
        return False

    async def _poll_binance(self) -> None:
        client = await self._client()
        r = await self._binance_get(
            client, "/api/v3/ticker/bookTicker", {"symbol": "PAXGUSDT"}, 4.0
        )
        d = r.json()
        bid, ask = float(d["bidPrice"]), float(d["askPrice"])
        if bid <= 0 or ask < bid:
            raise DataSourceError(f"binance bookTicker invalid: {d!r}")
        # Authoritative forming M1 (klines weight 2 — cheap alongside quotes)
        try:
            rk = await self._binance_get(
                client,
                "/api/v3/klines",
                {"symbol": "PAXGUSDT", "interval": "1m", "limit": 2},
                4.0,
            )
            closed, forming = _parse_binance_klines(rk.json())
            if closed:
                self._m1_recent_closed = closed[-2:]
            self._m1_forming = forming
        except Exception:  # noqa: BLE001 — quotes are the critical path
            logger.debug("binance m1 klines failed (quotes still live)", exc_info=True)
        self._apply_tick(
            bid, ask, datetime.now(tz=UTC),
            provider="binance",
            detail="Binance PAXG/USDT (tokenized gold · 24/7)",
        )

    async def _poll_goldapi(self) -> None:
        client = await self._client()
        r = await client.get("https://api.gold-api.com/price/XAU", timeout=4.0)
        r.raise_for_status()
        d = r.json()
        price = float(d.get("price", 0))
        if price <= 0:
            raise DataSourceError(f"gold-api invalid payload: {d!r}")
        self._apply_tick(
            price - GOLD_API_SPREAD / 2, price + GOLD_API_SPREAD / 2,
            datetime.now(tz=UTC),
            provider="goldapi",
            detail="gold-api.com XAU spot (synthetic spread)",
        )

    def _apply_tick(
        self, bid: float, ask: float, now: datetime, provider: str, detail: str
    ) -> None:
        self.tick = Tick(bid=bid, ask=ask, time=now)
        self.provider = provider
        self.provider_detail = detail
        self.last_data_monotonic = time_mod.monotonic()

        # Maintain the tick-built M1 series (forming + rolled-closed history).
        mid = (bid + ask) / 2
        bucket = int(now.timestamp() // 60) * 60
        cur = self._m1_tick.pop(bucket, None)
        if cur is None:
            # bucket rolled — close everything older than the new bucket
            for old_t in [t for t in self._m1_tick if t < bucket]:
                self._m1_tick_history.append(self._m1_tick.pop(old_t))
            self._m1_tick_history = self._m1_tick_history[-M1_TICK_KEEP:]
            self._m1_tick[bucket] = {"t": bucket, "o": mid, "h": mid, "l": mid, "c": mid, "v": 1}
        else:
            cur["h"] = max(cur["h"], mid)
            cur["l"] = min(cur["l"], mid)
            cur["c"] = mid
            cur["v"] += 1
            self._m1_tick[bucket] = cur
        self._wake_listeners()

    # ------------------------------------------------------------ tf requests

    async def ensure_tf(self, tf: str, min_count: int) -> tuple[list[dict], dict | None]:
        """(closed_rows, forming_row) for `tf` — cached, provider-failed-over."""
        tf_min = validate_tf(tf)
        now = time_mod.time()
        closed = self._tf_cache.get(tf, [])
        last_t = closed[-1]["t"] if closed else None
        # The bucket that JUST closed — a cache missing it is stale (the
        # bar-close reconcile path depends on this exactness).
        want_t = (int(now // (tf_min * 60)) - 1) * tf_min * 60
        age = now - self._tf_cache_ts.get(tf, 0.0)
        refetch_ok = now - self._tf_refetch_ts.get(tf, 0.0) >= MIN_REFETCH_S
        need = (
            age >= CACHE_TTL_S
            or len(closed) < min_count
            or (last_t != want_t and refetch_ok)
        )
        if need:
            self._tf_refetch_ts[tf] = now
            fetched: list[dict] | None = None
            try:
                fetched = await self._fetch_binance_tf(tf, min_count)
            except Exception as exc:  # noqa: BLE001
                logger.debug("binance tf fetch failed (%s): %s", tf, exc)
            if fetched is None:
                try:
                    fetched = await self._fetch_yahoo_tf(tf, min_count)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("yahoo tf fetch failed (%s): %s", tf, exc)
            if fetched is not None:
                if fetched:
                    self._tf_cache[tf] = _merge_rows(closed, fetched)[
                        -max(min_count, 600):
                    ]
                self._tf_cache_ts[tf] = now
            else:
                # Last resort: top up from the tick-built series so charts
                # keep filling in while every history provider is down.
                tick_built = self._tick_built_closed(tf)
                if tick_built:
                    self._tf_cache[tf] = _merge_rows(closed, tick_built)[
                        -max(min_count, 600):
                    ]
                self._tf_cache_ts[tf] = now  # rate-cap while degraded
        closed = self._tf_cache.get(tf, [])
        forming = self._authoritative_forming(tf) or self._tick_forming(tf)
        return closed, forming

    async def _fetch_binance_tf(self, tf: str, min_count: int) -> list[dict]:
        client = await self._client()
        limit = min(1000, max(min_count + 1, 120))
        r = await self._binance_get(
            client,
            "/api/v3/klines",
            {
                "symbol": "PAXGUSDT",
                "interval": BINANCE_INTERVALS[tf],
                "limit": limit,
            },
            6.0,
        )
        closed, _ = _parse_binance_klines(r.json())
        return closed

    async def _fetch_yahoo_tf(self, tf: str, min_count: int) -> list[dict]:
        client = await self._client()
        interval, rng = YAHOO_SPECS[tf]
        r = await client.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/GC=F",
            params={"interval": interval, "range": rng},
            headers={"User-Agent": "Mozilla/5.0 (xauusd-platform)"},
            timeout=6.0,
        )
        r.raise_for_status()
        d = r.json()
        result = (d.get("chart", {}).get("result") or [None])[0]
        if not result:
            raise DataSourceError("yahoo chart: empty result")
        ts = result.get("timestamp") or []
        quote = (result.get("indicators", {}).get("quote") or [{}])[0]
        opens = quote.get("open") or []
        highs = quote.get("high") or []
        lows = quote.get("low") or []
        closes = quote.get("close") or []
        vols = quote.get("volume") or []
        rows: list[dict] = []
        for i, t in enumerate(ts):
            try:
                if None in (opens[i], highs[i], lows[i], closes[i]):
                    continue
                rows.append({
                    "t": int(t), "o": float(opens[i]), "h": float(highs[i]),
                    "l": float(lows[i]), "c": float(closes[i]),
                    "v": int(vols[i] or 0),
                })
            except (IndexError, TypeError, ValueError):
                continue
        if tf == "H4":
            rows = _aggregate_rows(rows, 240)
        tf_min = TIMEFRAME_MINUTES[tf]
        now = time_mod.time()
        return [r_ for r_ in rows if r_["t"] + tf_min * 60 <= now]

    # ------------------------------------------------------- forming builders

    def _authoritative_forming(self, tf: str) -> dict | None:
        """Binance's own forming M1 candle, when the poll cache is current."""
        if tf != "M1" or self._m1_forming is None:
            return None
        bucket = int(time_mod.time() // 60) * 60
        if self._m1_forming["t"] != bucket:
            return None
        return self._m1_forming

    def _tick_built_closed(self, tf: str) -> list[dict]:
        """Closed tf-buckets aggregated from the tick-built M1 series."""
        tf_min = TIMEFRAME_MINUTES[tf]
        now_bucket = int(time_mod.time() // (tf_min * 60))
        out: dict[int, dict] = {}
        for src in (self._m1_tick_history, list(self._m1_tick.values())):
            for r in src:
                b = r["t"] // (tf_min * 60)
                if b >= now_bucket:
                    continue
                key = b * tf_min * 60
                cur = out.get(key)
                if cur is None:
                    out[key] = dict(r, t=key)
                else:
                    cur["h"] = max(cur["h"], r["h"])
                    cur["l"] = min(cur["l"], r["l"])
                    cur["c"] = r["c"]
                    cur["v"] += r["v"]
        return [out[k] for k in sorted(out)]

    def _tick_forming(self, tf: str) -> dict | None:
        """Forming tf-bar from tick-built M1 rows + the live tick."""
        if self.tick is None:
            return None
        tf_min = TIMEFRAME_MINUTES[tf]
        bucket = int(time_mod.time() // (tf_min * 60)) * tf_min * 60
        mids = [
            r for r in self._m1_tick_history + list(self._m1_tick.values())
            if r["t"] >= bucket
        ]
        mid = (self.tick.bid + self.tick.ask) / 2
        if not mids:
            return {"t": bucket, "o": mid, "h": mid, "l": mid, "c": mid, "v": 1}
        return {
            "t": bucket,
            "o": mids[0]["o"],
            "h": max(max(r["h"] for r in mids), mid),
            "l": min(min(r["l"] for r in mids), mid),
            "c": mid,
            "v": sum(r["v"] for r in mids) + 1,
        }


class LiveDataSource(DataSource):
    """REAL-TIME free-API gold market + paper account (DATA_SOURCE=live).

    Shares one `MarketFeed` with its siblings so per-user demo planes price
    orders off the identical public market (agent architecture, Phase 4).
    """

    SYMBOL = "XAUUSD"

    def __init__(
        self,
        http_factory: HttpFactory | None = None,
        poll_seconds: float = 2.0,
        binance_base: str = "",
        binance_bases: Sequence[str] | None = None,
        market: MarketFeed | None = None,
        starting_balance: float = DEMO_START_BALANCE,
        connect_timeout_s: float = 10.0,
    ) -> None:
        self.market = market or MarketFeed(
            http_factory=http_factory,
            poll_seconds=poll_seconds,
            binance_base=binance_base,
            binance_bases=binance_bases,
        )
        self._owns_market = market is None
        self._connect_timeout_s = float(connect_timeout_s)
        self._connected = False
        self._account = _Account(balance=starting_balance, equity=starting_balance)
        self._positions: list[Position] = []
        self._next_ticket = 100_000

    # ------------------------------------------------------------- lifecycle

    @classmethod
    def sibling(cls, public: LiveDataSource) -> LiveDataSource:
        """A paper plane sharing the public live market (same prices)."""
        return cls(market=public.market)

    def set_starting_balance(self, balance: float) -> None:
        self._account.balance = float(balance)
        self._account.equity = float(balance)

    async def connect(self, creds: dict) -> dict:
        if not self.market.running:
            await self.market.start()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._connect_timeout_s
        while self.market.tick is None and loop.time() < deadline:
            await asyncio.sleep(0.2)
        if self.market.tick is None and not await self.market.poll_once():
            if self._owns_market:
                await self.market.stop()
            raise DataSourceError(
                "no live market provider reachable (binance + gold-api)"
            )
        self._connected = True
        logger.info(
            "live market connected — provider=%s price=%.2f",
            self.market.provider,
            (self.market.tick.bid + self.market.tick.ask) / 2,
        )
        return {
            "login": "REALTIME",
            "server": f"LiveMarket • {self.market.provider}",
            "balance": self._account.balance,
            "equity": self._account.equity,
            "currency": self._account.currency,
            "leverage": self._account.leverage,
        }

    async def disconnect(self) -> None:
        self._connected = False
        if self._owns_market:
            await self.market.stop()

    async def is_connected(self) -> bool:
        return (
            self._connected
            and self.market.running
            and self.market.data_age_s < STALE_DATA_S
        )

    async def quick_check(self) -> bool:
        """One provider cycle — boot-time live-vs-mock decision (D-030)."""
        try:
            return await self.market.poll_once()
        except Exception:  # noqa: BLE001
            return False

    # ---------------------------------------------------------- market data

    async def get_rates(self, symbol: str, tf: str, count: int) -> pd.DataFrame:
        closed, _ = await self.market.ensure_tf(tf, count)
        rows = closed[-count:]
        if not rows:
            return empty_rates()
        return pd.DataFrame(
            {
                "time_utc": [datetime.fromtimestamp(r["t"], tz=UTC) for r in rows],
                "o": [r["o"] for r in rows],
                "h": [r["h"] for r in rows],
                "l": [r["l"] for r in rows],
                "c": [r["c"] for r in rows],
                "v": [int(r["v"]) for r in rows],
            },
            columns=RATES_COLUMNS,
        )

    async def get_forming_bar(self, symbol: str, tf: str) -> dict | None:
        _, forming = await self.market.ensure_tf(tf, 2)
        return forming

    async def get_tick(self, symbol: str) -> Tick:
        if (
            self.market.tick is None
            or self.market.data_age_s > max(3.0, self.market.poll_seconds * 1.5)
        ):
            try:
                await self.market.poll_once()
            except Exception as exc:  # noqa: BLE001
                raise DataSourceError(f"live quote unavailable: {exc}") from exc
        tick = self.market.tick
        if tick is None:
            raise DataSourceError("no live market data yet")
        return tick

    def subscribe_ticks(self, symbol: str) -> AsyncIterator[Tick]:
        return self._tick_stream(symbol)

    async def _tick_stream(self, symbol: str) -> AsyncIterator[Tick]:
        try:
            if self.market.tick is None:
                try:
                    await self.market.poll_once()
                except Exception:  # noqa: BLE001 — the loop below retries anyway
                    pass
            while True:
                tick = self.market.tick
                if tick is not None:
                    yield tick
                await self.market.next_data(
                    timeout=max(2.0, self.market.poll_seconds * 2)
                )
        except asyncio.CancelledError:
            return

    def discover_symbols(self, pattern: str = "*XAUUSD*") -> list[str]:
        return [self.SYMBOL]

    def symbol_info(self, symbol: str) -> SymbolInfo:
        return LIVE_SYMBOL_INFO

    def point_size(self, symbol: str) -> float:
        return LIVE_SYMBOL_INFO.point

    # ------------------------------------------------------- paper trading

    def feed_status(self) -> dict:
        """Live-feed transparency (user req #5) — surfaced in status + WS."""
        t = self.market.tick
        return {
            "provider": self.market.provider,
            "detail": self.market.provider_detail,
            "last_price": round((t.bid + t.ask) / 2, 2) if t else None,
            "spread": round(t.ask - t.bid, 2) if t else None,
            "last_tick_age_s": (
                round(self.market.data_age_s, 1)
                if self.market.last_data_monotonic > 0 else None
            ),
        }

    def _floating_profit(self, p: Position, bid: float, ask: float) -> float:
        exit_price = bid if p.side == "BUY" else ask
        direction = 1.0 if p.side == "BUY" else -1.0
        return (exit_price - p.price_open) * direction * 100.0 * p.volume

    async def place_order(self, order: Order) -> OrderResult:
        if not self._connected:
            return OrderResult(ok=False, retcode=0, comment="live: not connected")
        try:
            tick = await self.get_tick(order.symbol)
        except DataSourceError as exc:
            return OrderResult(ok=False, retcode=0, comment=f"live: {exc}")
        price = tick.ask if order.side == "BUY" else tick.bid
        self._next_ticket += 1
        self._positions.append(
            Position(
                ticket=self._next_ticket,
                symbol=order.symbol,
                side=order.side,
                volume=order.volume,
                price_open=price,
                sl=order.sl,
                tp=order.tp,
                profit=0.0,
                time=tick.time,
            )
        )
        return OrderResult(
            ok=True, ticket=self._next_ticket, price=price,
            retcode=TRADE_RETCODE_DONE, comment="live paper fill",
        )

    async def get_positions(self) -> list[Position]:
        if not self._positions:
            return []
        tick = self.market.tick
        out: list[Position] = []
        for p in self._positions:
            if tick is not None:
                out.append(
                    dataclasses_replace(
                        p, profit=self._floating_profit(p, tick.bid, tick.ask)
                    )
                )
            else:
                out.append(p)
        return out

    async def close_position(self, ticket: int, deviation: int = 30) -> OrderResult:
        if not self._connected:
            return OrderResult(ok=False, retcode=0, comment="live: not connected")
        pos = next((p for p in self._positions if p.ticket == ticket), None)
        if pos is None:
            return OrderResult(
                ok=False, retcode=10036, comment="live: position not found"
            )
        tick = await self.get_tick(pos.symbol)
        exit_price = tick.bid if pos.side == "BUY" else tick.ask
        profit = self._floating_profit(pos, tick.bid, tick.ask)
        self._account.balance += profit
        self._account.equity = self._account.balance
        self._positions = [p for p in self._positions if p.ticket != ticket]
        return OrderResult(
            ok=True, ticket=ticket, price=exit_price,
            retcode=TRADE_RETCODE_DONE,
            comment=f"live paper close profit={profit:.2f}",
        )

    def account_info(self) -> dict | None:  # type: ignore[override]
        if not self._connected:
            return None
        balance = self._account.balance
        floating = 0.0
        tick = self.market.tick
        if tick is not None:
            for p in self._positions:
                floating += self._floating_profit(p, tick.bid, tick.ask)
        self._account.equity = balance + floating
        return {
            "login": "REALTIME",
            "server": f"LiveMarket • {self.market.provider}",
            "balance": self._account.balance,
            "equity": self._account.equity,
            "currency": self._account.currency,
            "leverage": self._account.leverage,
        }
