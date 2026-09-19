"""MT5DataSource — thin async wrapper around the MetaTrader5 package.

The `MetaTrader5` package exists ONLY on Windows (SPEC C1) and is therefore
imported lazily inside `_import_mt5()` — importing this module on Linux must
never pull it in (SPEC C7, Phase 0 AC).

Full implementation (executor thread wrapper, auto-reconnect loop, broker UTC
offset detection per C4, filling-mode mapping per C5) lands in Phase 2.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pandas as pd

from app.mt5.base import (
    DataSource,
    DataSourceError,
    Order,
    OrderResult,
    Position,
    Tick,
    empty_rates,
)


class MT5DataSource(DataSource):
    """Real MT5 terminal data source (Windows-only, Phase 2)."""

    def __init__(self, terminal_path: str | None = None) -> None:
        self._terminal_path = terminal_path
        self._mt5: Any | None = None  # lazy MetaTrader5 module reference
        self._connected = False

    def _import_mt5(self) -> Any:
        """Lazily import the Windows-only MetaTrader5 package (SPEC C7)."""
        if self._mt5 is None:
            import MetaTrader5  # noqa: PLC0415 — deliberate lazy import (C7)

            self._mt5 = MetaTrader5
        return self._mt5

    # ---- DataSource interface (skeleton; real logic arrives in Phase 2) ----

    async def connect(self, creds: dict) -> dict:
        mt5 = self._import_mt5()
        if not mt5.initialize(
            path=self._terminal_path or None,
            login=int(creds["login"]),
            password=creds["password"],
            server=creds["server"],
        ):
            raise DataSourceError(f"mt5.initialize failed: {mt5.last_error()}")
        self._connected = True
        info = mt5.account_info()
        return {
            "login": str(info.login),
            "server": info.server,
            "balance": info.balance,
            "equity": info.equity,
            "currency": info.currency,
            "leverage": info.leverage,
        }

    async def disconnect(self) -> None:
        if self._mt5 is not None and self._connected:
            self._mt5.shutdown()
        self._connected = False

    async def is_connected(self) -> bool:
        return self._connected

    async def get_rates(self, symbol: str, tf: str, count: int) -> pd.DataFrame:
        raise NotImplementedError("Phase 2")

    async def get_tick(self, symbol: str) -> Tick:
        raise NotImplementedError("Phase 2")

    def subscribe_ticks(self, symbol: str) -> AsyncIterator[Tick]:
        raise NotImplementedError("Phase 2")

    def discover_symbols(self, pattern: str = "*XAUUSD*") -> list[str]:
        mt5 = self._import_mt5()
        symbols = mt5.symbols_get(pattern)
        return [s.name for s in symbols] if symbols else []

    async def place_order(self, order: Order) -> OrderResult:
        raise NotImplementedError("Phase 4")

    async def get_positions(self) -> list[Position]:
        raise NotImplementedError("Phase 2")

    @staticmethod
    def _empty_rates() -> pd.DataFrame:
        return empty_rates()
