"""MT5DataSource — thin async wrapper around the MetaTrader5 package.

The `MetaTrader5` package exists ONLY on Windows (SPEC C1) and is therefore
imported lazily inside `_import_mt5()` — importing this module on Linux must
never pull it in (SPEC C7, Phase 0 AC). Unit tests inject a fake
`MetaTrader5` module into `sys.modules` to verify this wrapper's logic
(UTC conversion, closed-bars-only, filling-mode mapping) on Linux.

Concurrency: every blocking MT5 call runs on a single-thread executor —
the MetaTrader5 IPC client is not designed for concurrent calls.

Broker time (C4): MT5 returns bar/tick times in SERVER time. The offset is
detected at connect (server tick time vs UTC now, rounded to 30 min) and
re-detected daily by the ConnectionManager; all timestamps leaving this
class are UTC.
"""

from __future__ import annotations

import asyncio
import logging
import time as time_mod
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

import pandas as pd

from app.mt5.base import (
    RATES_COLUMNS,
    DataSource,
    DataSourceError,
    Order,
    OrderResult,
    Position,
    SymbolInfo,
    Tick,
    validate_tf,
)

logger = logging.getLogger("xauusd.mt5")

# MetaTrader5 package timeframe constants (pos arg of copy_rates_from_pos)
MT5_TIMEFRAMES: dict[str, int] = {
    "M1": 1,
    "M5": 5,
    "M15": 15,
    "M30": 30,
    "H1": 16385,  # TIMEFRAME_H1
    "H4": 16388,  # TIMEFRAME_H4
    "D1": 16408,  # TIMEFRAME_D1
}

TRADE_RETCODE_DONE = 10009
POLL_INTERVAL_S = 0.15  # tick polling cadence (real MT5 has no push API)
KEEPALIVE_S = 2.0  # re-yield an unchanged tick so consumers see liveness


