"""Connection manager + DataSource factory (fleshed out in Phase 2, SPEC §7.1)."""

from __future__ import annotations

import logging

from app.config import Settings
from app.mt5.base import DataSource, Order, OrderResult, Position

logger = logging.getLogger("xauusd.mt5")


def create_data_source(settings: Settings) -> DataSource:
    """Select the DataSource implementation via DATA_SOURCE (SPEC C7).

    MT5DataSource is imported lazily so non-Windows machines never touch the
    MetaTrader5 package.
    """
    if settings.data_source == "mt5":
        from app.mt5.mt5_source import MT5DataSource  # lazy (C7)

        return MT5DataSource(terminal_path=settings.mt5_terminal_path)
    from app.mt5.mock_source import MockDataSource

    return MockDataSource()


class ConnectionManager:
    """Owns the active DataSource and connection state (single account, C2).

    Phase 2 adds: Fernet-encrypted credential storage, symbol discovery (C3),
    broker UTC offset detection (C4), heartbeat + auto-reconnect.
    """

    def __init__(self, source: DataSource) -> None:
        self._source = source

    @property
    def source(self) -> DataSource:
        return self._source

    async def connect(self, creds: dict) -> dict:
        return await self._source.connect(creds)

    async def disconnect(self) -> None:
        await self._source.disconnect()

    async def status(self) -> dict:
        connected = await self._source.is_connected()
        return {"status": "connected" if connected else "disconnected"}

    async def place_order(self, order: Order) -> OrderResult:
        return await self._source.place_order(order)

    async def get_positions(self) -> list[Position]:
        return await self._source.get_positions()

    async def shutdown(self) -> None:
        try:
            await self._source.disconnect()
        except Exception:  # noqa: BLE001 — shutdown must never raise
            logger.warning("error while disconnecting data source", exc_info=True)
