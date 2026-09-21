"""Real-time market data — MT5 TERMINAL ONLY (D-037).

The user's FIXED directive (D-037, Bengali): "অ্যাপ এর ক্যান্ডেল ডেটা শুধু মাত্র
meta 5 থেকে আসবে, এটা ফিক্স" — candle/quote data comes ONLY from the real
MetaTrader 5 terminal (real broker prices through Exness). The former
crypto-composite fallback (D-035) is disabled by default:

- MT5_ONLY=1 (default): XAUUSDm + BTCUSDm broker feed only. When a symbol
  is closed (gold on weekends) the feed reports `market: "closed"` and
  serves the terminal's chart HISTORY (real broker bars, no synthesis).
  BTCUSDm is 24/7 so the platform stays fully alive every day of the week.
- MT5_ONLY=0: the D-035 behavior (crypto composite when the broker feed
  is idle) is available for hosts without a terminal.

Every price the chart shows is a REAL market price from MetaTrader 5 —
nothing is ever synthesized. When the terminal is unreachable the platform
shows "no feed" and keeps retrying (NEVER demo, D-033).

Market-state honesty (D-037): each symbol reports one of
  open   — the broker is actively streaming it (e.g. BTCUSDm 24/7, gold
           during forex hours)
  closed — the symbol ticked before but is idle now (gold Sat/Sun):
           the terminal still serves its chart HISTORY (real broker bars)
  unavailable — the terminal bridge itself is down
so weekends never break the chart: BTCUSD stays live, XAUUSD shows its
last real broker bars with a "market closed" chip.

Paper trading: the platform plane and per-user demo planes run paper
accounts priced off THIS feed — real prices, simulated fills.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time as time_mod
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from dataclasses import replace as dataclasses_replace
from datetime import UTC, datetime
from typing import Any

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
# Yahoo chart API (interval, range) per timeframe — gold = GC=F futures,
# BTC = BTC-USD spot.
YAHOO_GOLD = {
    "M1": ("1m", "5d"), "M5": ("5m", "1mo"), "M15": ("15m", "1mo"),
    "M30": ("30m", "1mo"), "H1": ("60m", "3mo"), "H4": ("60m", "3mo"),
    "D1": ("1d", "2y"),
}
YAHOO_BTC = {
    "M1": ("1m", "5d"), "M5": ("5m", "1mo"), "M15": ("15m", "1mo"),
    "M30": ("30m", "1mo"), "H1": ("60m", "3mo"), "H4": ("60m", "3mo"),
    "D1": ("1d", "2y"),
}

GOLD_API_SPREAD = 0.35  # synthetic XAUUSD spread for the quote fallback ($)
STALE_DATA_S = 90.0     # is_connected -> False past this quote age
CACHE_TTL_S = 15.0      # TF kline cache freshness

#: D-037 — candles/quotes come ONLY from the MetaTrader 5 terminal
#: (user's fixed directive). Set MT5_ONLY=0 to restore the D-035
#: crypto-composite fallback on hosts without a terminal.
MT5_ONLY = os.environ.get("MT5_ONLY", "1").strip().lower() not in {
    "0", "false", "no", "off",
}
MIN_REFETCH_S = 5.0     # forced-refetch rate cap (session gaps must not hammer)
M1_TICK_KEEP = 4320     # 3 days of tick-built M1 bars kept in memory
WS_REST_REFRESH_S = 30.0  # WS healthy: REST only refreshes klines this often

# Lot-sizing metadata mirrors a typical Exness standard account.
GOLD_SYMBOL_INFO = SymbolInfo(
    name="XAUUSD", point=0.01, contract_size=100.0,
    volume_min=0.01, volume_max=100.0, volume_step=0.01,
)
BTC_SYMBOL_INFO = SymbolInfo(
    name="BTCUSD", point=0.01, contract_size=1.0,
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


# =========================================================================
# SymbolFeedCore — all real-time data for ONE platform symbol (D-035)
# =========================================================================

@dataclass
class SymbolSpec:
    """Per-symbol provider configuration."""
    key: str                          # platform symbol ("XAUUSD" / "BTCUSD")
    binance_symbol: str               # Binance REST/WS symbol ("PAXGUSDT")
    ws_venues: Any                    # tuple[VenueConfig, ...] | None
    goldapi: bool = False             # gold-api.com quote fallback (gold only)
    yahoo_specs: dict | None = None   # Yahoo history fallback per TF
    yahoo_symbol: str = "GC=F"
    mt5_symbol: str = ""              # platform key for the MT5 overlay


GOLD_SPEC = SymbolSpec(
    key="XAUUSD",
    binance_symbol="PAXGUSDT",
    ws_venues="GOLD",  # resolved to tick_feed.VENUES at runtime
    goldapi=True,
    yahoo_specs=YAHOO_GOLD,
    yahoo_symbol="GC=F",
)


class SymbolFeedCore:
    """Quote + candle pipeline for one symbol with the MT5-first policy.

    Quote authority: MT5 terminal ticks while `mt5.fresh(key)`; otherwise the
    crypto composite (venue WS events + REST poll chain). Candle authority:
    MT5 chart history while fresh; otherwise Binance klines (+ Yahoo history
    + tick-built fallbacks).
    """

    def __init__(
        self,
        spec: SymbolSpec,
        http_factory: HttpFactory | None = None,
        poll_seconds: float = 2.0,
        binance_base: str = "",
        binance_bases: Sequence[str] | None = None,
        enable_ws: bool = True,
        ws_venues: Sequence[str] | None = None,
        mt5: Any = None,  # McpMarketFeed | None
        mt5_only: bool = MT5_ONLY,  # D-037: broker feed only, no composite
    ) -> None:
        self.spec = spec
        self._mt5_only = bool(mt5_only)
        self._http_factory = http_factory or _default_http_client
        self.poll_seconds = float(poll_seconds)
        # Mirror chain with a sticky "currently working" index (D-032)
        if binance_bases:
            bases = [b.rstrip("/") for b in binance_bases]
        else:
            bases = list(BINANCE_BASES_DEFAULT)
        if binance_base and binance_base.rstrip("/") not in bases:
            bases.insert(0, binance_base.rstrip("/"))
        self._binance_bases = bases or list(BINANCE_BASES_DEFAULT)
        self._binance_idx = 0

        self.tick: Tick | None = None
        self.provider = "init"  # init | mt5 | aggregate | binance | goldapi | degraded
        self.provider_detail = "waiting for first poll"
        self.last_data_monotonic = 0.0

        # Authoritative M1 (from the 2-row kline poll / MT5 chart history)
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

        # D-033 venue WS aggregate (per-symbol venue set) + D-035 MT5 overlay
        self._agg: Any = None
        self._enable_ws = bool(enable_ws)
        self._ws_venue_names = tuple(ws_venues) if ws_venues else None
        self._mt5 = mt5

    # ------------------------------------------------------------ mt5 policy

    def _mt5_fresh(self) -> bool:
        return self._mt5 is not None and self._mt5.fresh(self.spec.key)

    def on_mt5_tick(self, bid: float, ask: float, ts: float) -> None:
        """A REAL broker tick from the terminal -> authoritative quote."""
        now = datetime.fromtimestamp(ts, tz=UTC)
        self.tick = Tick(bid=bid, ask=ask, time=now)
        self.provider = "mt5"
        self.provider_detail = (
            f"MetaTrader 5 terminal · real broker feed ({self.spec.key})"
        )
        self.last_data_monotonic = time_mod.monotonic()
        mid = (bid + ask) / 2
        self._update_m1_tick(mid, 0.0, now)
        self._wake_listeners()

    def _on_ws_event(self, ev: Any) -> None:
        """One crypto market event (composite mode only, D-035).

        D-037 MT5_ONLY: never used — the broker feed is the single authority.
        """
        if self._mt5_only or self._mt5_fresh():
            return
        now = datetime.fromtimestamp(ev.ts, tz=UTC)
        self.tick = Tick(bid=ev.bid, ask=ev.ask, time=now)
        n_venues = self._agg.healthy_venue_count() if self._agg else 0
        self.provider = "aggregate"
        self.provider_detail = (
            f"{n_venues}-venue real-time feed "
            "(crypto composite — MT5 forex feed inactive)"
        )
        self.last_data_monotonic = time_mod.monotonic()
        mid = (ev.bid + ev.ask) / 2
        self._update_m1_tick(mid, ev.qty, now)
        self._wake_listeners()

    # ------------------------------------------------------------- lifecycle

    def _resolve_venues(self) -> Any:
        from app.mt5.tick_feed import BTC_VENUES, VENUES

        base = BTC_VENUES if self.spec.ws_venues == "BTC" else VENUES
        if self._ws_venue_names is None:
            return base
        return tuple(v for v in base if v.name in self._ws_venue_names)

    def _make_aggregator(self) -> Any:
        from app.mt5.tick_feed import TickAggregator

        return TickAggregator(
            on_event=self._on_ws_event,
            venues=self._resolve_venues(),
        )

    async def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._task = asyncio.create_task(
            self._poll_loop(), name=f"live-feed-{self.spec.key}"
        )
        if self._enable_ws and not self._mt5_only and self._resolve_venues():
            self._agg = self._make_aggregator()
            await self._agg.start()
            logger.info(
                "%s: crypto aggregate started — %d venue stream(s)",
                self.spec.key, len(self._agg.states),
            )

    async def stop(self) -> None:
        self._stop.set()
        if self._agg is not None:
            await self._agg.stop()
            self._agg = None
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
                    logger.debug("%s poll cycle failed", self.spec.key, exc_info=True)
                # MT5 or WS healthy -> REST only augments (30s)
                delay = (
                    WS_REST_REFRESH_S
                    if (self._mt5_fresh() or self._ws_healthy())
                    else self.poll_seconds
                )
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise

    def _wake_listeners(self) -> None:
        evt = self._new_data
        self._new_data = asyncio.Event()
        evt.set()

    async def next_data(self, timeout: float) -> bool:
        """Wait for the next fresh event (True) or timeout (False)."""
        evt = self._new_data
        if evt.is_set():
            return True
        try:
            await asyncio.wait_for(evt.wait(), timeout)
            return True
        except TimeoutError:
            return False

    async def tick_stream(self) -> AsyncIterator[Tick]:
        """Every quote update for this symbol (MT5 or crypto authority)."""
        try:
            if self.tick is None:
                try:
                    await self.poll_once()
                except Exception:  # noqa: BLE001 — the loop below retries anyway
                    pass
            while True:
                tick = self.tick
                if tick is not None:
                    yield tick
                await self.next_data(
                    timeout=max(2.0, self.poll_seconds * 2)
                )
        except asyncio.CancelledError:
            return

    # -------------------------------------------------------------- providers

    def _ws_healthy(self) -> bool:
        return self._agg is not None and self._agg.healthy_venue_count() > 0

    async def _client(self) -> httpx.AsyncClient:
        c = self._http_factory()
        if asyncio.iscoroutine(c) or hasattr(c, "__await__"):
            c = await c
        return c  # type: ignore[return-value]

    async def _binance_get(
        self, client: httpx.AsyncClient, path: str, params: dict, timeout: float
    ) -> httpx.Response:
        """GET a Binance endpoint with mirror failover (D-032)."""
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
        """One provider cycle. True when the feed has data.

        D-037 MT5_ONLY: the broker feed drives everything; when it is idle
        (market closed) the state is reported honestly — never substituted.
        """
        if self._mt5_only:
            if self._mt5_fresh():
                return True
            if self.tick is not None:
                # real broker price known, market currently idle (closed)
                self.provider = "mt5"
                self.provider_detail = (
                    f"MetaTrader 5 · {self.spec.key} market closed "
                    "(weekend/session) — broker history still served"
                )
                return True
            self.provider = "degraded"
            self.provider_detail = (
                "MetaTrader 5 terminal not streaming this symbol yet "
                "(closed or unavailable) — no fallback (MT5-only, D-037)"
            )
            return False
        if self._mt5_fresh():
            return True
        ws = self._ws_healthy()
        try:
            await self._poll_binance()
            return True
        except Exception as exc:  # noqa: BLE001 — try the fallback
            logger.debug("binance poll failed: %s", exc)
        if ws:
            return True  # the WS aggregate carries the feed
        if self.spec.goldapi:
            try:
                await self._poll_goldapi()
                return True
            except Exception as exc:  # noqa: BLE001
                logger.debug("gold-api poll failed: %s", exc)
        self.provider = "degraded"
        self.provider_detail = "all providers unreachable — retrying (never demo data)"
        return False

    async def _poll_binance(self) -> None:
        client = await self._client()
        bid = ask = None
        if not self._ws_healthy():
            r = await self._binance_get(
                client, "/api/v3/ticker/bookTicker",
                {"symbol": self.spec.binance_symbol}, 4.0,
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
                {"symbol": self.spec.binance_symbol, "interval": "1m", "limit": 2},
                4.0,
            )
            closed, forming = _parse_binance_klines(rk.json())
            if closed:
                self._m1_recent_closed = closed[-2:]
            self._m1_forming = forming
        except Exception:  # noqa: BLE001 — quotes are the critical path
            logger.debug("binance m1 klines failed (quotes still live)", exc_info=True)
        if bid is not None:
            self._apply_tick(
                bid, ask, datetime.now(tz=UTC),
                provider="binance",
                detail=f"Binance {self.spec.binance_symbol} (crypto composite · 24/7)",
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
        self, bid: float, ask: float, now: datetime, provider: str, detail: str,
        qty: float = 0.0,
    ) -> None:
        self.tick = Tick(bid=bid, ask=ask, time=now)
        if not (provider == "aggregate" and self._ws_healthy()):
            self.provider = provider
            self.provider_detail = detail
        self.last_data_monotonic = time_mod.monotonic()
        self._update_m1_tick((bid + ask) / 2, qty, now)
        self._wake_listeners()

    def _update_m1_tick(self, mid: float, qty: float, now: datetime) -> None:
        """Fold one real event into the tick-built M1 series (D-033)."""
        bucket = int(now.timestamp() // 60) * 60
        cur = self._m1_tick.pop(bucket, None)
        if cur is None:
            # bucket rolled — close everything older than the new bucket
            for old_t in [t for t in self._m1_tick if t < bucket]:
                self._m1_tick_history.append(self._m1_tick.pop(old_t))
            self._m1_tick_history = self._m1_tick_history[-M1_TICK_KEEP:]
            self._m1_tick[bucket] = {
                "t": bucket, "o": mid, "h": mid, "l": mid, "c": mid,
                "v": max(1, int(round(qty))) if qty > 0 else 1,
            }
        else:
            cur["h"] = max(cur["h"], mid)
            cur["l"] = min(cur["l"], mid)
            cur["c"] = mid
            cur["v"] += max(1, int(round(qty))) if qty > 0 else 1
            self._m1_tick[bucket] = cur

    # ------------------------------------------------------------ tf requests

    async def ensure_tf(self, tf: str, min_count: int) -> tuple[list[dict], dict | None]:
        """(closed_rows, forming_row) for `tf` — MT5 first, then the chain."""
        tf_min = validate_tf(tf)
        now = time_mod.time()
        closed = self._tf_cache.get(tf, [])
        last_t = closed[-1]["t"] if closed else None
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
            mt5_authoritative = False
            # 1) MT5 terminal chart history (real forex/broker candles, D-035;
            #    D-037: also while the market is CLOSED — the terminal still
            #    serves its chart history, so weekends keep real broker bars)
            if self._mt5 is not None and (self._mt5_fresh() or self._mt5_only):
                try:
                    rows, _ = await self._mt5.bars(self.spec.key, tf, min_count)
                    if rows:
                        fetched = rows
                        mt5_authoritative = True
                except Exception as exc:  # noqa: BLE001
                    logger.debug("mt5 bars fetch failed (%s %s): %s",
                                 self.spec.key, tf, exc)
            # 2) Binance klines (composite mode only — D-037)
            if fetched is None and not self._mt5_only:
                try:
                    fetched = await self._fetch_binance_tf(tf, min_count)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("binance tf fetch failed (%s): %s", tf, exc)
            # 3) Yahoo history fallback (composite mode only — D-037)
            if fetched is None and not self._mt5_only and self.spec.yahoo_specs:
                try:
                    fetched = await self._fetch_yahoo_tf(tf, min_count)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("yahoo tf fetch failed (%s): %s", tf, exc)
            if fetched is not None:
                if fetched:
                    if mt5_authoritative:
                        # Broker candles REPLACE the cache — different price
                        # basis than the composite (never mix the two series);
                        # the terminal carries years of history itself.
                        self._tf_cache[tf] = fetched[-max(min_count, 600):]
                    else:
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
                "symbol": self.spec.binance_symbol,
                "interval": BINANCE_INTERVALS[tf],
                "limit": limit,
            },
            6.0,
        )
        closed, _ = _parse_binance_klines(r.json())
        return closed

    async def _fetch_yahoo_tf(self, tf: str, min_count: int) -> list[dict]:
        if not self.spec.yahoo_specs or tf not in self.spec.yahoo_specs:
            return []
        client = await self._client()
        interval, rng = self.spec.yahoo_specs[tf]
        r = await client.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{self.spec.yahoo_symbol}",
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
        """Provider-native forming bar (MT5 any-TF, else Binance M1)."""
        if self._mt5_fresh() and self._mt5 is not None:
            try:
                return self._mt5.forming(self.spec.key, tf)
            except Exception:  # noqa: BLE001
                return None
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


# =========================================================================
# MarketFeed — the container: symbol cores + MT5 overlay + aggregators
# =========================================================================

class MarketFeed:
    """Owns provider polling + candle caches for every platform symbol.

    One feed per process (the public market). Sibling paper planes share it,
    so every user's demo orders are priced off the identical live market.
    Back-compat: attribute access proxies to the GOLD core (D-030/D-033
    tests + callers); multi-symbol callers use `feed_for()` / `ensure_tf_sym()`.
    """

    #: platform symbols served (D-035: XAUUSD + the new BTCUSD pair)
    SYMBOLS = ("XAUUSD", "BTCUSD")

    def __init__(
        self,
        http_factory: HttpFactory | None = None,
        poll_seconds: float = 2.0,
        binance_base: str = "",
        binance_bases: Sequence[str] | None = None,
        enable_ws: bool = True,
        ws_venues: Sequence[str] | None = None,
        mcp_market: Any = None,  # McpMarketFeed | None (D-035)
        mt5_only: bool = MT5_ONLY,  # D-037: broker feed only, no composite
    ) -> None:
        # D-035 — the REAL broker overlay (None when the terminal is absent,
        # e.g. Railway: crypto composite only, zero behavior change).
        self.mcp = mcp_market
        if self.mcp is None:
            try:
                from app.mt5.mcp_market import mcp_market_available

                if mcp_market_available():
                    from app.mt5.mcp import terminal_client
                    from app.mt5.mcp_market import McpMarketFeed

                    self.mcp = McpMarketFeed(
                        client=terminal_client(), watch=list(self.SYMBOLS),
                        on_tick=self._route_mt5_tick,
                    )
            except Exception:  # noqa: BLE001 — overlay is strictly optional
                self.mcp = None

        btc_spec = SymbolSpec(
            key="BTCUSD", binance_symbol="BTCUSDT", ws_venues="BTC",
            goldapi=False, yahoo_specs=YAHOO_BTC, yahoo_symbol="BTC-USD",
        )
        self.feeds: dict[str, SymbolFeedCore] = {
            spec.key: SymbolFeedCore(
                spec=spec,
                http_factory=http_factory,
                poll_seconds=poll_seconds,
                binance_base=binance_base,
                binance_bases=binance_bases,
                enable_ws=enable_ws,
                ws_venues=ws_venues if spec.key == "XAUUSD" else None,
                mt5=self.mcp,
                mt5_only=mt5_only,
            )
            for spec in (GOLD_SPEC, btc_spec)
        }

    def _route_mt5_tick(self, key: str, bid: float, ask: float, ts: float) -> None:
        feed = self.feeds.get(key)
        if feed is not None:
            feed.on_mt5_tick(bid, ask, ts)

    # ------------------------------------------------------ symbol routing

    def feed_for(self, symbol: str | None) -> SymbolFeedCore:
        """Resolve a (possibly broker-suffixed) symbol to its feed core."""
        if symbol:
            if symbol in self.feeds:
                return self.feeds[symbol]
            base = symbol.rstrip("m.")  # XAUUSDm / BTCUSDm broker suffixes
            if base in self.feeds:
                return self.feeds[base]
            for key in self.feeds:
                if symbol.startswith(key):
                    return self.feeds[key]
        return self.feeds["XAUUSD"]

    async def ensure_tf_sym(
        self, symbol: str, tf: str, min_count: int
    ) -> tuple[list[dict], dict | None]:
        return await self.feed_for(symbol).ensure_tf(tf, min_count)

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        if self.mcp is not None and not self.mcp.running:
            await self.mcp.start()
        for feed in self.feeds.values():
            await feed.start()

    async def stop(self) -> None:
        for feed in self.feeds.values():
            await feed.stop()
        if self.mcp is not None:
            await self.mcp.stop()

    @property
    def running(self) -> bool:
        return self.feeds["XAUUSD"].running

    # ------------------------------------------- gold-core proxies (compat)

    @property
    def tick(self) -> Tick | None:
        return self.feeds["XAUUSD"].tick

    @property
    def provider(self) -> str:
        return self.feeds["XAUUSD"].provider

    @property
    def provider_detail(self) -> str:
        return self.feeds["XAUUSD"].provider_detail

    @property
    def last_data_monotonic(self) -> float:
        return self.feeds["XAUUSD"].last_data_monotonic

    @last_data_monotonic.setter
    def last_data_monotonic(self, v: float) -> None:
        self.feeds["XAUUSD"].last_data_monotonic = v

    @property
    def _m1_tick(self) -> dict[int, dict]:
        return self.feeds["XAUUSD"]._m1_tick  # noqa: SLF001 — compat proxy

    @_m1_tick.setter
    def _m1_tick(self, v: dict[int, dict]) -> None:
        self.feeds["XAUUSD"]._m1_tick = v  # noqa: SLF001

    @property
    def _m1_tick_history(self) -> list[dict]:
        return self.feeds["XAUUSD"]._m1_tick_history  # noqa: SLF001

    @_m1_tick_history.setter
    def _m1_tick_history(self, v: list[dict]) -> None:
        self.feeds["XAUUSD"]._m1_tick_history = v  # noqa: SLF001

    @property
    def _m1_forming(self) -> dict | None:
        return self.feeds["XAUUSD"]._m1_forming  # noqa: SLF001

    @property
    def _tf_cache(self) -> dict[str, list[dict]]:
        return self.feeds["XAUUSD"]._tf_cache  # noqa: SLF001 — compat proxy

    @property
    def _tf_cache_ts(self) -> dict[str, float]:
        return self.feeds["XAUUSD"]._tf_cache_ts  # noqa: SLF001

    @property
    def _tf_refetch_ts(self) -> dict[str, float]:
        return self.feeds["XAUUSD"]._tf_refetch_ts  # noqa: SLF001

    @_tf_refetch_ts.setter
    def _tf_refetch_ts(self, v: dict[str, float]) -> None:
        self.feeds["XAUUSD"]._tf_refetch_ts = v  # noqa: SLF001

    @property
    def data_age_s(self) -> float:
        return self.feeds["XAUUSD"].data_age_s

    async def poll_once(self) -> bool:
        """One provider cycle for every symbol; returns the GOLD verdict."""
        gold = True
        try:
            gold = await self.feeds["XAUUSD"].poll_once()
        except Exception:  # noqa: BLE001
            gold = False
        try:
            await self.feeds["BTCUSD"].poll_once()
        except Exception:  # noqa: BLE001 — BTC failure never breaks gold
            logger.debug("btc poll cycle failed", exc_info=True)
        return gold

    async def ensure_tf(self, tf: str, min_count: int) -> tuple[list[dict], dict | None]:
        return await self.feeds["XAUUSD"].ensure_tf(tf, min_count)

    async def next_data(self, timeout: float) -> bool:
        return await self.feeds["XAUUSD"].next_data(timeout)

    # -------------------------------------------------------------- status

    def _agg_tps(self) -> float | None:
        agg = self.feeds["XAUUSD"]._agg  # noqa: SLF001
        return round(agg.tps(), 1) if agg is not None else None

    def symbol_status(self, key: str) -> dict:
        """Per-symbol transparency block (D-035 + D-037 market state)."""
        f = self.feeds[key]
        t = f.tick
        mt5_on = f._mt5_fresh()  # noqa: SLF001
        st: dict = {
            "provider": f.provider,
            "detail": f.provider_detail,
            "mt5": mt5_on,
            "market": self._market_state(key),
            "last_price": round((t.bid + t.ask) / 2, 2) if t else None,
            "spread": round(t.ask - t.bid, 2) if t else None,
            "last_tick_age_s": (
                round(f.data_age_s, 1) if f.last_data_monotonic > 0 else None
            ),
        }
        if mt5_on and self.mcp is not None:
            st["tps"] = self.mcp.tps(key) or 0.0
        elif f._agg is not None:  # noqa: SLF001
            st["tps"] = round(f._agg.tps(), 1)  # noqa: SLF001
            st["venues"] = f._agg.stats()  # noqa: SLF001
        return st

    def _market_state(self, key: str) -> str:
        """D-037: open | closed | unavailable for one platform symbol."""
        state_fn = getattr(self.mcp, "market_state", None) if self.mcp else None
        if state_fn is not None:
            try:
                return state_fn(key)
            except Exception:  # noqa: BLE001 — status must never raise
                pass
        f = self.feeds.get(key)
        if f is None:
            return "unavailable"
        if f.last_data_monotonic > 0 and f.data_age_s < STALE_DATA_S:
            return "open"
        return "closed" if f.tick is not None else "unavailable"


# =========================================================================
# LiveDataSource
# =========================================================================

class LiveDataSource(DataSource):
    """REAL-TIME market + paper account (DATA_SOURCE=live).

    MT5 terminal first (real broker prices, D-035), crypto composite 24/7
    fallback; XAUUSD + BTCUSD pairs. Shares one `MarketFeed` with its
    siblings so per-user demo planes price orders off the identical public
    market (agent architecture, Phase 4).
    """

    SYMBOL = "XAUUSD"
    SYMBOLS = ("XAUUSD", "BTCUSD")
    SYMBOL_INFOS = {"XAUUSD": GOLD_SYMBOL_INFO, "BTCUSD": BTC_SYMBOL_INFO}

    def __init__(
        self,
        http_factory: HttpFactory | None = None,
        poll_seconds: float = 2.0,
        binance_base: str = "",
        binance_bases: Sequence[str] | None = None,
        market: MarketFeed | None = None,
        starting_balance: float = DEMO_START_BALANCE,
        connect_timeout_s: float = 15.0,
        enable_ws: bool = True,
        ws_venues: Sequence[str] | None = None,
        mcp_market: Any = None,
        mt5_only: bool = MT5_ONLY,  # D-037
    ) -> None:
        self.market = market or MarketFeed(
            http_factory=http_factory,
            poll_seconds=poll_seconds,
            binance_base=binance_base,
            binance_bases=binance_bases,
            enable_ws=enable_ws,
            ws_venues=ws_venues,
            mcp_market=mcp_market,
            mt5_only=mt5_only,
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
            # D-037 weekend-robust connect: gold may simply be CLOSED — the
            # platform is still "connected" while ANY symbol ticks (BTCUSDm
            # is 24/7) or the terminal serves its chart history.
            others = [k for k, f in self.market.feeds.items() if f.tick is not None]
            if others:
                self._connected = True
                logger.info(
                    "live market connected via %s (gold feed idle — market "
                    "closed; MT5-only D-037)", others,
                )
                return {
                    "login": "REALTIME",
                    "server": "MetaTrader 5 (gold closed — BTCUSD 24/7 live)",
                    "balance": self._account.balance,
                    "equity": self._account.equity,
                    "currency": self._account.currency,
                    "leverage": self._account.leverage,
                }
            if await self._mt5_bars_alive():
                self._connected = True
                logger.info(
                    "live market connected — terminal serves chart history "
                    "(market closed, MT5-only D-037)"
                )
                return {
                    "login": "REALTIME",
                    "server": "MetaTrader 5 (market closed — history live)",
                    "balance": self._account.balance,
                    "equity": self._account.equity,
                    "currency": self._account.currency,
                    "leverage": self._account.leverage,
                }
            if self._owns_market:
                await self.market.stop()
            raise DataSourceError(
                "MetaTrader 5 terminal not reachable — no market data, no "
                "demo fallback (MT5-only, D-037); retrying"
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
        if not (self._connected and self.market.running):
            return False
        if self.market.data_age_s < STALE_DATA_S:
            return True
        # D-037: gold closed (weekend) — still connected while any platform
        # symbol is fresh (BTCUSDm 24/7) or the terminal serves history
        for f in self.market.feeds.values():
            if f.last_data_monotonic > 0 and f.data_age_s < STALE_DATA_S:
                return True
        return await self._mt5_bars_alive()

    async def _mt5_bars_alive(self) -> bool:
        """Does the terminal still answer chart history? (closed-market OK)"""
        mcp = getattr(self.market, "mcp", None)
        if mcp is None:
            return False
        try:
            rows, _ = await mcp.bars("XAUUSD", "M1", 5)
            return bool(rows)
        except Exception:  # noqa: BLE001
            return False

    async def quick_check(self) -> bool:
        """One provider cycle — boot-time live-vs-mock decision (D-030)."""
        try:
            return await self.market.poll_once()
        except Exception:  # noqa: BLE001
            return False

    @property
    def platform_symbols(self) -> list[str]:
        """Symbols this source streams (ConnectionManager starts runtimes)."""
        return list(self.market.feeds.keys())

    # ---------------------------------------------------------- market data

    async def get_rates(self, symbol: str, tf: str, count: int) -> pd.DataFrame:
        closed, _ = await self.market.feed_for(symbol).ensure_tf(tf, count)
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
        _, forming = await self.market.feed_for(symbol).ensure_tf(tf, 2)
        return forming

    async def get_tick(self, symbol: str) -> Tick:
        feed = self.market.feed_for(symbol)
        if (
            feed.tick is None
            or feed.data_age_s > max(3.0, feed.poll_seconds * 1.5)
        ):
            try:
                await feed.poll_once()
            except Exception as exc:  # noqa: BLE001
                raise DataSourceError(f"live quote unavailable: {exc}") from exc
        tick = feed.tick
        if tick is None:
            raise DataSourceError("no live market data yet")
        return tick

    def subscribe_ticks(self, symbol: str) -> AsyncIterator[Tick]:
        return self.market.feed_for(symbol).tick_stream()

    def discover_symbols(self, pattern: str = "*XAUUSD*") -> list[str]:
        return [self.SYMBOL]

    def symbol_info(self, symbol: str) -> SymbolInfo:
        feed = self.market.feed_for(symbol)
        return self.SYMBOL_INFOS.get(feed.spec.key, GOLD_SYMBOL_INFO)

    def point_size(self, symbol: str) -> float:
        return self.symbol_info(symbol).point

    # ------------------------------------------------------- paper trading

    def feed_status(self) -> dict:
        """Live-feed transparency (user req #5) — surfaced in status + WS.

        Top-level block = the GOLD feed (back-compat); `symbols` carries the
        per-symbol D-035 view (incl. BTCUSD + MT5 authority flags) and `mt5`
        exposes the terminal overlay state for honest weekend badges.
        """
        gold = self.market.symbol_status("XAUUSD")
        st: dict = {
            "provider": gold["provider"],
            "detail": gold["detail"],
            "last_price": gold["last_price"],
            "spread": gold["spread"],
            "last_tick_age_s": gold["last_tick_age_s"],
        }
        if "tps" in gold:
            st["tps"] = gold["tps"]
        if "venues" in gold:
            st["venues"] = gold["venues"]
        st["symbols"] = {
            key: self.market.symbol_status(key) for key in self.market.feeds
        }
        if self.market.mcp is not None:
            st["mt5"] = self.market.mcp.status()
            if not gold.get("mt5"):
                st["note"] = (
                    "MT5 forex feed inactive (weekend/holiday or terminal down) "
                    "— live crypto composite active; MT5 resumes automatically"
                )
        return st

    def _contract_for(self, symbol: str) -> float:
        return self.symbol_info(symbol).contract_size

    def _floating_profit(self, p: Position, bid: float, ask: float) -> float:
        exit_price = bid if p.side == "BUY" else ask
        direction = 1.0 if p.side == "BUY" else -1.0
        return (exit_price - p.price_open) * direction * self._contract_for(p.symbol) * p.volume

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
        out: list[Position] = []
        for p in self._positions:
            feed = self.market.feed_for(p.symbol)
            tick = feed.tick
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
        for p in self._positions:
            feed = self.market.feed_for(p.symbol)
            tick = feed.tick
            if tick is not None:
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
