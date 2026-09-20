"""MarketStream — the tick-by-tick core (SPEC §7.2 events, Phase 2 AC).

Subscribes to the DataSource tick stream and:
- broadcasts `tick` events (bid/ask/ts ms),
- builds FORMING bars for every watched timeframe (client subscriptions ∪
  the engine timeframe) from ticks -> `bar_open` / `bar_update`,
- on bucket roll: reconciles the closed bar with the AUTHORITATIVE
  get_rates() bar (terminal-built OHLC wins over tick-built approximation),
  broadcasts `bar_close`, then fires the engine's on_bar_close callback,
- seeds a newly-watched TF's forming bar via get_forming_bar() so charts
  that subscribe mid-bucket don't show a partial candle.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.mt5.base import TIMEFRAME_MINUTES, DataSource, Tick

logger = logging.getLogger("xauusd.market")


@dataclass
class FormingBar:
    t: int  # bucket open, epoch seconds (UTC)
    o: float
    h: float
    low: float  # bar low ("l" on the wire)
    c: float
    v: int

    def as_dict(self) -> dict:
        return {"t": self.t, "o": self.o, "h": self.h, "l": self.low,
                "c": self.c, "v": self.v}


class MarketStream:
    def __init__(
        self,
        source: DataSource,
        hub: Any,
        symbol: str,
        engine_tf: str,
        point_size: float = 0.01,
    ) -> None:
        self._source = source
        self._hub = hub
        self._symbol = symbol
        self._engine_tf = engine_tf
        self._point = point_size
        self._forming: dict[str, FormingBar] = {}
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        # async callbacks (wired by EngineRuntime)
        self.on_bar_close: Any = None  # async (tf: str, bar: dict) -> None
        self.on_tick: Any = None  # async (tick: Tick) -> None

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="market-stream")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        self._forming.clear()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    # ------------------------------------------------------------- main loop

    async def _run(self) -> None:
        try:
            tick_iter = self._source.subscribe_ticks(self._symbol)
            async for tick in tick_iter:
                if self._stop.is_set():
                    break
                await self._handle_tick(tick)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — stream death is handled by runtime
            logger.exception("market stream died")
            raise

    async def _handle_tick(self, tick: Tick) -> None:
        await self._hub.broadcast_ticks(
            "tick",
            self._symbol,
            {"bid": round(tick.bid, 2), "ask": round(tick.ask, 2),
             "ts": int(tick.time.timestamp() * 1000)},
        )
        if self.on_tick is not None:
            await self.on_tick(tick)

        wanted = self._hub.subscribed_tfs() | {self._engine_tf}
        for tf in wanted:
            tf_min = TIMEFRAME_MINUTES[tf]
            bucket = int(tick.time.timestamp() // (tf_min * 60)) * tf_min * 60
            fb = self._forming.get(tf)
            if fb is None:
                fb = await self._seed_forming(tf, bucket, tick)
                self._forming[tf] = fb
                await self._hub.broadcast_market(
                    "bar_open", self._symbol, tf, {"candle": fb.as_dict()}
                )
            elif fb.t != bucket:
                await self._close_bar(tf, fb)
                nb = FormingBar(t=bucket, o=tick.bid, h=tick.bid, low=tick.bid, c=tick.bid, v=1)
                self._forming[tf] = nb
                await self._hub.broadcast_market(
                    "bar_open", self._symbol, tf, {"candle": nb.as_dict()}
                )
            else:
                fb.h = max(fb.h, tick.bid)
                fb.low = min(fb.low, tick.bid)
                fb.c = tick.bid
                fb.v += 1
                await self._hub.broadcast_market(
                    "bar_update", self._symbol, tf, {"candle": fb.as_dict()}
                )

    async def _seed_forming(self, tf: str, bucket: int, tick: Tick) -> FormingBar:
        """Mid-bucket subscribe: seed from the source's forming bar if available."""
        seeded = await self._safe_get_forming(tf)
        if seeded is not None and seeded["t"] == bucket:
            return FormingBar(
                t=seeded["t"], o=seeded["o"], h=seeded["h"],
                low=seeded["l"], c=seeded["c"], v=seeded["v"],
            )
        return FormingBar(t=bucket, o=tick.bid, h=tick.bid, low=tick.bid, c=tick.bid, v=1)

    async def _safe_get_forming(self, tf: str) -> dict | None:
        try:
            getter = getattr(self._source, "get_forming_bar", None)
            if getter is None:
                return None
            res = getter(self._symbol, tf)
            if asyncio.iscoroutine(res):
                res = await res
            return res
        except Exception:  # noqa: BLE001
            return None

    async def _close_bar(self, tf: str, fb: FormingBar) -> None:
        """Close + reconcile with the authoritative terminal bar, then notify."""
        bar = fb.as_dict()
        try:
            df = await self._source.get_rates(self._symbol, tf, 1)
            if len(df) == 1:
                t = int(df["time_utc"].iloc[-1].timestamp())
                if t == fb.t:
                    bar = {
                        "t": t,
                        "o": float(df["o"].iloc[-1]),
                        "h": float(df["h"].iloc[-1]),
                        "l": float(df["l"].iloc[-1]),
                        "c": float(df["c"].iloc[-1]),
                        "v": int(df["v"].iloc[-1]),
                    }
        except Exception:  # noqa: BLE001 — fall back to the tick-built bar
            logger.debug("authoritative reconcile failed for %s close", tf)
        self._forming.pop(tf, None)
        await self._hub.broadcast_market("bar_close", self._symbol, tf, {"candle": bar})
        if self.on_bar_close is not None:
            try:
                await self.on_bar_close(tf, bar)
            except Exception:  # noqa: BLE001 — engine errors must not kill the stream
                logger.exception("on_bar_close callback failed")


def bar_bucket(ts: datetime, tf_min: int) -> int:
    """UTC bucket-open epoch seconds for a timestamp."""
    return int(ts.replace(tzinfo=ts.tzinfo or UTC).timestamp() // (tf_min * 60)) * tf_min * 60
