"""UserTradingManager — per-user trading planes (Phase 4, agent architecture).

The platform is an AGENT: everyone watches the same public market feed
(candles, ticks, signals) from the platform ConnectionManager, but a user who
wants to trade connects their OWN MT5/Exness account and gets an isolated
trading plane:

  plane = { DataSource (demo: market sibling mock / live: Windows bridge),
            OrderExecutor (§9 risk + kill switches, owner-scoped),
            per-user auto_trade arm, per-user credential row }

Isolation guarantees:
- Credentials are Fernet-encrypted per owner row and NEVER returned to the
  frontend after save (masked as ••••; SPEC §13).
- One user can never see or touch another user's positions/orders/trades —
  every plane operation takes the owner id and routes WS events with
  `broadcast_user`.
- Engine signals are relayed to every ARMED plane (copy-trading agent): the
  same §9 executor math (lot sizing + kill switches) runs per user.

Live mode (C1): the MetaTrader5 package binds to one terminal per process,
so per-user LIVE trading requires a per-user worker on the Windows bridge
(v2 scope, DECISIONS.md D-024). On Linux/Railway a live connect stores the
credentials with status "bridge_required" instead of silently simulating.
Demo mode works everywhere: the plane prices orders/positions off the SAME
deterministic market as the public chart (MockDataSource.sibling).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.config import Settings
from app.engine.config import EngineConfig
from app.engine.executor import OrderExecutor, TradeRepo
from app.mt5.base import Order, Position

logger = logging.getLogger("xauusd.trading")

ACCOUNT_POLL_S = 5.0
DEMO_START_BALANCE = 10_000.0

#: D-044 — per-user money-management settings (persisted in
#: user_accounts.settings, merged over the global engine config so every
#: user's risk profile is their own).
USER_SETTING_FIELDS = (
    "risk_mode", "risk_percent", "fixed_lot", "max_positions",
    "daily_max_loss_pct", "rr", "min_sl_atr", "max_spread_points",
)

#: D-046 — upsert of the user's settings row. NOTE: the jsonb cast MUST be
#: written as CAST(:s AS JSONB), never as a bind name glued to a double-colon
#: cast — SQLAlchemy's text() does not treat a ':name' immediately followed
#: by a double-colon as a bind param, so under the asyncpg dialect the named
#: param leaks into the SQL as a literal and Postgres rejects the statement
#: (syntax error at the stray colon). See tests/test_d044_isolation.py.
UPSERT_USER_SETTINGS_SQL = (
    "insert into user_accounts (owner, settings, updated_at)"
    " values (:o, CAST(:s AS JSONB), now())"
    " on conflict (owner) do update set"
    " settings = excluded.settings, updated_at = now()"
)


@dataclass
class TradingPlane:
    """One user's isolated trading account state."""

    owner: str
    mode: str  # "demo" | "live" | "bridge_required"
    server: str
    login: str  # masked on output
    source: Any  # DataSource (demo sibling / live)
    executor: OrderExecutor
    symbol: str = "XAUUSDm"  # plane's own trading symbol (D-034)
    connected_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    poll_task: asyncio.Task | None = None

    def status(self) -> dict:
        info = self.source.account_info()
        if asyncio.iscoroutine(info):
            info = None  # sync contexts call status_async instead
        return {
            "connected": True,
            "mode": self.mode,
            "server": self.server,
            "login_masked": _mask_login(self.login),
            "auto_trade": self.executor.auto_trade,
            "account": info,
        }

    async def status_async(self) -> dict:
        info = self.source.account_info()
        if asyncio.iscoroutine(info):
            info = await info
        return {
            "connected": True,
            "mode": self.mode,
            "server": self.server,
            "login_masked": _mask_login(self.login),
            "auto_trade": self.executor.auto_trade,
            "account": info,
        }


def _mask_login(login: str) -> str:
    """Never echo a full account number back (SPEC §13 masking)."""
    if len(login) <= 2:
        return "••"
    return f"{login[:2]}{'•' * max(len(login) - 4, 2)}{login[-2:]}"


