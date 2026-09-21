"""McpMarketFeed — the REAL broker feed from the MetaTrader 5 terminal (D-035).

The user's directive: market data must come from the FOREX market THROUGH
MetaTrader 5 (real broker prices), not only from crypto composites. This
module polls the terminal's built-in MCP server (D-034) for:

- TICKS  — `get_chart_ticks_history` per watched symbol every second; each
  new broker tick becomes a quote event (real bid/ask from Exness).
- BARS   — `get_chart_history` (M1..D1) as the AUTHORITATIVE candle source
  while the symbol's ticks are fresh (market open); includes the forming bar.
- SYMBOLS — Market Watch discovery maps platform symbols to broker names
  (XAUUSD -> XAUUSDm, BTCUSD -> BTCUSDm).

Weekend logic (user directive): when the forex market is closed (no fresh
MT5 ticks — e.g. XAU Sat/Sun) `fresh()` goes False and the caller
(SymbolFeed) transparently falls back to the 24/7 crypto composite; when
the market reopens (Monday) MT5 automatically becomes the authority again.
Nothing is ever synthesized — every price is a real market price.

Terminal timestamps ("2026.09.20 17:11:30.713") are empirically UTC on this
terminal; a one-time calibration against a 24/7 symbol (BTCUSDm) verifies
the offset and `MT5_TIME_SHIFT_S` can override it.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time as time_mod
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger("xauusd.mcpmarket")

#: tick poll cadence per symbol (D-037: 4/s — MT5-like liveness)
POLL_S = float(os.environ.get("MT5_TICK_POLL_S", "0.25"))
FRESH_S = 15.0               # MT5 authority window since last broker tick
BARS_CACHE_TTL_S = 20.0      # chart-history cache per (symbol, tf)
BARS_MIN_REFETCH_S = 3.0     # forced-refetch rate cap

#: platform symbol -> default broker symbol (refined by Market Watch discovery)
DEFAULT_MAP = {"XAUUSD": "XAUUSDm", "BTCUSD": "BTCUSDm"}
#: chart-history period names per platform timeframe
PERIODS = {"M1": "M1", "M5": "M5", "M15": "M15", "M30": "M30",
           "H1": "H1", "H4": "H4", "D1": "D1"}


def _time_shift_s() -> float:
    """Optional manual terminal-clock offset (seconds)."""
    try:
        return float(os.environ.get("MT5_TIME_SHIFT_S", "0") or 0)
    except ValueError:
        return 0.0


def parse_terminal_time(s: str, shift_s: float = 0.0) -> float:
    """'2026.09.20 17:11:30.713' or '...17:32:00' -> epoch s (UTC + shift).

    Tick timestamps carry milliseconds; bar (chart-history) timestamps are
    second-precision — both formats must parse.
    """
    s = s.strip()
    try:
        dt = datetime.strptime(s, "%Y.%m.%d %H:%M:%S.%f")
    except ValueError:
        dt = datetime.strptime(s, "%Y.%m.%d %H:%M:%S")
    return dt.replace(tzinfo=UTC).timestamp() + shift_s


class McpMarketFeed:
    """Owns one poll task per watched platform symbol + bar caches.

    `client` is duck-typed (MT5TerminalClient in prod, fakes in tests) and
    must expose: ticks(symbol, dt_from, dt_to) -> list[dict],
    bars(symbol, period, dt_from, dt_to, limit) -> list[dict],
    symbols() -> list[dict]. All calls run in worker threads.
    """

    def __init__(
        self,
        client: Any,
        watch: list[str] | None = None,
        poll_s: float = POLL_S,
        fresh_s: float = FRESH_S,
        on_tick: Callable[[str, float, float, float], None] | None = None,
    ) -> None:
        self._client = client
        self._watch = list(watch or ["XAUUSD", "BTCUSD"])
        self._poll_s = float(poll_s)
        self._fresh_s = float(fresh_s)
        self._on_tick = on_tick
        self.symbol_map: dict[str, str] = {}
        # per platform-symbol poller state
        self._last_ts: dict[str, float] = {}
        self._last_monotonic: dict[str, float] = {}
        self._events: dict[str, int] = {}
        self._err: dict[str, str] = {}
        self._event_times: dict[str, deque[float]] = {}
        # bars cache: (symbol, tf) -> (rows, fetched_monotonic)
        self._bars: dict[tuple[str, str], tuple[list[dict], float]] = {}
        self._bars_refetch: dict[tuple[str, str], float] = {}
        self._shift = _time_shift_s()
        self._calibrated = self._shift != 0.0
        self._tasks: list[asyncio.Task] = []
        self._stop = asyncio.Event()

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        await self._discover_symbols()
        for sym in self._watch:
            self._event_times.setdefault(sym, deque(maxlen=512))
            self._tasks.append(asyncio.create_task(
                self._poll_loop(sym), name=f"mcp-market-{sym}"
            ))
        # D-037: an abruptly-killed terminal can lose Market Watch entries
        # (state not flushed) — re-ensure our symbols periodically so the
        # feed self-heals after ANY terminal restart.
        self._tasks.append(asyncio.create_task(
            self._ensure_symbols_loop(), name="mcp-market-ensure"
        ))
        logger.info(
            "MT5 market feed started — symbols %s (broker %s)",
            self._watch, self.symbol_map,
        )

    async def _ensure_symbols_loop(self) -> None:
        """Re-run discovery (idempotent Market Watch re-add) every 60s."""
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=60.0)
            except TimeoutError:
                pass
            if self._stop.is_set():
                return
            try:
                await self._discover_symbols()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — loop never dies
                logger.debug("MT5 symbol re-ensure failed: %s", exc)

    async def stop(self) -> None:
        self._stop.set()
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._tasks = []

    @property
    def running(self) -> bool:
        return bool(self._tasks) and not self._stop.is_set()

    async def _discover_symbols(self) -> None:
        """Map platform -> broker symbols from Market Watch (best prefix match).

        D-037: a FRESH terminal ships a default Market Watch that may not
        include our broker symbols — ensure they are added (idempotent) so
        their ticks flow; otherwise polls return 0 rows forever.
        """
        self.symbol_map = dict(DEFAULT_MAP)
        try:
            rows = await asyncio.to_thread(self._client.symbols)
        except Exception as exc:  # noqa: BLE001 — defaults keep us alive
            logger.warning("MT5 symbol discovery failed (%s) — using defaults", exc)
            return
        names = [str(r.get("symbol", "")) for r in rows if isinstance(r, dict)]
        for plat, _default in DEFAULT_MAP.items():
            exact = [n for n in names if n == plat]
            prefix = [n for n in names if n.startswith(plat)]
            if exact:
                self.symbol_map[plat] = exact[0]
            elif prefix:
                # prefer the shortest (e.g. XAUUSDm over XAUUSDmicro)
                self.symbol_map[plat] = min(prefix, key=len)
            if self.symbol_map[plat] not in names:
                try:
                    await asyncio.to_thread(
                        self._client._call,  # noqa: SLF001 — own client
                        "add_marketwatch_symbol",
                        {"symbol": self.symbol_map[plat]},
                    )
                    logger.info("MT5 Market Watch: added %s", self.symbol_map[plat])
                except Exception as exc:  # noqa: BLE001 — best-effort
                    logger.warning(
                        "MT5 Market Watch add %s failed: %s",
                        self.symbol_map[plat], exc,
                    )

    # --------------------------------------------------------------- polling

    async def _poll_loop(self, plat: str) -> None:
        backoff = self._poll_s
        while not self._stop.is_set():
            try:
                got = await self._poll_ticks(plat)
                backoff = self._poll_s
                if got and not self._calibrated:
                    self._calibrate(plat)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — poller never dies
                self._err[plat] = f"{type(exc).__name__}: {exc}"[:160]
                backoff = min(backoff * 2.0, 10.0)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
            except TimeoutError:
                pass

    async def _poll_ticks(self, plat: str) -> int:
        """Fetch new broker ticks for one platform symbol; emit callbacks."""
        broker = self.symbol_map.get(plat) or DEFAULT_MAP.get(plat)
        if not broker:
            return 0
        now_epoch = time_mod.time()
        last = self._last_ts.get(plat)
        frm_s = last if last else now_epoch - 30.0
        # small overlap + dedupe keeps us safe against poll jitter
        frm = datetime.fromtimestamp(frm_s - 1.0, tz=UTC)
        to = datetime.fromtimestamp(now_epoch + 120.0, tz=UTC)
        rows = await asyncio.to_thread(
            self._client.ticks, broker,
            frm.strftime("%Y-%m-%dT%H:%M:%SZ"), to.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        now_mono = time_mod.monotonic()
        new = 0
        for r in rows or []:
            try:
                ts = parse_terminal_time(str(r.get("time_ms", "")), self._shift)
                bid = float(r.get("bid", 0.0))
                ask = float(r.get("ask", 0.0))
            except (TypeError, ValueError):
                continue
            if bid <= 0 or ask <= 0 or ask < bid:
                continue
            if last is not None and ts <= last:
                continue  # dedupe against the previous poll window
            last = ts
            new += 1
            self._events[plat] = self._events.get(plat, 0) + 1
            self._event_times.setdefault(plat, deque(maxlen=512)).append(now_mono)
            if self._on_tick is not None:
                try:
                    self._on_tick(plat, bid, ask, ts)
                except Exception:  # noqa: BLE001 — callback must not kill polling
                    logger.debug("mt5 on_tick callback failed", exc_info=True)
        if new:
            self._last_ts[plat] = last
            self._last_monotonic[plat] = now_mono
            self._err.pop(plat, None)
        return new

    def _calibrate(self, plat: str) -> None:
        """Verify terminal-clock offset using an always-ticking symbol."""
        if abs(self._shift) > 0.0:
            return
        drift = time_mod.time() - self._last_ts.get(plat, 0.0)
        # Only 24/7 symbols can calibrate (crypto ticks right now);
        # gold on the weekend would produce a huge false drift.
        if "BTC" not in self.symbol_map.get(plat, ""):
            return
        if abs(drift) > 120.0:
            shift = round(drift / 900.0) * 900.0
            self._shift = shift
            logger.warning(
                "MT5 terminal clock drift %.0fs detected — applying shift %.0fs",
                drift, shift,
            )
        else:
            logger.info("MT5 terminal clock verified (drift %.1fs)", drift)

    # ----------------------------------------------------------------- state

    def fresh(self, plat: str) -> bool:
        """True while the broker is actively ticking this symbol (market open)."""
        last = self._last_monotonic.get(plat, 0.0)
        return last > 0 and time_mod.monotonic() - last < self._fresh_s

    def market_state(self, plat: str) -> str:
        """D-037: open | closed | unavailable for one platform symbol.

        open  — broker ticks arriving right now (BTCUSDm 24/7, gold during
                forex sessions)
        closed — the symbol ticked before but went idle (gold Sat/Sun):
                the terminal still serves its real chart history
        unavailable — the terminal bridge itself is unreachable
        """
        if self.fresh(plat):
            return "open"
        if self._last_ts.get(plat):
            return "closed"
        if self._err.get(plat):
            return "unavailable"
        return "unknown"

    def tps(self, plat: str, window_s: float = 5.0) -> float | None:
        times = self._event_times.get(plat)
        if not times:
            return None
        now = time_mod.monotonic()
        n = sum(1 for t in times if now - t <= window_s)
        return round(n / window_s, 1)

    def status(self) -> dict:
        now = time_mod.monotonic()
        out: dict[str, dict] = {}
        for plat in self._watch:
            last = self._last_monotonic.get(plat, 0.0)
            out[plat] = {
                "ok": last > 0 and now - last < self._fresh_s,
                "broker_symbol": self.symbol_map.get(plat),
                "events": self._events.get(plat, 0),
                "age_s": round(now - last, 1) if last else None,
                "tps": self.tps(plat),
                "err": self._err.get(plat) or None,
            }
        return out

    # ------------------------------------------------------------------ bars

    async def bars(self, plat: str, tf: str, count: int) -> tuple[list[dict], dict | None]:
        """(closed_rows, forming_row) from the terminal's chart history.

        Rows: {t: epoch, o, h, l, c, v}. The terminal includes the forming
        bar; it is split off by comparing against the current bucket.
        """
        from app.mt5.base import validate_tf

        tf_min = validate_tf(tf)
        key = (plat, tf)
        now_mono = time_mod.monotonic()
        cached = self._bars.get(key)
        if cached and now_mono - cached[1] < BARS_CACHE_TTL_S:
            return cached[0][:-1] if self._has_forming(key) else cached[0], \
                cached[0][-1] if self._has_forming(key) else None
        if now_mono - self._bars_refetch.get(key, 0.0) < BARS_MIN_REFETCH_S:
            if cached:
                forming = cached[0][-1] if self._has_forming(key) else None
                return (cached[0][:-1] if forming else cached[0]), forming
            return [], None
        self._bars_refetch[key] = now_mono

        broker = self.symbol_map.get(plat) or DEFAULT_MAP.get(plat)
        if not broker:
            return [], None
        span_s = max(count, 30) * tf_min * 60 + tf_min * 60 * 2
        frm = datetime.fromtimestamp(time_mod.time() - span_s, tz=UTC)
        to = datetime.fromtimestamp(time_mod.time() + 120.0, tz=UTC)
        try:
            raw = await asyncio.to_thread(
                self._client.bars, broker, PERIODS[tf],
                frm.strftime("%Y-%m-%dT%H:%M:%SZ"), to.strftime("%Y-%m-%dT%H:%M:%SZ"),
                min(count + 10, 5000),
            )
        except Exception as exc:  # noqa: BLE001 — cache (if any) stays valid
            logger.debug("MT5 bars fetch failed (%s %s): %s", plat, tf, exc)
            if cached:
                forming = cached[0][-1] if self._has_forming(key) else None
                return (cached[0][:-1] if forming else cached[0]), forming
            return [], None

        bucket_now = int(time_mod.time() // (tf_min * 60))
        rows: list[dict] = []
        forming: dict | None = None
        for r in raw or []:
            try:
                t = int(parse_terminal_time(str(r.get("time", "")), self._shift))
                b_bucket = t // (tf_min * 60)
                row = {
                    "t": b_bucket * tf_min * 60,
                    "o": float(r["open"]), "h": float(r["high"]),
                    "l": float(r["low"]), "c": float(r["close"]),
                    "v": max(1, int(r.get("tick_volume", 1))),
                }
            except (KeyError, TypeError, ValueError):
                continue
            if b_bucket >= bucket_now:
                forming = row  # current (still forming) bar
            else:
                rows.append(row)
        # dedupe + keep the requested tail
        dedup = {r["t"]: r for r in rows}
        rows = [dedup[k] for k in sorted(dedup)][-max(count, 10):]
        self._bars[key] = (rows + ([forming] if forming else []), now_mono)
        return rows, forming

    def _has_forming(self, key: tuple[str, str]) -> bool:
        """Cached bars include the forming row when the last bucket is current."""
        cached = self._bars.get(key)
        if not cached or not cached[0]:
            return False
        from app.mt5.base import TIMEFRAME_MINUTES

        tf_min = TIMEFRAME_MINUTES[key[1]]
        last_t = cached[0][-1]["t"]
        return last_t >= int(time_mod.time() // (tf_min * 60)) * tf_min * 60

    def forming(self, plat: str, tf: str) -> dict | None:
        """Cached forming bar for (symbol, tf) without refetching (fast path)."""
        key = (plat, tf)
        if not self._has_forming(key):
            return None
        return self._bars[key][0][-1]


def mcp_market_available() -> bool:
    """Fast probe — is the terminal MCP bridge reachable? (boot decision)"""
    try:
        from app.mt5.mcp import terminal_client

        return terminal_client().available()
    except Exception:  # noqa: BLE001
        return False
