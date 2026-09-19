"""DataSource abstraction (SPEC §8.1).

All engine/market code depends on this interface only, so the platform runs
either against a real MT5 terminal (MT5DataSource, Windows-only) or against
deterministic synthetic data (MockDataSource) — selected via DATA_SOURCE env
var (SPEC C7).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime

import pandas as pd

# Minutes per timeframe (SPEC §7.1 candles, §8.2 M15 base / H1 context).
TIMEFRAME_MINUTES: dict[str, int] = {
    "M1": 1,
    "M5": 5,
    "M15": 15,
    "M30": 30,
    "H1": 60,
    "H4": 240,
    "D1": 1440,
}

# Canonical get_rates DataFrame columns (ascending by time, closed bars only —
# DECISIONS.md D-003).
RATES_COLUMNS = ["time_utc", "o", "h", "l", "c", "v"]


@dataclass(frozen=True)
class Tick:
    bid: float
    ask: float
    time: datetime  # UTC


@dataclass(frozen=True)
class Order:
    symbol: str
    side: str  # "BUY" | "SELL"
    volume: float
    sl: float | None = None
    tp: float | None = None
    deviation: int = 30  # points (SPEC §9)
    magic: int = 0
    comment: str = ""


@dataclass(frozen=True)
class OrderResult:
    ok: bool
    ticket: int | None = None
    price: float | None = None
    retcode: int | None = None  # MT5 retcode (10009 = TRADE_RETCODE_DONE)
    comment: str = ""


@dataclass(frozen=True)
class Position:
    ticket: int
    symbol: str
    side: str  # "BUY" | "SELL"
    volume: float
    price_open: float
    sl: float | None
    tp: float | None
    profit: float
    time: datetime  # UTC


class DataSourceError(RuntimeError):
    """Raised by data sources on unrecoverable errors."""


class DataSource(ABC):
    """Market data + trading interface (SPEC §8.1).

    Note on `subscribe_ticks`: declared as a plain method returning an
    AsyncIterator (implemented as an async generator) — see DECISIONS.md D-008.
    """

    @abstractmethod
    async def connect(self, creds: dict) -> dict:
        """Connect with credentials; returns account info."""

    @abstractmethod
    async def disconnect(self) -> None: ...

    @abstractmethod
    async def is_connected(self) -> bool: ...

    @abstractmethod
    async def get_rates(self, symbol: str, tf: str, count: int) -> pd.DataFrame:
        """Last `count` CLOSED bars of `tf` as a DataFrame with RATES_COLUMNS."""

    def get_forming_bar(self, symbol: str, tf: str) -> dict | None:
        """Current FORMING bar {t,o,h,l,c,v} or None (optional; sync or async).

        Used by MarketStream to seed mid-bucket chart subscriptions.
        """
        return None

    def account_info(self) -> dict | None:
        """Account snapshot {login, server, balance, equity, currency, leverage}.

        Optional (sync or async); None when not connected.
        """
        return None

    @property
    def broker_utc_offset_minutes(self) -> int:
        """Broker server-time offset from UTC in minutes (C4; 0 for mock)."""
        return 0

    @abstractmethod
    async def get_tick(self, symbol: str) -> Tick: ...

    @abstractmethod
    def subscribe_ticks(self, symbol: str) -> AsyncIterator[Tick]: ...

    @abstractmethod
    def discover_symbols(self, pattern: str = "*XAUUSD*") -> list[str]: ...

    @abstractmethod
    async def place_order(self, order: Order) -> OrderResult:
        """Real order placement (MT5 impl; Mock simulates a fill)."""

    @abstractmethod
    async def get_positions(self) -> list[Position]: ...


def validate_tf(tf: str) -> int:
    """Return minutes for a known timeframe or raise."""
    try:
        return TIMEFRAME_MINUTES[tf]
    except KeyError as exc:
        raise ValueError(f"unknown timeframe {tf!r}") from exc


def empty_rates() -> pd.DataFrame:
    return pd.DataFrame(columns=RATES_COLUMNS)