class UserTradingManager:
    """Owns every connected user plane + the admin platform executor."""

    def __init__(
        self,
        settings: Settings,
        public_source: Any,  # platform DataSource (market feed + admin plane)
        hub: Any,  # WSHub
        db_engine: Any,
        config_repo: Any,  # ConfigRepo — live engine config for executors
        fernet: Any = None,  # shared Fernet cipher (from ConnectionManager)
        platform_manager: Any = None,  # ConnectionManager (admin plane)
    ) -> None:
        self._settings = settings
        self._public = public_source
        self._hub = hub
        self._db = db_engine
        self._config_repo = config_repo
        self._fernet = fernet
        self._platform = platform_manager
        self._planes: dict[str, TradingPlane] = {}
        self._repo = TradeRepo(db_engine)
        self._platform_executor: OrderExecutor | None = None
        self._live_auto: Any | None = None  # D-036 McpAutoTrader (real terminal)
        self._persist_fp: dict[str, str] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------ admin plane

    def attach_platform_executor(self, executor: OrderExecutor) -> None:
        """The admin's own plane: auto_trade == engine_config.auto_trade."""
        self._platform_executor = executor

    @property
    def platform_executor(self) -> OrderExecutor | None:
        return self._platform_executor

    # ------------------------------------------------------- live MT5 plane

    def attach_live_auto_trader(self, trader: Any) -> None:
        """D-036 — the REAL-terminal auto-executor (AI signal -> MT5 order)."""
        self._live_auto = trader

    @property
    def live_auto_trader(self) -> Any | None:
        return self._live_auto

    # ------------------------------------------------------------- encryption

    def _cipher(self) -> Any | None:
        if self._fernet is not None:
            return self._fernet
        key = getattr(self._settings, "fernet_key", None)
        if not key:
            return None
        try:
            from cryptography.fernet import Fernet

            self._fernet = Fernet(key.encode() if isinstance(key, str) else key)
            return self._fernet
        except Exception:  # noqa: BLE001
            return None

    def _encrypt(self, password: str) -> str | None:
        cipher = self._cipher()
        if cipher is None:
            return None
        try:
            return cipher.encrypt(password.encode()).decode()
        except Exception:  # noqa: BLE001
            return None

    def _decrypt(self, enc: str) -> str | None:
        cipher = self._cipher()
        if cipher is None:
            return None
        try:
            return cipher.decrypt(enc.encode()).decode()
        except Exception:  # noqa: BLE001
            return None

    # ---------------------------------------------------------------- connect

    async def connect(self, owner: str, creds: dict) -> dict:
        """Connect a user's own trading account (agent flow).

        mode "demo": instant paper-trading plane priced off the public market.
        mode "live": REAL MT5 execution through the wine bridge when it is
        reachable (D-034 — the sandbox/VPS runs the MT5 terminal + gateway);
        otherwise credentials are stored with status "bridge_required"
        (honest state — never silently simulate live).
        """
        async with self._lock:
            if owner in self._planes:
                await self._teardown_plane(owner)

            mode = creds.get("mode", "demo")
            if mode == "live":
                source = await self._make_live_source()
                if source is None:
                    stored = await self._store_creds(
                        owner, creds, status="bridge_required", mode="live"
                    )
                    return {
                        "connected": False,
                        "mode": "live",
                        "status": "bridge_required",
                        "detail": (
                            "Live MT5 execution needs the MT5 bridge "
                            "(terminal + gateway, DECISIONS D-024/D-034). "
                            "Credentials stored encrypted; retry when the "
                            "bridge host is running."
                        ),
                        "login_masked": _mask_login(str(creds.get("login", ""))),
                        "stored": stored,
                    }
            else:
                source = self._make_user_source(creds)
            await source.connect(creds)
            symbol = self._discover(source)
            point = 0.01
            point_getter = getattr(source, "point_size", None)
            if callable(point_getter):
                point = point_getter(symbol)

            cfg, _auto = await self._config_repo.load(self._db)
            executor = OrderExecutor(
                source=source,
                cfg=cfg,
                repo=self._repo,
                hub=self._hub,
                owner=owner,
            )
            plane = TradingPlane(
                owner=owner,
                mode=mode if mode != "live" else "live",
                server=str(creds.get("server", "")),
                login=str(creds.get("login", "")),
                source=source,
                executor=executor,
                symbol=symbol,
            )
            self._planes[owner] = plane
            await self._store_creds(owner, creds, status="connected", mode=mode)
            plane.poll_task = asyncio.create_task(
                self._plane_poll(owner), name=f"plane-poll-{owner[:8]}"
            )
            masked = _mask_login(str(creds.get("login", "")))
            await self._trading_log(
                owner, "info",
                f"trading plane connected ({mode}) — login {masked}",
            )
            st = await plane.status_async()
            st["symbol"] = symbol
            st["point_size"] = point
            return st

    async def _make_live_source(self) -> Any | None:
        """Real MT5 source for live planes when the bridge is reachable (D-034).

        DATA_SOURCE=mt5 (Windows native) always qualifies; on Linux the wine
        bridge (gateway.py over the real terminal) must answer /health.
        """
        if self._settings.data_source == "mt5":
            from app.mt5.mt5_source import MT5DataSource

            return MT5DataSource()
        from app.mt5.bridge import bridge_available

        if bridge_available():
            from app.mt5.mt5_source import MT5DataSource

            return MT5DataSource()
        return None

    def _make_user_source(self, creds: dict) -> Any:
        """Demo planes share the public market (identical prices).

        With the live feed (D-030) siblings are paper accounts priced off the
        REAL market — every user sees the same real-time gold prices.
        """
        from app.mt5.live_source import LiveDataSource
        from app.mt5.mock_source import MockDataSource

        if isinstance(self._public, LiveDataSource):
            src = LiveDataSource.sibling(self._public)
            src.set_starting_balance(DEMO_START_BALANCE)
            return src
        if isinstance(self._public, MockDataSource):
            src = MockDataSource.sibling(self._public)
            src.set_starting_balance(DEMO_START_BALANCE)
            return src
        return MockDataSource()

    def _discover(self, source: Any) -> str:
        try:
            candidates = source.discover_symbols("*XAUUSD*") or []
            return min(candidates, key=len) if candidates else "XAUUSDm"
        except Exception:  # noqa: BLE001
            return "XAUUSDm"

    async def disconnect(self, owner: str) -> dict:
        async with self._lock:
            await self._teardown_plane(owner)
        await self._update_stored_status(owner, "disconnected")
        return {"connected": False, "mode": None, "auto_trade": False}

    async def _teardown_plane(self, owner: str) -> None:
        plane = self._planes.pop(owner, None)
        if plane is None:
            return
        if plane.poll_task is not None:
            plane.poll_task.cancel()
            try:
                await plane.poll_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        try:
            await plane.source.disconnect()
        except Exception:  # noqa: BLE001
            pass
        await self._trading_log(owner, "info", "trading plane disconnected")

    # ------------------------------------------------------------ persistence

    async def _store_creds(self, owner: str, creds: dict, status: str, mode: str) -> bool:
        enc = self._encrypt(str(creds.get("password", "")))
        if enc is None or self._db is None:
            if enc is None:
                logger.warning(
                    "FERNET_KEY missing — user credentials not persisted (memory only)"
                )
            return False
        try:
            from sqlalchemy import text

            async with self._db.begin() as conn:
                await conn.execute(
                    text("delete from mt5_connections where owner = :o"),
                    {"o": owner},
                )
                await conn.execute(
                    text(
                        """
                        insert into mt5_connections
                            (owner, server, login, enc_password, terminal_path,
                             symbol, status, last_heartbeat, mode)
                        values (:o, :server, :login, :enc, :tp, :symbol, :status,
                                now(), :mode)
                        """
                    ),
                    {
                        "o": owner,
                        "server": str(creds.get("server", "")),
                        "login": str(creds.get("login", "")),
                        "enc": enc,
                        "tp": creds.get("terminal_path"),
                        "symbol": self._symbol_hint(),
                        "status": status,
                        "mode": mode,
                    },
                )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("user credential store failed: %s", exc)
            return False

    def _symbol_hint(self) -> str:
        try:
            return self._platform.symbol or "XAUUSDm"
        except Exception:  # noqa: BLE001
            return "XAUUSDm"

    async def _update_stored_status(self, owner: str, status: str) -> None:
        if self._db is None:
            return
        try:
            from sqlalchemy import text

            async with self._db.begin() as conn:
                await conn.execute(
                    text(
                        "update mt5_connections set status = :s, last_heartbeat = now()"
                        " where owner = :o"
                    ),
                    {"s": status, "o": owner},
                )
        except Exception:  # noqa: BLE001
            pass

    async def try_restore_planes(self) -> int:
        """Reconnect demo planes for users with stored connected credentials."""
        if self._db is None:
            return 0
        try:
            from sqlalchemy import text

            async with self._db.connect() as conn:
                rows = (
                    await conn.execute(
                        text(
                            "select owner, server, login, enc_password, mode, symbol"
                            " from mt5_connections where status = 'connected'"
                            " and mode in ('demo','live')"
                        )
                    )
                ).fetchall()
        except Exception:  # noqa: BLE001
            return 0
        restored = 0
        for owner, server, login, enc, mode, symbol in rows:
            password = self._decrypt(enc)
            if password is None:
                continue
            try:
                await self.connect(
                    owner,
                    {
                        "server": server,
                        "login": login,
                        "password": password,
                        "mode": mode,
                        "symbol": symbol,
                    },
                )
                restored += 1
            except Exception:  # noqa: BLE001
                logger.warning("plane restore failed for %s", owner)
        return restored

    # ------------------------------------------------------- practice planes

    async def ensure_plane(self, owner: str) -> TradingPlane:
        """D-044 — auto-provision the user's PRACTICE plane on first touch.

        Every platform user gets an isolated paper account (own balance,
        positions, trades, risk settings) priced off the SAME institutional
        market feed — no broker link required. State persists in
        user_accounts/user_positions and is restored here after restarts.
        Idempotent: an existing plane (demo creds / practice) is returned.
        """
        plane = self._planes.get(owner)
        if plane is not None:
            return plane
        async with self._lock:
            plane = self._planes.get(owner)
            if plane is not None:
                return plane
            account = await self._load_account(owner)
            source = self._make_user_source(
                {"server": "practice", "login": owner, "password": "none"}
            )
            try:
                await source.connect(
                    {"server": "practice", "login": owner, "password": "none",
                     "mode": "practice"}
                )
            except Exception as exc:  # noqa: BLE001 — market may still be warming
                logger.debug("practice plane connect deferred: %s", exc)
            if account is not None:
                source.set_starting_balance(float(account.get("balance", DEMO_START_BALANCE)))
                positions, next_ticket = await self._load_positions(owner)
                if positions:
                    source.restore_positions(positions, next_ticket)
            symbol = self._discover(source)
            cfg = await self._user_cfg(owner)
            executor = OrderExecutor(
                source=source, cfg=cfg, repo=self._repo, hub=self._hub, owner=owner,
            )
            if account is not None and account.get("auto_trade"):
                executor.arm(True)
            plane = TradingPlane(
                owner=owner, mode="practice", server="Institutional Feed",
                login=owner, source=source, executor=executor, symbol=symbol,
            )
            self._planes[owner] = plane
            plane.poll_task = asyncio.create_task(
                self._plane_poll(owner), name=f"plane-poll-{owner[:8]}"
            )
            if account is None:
                await self._persist_plane(plane)  # create the default row
            return plane

    async def _load_account(self, owner: str) -> dict | None:
        """user_accounts row for the owner (None = first ever touch)."""
        if self._db is None:
            return None
        try:
            from sqlalchemy import text

            async with self._db.connect() as conn:
                row = (
                    await conn.execute(
                        text(
                            "select balance, currency, auto_trade, settings"
                            " from user_accounts where owner = :o"
                        ),
                        {"o": owner},
                    )
                ).first()
            if row is None:
                return None
            settings = row[3]
            if isinstance(settings, str):
                import json

                try:
                    settings = json.loads(settings)
                except ValueError:
                    settings = {}
            return {
                "balance": float(row[0]),
                "currency": row[1],
                "auto_trade": bool(row[2]),
                "settings": settings or {},
            }
        except Exception:  # noqa: BLE001 — DB hiccup: defaults apply
            return None

    async def _load_positions(self, owner: str) -> tuple[list[Position], int | None]:
        """Persisted open positions for the owner's practice plane."""
        if self._db is None:
            return [], None
        try:
            from sqlalchemy import text

            async with self._db.connect() as conn:
                rows = (
                    await conn.execute(
                        text(
                            "select ticket, symbol, side, volume, price_open,"
                            " sl, tp, opened_at from user_positions"
                            " where owner = :o order by ticket"
                        ),
                        {"o": owner},
                    )
                ).fetchall()
        except Exception:  # noqa: BLE001
            return [], None
        positions = [
            Position(
                ticket=int(r[0]), symbol=r[1], side=r[2], volume=float(r[3]),
                price_open=float(r[4]),
                sl=float(r[5]) if r[5] is not None else None,
                tp=float(r[6]) if r[6] is not None else None,
                profit=0.0,
                time=r[7] if r[7].tzinfo else r[7].replace(tzinfo=UTC),
            )
            for r in rows
        ]
        next_ticket = max((p.ticket for p in positions), default=0) or None
        return positions, next_ticket

    def _plane_fingerprint(self, plane: TradingPlane, info: dict | None) -> str:
        try:
            positions = plane.source._positions  # noqa: SLF001 — same-package state
            payload = (
                f"{info.get('balance') if info else '?'}|"
                + ",".join(
                    f"{p.ticket}:{p.volume}:{p.price_open}" for p in positions
                )
            )
            return hashlib.sha256(payload.encode()).hexdigest()
        except Exception:  # noqa: BLE001
            return ""

    async def _persist_plane(self, plane: TradingPlane) -> None:
        """Snapshot the practice plane (balance + open positions) to the DB.

        Called from the poll loop on CHANGES only (fingerprint compare), so
        every fill/close/SL-TP is durable across restarts.
        """
        if self._db is None:
            return
        try:
            from sqlalchemy import text

            info = plane.source.account_info()
            if asyncio.iscoroutine(info):
                info = await info
            fp = self._plane_fingerprint(plane, info)
            if fp and fp == self._persist_fp.get(plane.owner):
                return
            self._persist_fp[plane.owner] = fp
            balance = float(info.get("balance", DEMO_START_BALANCE)) if info else None
            positions = list(plane.source._positions)  # noqa: SLF001
            async with self._db.begin() as conn:
                await conn.execute(
                    text(
                        "insert into user_accounts (owner, balance, auto_trade, updated_at)"
                        " values (:o, :b, :a, now())"
                        " on conflict (owner) do update set"
                        " balance = excluded.balance, auto_trade = excluded.auto_trade,"
                        " updated_at = now()"
                    ),
                    {"o": plane.owner, "b": balance, "a": plane.executor.auto_trade},
                )
                await conn.execute(
                    text("delete from user_positions where owner = :o"),
                    {"o": plane.owner},
                )
                for p in positions:
                    await conn.execute(
                        text(
                            "insert into user_positions"
                            " (ticket, owner, symbol, side, volume, price_open,"
                            "  sl, tp, opened_at)"
                            " values (:t, :o, :s, :sd, :v, :po, :sl, :tp, :oa)"
                            " on conflict (ticket) do nothing"
                        ),
                        {
                            "t": int(p.ticket), "o": plane.owner, "s": p.symbol,
                            "sd": p.side, "v": float(p.volume),
                            "po": float(p.price_open),
                            "sl": float(p.sl) if p.sl is not None else None,
                            "tp": float(p.tp) if p.tp is not None else None,
                            "oa": p.time,
                        },
                    )
        except Exception as exc:  # noqa: BLE001 — persistence must never kill the loop
            logger.debug("plane persist failed for %s: %s", plane.owner[:8], exc)

    async def _user_cfg(self, owner: str) -> EngineConfig:
        """Global engine config with the USER's money-management overrides."""
        cfg, _ = await self._config_repo.load(self._db)
        account = await self._load_account(owner)
        settings = (account or {}).get("settings") or {}
        if settings:
            data = cfg.model_dump()
            for k in USER_SETTING_FIELDS:
                if k in settings and settings[k] is not None:
                    data[k] = settings[k]
            try:
                cfg = EngineConfig(**data)
            except Exception as exc:  # noqa: BLE001 — bad stored values: global cfg
                logger.warning("user settings for %s invalid: %s", owner[:8], exc)
        return cfg

    async def user_settings(self, owner: str) -> dict:
        """The user's money-management settings (defaults from global cfg)."""
        cfg, _ = await self._config_repo.load(self._db)
        account = await self._load_account(owner)
        settings = (account or {}).get("settings") or {}
        out = {k: settings.get(k, getattr(cfg, k, None)) for k in USER_SETTING_FIELDS}
        out["balance"] = (account or {}).get("balance", DEMO_START_BALANCE)
        out["currency"] = (account or {}).get("currency", "USD")
        return out

    async def set_user_settings(self, owner: str, patch: dict) -> dict:
        """Validate + persist the user's settings and live-apply them."""
        clean: dict = {}
        numeric_bounds = {
            "risk_percent": (0.01, 10.0),
            "fixed_lot": (0.01, 100.0),
            "max_positions": (1, 50),
            "daily_max_loss_pct": (0.5, 100.0),
            "rr": (0.5, 10.0),
            "min_sl_atr": (0.3, 6.0),
            "max_spread_points": (5, 500),
        }
        for k, v in (patch or {}).items():
            if k not in USER_SETTING_FIELDS or v is None:
                continue
            if k == "risk_mode":
                if str(v) not in ("percent", "fixed"):
                    raise ValueError("risk_mode must be 'percent' or 'fixed'")
                clean[k] = str(v)
                continue
            try:
                num = float(v)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{k} must be a number") from exc
            lo, hi = numeric_bounds.get(k, (0.0, 1e9))
            if not lo <= num <= hi:
                raise ValueError(f"{k} out of range ({lo}..{hi})")
            clean[k] = int(num) if k in ("max_positions", "max_spread_points") else num
        if self._db is None:
            return clean
        import json

        from sqlalchemy import text

        async with self._db.begin() as conn:
            await conn.execute(
                text(UPSERT_USER_SETTINGS_SQL),
                {"o": owner, "s": json.dumps(clean)},
            )
        # live-apply to the plane executor if it exists
        plane = self._planes.get(owner)
        if plane is not None:
            await plane.executor.apply_config(await self._user_cfg(owner))
        return clean

    async def reset_practice_account(self, owner: str) -> dict:
        """Reset the practice account to the starting balance, flat."""
        plane = self._planes.get(owner)
        if plane is not None:
            try:
                for p in list(plane.source._positions):  # noqa: SLF001
                    await plane.source.close_position(p.ticket)
            except Exception:  # noqa: BLE001
                pass
            plane.source.set_starting_balance(DEMO_START_BALANCE)
            plane.executor.arm(False)
            await self._persist_plane(plane)
        if self._db is not None:
            from sqlalchemy import text

            try:
                async with self._db.begin() as conn:
                    await conn.execute(
                        text(
                            "update user_accounts set balance = :b, auto_trade = false,"
                            " updated_at = now() where owner = :o"
                        ),
                        {"b": DEMO_START_BALANCE, "o": owner},
                    )
            except Exception:  # noqa: BLE001
                pass
        await self._trading_log(
            owner, "info", f"practice account reset to ${DEMO_START_BALANCE:,.0f}"
        )
        return {"balance": DEMO_START_BALANCE, "auto_trade": False}

    async def restore_practice_planes(self) -> int:
        """Boot-time restore of every persisted practice plane (auto-trade
        users keep executing signals even before they open the app)."""
        if self._db is None:
            return 0
        try:
            from sqlalchemy import text

            async with self._db.connect() as conn:
                rows = (
                    await conn.execute(
                        text("select owner from user_accounts")
                    )
                ).fetchall()
        except Exception:  # noqa: BLE001
            return 0
        restored = 0
        for (owner,) in rows:
            try:
                await self.ensure_plane(str(owner))
                restored += 1
            except Exception:  # noqa: BLE001
                logger.warning("practice plane restore failed for %s", str(owner)[:8])
        return restored

    # --------------------------------------------------------------- plane ops

    def plane(self, owner: str) -> TradingPlane | None:
        return self._planes.get(owner)

    async def status(self, owner: str) -> dict:
        plane = self._planes.get(owner)
        if plane is None:
            # D-044 — auto-provision the practice plane on first status read
            try:
                plane = await self.ensure_plane(owner)
            except Exception:  # noqa: BLE001 — degraded DB/feed: stored state only
                stored = await self._stored_status(owner)
                return {
                    "connected": False,
                    "mode": "practice",
                    "auto_trade": False,
                    **stored,
                }
        st = await plane.status_async()
        return st

    async def _stored_status(self, owner: str) -> dict:
        if self._db is None:
            return {}
        try:
            from sqlalchemy import text

            async with self._db.connect() as conn:
                row = (
                    await conn.execute(
                        text(
                            "select status, mode, login, server from mt5_connections"
                            " where owner = :o order by created_at desc limit 1"
                        ),
                        {"o": owner},
                    )
                ).first()
            if row is None:
                return {}
            return {
                "status": row[0],
                "mode": row[1],
                "login_masked": _mask_login(row[2]) if row[2] else None,
                "server": row[3],
            }
        except Exception:  # noqa: BLE001
            return {}

    async def positions(self, owner: str) -> list[dict]:
        plane = self._planes.get(owner)
        if plane is None:
            try:
                plane = await self.ensure_plane(owner)
            except Exception:  # noqa: BLE001
                return []
        positions = await plane.source.get_positions()
        return [
            {
                "ticket": p.ticket,
                "symbol": p.symbol,
                "side": p.side,
                "volume": p.volume,
                "price_open": p.price_open,
                "sl": p.sl,
                "tp": p.tp,
                "profit": p.profit,
                "time": p.time.isoformat(),
            }
            for p in positions
        ]

    async def place_manual_order(
        self, owner: str, side: str, volume: float,
        sl: float | None = None, tp: float | None = None,
    ) -> dict:
        """Manual market order on the user's OWN plane (TradePanel)."""
        plane = self._planes.get(owner)
        if plane is None:
            plane = await self.ensure_plane(owner)  # D-044 practice plane
        from app.mt5.base import validate_tf  # noqa: F401 — keep imports local

        symbol = getattr(plane, "symbol", None) or self._symbol_hint()
        order = Order(
            symbol=symbol, side=side, volume=volume, sl=sl, tp=tp,
            deviation=30, magic=0, comment="xauai-manual",
        )
        result = await plane.source.place_order(order)
        await self._repo.insert(
            {
                "signal_id": None,
                "owner": owner,
                "ticket": result.ticket,
                "side": side,
                "volume": volume,
                "price_open": result.price,
                "sl": sl,
                "tp": tp,
                "opened_at": datetime.now(tz=UTC).isoformat(),
            }
        )
        await self._trading_log(
            owner,
            "info" if result.ok else "warning",
            f"manual {side} {volume} {symbol} -> retcode={result.retcode} {result.comment}",
        )
        return {
            "ok": result.ok,
            "ticket": result.ticket,
            "price": result.price,
            "retcode": result.retcode,
            "comment": result.comment,
        }

    async def close_position(self, owner: str, ticket: int) -> dict:
        plane = self._planes.get(owner)
        if plane is None:
            try:
                plane = await self.ensure_plane(owner)
            except Exception:  # noqa: BLE001
                raise ValueError("trading plane not available") from None
        result = await plane.source.close_position(ticket)
        await self._trading_log(
            owner,
            "info" if result.ok else "warning",
            f"close #{ticket} -> {result.comment}",
        )
        await self._close_trade_rows(owner, ticket, result)
        return {
            "ok": result.ok,
            "ticket": ticket,
            "price": result.price,
            "retcode": result.retcode,
            "comment": result.comment,
        }

    async def _close_trade_rows(self, owner: str, ticket: int, result: Any) -> None:
        if self._db is None:
            return
        try:
            from sqlalchemy import text

            async with self._db.begin() as conn:
                await conn.execute(
                    text(
                        "update trades set price_close = :p, closed_at = now()"
                        " where owner = :o and ticket = :t"
                    ),
                    {"p": result.price, "o": owner, "t": ticket},
                )
        except Exception:  # noqa: BLE001
            pass

    async def trade_history(self, owner: str, limit: int = 100) -> list[dict]:
        return await self._repo.list_for_owner(owner, limit)

    async def set_auto_trade(self, owner: str, enabled: bool) -> dict:
        """D-044 — arm/disarm the USER's own plane (practice account by
        default; no broker link needed). Persists the arm state."""
        plane = self._planes.get(owner)
        if plane is None:
            if not enabled:
                return {"auto_trade": False}
            plane = await self.ensure_plane(owner)  # practice plane always available
        plane.executor.arm(enabled)
        if self._db is not None:
            try:
                from sqlalchemy import text

                async with self._db.begin() as conn:
                    await conn.execute(
                        text(
                            "update user_accounts set auto_trade = :a,"
                            " updated_at = now() where owner = :o"
                        ),
                        {"a": enabled, "o": owner},
                    )
                    await conn.execute(
                        text(
                            "update mt5_connections set auto_trade = :a"
                            " where owner = :o"
                        ),
                        {"a": enabled, "o": owner},
                    )
            except Exception:  # noqa: BLE001
                pass
        await self._trading_log(
            owner,
            "warning" if enabled else "info",
            f"auto-trade {'ARMED' if enabled else 'disarmed'} for this account",
        )
        return {"auto_trade": enabled}

    # ------------------------------------------------------------ signal relay

    async def relay_signal(self, signal: dict, symbol: str, point_size: float) -> None:
        """Fan a platform engine signal out to every ARMED plane + the admin
        plane (copy-trading agent core, SPEC §9 executor per user) + the
        REAL MT5 terminal plane (D-036 AI signal -> auto-order)."""
        if self._platform_executor is not None and self._platform_executor.auto_trade:
            try:
                await self._platform_executor.execute_signal(signal, symbol, point_size)
            except Exception:  # noqa: BLE001 — one plane must not break others
                logger.exception("platform executor failed on signal relay")
        if self._live_auto is not None:
            try:
                await self._live_auto.on_signal(signal, symbol, point_size)
            except Exception:  # noqa: BLE001 — live plane must not break others
                logger.exception("live auto-trader failed on signal relay")
        for owner, plane in list(self._planes.items()):
            if not plane.executor.auto_trade:
                continue
            try:
                await plane.executor.execute_signal(signal, symbol, point_size)
            except Exception:  # noqa: BLE001
                logger.exception("plane %s executor failed on signal relay", owner[:8])

    async def notify_signal_status(self, signal_id: str, status: str) -> None:
        """D-036 — tracker outcomes reach the live plane (expiry -> close)."""
        if self._live_auto is None:
            return
        try:
            await self._live_auto.notify_signal_status(signal_id, status)
        except Exception:  # noqa: BLE001 — exit sync must never crash the runtime
            logger.exception("live auto-trader exit sync failed")

    async def apply_config(self, cfg: EngineConfig) -> None:
        """Live config updates reach every executor (PUT /api/config)."""
        if self._platform_executor is not None:
            await self._platform_executor.apply_config(cfg)
        if self._live_auto is not None:
            try:
                await self._live_auto.apply_config(cfg)
            except Exception:  # noqa: BLE001
                logger.exception("live auto-trader config apply failed")
        for plane in self._planes.values():
            await plane.executor.apply_config(cfg)

    # ----------------------------------------------------------------- loops

    async def _plane_poll(self, owner: str) -> None:
        """5s loop per plane: SL/TP watcher (D-044), equity/positions
        broadcast to just this user, and change-driven DB persistence."""
        try:
            while True:
                plane = self._planes.get(owner)
                if plane is None:
                    return
                try:
                    # D-044 — broker-like SL/TP execution on paper positions
                    check = getattr(plane.source, "check_stops", None)
                    if check is not None:
                        for hit in await check():
                            await self._repo_mark_closed(owner, hit)
                            await self._trading_log(
                                owner,
                                "info" if hit["profit"] >= 0 else "warning",
                                f"{hit['kind'].upper()} hit — position #{hit['ticket']}"
                                f" closed @ {hit['price']:.2f}"
                                f" (P/L {hit['profit']:+.2f})",
                            )
                    info = plane.source.account_info()
                    if asyncio.iscoroutine(info):
                        info = await info
                    positions = await plane.source.get_positions()
                    if info:
                        await self._hub.broadcast_user(
                            owner,
                            "trading_account",
                            {
                                "mode": plane.mode,
                                "balance": info.get("balance"),
                                "equity": info.get("equity"),
                                "currency": info.get("currency", "USD"),
                                "auto_trade": plane.executor.auto_trade,
                                "positions": [
                                    {
                                        "ticket": p.ticket,
                                        "symbol": p.symbol,
                                        "side": p.side,
                                        "volume": p.volume,
                                        "price_open": p.price_open,
                                        "sl": p.sl,
                                        "tp": p.tp,
                                        "profit": p.profit,
                                        "time": p.time.isoformat(),
                                    }
                                    for p in positions
                                ],
                            },
                        )
                    # D-044 — persist balance/positions on change
                    await self._persist_plane(plane)
                except Exception:  # noqa: BLE001 — poll must survive
                    logger.debug("plane poll hiccup for %s", owner[:8], exc_info=True)
                await asyncio.sleep(ACCOUNT_POLL_S)
        except asyncio.CancelledError:
            raise

    async def _repo_mark_closed(self, owner: str, hit: dict) -> None:
        """SL/TP close -> reconcile the trades row for that position."""
        if self._db is None:
            return
        try:
            from sqlalchemy import text

            async with self._db.begin() as conn:
                await conn.execute(
                    text(
                        "update trades set price_close = :p, profit = :pr,"
                        " closed_at = now() where owner = :o and ticket = :t"
                        " and closed_at is null"
                    ),
                    {
                        "p": hit["price"], "pr": hit["profit"],
                        "o": owner, "t": int(hit["ticket"]),
                    },
                )
        except Exception:  # noqa: BLE001
            pass

    async def _trading_log(self, owner: str, level: str, message: str) -> None:
        logger.log(
            getattr(logging, level.upper(), logging.INFO),
            "[plane %s] %s", owner[:8], message,
        )
        try:
            await self._hub.broadcast_user(
                owner, "trading_log", {"level": level, "message": message}
            )
        except Exception:  # noqa: BLE001
            pass

    async def shutdown(self) -> None:
        for owner in list(self._planes):
            await self._teardown_plane(owner)
