"""ConnectionManager — owns the DataSource, credentials, runtime lifecycle.

Responsibilities (SPEC §7.1 mt5 routes, C2/C3/C4, Phase 2):
- connect(creds): source.connect -> symbol discovery (C3) -> start EngineRuntime
- Fernet-encrypted credential persistence in mt5_connections (schema §6);
  without FERNET_KEY credentials stay in memory only (logged warning).
- heartbeat: poll is_connected every 5s; on drop -> reconnect loop with the
  stored credentials (survives MT5 terminal restarts within ~10s) and restart
  the runtime + stream (fresh forming bars, clients heal via REST refetch).
- daily broker-offset refresh (C4) for the real MT5 source.
- mock source: auto-connects at startup so the demo streams immediately
  (DECISIONS.md D-018).
"""

from __future__ import annotations

import asyncio
import logging
import time as time_mod
from typing import Any

from app.config import Settings
from app.mt5.base import DataSource, Order, OrderResult, Position

logger = logging.getLogger("xauusd.mt5")

HEARTBEAT_S = 5.0
RECONNECT_DELAY_S = 5.0
OFFSET_REFRESH_S = 12 * 3600.0  # C4: re-detect twice a day


class ConnectionState:
    """Snapshot for GET /api/mt5/status (SPEC §7.1)."""

    def __init__(self) -> None:
        self.status = "disconnected"  # connected | disconnected | reconnecting
        self.symbol: str | None = None
        self.login: str | None = None
        self.server: str | None = None
        self.point_size = 0.01
        self.account: dict | None = None
        self.connected_at: float | None = None


