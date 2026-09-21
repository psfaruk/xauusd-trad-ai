"""MarketStream — the tick-by-tick core (SPEC §7.2 events, Phase 2 AC).

Subscribes to the DataSource tick stream and:
- broadcasts `tick` events (bid/ask/ts ms) — D-033: the feed now emits
  every REAL market event (20-50+/s in active sessions); WS frames are
  throttled to 10/s per client (latest-quote-wins) with `tps` + `n` fields
  so the UI shows the true event rate without re-rendering 50x/s,
- builds FORMING bars for every watched timeframe (client subscriptions ∪
  the engine timeframe) from EVERY tick (full tick resolution, D-033) ->
  `bar_open` / `bar_update` (throttled to 4/s per TF — charts look instant),
- on bucket roll: reconciles the closed bar with the AUTHORITATIVE
  get_rates() bar (terminal-built OHLC wins over tick-built approximation),
  broadcasts `bar_close`, then fires the engine's on_bar_close callback,
- seeds a newly-watched TF's forming bar via get_forming_bar() so charts
  that subscribe mid-bucket don't show a partial candle.
"""

from __future__ import annotations

import asyncio
import logging
import time as time_mod
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.mt5.base import TIMEFRAME_MINUTES, DataSource, Tick

logger = logging.getLogger("xauusd.market")

# D-033/D-039 — real-time broadcast budgets (the candle itself absorbs EVERY
# tick; these only cap how often frames hit each browser client). D-039 raised
# the frame rate for an instant, MT5-terminal feel: tick frames at up to 20/s
# and forming-bar frames at up to 10/s — beyond visual perception thresholds,
# while `n`+`tps` still disclose the full-resolution event rate.
TICK_SEND_MIN_S = 0.05   # ≤20 tick frames/s per client (latest quote wins)
BAR_SEND_MIN_S = 0.10    # ≤10 bar_update frames/s per TF (bar_open/close always go)
TPS_WINDOW_S = 5.0       # trailing window for the real events/sec metric


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
        # D-033 — broadcast throttling + real tick-rate meter
        self._last_tick_sent = 0.0
        self._ticks_since_send = 0
        self._last_bar_sent: dict[str, float] = {}
        self._event_times: deque[float] = deque(maxlen=512)
        # async callbacks (wired by EngineRuntime)
        self.on_bar_close: Any = None  # async (tf: str, bar: dict) -> None
        self.on_tick: Any = None  # async (tick: Tick) -> None

    def _tps(self) -> float:
        """Real feed events per second over the trailing window."""
        now = time_mod.monotonic()
        while self._event_times and now - self._event_times[0] > TPS_WINDOW_S:
            self._event_times.popleft()
        return len(self._event_times) / TPS_WINDOW_S

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
        now_mono = time_mod.monotonic()
        self._event_times.append(now_mono)
        self._ticks_since_send += 1
        mid = (tick.bid + tick.ask) / 2  # D-035: bars track the mid, not just bid

        # tick frames — high-rate (20/s cap), latest-quote-wins, REAL rate.
        if now_mono - self._last_tick_sent >= TICK_SEND_MIN_S:
            self._last_tick_sent = now_mono
            payload = {
                "bid": round(tick.bid, 2),
                "ask": round(tick.ask, 2),
                "ts": int(tick.time.timestamp() * 1000),
                "n": self._ticks_since_send,        # real events in this frame
                "tps": round(self._tps(), 1),       # real events/sec (5s window)
            }
            self._ticks_since_send = 0
            await self._hub.broadcast_ticks("tick", self._symbol, payload)

        # the engine + SL/TP tracker see EVERY real tick (full resolution)
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
                self._last_bar_sent[tf] = now_mono
            elif fb.t != bucket:
                await self._close_bar(tf, fb)
                nb = FormingBar(t=bucket, o=mid, h=mid, low=mid, c=mid, v=1)
                self._forming[tf] = nb
                await self._hub.broadcast_market(
                    "bar_open", self._symbol, tf, {"candle": nb.as_dict()}
                )
                self._last_bar_sent[tf] = now_mono
            else:
                fb.h = max(fb.h, mid)
                fb.low = min(fb.low, mid)
                fb.c = mid
                fb.v += 1
                # forming bar ALWAYS absorbs the tick; frames are throttled
                if now_mono - self._last_bar_sent.get(tf, 0.0) >= BAR_SEND_MIN_S:
                    self._last_bar_sent[tf] = now_mono
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
        """Close + reconcile with the authoritative bar, then notify.

        D-035: MERGE instead of replace — the authoritative OHLC wins on
        open/close/volume, but high/low never move backward (a late quote
        during the roll second must not shrink an already-seen extreme).
        """
        bar = fb.as_dict()
        try:
            df = await self._source.get_rates(self._symbol, tf, 1)
            if len(df) == 1:
                t = int(df["time_utc"].iloc[-1].timestamp())
                if t == fb.t:
                    auth = {
                        "t": t,
                        "o": float(df["o"].iloc[-1]),
                        "h": float(df["h"].iloc[-1]),
                        "l": float(df["l"].iloc[-1]),
                        "c": float(df["c"].iloc[-1]),
                        "v": int(df["v"].iloc[-1]),
                    }
                    bar = {
                        "t": t,
                        "o": auth["o"],
                        "h": max(auth["h"], fb.h),
                        "l": min(auth["l"], fb.low),
                        "c": auth["c"],
                        "v": max(auth["v"], fb.v),
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
