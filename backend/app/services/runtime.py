"""EngineRuntime — glues MarketStream + SignalEngine + SignalTracker together.

Started by the ConnectionManager after a successful connect; stopped on
disconnect. Owns:
- the tick -> tracker.on_tick (SL/TP touch) + engine.note_spread path,
- the bar_close -> engine.on_bar_close + tracker expiry path,
- the tracker status callback -> repo.update_status + WS signal_update,
- a 5s account poller -> WS account events (SPEC §7.2).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.engine.config import EngineConfig
from app.engine.engine import SignalEngine
from app.engine.repo import SignalRepo
from app.engine.tracker import SignalTracker, TrackedSignal
from app.mt5.base import TIMEFRAME_MINUTES, DataSource
from app.services.market import MarketStream

logger = logging.getLogger("xauusd.runtime")

ACCOUNT_POLL_S = 5.0


class EngineRuntime:
    def __init__(
        self,
        source: DataSource,
        hub: Any,
        repo: SignalRepo,
        symbol: str,
        point_size: float,
        cfg: EngineConfig,
        news_service: Any | None = None,
        trading_manager: Any | None = None,  # Phase 4 signal relay
    ) -> None:
        self._source = source
        self._hub = hub
        self._symbol = symbol
        self.trading_manager = trading_manager
        self.tracker = SignalTracker()
        self.engine = SignalEngine(
            cfg=cfg, hub=hub, repo=repo, news_service=news_service,
            point_size=point_size,
        )
        self.engine.on_signal = self._relay_signal
        self.stream = MarketStream(
            source=source, hub=hub, symbol=symbol,
            engine_tf=cfg.timeframe, point_size=point_size,
        )
        self.stream.on_tick = self._on_tick
        self.stream.on_bar_close = self._on_bar_close
        self.tracker.on_status = self._on_signal_status
        self._account_task: asyncio.Task | None = None
        self._watchdog_task: asyncio.Task | None = None
        self._stopped = asyncio.Event()

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        self._stopped.clear()
        await self.stream.start()
        self._account_task = asyncio.create_task(
            self._account_loop(), name="account-poll"
        )
        self._watchdog_task = asyncio.create_task(
            self._stream_watchdog(), name="stream-watchdog"
        )
        logger.info("engine runtime started (%s)", self._symbol)

    async def stop(self) -> None:
        self._stopped.set()
        await self.stream.stop()
        for task in (self._account_task, self._watchdog_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        self._account_task = self._watchdog_task = None
        logger.info("engine runtime stopped")

    @property
    def running(self) -> bool:
        return self.stream.running and not self._stopped.is_set()

    @property
    def symbol(self) -> str:
        """D-051 — the market this runtime's engine evaluates."""
        return self._symbol

    async def apply_config(self, cfg: EngineConfig) -> None:
        """Live config update (PUT /api/config) — engine + stream TF follow."""
        await self.engine.apply_config(cfg)
        self.stream._engine_tf = cfg.timeframe  # noqa: SLF001 — same package glue
        if self.trading_manager is not None:
            await self.trading_manager.apply_config(cfg)

    async def _relay_signal(self, payload: dict, symbol: str, point_size: float) -> None:
        """Engine signal -> every armed trading plane (agent core, Phase 4)."""
        if self.trading_manager is None:
            return
        await self.trading_manager.relay_signal(payload, symbol, point_size)

    # -------------------------------------------------------------- callbacks

    async def _on_tick(self, tick) -> None:
        self.engine.note_spread(tick.bid, tick.ask)
        await self.tracker.on_tick(tick.bid, tick.ask)

    async def _on_bar_close(self, tf: str, bar: dict) -> None:
        cfg = self.engine.cfg
        # engine evaluation
        await self.engine.on_bar_close(tf, bar, self._source, self.tracker, self._symbol)
        # tracker expiry counts engine-TF bars
        if tf == cfg.timeframe:
            from datetime import UTC, datetime, timedelta

            close_time = datetime.fromtimestamp(bar["t"], tz=UTC) + timedelta(
                minutes=TIMEFRAME_MINUTES[tf]
            )
            # D-050 — pending-fill fallback (bar low/high) + the separate
            # pending-expiry budget for unfilled limit orders
            await self.tracker.on_bar_close(
                cfg.expiry_bars, close_time, bar["c"],
                pending_expiry_bars=cfg.pending_expiry_bars,
                bar_low=bar.get("l"), bar_high=bar.get("h"),
            )

    async def _on_signal_status(self, sig: TrackedSignal) -> None:
        from app.engine.repo import SignalRepo  # noqa: F401 — type hint only

        repo: SignalRepo = self.engine._repo  # noqa: SLF001 — glue layer
        await repo.update_status(sig.id, sig.status, sig.result_r, sig.closed_at)
        await self._hub.broadcast_all(
            "signal_update",
            {
                "id": sig.id,
                "status": sig.status,
                "result_r": sig.result_r,
                "closed_at": sig.closed_at.isoformat() if sig.closed_at else None,
            },
        )
        logger.info(
            "signal %s -> %s (r=%s)", sig.id[:8], sig.status, sig.result_r
        )
        # D-036 — the live MT5 plane keeps its REAL position in step
        # (expiry -> terminal close; won/lost -> record reconciliation).
        if self.trading_manager is not None:
            notify = getattr(self.trading_manager, "notify_signal_status", None)
            if notify is not None:
                try:
                    await notify(sig.id, sig.status)
                except Exception:  # noqa: BLE001 — must never kill the callback
                    logger.exception("live exit sync notification failed")

    # ----------------------------------------------------------------- loops

    async def _account_loop(self) -> None:
        """D-044 — institution-account events are ADMIN-ONLY (the terminal
        balance/positions are company data; each user's own account streams
        to them via their trading plane's broadcast_user loop)."""
        try:
            while not self._stopped.is_set():
                info = self._source.account_info()
                if asyncio.iscoroutine(info):
                    info = await info
                if info is not None:
                    positions = await self._source.get_positions()
                    await self._hub.broadcast_admins(
                        "account",
                        {
                            "balance": info["balance"],
                            "equity": info["equity"],
                            "currency": info["currency"],
                            "positions": [
                                {
                                    "ticket": p.ticket,
                                    "symbol": p.symbol,
                                    "side": p.side,
                                    "volume": p.volume,
                                    "profit": p.profit,
                                }
                                for p in positions
                            ],
                        },
                    )
                await asyncio.sleep(ACCOUNT_POLL_S)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — poller must not die silently
            logger.exception("account poll loop crashed")

    async def _stream_watchdog(self) -> None:
        """Restart the stream task if it dies while the runtime is up."""
        try:
            while not self._stopped.is_set():
                await asyncio.sleep(2.0)
                if not self.stream.running:
                    logger.warning("market stream not running — restarting")
                    await self.stream.start()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("stream watchdog crashed")
