"""MarketStream — the tick-by-tick core (SPEC §7.2 events, Phase 2 AC).

Subscribes to the DataSource tick stream and:
- broadcasts `tick` events (bid/ask/ts ms) — D-033: the feed emits every
  REAL market event; WS frames are throttled to 20/s per client
  (latest-quote-wins) with `tps` + `n` fields so the UI shows the true
  event rate without re-rendering 50x/s,
- builds FORMING bars for every watched timeframe (client subscriptions ∪
  the engine timeframe) from EVERY tick (full tick resolution) ->
  `bar_open` / `bar_update` (throttled to 10/s per TF — charts look
  instant),
- on bucket roll: reconciles the closed bar with the AUTHORITATIVE
  get_rates() bar (terminal-built OHLC wins — D-042: authoritative-wins
  so interpolation noise can never survive a bar close),
  broadcasts `bar_close`, then fires the engine's on_bar_close callback,
- seeds a newly-watched TF's forming bar via get_forming_bar() so charts
  that subscribe mid-bucket don't show a partial candle.

D-042 — micro-tick interpolation (user directive: "candles must move
like the real market — send 10-20 ticks per second per candle; hard-code
the fill when the broker feed is limited"). The public MCP bridge
delivers real broker ticks at ~2-5/s (transport RTT); between real
ticks this module now emits DISPLAY-ONLY micro-ticks at 15 Hz:
- a bounded Ornstein-Uhlenbeck walk anchored to the last REAL mid,
  clamped well inside the live spread — the candle breathes exactly
  like a terminal chart,
- micro-ticks NEVER reach the engine, the signal tracker or the SL/TP
  monitors (those see every REAL tick, full resolution),
- the REAL forming bar (engine + bar_close) and the DISPLAY forming bar
  (WS broadcasts) are tracked separately; on close the terminal's own
  OHLC replaces the display values, so displayed history stays 100%
  real broker data,
- frames carry `"i": true` and `n: 0`; `tps` keeps counting REAL
  events only.
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
import time as time_mod
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.mt5.base import TIMEFRAME_MINUTES, DataSource, Tick

logger = logging.getLogger("xauusd.market")

# D-033/D-039 — real-time broadcast budgets (the candle itself absorbs EVERY
# tick; these only cap how often frames hit each browser client).
TICK_SEND_MIN_S = 0.05   # ≤20 tick frames/s per client (latest quote wins)
REAL_SEND_MIN_S = 0.20   # real-tick frames bypass the budget at ≥5/s (truth first)
BAR_SEND_MIN_S = 0.10    # ≤10 bar_update frames/s per TF (bar_open/close always go)
TPS_WINDOW_S = 5.0       # trailing window for the real events/sec metric

# D-042 — micro-tick interpolation (display-only candle fill)
INTERP_HZ = 15.0             # 15 micro-ticks/s (user spec: 10-20/s)
INTERP_MAX_GAP_S = 4.0       # stop filling when the real feed went quiet
INTERP_THETA = 0.55          # OU mean-reversion strength (per second)
INTERP_DEV_SPREAD = 0.60     # max deviation from the real mid, in spreads
INTERP_MIN_DEV = 0.02        # absolute floor of the clamp (quiet symbols)


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
        self._forming: dict[str, FormingBar] = {}   # REAL ticks only (engine)
        self._disp: dict[str, FormingBar] = {}      # display bar (real + micro)
        self._task: asyncio.Task | None = None
        self._interp_task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        # D-033 — broadcast throttling + real tick-rate meter
        self._last_tick_sent = 0.0
        self._last_real_sent = 0.0
        self._ticks_since_send = 0
        self._last_bar_sent: dict[str, float] = {}
        self._event_times: deque[float] = deque(maxlen=512)
        # D-042 — interpolation state (anchored to the last REAL tick)
        self._real_mid: float = 0.0
        self._real_spread: float = 0.0
        self._real_at: float = 0.0            # monotonic of the last real tick
        self._interp_dev: float = 0.0         # current OU deviation
        self._interp_sent: int = 0
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
        # D-042 — display fill loop (15 Hz between real ticks)
        self._interp_task = asyncio.create_task(
            self._interp_loop(), name="market-interp"
        )

    async def stop(self) -> None:
        self._stop.set()
        for t in (self._task, self._interp_task):
            if t is not None:
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        self._task = None
        self._interp_task = None
        self._forming.clear()
        self._disp.clear()

    @property
    def running(self) -> bool:
        alive = lambda t: t is not None and not t.done()  # noqa: E731
        return alive(self._task) or alive(self._interp_task)

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

        # D-042 — anchor the interpolation to this REAL quote
        self._real_mid = mid
        self._real_spread = max(tick.ask - tick.bid, 1e-9)
        self._real_at = now_mono

        # tick frames — REAL ticks get priority (bypass the shared budget
        # at >=5/s so the true quote can never be starved by display fill)
        if (
            now_mono - self._last_tick_sent >= TICK_SEND_MIN_S
            or now_mono - self._last_real_sent >= REAL_SEND_MIN_S
        ):
            self._last_tick_sent = now_mono
            self._last_real_sent = now_mono
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
                self._disp[tf] = FormingBar(t=fb.t, o=fb.o, h=fb.h,
                                            low=fb.low, c=fb.c, v=fb.v)
                await self._hub.broadcast_market(
                    "bar_open", self._symbol, tf, {"candle": fb.as_dict()}
                )
                self._last_bar_sent[tf] = now_mono
            elif fb.t != bucket:
                await self._close_bar(tf, fb)
                nb = FormingBar(t=bucket, o=mid, h=mid, low=mid, c=mid, v=1)
                self._forming[tf] = nb
                self._disp[tf] = FormingBar(t=bucket, o=mid, h=mid, low=mid,
                                            c=mid, v=1)
                await self._hub.broadcast_market(
                    "bar_open", self._symbol, tf, {"candle": nb.as_dict()}
                )
                self._last_bar_sent[tf] = now_mono
            else:
                fb.h = max(fb.h, mid)
                fb.low = min(fb.low, mid)
                fb.c = mid
                fb.v += 1
                # the display bar absorbs the real tick too
                db = self._disp.get(tf)
                if db is not None and db.t == bucket:
                    db.h = max(db.h, mid)
                    db.low = min(db.low, mid)
                    db.c = mid
                # forming bar ALWAYS absorbs the tick; frames are throttled
                if now_mono - self._last_bar_sent.get(tf, 0.0) >= BAR_SEND_MIN_S:
                    self._last_bar_sent[tf] = now_mono
                    shown = self._disp.get(tf) or fb
                    await self._hub.broadcast_market(
                        "bar_update", self._symbol, tf, {"candle": shown.as_dict()}
                    )

    # ------------------------------------------- D-042 micro-tick fill loop

    async def _interp_loop(self) -> None:
        """Display-only micro-ticks between REAL broker ticks (15 Hz).

        Bounded OU walk around the last real mid; clamped inside the
        spread so the candle can never invent a new price level. Stops
        (and freezes the deviation) whenever the real feed goes quiet
        beyond INTERP_MAX_GAP_S — a quiet market STAYS quiet (no fake
        action during news lulls / weekends).
        """
        dt = 1.0 / INTERP_HZ
        try:
            while not self._stop.is_set():
                await asyncio.sleep(dt)
                now = time_mod.monotonic()
                if self._real_mid <= 0:
                    continue  # no real anchor yet
                if now - self._real_at > INTERP_MAX_GAP_S:
                    continue  # real feed quiet — no fill
                # OU step toward the real mid
                spread = self._real_spread
                clamp = max(INTERP_DEV_SPREAD * spread / 2.0, INTERP_MIN_DEV)
                revert = math.exp(-INTERP_THETA * dt)
                shock = random.gauss(0.0, clamp * 0.45) * math.sqrt(dt)
                self._interp_dev = self._interp_dev * revert + shock
                self._interp_dev = max(-clamp, min(clamp, self._interp_dev))
                mid = self._real_mid + self._interp_dev
                bid = mid - spread / 2.0
                ask = mid + spread / 2.0
                ts_ms = int(time_mod.time() * 1000)

                # tick frame (shared 20/s budget with real ticks)
                if now - self._last_tick_sent >= TICK_SEND_MIN_S:
                    self._last_tick_sent = now
                    self._interp_sent += 1
                    await self._hub.broadcast_ticks("tick", self._symbol, {
                        "bid": round(bid, 2),
                        "ask": round(ask, 2),
                        "ts": ts_ms,
                        "n": 0,                      # no real events inside
                        "tps": round(self._tps(), 1),
                        "i": True,                   # interpolated marker
                    })

                # display bars absorb the micro-tick (REAL bars untouched)
                wanted = self._hub.subscribed_tfs() | {self._engine_tf}
                for tf in wanted:
                    real = self._forming.get(tf)
                    if real is None:
                        continue
                    db = self._disp.get(tf)
                    if db is None or db.t != real.t:
                        db = FormingBar(t=real.t, o=mid, h=mid, low=mid,
                                        c=mid, v=real.v)
                        self._disp[tf] = db
                    else:
                        db.h = max(db.h, mid)
                        db.low = min(db.low, mid)
                        db.c = mid
                    if now - self._last_bar_sent.get(tf, 0.0) >= BAR_SEND_MIN_S:
                        self._last_bar_sent[tf] = now
                        await self._hub.broadcast_market(
                            "bar_update", self._symbol, tf,
                            {"candle": db.as_dict()},
                        )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — fill must never kill the stream
            logger.exception("interp loop died (display fill paused)")

    # ----------------------------------------------------------- bar helpers

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

        D-042: the terminal's own bar is AUTHORITATIVE for O/H/L/C —
        every real tick the terminal saw is inside it, so display-fill
        noise can never survive into closed history. The tick-built bar
        is the fallback when the terminal fetch fails.

        D-064 flake fix: the fetch takes a small WINDOW (not just the
        last row) and matches the bar by its own timestamp — under
        event-loop load the close processing can run after the virtual
        clock already rolled into the NEXT bucket (or the terminal
        already has a newer closed bar), and the old last-row-only
        fetch then mismatched `t`, silently falling back to the
        tick-built (interpolated) OHLC that failed reconciliation.
        """
        bar = fb.as_dict()
        try:
            df = await self._source.get_rates(self._symbol, tf, 4)
            if len(df):
                ts = df["time_utc"].map(lambda x: int(x.timestamp()))
                hit = df.index[ts == fb.t]
                if len(hit):
                    r = df.loc[hit[0]]
                    bar = {
                        "t": fb.t,
                        "o": float(r["o"]),
                        "h": float(r["h"]),
                        "l": float(r["l"]),
                        "c": float(r["c"]),
                        "v": max(int(r["v"]), fb.v),
                    }
        except Exception:  # noqa: BLE001 — fall back to the tick-built bar
            logger.debug("authoritative reconcile failed for %s close", tf)
        self._forming.pop(tf, None)
        self._disp.pop(tf, None)
        await self._hub.broadcast_market("bar_close", self._symbol, tf, {"candle": bar})
        if self.on_bar_close is not None:
            try:
                await self.on_bar_close(tf, bar)
            except Exception:  # noqa: BLE001 — engine errors must not kill the stream
                logger.exception("on_bar_close callback failed")

    # -------------------------------------------------------------- metrics

    def interp_stats(self) -> dict:
        """D-042 — display-fill state for health/status endpoints."""
        return {
            "hz": INTERP_HZ,
            "active": (
                self._real_mid > 0
                and time_mod.monotonic() - self._real_at <= INTERP_MAX_GAP_S
            ),
            "frames_sent": self._interp_sent,
        }


def bar_bucket(ts: datetime, tf_min: int) -> int:
    """UTC bucket-open epoch seconds for a timestamp."""
    return int(ts.replace(tzinfo=ts.tzinfo or UTC).timestamp() // (tf_min * 60)) * tf_min * 60