class ConnectionManager:
    def __init__(
        self,
        source: DataSource,
        settings: Settings | None = None,
        hub: Any = None,
        db_engine: Any = None,
        repo: Any = None,
        news_service: Any = None,
        config_repo: Any = None,
    ) -> None:
        self._source = source
        self._settings = settings
        self._hub = hub
        self._db = db_engine
        self._repo = repo
        self._news = news_service
        self._config_repo = config_repo
        self.state = ConnectionState()
        self.runtime: Any = None  # EngineRuntime while connected
        self._creds: dict | None = None  # last creds (memory) for reconnect
        self._heartbeat_task: asyncio.Task | None = None
        self._fernet: Any = None
        self._stop = asyncio.Event()
        self._connecting = asyncio.Lock()

    # ---------------------------------------------------------------- fernet

    def _fernet_cipher(self) -> Any | None:
        if self._fernet is not None:
            return self._fernet
        key = getattr(self._settings, "fernet_key", None) if self._settings else None
        if not key:
            return None
        try:
            from cryptography.fernet import Fernet

            self._fernet = Fernet(key.encode() if isinstance(key, str) else key)
            return self._fernet
        except Exception as exc:  # noqa: BLE001
            logger.warning("Fernet init failed — passwords not persisted: %s", exc)
            return None

    def _encrypt(self, password: str) -> str | None:
        cipher = self._fernet_cipher()
        if cipher is None:
            return None
        try:
            return cipher.encrypt(password.encode()).decode()
        except Exception:  # noqa: BLE001
            return None

    def _decrypt(self, enc: str) -> str | None:
        cipher = self._fernet_cipher()
        if cipher is None:
            return None
        try:
            return cipher.decrypt(enc.encode()).decode()
        except Exception:  # noqa: BLE001
            return None

    # ---------------------------------------------------------------- connect

    async def connect(self, creds: dict) -> dict:
        """Connect + discover symbol + start engine runtime (SPEC §7.1)."""
        async with self._connecting:
            info = await self._source.connect(creds)
            self._creds = dict(creds)
            self.state.status = "connected"
            self.state.login = str(info.get("login", creds.get("login")))
            self.state.server = str(info.get("server", creds.get("server", "")))
            self.state.account = info
            self.state.connected_at = time_mod.monotonic()

            # C3 — symbol discovery (prefer the shortest XAUUSD-ish name)
            symbol = creds.get("symbol")
            if not symbol:
                candidates = self._source.discover_symbols("*XAUUSD*") or []
                if not candidates:
                    await self._disconnect_quiet()
                    raise ValueError("no XAUUSD symbol found on this account/server")
                symbol = min(candidates, key=len)
            self.state.symbol = symbol
            point_getter = getattr(self._source, "point_size", None)
            self.state.point_size = (
                point_getter(symbol) if callable(point_getter) else 0.01
            )

            await self._persist_connection(creds)
            await self._start_runtime()
            await self._broadcast_status()
            self._ensure_heartbeat()
            logger.info(
                "connected: %s (%s) symbol=%s point=%s",
                self.state.login, self.state.server, symbol, self.state.point_size,
            )
            return await self.status()

    async def disconnect(self) -> dict:
        self._stop_heartbeat()
        await self._stop_runtime()
        self._creds = None
        await self._source.disconnect()
        self.state.status = "disconnected"
        self.state.account = None
        await self._update_stored_status("disconnected")
        await self._broadcast_status()
        logger.info("disconnected")
        return await self.status()

    async def _disconnect_quiet(self) -> None:
        try:
            await self._source.disconnect()
        except Exception:  # noqa: BLE001
            pass

    # ---------------------------------------------------------------- runtime

    async def _start_runtime(self) -> None:
        if self.runtime is not None:
            await self.runtime.stop()
            self.runtime = None
        if self._hub is None or self._repo is None:
            return  # bare mode (tests) — no streaming
        cfg = None
        if self._config_repo is not None and self._db is not None:
            cfg, _auto = await self._config_repo.load(self._db)
        if cfg is None:
            from app.engine.config import DEFAULT_CONFIG

            cfg = DEFAULT_CONFIG
        from app.services.runtime import EngineRuntime

        self.runtime = EngineRuntime(
            source=self._source,
            hub=self._hub,
            repo=self._repo,
            symbol=self.state.symbol or "XAUUSDm",
            point_size=self.state.point_size,
            cfg=cfg,
            news_service=self._news,
        )
        await self.runtime.start()

    async def _stop_runtime(self) -> None:
        if self.runtime is not None:
            try:
                await self.runtime.stop()
            except Exception:  # noqa: BLE001
                logger.warning("runtime stop raised", exc_info=True)
            self.runtime = None

    # ------------------------------------------------------------- persistence

    async def _persist_connection(self, creds: dict) -> None:
        if self._db is None:
            return
        enc = self._encrypt(str(creds.get("password", "")))
        if enc is None:
            logger.warning(
                "FERNET_KEY not set — credentials kept in memory only (not persisted)"
            )
            return
        try:
            from sqlalchemy import text

            async with self._db.begin() as conn:
                await conn.execute(text("delete from mt5_connections"))
                await conn.execute(
                    text(
                        """
                        insert into mt5_connections
                            (server, login, enc_password, terminal_path, symbol, status,
                             last_heartbeat)
                        values (:server, :login, :enc, :tp, :symbol, 'connected', now())
                        """
                    ),
                    {
                        "server": self.state.server,
                        "login": self.state.login,
                        "enc": enc,
                        "tp": creds.get("terminal_path"),
                        "symbol": self.state.symbol,
                    },
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("mt5 connection persist failed: %s", exc)

    async def _update_stored_status(self, status: str) -> None:
        if self._db is None:
            return
        try:
            from sqlalchemy import text

            async with self._db.begin() as conn:
                await conn.execute(
                    text(
                        "update mt5_connections set status = :s, last_heartbeat = now()"
                    ),
                    {"s": status},
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("stored status update failed: %s", exc)

    async def try_restore(self) -> bool:
        """Startup reconnect from the stored (encrypted) credentials."""
        if self._db is None:
            return False
        try:
            from sqlalchemy import text

            async with self._db.connect() as conn:
                row = (
                    await conn.execute(
                        text(
                            "select server, login, enc_password, terminal_path, symbol"
                            " from mt5_connections order by created_at desc limit 1"
                        )
                    )
                ).first()
        except Exception as exc:  # noqa: BLE001
            logger.warning("credential restore failed: %s", exc)
            return False
        if row is None:
            return False
        password = self._decrypt(row[2])
        if password is None:
            logger.warning("stored password undecryptable — manual reconnect needed")
            return False
        try:
            await self.connect(
                {
                    "server": row[0],
                    "login": row[1],
                    "password": password,
                    "terminal_path": row[3],
                    "symbol": row[4],
                }
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("stored-credential reconnect failed: %s", exc)
            return False

    # -------------------------------------------------------------- heartbeat

    def _ensure_heartbeat(self) -> None:
        if self._heartbeat_task is None or self._heartbeat_task.done():
            self._stop.clear()
            self._heartbeat_task = asyncio.create_task(
                self._heartbeat_loop(), name="mt5-heartbeat"
            )

    def _stop_heartbeat(self) -> None:
        self._stop.set()
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None

    async def _heartbeat_loop(self) -> None:
        """Monitor + auto-reconnect (SPEC Phase 2: reconnect within 10s)."""
        offset_checked = time_mod.monotonic()
        try:
            while not self._stop.is_set():
                await asyncio.sleep(HEARTBEAT_S)
                if self.state.status == "disconnected":
                    continue
                ok = await self._source.is_connected()
                if ok:
                    self.state.status = "connected"
                    await self._update_stored_status("connected")
                    # C4 — refresh broker offset twice a day
                    if (
                        hasattr(self._source, "refresh_offset_daily")
                        and time_mod.monotonic() - offset_checked > OFFSET_REFRESH_S
                    ):
                        try:
                            await self._source.refresh_offset_daily()
                        except Exception:  # noqa: BLE001
                            pass
                        offset_checked = time_mod.monotonic()
                    continue
                if self.state.status != "reconnecting":
                    self.state.status = "reconnecting"
                    await self._broadcast_status()
                    await self._stop_runtime()
                    logger.warning("connection lost — reconnecting every %.0fs", RECONNECT_DELAY_S)
                if self._creds is not None:
                    try:
                        await self._source.connect(self._creds)
                    except Exception as exc:  # noqa: BLE001 — retry next beat
                        logger.info("reconnect attempt failed: %s", exc)
                        continue
                    self.state.status = "connected"
                    await self._start_runtime()
                    await self._broadcast_status()
                    logger.info("reconnected successfully")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("heartbeat loop crashed")

    # ----------------------------------------------------------------- status

    async def status(self) -> dict:
        connected = self.state.status == "connected"
        account = self.state.account
        if connected and self._source is not None:
            info = self._source.account_info()
            if asyncio.iscoroutine(info):
                info = await info
            if info is not None:
                account = info
                self.state.account = info
        offset = getattr(self._source, "broker_utc_offset_minutes", 0)
        return {
            "status": self.state.status,
            "symbol": self.state.symbol,
            "account": {
                "balance": account.get("balance") if account else None,
                "equity": account.get("equity") if account else None,
                "currency": account.get("currency", "USD") if account else None,
                "login": account.get("login") if account else None,
                "server": account.get("server") if account else None,
                "leverage": account.get("leverage") if account else None,
            },
            "broker_time_utc_offset": offset,
            "engine_running": self.runtime is not None and self.runtime.running,
        }

    async def _broadcast_status(self) -> None:
        if self._hub is None:
            return
        st = await self.status()
        await self._hub.broadcast_all(
            "mt5_status", {"status": st["status"], "symbol": st["symbol"]}
        )

    # ------------------------------------------------------------- delegation

    @property
    def source(self) -> DataSource:
        return self._source

    @property
    def symbol(self) -> str | None:
        return self.state.symbol

    async def place_order(self, order: Order) -> OrderResult:
        return await self._source.place_order(order)

    async def get_positions(self) -> list[Position]:
        return await self._source.get_positions()

    async def shutdown(self) -> None:
        self._stop_heartbeat()
        await self._stop_runtime()
        try:
            await self._source.disconnect()
        except Exception:  # noqa: BLE001 — shutdown must never raise
            logger.warning("error while disconnecting data source", exc_info=True)


def create_data_source(settings: Settings) -> DataSource:
    """Select the DataSource implementation via DATA_SOURCE (SPEC C7)."""
    if settings.data_source == "mt5":
        from app.mt5.mt5_source import MT5DataSource  # lazy (C7)

        return MT5DataSource(terminal_path=settings.mt5_terminal_path)
    from app.mt5.mock_source import MockDataSource

    return MockDataSource()