class MT5DataSource(DataSource):
    """Real MT5 terminal data source (Windows-only)."""

    def __init__(self, terminal_path: str | None = None) -> None:
        self._terminal_path = terminal_path
        self._mt5: Any | None = None  # lazy MetaTrader5 module reference
        self._connected = False
        self._symbol_point: dict[str, float] = {}
        self._symbol_info_cache: dict[str, SymbolInfo] = {}
        self._offset_minutes = 0  # broker server time - UTC (C4)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mt5")

    # ------------------------------------------------------------ internals

    def _import_mt5(self) -> Any:
        """Lazily import the Windows-only MetaTrader5 package (SPEC C7)."""
        if self._mt5 is None:
            import MetaTrader5  # noqa: PLC0415 — deliberate lazy import (C7)

            self._mt5 = MetaTrader5
        return self._mt5

    async def _run(self, fn, *args):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, lambda: fn(*args))

    # ---------------------------------------------------- connection lifecycle

    async def connect(self, creds: dict) -> dict:
        mt5 = self._import_mt5()
        ok = await self._run(
            mt5.initialize,
            self._terminal_path or None,
            int(creds["login"]),
            str(creds["password"]),
            str(creds["server"]),
        )
        if not ok:
            err = await self._run(mt5.last_error)
            raise DataSourceError(f"mt5.initialize failed: {err}")
        self._connected = True
        self._refresh_offset()
        info = await self.account_info()
        if info is None:
            raise DataSourceError("connected but account_info() returned None")
        return info

    async def disconnect(self) -> None:
        if self._mt5 is not None and self._connected:
            try:
                await self._run(self._mt5.shutdown)
            except Exception:  # noqa: BLE001
                logger.warning("mt5.shutdown raised", exc_info=True)
        self._connected = False

    async def is_connected(self) -> bool:
        if not self._connected or self._mt5 is None:
            return False
        try:
            mt5 = self._import_mt5()
            term = await self._run(mt5.terminal_info)
            return bool(term and term.trade_allowed is not False and term.connected)
        except Exception:  # noqa: BLE001
            return False

    def _refresh_offset(self) -> None:
        """C4 — broker offset from a fresh tick's server timestamp."""
        try:
            mt5 = self._import_mt5()
            symbols = mt5.symbols_get("*XAUUSD*") or []
            if not symbols:
                return
            tick = mt5.symbol_info_tick(symbols[0].name)
            if tick is None or not tick.time:
                return
            server = datetime.fromtimestamp(tick.time, tz=UTC)
            delta_min = (server - datetime.now(UTC)).total_seconds() / 60.0
            self._offset_minutes = int(round(delta_min / 30.0) * 30)
            logger.info("broker UTC offset detected: %+d min", self._offset_minutes)
        except Exception:  # noqa: BLE001
            logger.warning("offset detection failed — keeping %+d", self._offset_minutes)

    @property
    def broker_utc_offset_minutes(self) -> int:  # type: ignore[override]
        return self._offset_minutes

    # ------------------------------------------------------------- market data

    def _to_utc_epoch(self, server_epoch: int) -> int:
        return int(server_epoch) - self._offset_minutes * 60

    async def get_rates(self, symbol: str, tf: str, count: int) -> pd.DataFrame:
        validate_tf(tf)
        mt5 = self._import_mt5()
        # pos=1 -> start AFTER the forming bar (closed bars only, D-003)
        rates = await self._run(
            mt5.copy_rates_from_pos, symbol, MT5_TIMEFRAMES[tf], 1, count
        )
        if rates is None or len(rates) == 0:
            err = await self._run(mt5.last_error)
            raise DataSourceError(f"copy_rates_from_pos({symbol},{tf}) failed: {err}")
        df = pd.DataFrame(rates)
        df["time_utc"] = pd.to_datetime(
            df["time"].map(self._to_utc_epoch), unit="s", utc=True
        )
        df = df.rename(columns={"open": "o", "high": "h", "low": "l", "close": "c"})
        df["v"] = df["tick_volume"].astype(int)
        return df[RATES_COLUMNS].tail(count).reset_index(drop=True)

    def get_forming_bar(self, symbol: str, tf: str) -> dict | None:
        """Sync forming bar (pos=0). Returns None when the market is closed."""
        try:
            mt5 = self._import_mt5()
            validate_tf(tf)
            rates = mt5.copy_rates_from_pos(symbol, MT5_TIMEFRAMES[tf], 0, 1)
            if rates is None or len(rates) == 0:
                return None
            r = rates[-1]
            t = self._to_utc_epoch(r["time"])
            now_bucket = (
                int(time_mod.time()) // (validate_tf(tf) * 60)
            ) * validate_tf(tf) * 60
            if t != now_bucket:
                return None  # last bar is a closed one -> market quiet
            return {
                "t": t,
                "o": float(r["open"]),
                "h": float(r["high"]),
                "l": float(r["low"]),
                "c": float(r["close"]),
                "v": int(r["tick_volume"]),
            }
        except Exception:  # noqa: BLE001
            return None

    async def get_tick(self, symbol: str) -> Tick:
        mt5 = self._import_mt5()
        t = await self._run(mt5.symbol_info_tick, symbol)
        if t is None:
            raise DataSourceError(f"symbol_info_tick({symbol}) returned None")
        ts = datetime.fromtimestamp(self._to_utc_epoch(t.time), tz=UTC)
        return Tick(bid=float(t.bid), ask=float(t.ask), time=ts)

    def subscribe_ticks(self, symbol: str) -> AsyncIterator[Tick]:
        return self._tick_stream(symbol)

    async def _tick_stream(self, symbol: str) -> AsyncIterator[Tick]:
        """Poll symbol_info_tick; yield on change + keepalive every 2s."""
        last = None
        last_keepalive = 0.0
        try:
            while True:
                tick = await self.get_tick(symbol)
                now = time_mod.monotonic()
                key = (tick.bid, tick.ask, tick.time)
                if key != last or (now - last_keepalive) >= KEEPALIVE_S:
                    last = key
                    last_keepalive = now
                    yield tick
                await asyncio.sleep(POLL_INTERVAL_S)
        except asyncio.CancelledError:
            return

    def discover_symbols(self, pattern: str = "*XAUUSD*") -> list[str]:
        try:
            mt5 = self._import_mt5()
            syms = mt5.symbols_get(pattern) or []
            return sorted(s.name for s in syms)
        except Exception:  # noqa: BLE001
            return []

    def point_size(self, symbol: str) -> float:
        """Price point of the symbol (XAUUSD 2-digit -> 0.01) — spread math."""
        if symbol not in self._symbol_point:
            try:
                mt5 = self._import_mt5()
                info = mt5.symbol_info(symbol)
                self._symbol_point[symbol] = float(info.point) if info else 0.01
            except Exception:  # noqa: BLE001
                self._symbol_point[symbol] = 0.01
        return self._symbol_point[symbol]

    # ---------------------------------------------------------------- account

    async def account_info(self) -> dict | None:
        if not self._connected:
            return None
        mt5 = self._import_mt5()
        info = await self._run(mt5.account_info)
        if info is None:
            return None
        return {
            "login": str(info.login),
            "server": info.server,
            "balance": float(info.balance),
            "equity": float(info.equity),
            "currency": info.currency,
            "leverage": int(info.leverage),
        }

    # ---------------------------------------------------------------- trading

    async def place_order(self, order: Order) -> OrderResult:
        """Send a market order with filling-mode mapping (SPEC C5, §9)."""
        mt5 = self._import_mt5()
        tick = await self.get_tick(order.symbol)
        price = tick.ask if order.side == "BUY" else tick.bid
        filling = await self._filling_mode(order.symbol)
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": order.symbol,
            "volume": float(order.volume),
            "type": mt5.ORDER_TYPE_BUY if order.side == "BUY" else mt5.ORDER_TYPE_SELL,
            "price": float(price),
            "sl": float(order.sl) if order.sl else 0.0,
            "tp": float(order.tp) if order.tp else 0.0,
            "deviation": int(order.deviation),
            "magic": int(order.magic),
            "comment": order.comment[:31],
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": filling,
        }
        result = await self._run(mt5.order_send, request)
        if result is None:
            return OrderResult(ok=False, retcode=None, comment="order_send returned None")
        return OrderResult(
            ok=result.retcode == TRADE_RETCODE_DONE,
            ticket=int(result.order) if result.order else None,
            price=float(result.price) if result.price else price,
            retcode=int(result.retcode),
            comment=str(result.comment),
        )

    async def _filling_mode(self, symbol: str) -> int:
        """Map symbol_info().filling_mode flags to an order_send filling mode."""
        mt5 = self._import_mt5()
        info = await self._run(mt5.symbol_info, symbol)
        if info is None:
            return mt5.ORDER_FILLING_IOC
        flags = int(info.filling_mode)
        if flags & mt5.SYMBOL_FILLING_FOK:
            return mt5.ORDER_FILLING_FOK
        if flags & mt5.SYMBOL_FILLING_IOC:
            return mt5.ORDER_FILLING_IOC
        return mt5.ORDER_FILLING_RETURN

    async def symbol_info(self, symbol: str) -> SymbolInfo | None:  # type: ignore[override]
        """Lot-sizing metadata from the terminal (SPEC §9); cached per symbol."""
        if symbol in self._symbol_info_cache:
            return self._symbol_info_cache[symbol]
        try:
            mt5 = self._import_mt5()
            info = await self._run(mt5.symbol_info, symbol)
            if info is None:
                return None
            result = SymbolInfo(
                name=symbol,
                point=float(info.point),
                contract_size=float(getattr(info, "trade_contract_size", 100.0)),
                volume_min=float(info.volume_min),
                volume_max=float(info.volume_max),
                volume_step=float(info.volume_step),
            )
            self._symbol_info_cache[symbol] = result
            return result
        except Exception:  # noqa: BLE001 — executor falls back to defaults
            return None

    async def close_position(self, ticket: int, deviation: int = 30) -> OrderResult:
        """Close an open position by ticket with an opposite DEAL (SPEC §9)."""
        mt5 = self._import_mt5()
        positions = await self.get_positions()
        pos = next((p for p in positions if p.ticket == ticket), None)
        if pos is None:
            return OrderResult(ok=False, retcode=10036, comment="position not found")
        tick = await self.get_tick(pos.symbol)
        # Closing BUY -> sell at bid; closing SELL -> buy at ask
        price = tick.bid if pos.side == "BUY" else tick.ask
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": pos.symbol,
            "volume": float(pos.volume),
            "type": mt5.ORDER_TYPE_SELL if pos.side == "BUY" else mt5.ORDER_TYPE_BUY,
            "position": int(ticket),  # identifies the position being closed
            "price": float(price),
            "deviation": int(deviation),
            "magic": 0,
            "comment": "xauai-close",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": await self._filling_mode(pos.symbol),
        }
        result = await self._run(mt5.order_send, request)
        if result is None:
            return OrderResult(ok=False, retcode=None, comment="order_send returned None")
        return OrderResult(
            ok=result.retcode == TRADE_RETCODE_DONE,
            ticket=int(result.order) if result.order else None,
            price=float(result.price) if result.price else price,
            retcode=int(result.retcode),
            comment=str(result.comment),
        )

    async def get_positions(self) -> list[Position]:
        mt5 = self._import_mt5()
        raw = await self._run(mt5.positions_get)
        out: list[Position] = []
        for p in raw or []:
            out.append(
                Position(
                    ticket=int(p.ticket),
                    symbol=p.symbol,
                    side="BUY" if p.type == mt5.POSITION_TYPE_BUY else "SELL",
                    volume=float(p.volume),
                    price_open=float(p.price_open),
                    sl=float(p.sl) or None,
                    tp=float(p.tp) or None,
                    profit=float(p.profit),
                    time=datetime.fromtimestamp(
                        self._to_utc_epoch(p.time), tz=UTC
                    ),
                )
            )
        return out

    # ---------------------------------------------------------------- misc

    async def refresh_offset_daily(self) -> None:
        """C4 — DST shifts: re-detect the broker offset (called by manager)."""
        await self._run(self._refresh_offset)
