"""BrokerConnectionService — per-user broker connections over the REAL MT5
terminal (D-037).

User requirement (Bengali, D-037): "যখন যে লোক তার exness তথ্য দিয়ে
একাউন্ট করবে, সেই ট্রেড শুধু তার একাউন্টে দেখা যাবে" — every platform user
connects with THEIR OWN Exness credentials; trades/history/balance are then
visible only inside that user's session.

Architecture (honest, terminal-first):
- The platform NEVER talks to the broker directly — everything goes through
  the real MetaTrader 5 terminal (user requirement since D-034).
- The terminal host keeps broker sessions with STORED credentials
  (terminal auto-login, D-034). A user "connects" by entering
  (server, login, password); the service verifies the entered login+server
  against the LIVE terminal session and binds the user's app account to it.
- The password is Fernet-encrypted at rest (mt5_connections.enc_password)
  for auditing and for the future direct-login provisioning flow.
- If the entered account is not the terminal's session (another Exness
  account), the user gets an honest 422: only accounts provisioned on the
  terminal host can connect (the admin provisions new broker accounts via
  scripts/mt5_login.sh on the VPS — see DECISIONS.md D-037).
- All /api/mt5/* trading routes are gated on the USER's active connection,
  so users never see anyone else's trades: the account they see is the one
  THEY connected (and only they are bound to it).
"""

from __future__ import annotations

import asyncio
import logging
import time as time_mod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger("xauusd.broker")


@dataclass
class BrokerConnection:
    """One user's active broker connection (in-memory state).

    `admin_verified` — D-075: the link was verified against the LIVE
    institution terminal (the admin bind). True means status() may probe
    the terminal for the account snapshot; False is a public user's
    broker-link profile which must NEVER touch institution data.
    Restored-from-DB connections carry the persisted flag.
    """

    user_id: str
    login: str
    server: str
    connected_at: float = field(default_factory=time_mod.time)
    last_seen: float = field(default_factory=time_mod.time)
    # last verified account snapshot (from the terminal MCP)
    account: dict[str, Any] = field(default_factory=dict)
    # D-075 — restored/persisted admin-verified bind flag
    admin_verified: bool = False


class NotConnectedError(RuntimeError):
    """The user has no active broker connection."""


class BrokerUnavailableError(RuntimeError):
    """The MT5 terminal bridge is not reachable on this host."""


class AccountMismatchError(ValueError):
    """The entered account is not provisioned on the terminal host."""


def _norm(s: str | None) -> str:
    return (s or "").strip().lower()


class BrokerConnectionService:
    """Owns the per-user broker connection states (D-037).

    demo_mode (DATA_SOURCE=mock, tests): connect() records the entered
    credentials without a terminal round-trip — the mock market keeps
    streaming (D-018 semantics). Production (live + terminal) verifies
    against the LIVE terminal session as documented above.
    """

    def __init__(self, db_engine: Any = None, settings: Any = None,
                 fernet: Any = None, demo_mode: bool = False) -> None:
        self._db = db_engine
        self._settings = settings
        self._fernet = fernet
        self._demo = bool(demo_mode)
        self._connections: dict[str, BrokerConnection] = {}
        self._lock = asyncio.Lock()

    # --------------------------------------------------------------- db
    async def _run_db(self, fn):
        """Run one DB operation `fn(conn) -> T` inside a transaction.

        D-076 — THE D-075 REGRESSION, root-caused live: restore/_persist/
        disconnect used `with self._db.begin()` inside asyncio.to_thread,
        which only works on a SYNC engine (what tests pass). Production
        passes the ASYNC engine from app.state — a sync `with` on it raises
        TypeError ('_AsyncGeneratorContextManager' object does not support
        the context manager protocol'), so the broker-link RESTORE silently
        returned 0 rows on every Railway boot and the connect flow's
        persist 500ed. On the async engine the operation runs through
        AsyncConnection.run_sync (fn receives the REAL sync Connection in
        the greenlet context, transaction commits on exit); sync engines
        keep the thread hop (test path unchanged).
        """
        if self._db is None:
            return None
        try:
            from sqlalchemy.ext.asyncio import AsyncEngine

            is_async = isinstance(self._db, AsyncEngine)
        except ImportError:  # pragma: no cover — greenlet guaranteed in prod
            is_async = False
        if is_async:
            async with self._db.begin() as acx:
                return await acx.run_sync(fn)

        def _sync() -> Any:
            with self._db.begin() as cx:  # type: ignore[union-attr]
                return fn(cx)

        return await asyncio.to_thread(_sync)

    # ------------------------------------------------------------ restore
    async def restore(self) -> int:
        """D-075 — rebuild the in-memory broker links from the DB at boot.

        The bug this kills (user report, Bengali): "Fronted এ exness not
        connected দেখাচ্ছে, কিন্তু অটো সিগন্যাল এন্ট্রি হচ্ছে" — the MT5
        terminal bridge survives server restarts (it is the terminal app
        on the host) and the auto-trader restores its armed state, so
        orders kept flowing while every per-user broker link read
        "disconnected" until the user re-linked by hand: the links lived
        ONLY in memory, the persisted rows were never read back.

        Restored admin-verified links (broker_admin=true) probe the
        terminal on the next status() call — reachable => "connected"
        with the live account snapshot, unreachable => "reconnecting"
        (honest). Public broker-links come back as "linked" and never
        touch institution data (D-044 isolation preserved). Rows whose
        status was set 'disconnected' by an explicit unlink are skipped.
        """
        if self._db is None:
            return 0

        def _run(cx) -> list[dict]:
            import sqlalchemy as sa  # local: tests run without it

            rows = cx.execute(sa.text(
                "select owner, server, login, broker_admin "
                "from mt5_connections "
                "where status in ('connected', 'linked')"
            )).mappings().all()
            return [dict(r) for r in rows]

        try:
            rows = await self._run_db(_run)
        except Exception as exc:  # noqa: BLE001 — restore is best-effort
            logger.warning("broker link restore read failed: %s", exc)
            return 0

        restored = 0
        async with self._lock:
            for row in rows:
                try:
                    uid = str(row["owner"])
                except (KeyError, TypeError):
                    continue
                if not uid or uid in self._connections:
                    continue
                self._connections[uid] = BrokerConnection(
                    user_id=uid,
                    login=str(row.get("login") or ""),
                    server=str(row.get("server") or ""),
                    # demo stacks never probe the terminal (D-044 guard)
                    admin_verified=bool(row.get("broker_admin")) and not self._demo,
                )
                restored += 1
        if restored:
            logger.info(
                "restored %d broker link(s) from persistence — the status is "
                "honest after restarts (D-075)",
                restored,
            )
        return restored

    # ------------------------------------------------------------- fernet
    def _cipher(self) -> Any | None:
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
            logger.warning("Fernet init failed — broker passwords not persisted: %s", exc)
            return None

    def _encrypt(self, password: str) -> str | None:
        cipher = self._cipher()
        if cipher is None:
            return None
        try:
            return cipher.encrypt(password.encode()).decode()
        except Exception:  # noqa: BLE001
            return None

    # ---------------------------------------------------------- terminal
    @staticmethod
    def _terminal_account() -> dict[str, Any]:
        """Live account snapshot from the REAL terminal (blocking MCP call).

        D-078 — failures carry the actionable remediation (what the 502
        MEANS, where to fix it) instead of a raw transport error: the
        user's "exness লগিং দিলে frontend এ কানেক্ট হয় না" report was this
        exact opaque string.
        """
        from app.mt5.bridge_config import hint_for_error
        from app.mt5.mcp import MCPError, terminal_client

        try:
            return terminal_client().account()
        except MCPError as exc:
            raise BrokerUnavailableError(
                f"MetaTrader 5 terminal bridge unavailable: {exc} — "
                f"{hint_for_error(str(exc))}"
            ) from exc
        except Exception as exc:  # noqa: BLE001 — any transport failure
            raise BrokerUnavailableError(
                f"MetaTrader 5 terminal bridge unreachable: {exc} — "
                f"{hint_for_error(str(exc))}"
            ) from exc

    # ----------------------------------------------------------- connect
    async def connect(self, user_id: str, server: str, login: str,
                      password: str, admin: bool = True) -> dict[str, Any]:
        """Link a broker account to THIS user (D-044 per-user isolation).

        - admin=True: verify (server, login) against the live INSTITUTION
          terminal session and bind (the institution's own account).
        - admin=False (public user): store the user's OWN broker profile
          (Fernet-encrypted) with status "linked" — no institution-terminal
          data is exposed or required. Execution stays on the user's
          practice plane until the institution provisions live routing.
        """
        async with self._lock:
            if self._demo or not admin:
                masked = self._mask(login)
                conn = BrokerConnection(
                    user_id=user_id, login=login, server=server,
                    account={"login": masked, "server": server,
                             "currency": "USD", "balance": None},
                )
                self._connections[user_id] = conn
                await self._persist(conn, password)
                return {
                    "status": "linked",
                    "login_masked": masked,
                    "server": server,
                    "mode": "broker-link",
                    "detail": (
                        "Broker linked to your account. Trading executes on "
                        "your practice balance; live routing is activated by "
                        "the institution after verification."
                    ),
                }
            res = await asyncio.to_thread(self._terminal_account)
            acct = res.get("account", {}) or {}
            term = res.get("terminal", {}) or {}

            if term.get("server_connected") is not True:
                raise BrokerUnavailableError(
                    "the trading terminal has no active broker session on "
                    "this host (market/server down or not provisioned)"
                )

            term_login = str(acct.get("login") or "")
            term_server = str(acct.get("server") or "")
            if _norm(term_login) != _norm(login) or _norm(term_server) != _norm(server):
                raise AccountMismatchError(
                    f"no terminal session for account {login} @ {server} — only "
                    "broker accounts provisioned on this terminal host can be "
                    "connected (ask the admin to provision your account)"
                )

            conn = BrokerConnection(
                user_id=user_id,
                login=term_login or login,
                server=term_server or server,
                account=self._snapshot(acct, term),
                admin_verified=True,  # D-075 — terminal-verified bind
            )
            self._connections[user_id] = conn
            await self._persist(conn, password)
            logger.info(
                "broker connect OK: user=%s account=%s@%s balance=%s",
                user_id, conn.login, conn.server, acct.get("balance"),
            )
            return {
                "status": "connected",
                "login": conn.login,
                "server": conn.server,
                "account": dict(conn.account),
            }

    @staticmethod
    def _mask(login: str) -> str:
        """Never echo a full account number back (privacy)."""
        if len(login) <= 2:
            return "••"
        return f"{login[:2]}{'•' * max(len(login) - 4, 2)}{login[-2:]}"

    @staticmethod
    def _snapshot(acct: dict, term: dict) -> dict[str, Any]:
        return {
            "login": acct.get("login"),
            "name": acct.get("name"),
            "server": acct.get("server"),
            "broker": acct.get("broker"),
            "type": acct.get("type"),
            "currency": acct.get("currency"),
            "leverage": acct.get("leverage"),
            "balance": acct.get("balance"),
            "equity": acct.get("equity"),
            "margin_free": acct.get("margin_free"),
            "profit": acct.get("profit"),
            "mcp_trade_allowed": term.get("mcp_trade_allowed"),
            "build": term.get("build"),
        }

    async def _persist(self, conn: BrokerConnection, password: str) -> None:
        """Store the connection row (encrypted password) when a DB exists."""
        if self._db is None:
            return
        enc = self._encrypt(password)
        if enc is None:
            logger.warning(
                "broker connect for %s not persisted (no FERNET_KEY configured)",
                conn.user_id,
            )
            return

        def _run(cx) -> None:
            import sqlalchemy as sa  # local: tests run without it

            hb = datetime.now(UTC)  # bound in Python — portable across PG/sqlite
            cx.execute(
                sa.text(
                    "delete from mt5_connections where owner = :owner"
                ),
                {"owner": conn.user_id},
            )
            cx.execute(
                sa.text(
                    "insert into mt5_connections "
                    "(owner, server, login, enc_password, mode, status, "
                    " last_heartbeat, broker_admin) values "
                    "(:owner, :server, :login, :enc, 'live', 'connected', "
                    " :hb, :admin)"
                ),
                {
                    "owner": conn.user_id,
                    "server": conn.server,
                    "login": conn.login,
                    "enc": enc,
                    "hb": hb,
                    # D-075 — restore() reads this back: admin-verified
                    # binds probe the terminal, public links never do
                    "admin": conn.admin_verified,
                },
            )

        try:
            await self._run_db(_run)
        except Exception as exc:  # noqa: BLE001 — persistence is best-effort
            logger.warning("broker connection persist failed: %s", exc)

    # -------------------------------------------------------- disconnect
    async def disconnect(self, user_id: str) -> dict[str, Any]:
        async with self._lock:
            self._connections.pop(user_id, None)
        if self._db is not None:
            try:
                import sqlalchemy as sa

                def _run(cx) -> None:
                    cx.execute(
                        sa.text(
                            "update mt5_connections set status = "
                            "'disconnected' where owner = :owner"
                        ),
                        {"owner": user_id},
                    )

                await self._run_db(_run)
            except Exception as exc:  # noqa: BLE001
                logger.debug("broker disconnect persist failed: %s", exc)
        return {"status": "disconnected"}

    # ------------------------------------------------------------- status
    async def status(self, user_id: str) -> dict[str, Any]:
        conn = self._connections.get(user_id)
        if conn is None:
            return {"status": "disconnected"}
        # D-075 — a terminal-verified bind (fresh connect OR restored from
        # persistence) may probe the terminal for its account snapshot; a
        # public broker-link profile must never see institution data.
        terminal_verified = conn.admin_verified or bool(
            conn.account.get("balance") is not None or conn.account.get("name")
        )
        if not terminal_verified:
            # D-044 broker-LINK (public user): per-user profile only — no
            # institution-terminal data to expose or refresh.
            return {
                "status": "linked",
                "login_masked": self._mask(conn.login),
                "server": conn.server,
                "connected_at": conn.connected_at,
            }
        if self._demo:
            return {
                "status": "connected",
                "mode": "demo",
                "login": conn.login,
                "server": conn.server,
                "connected_at": conn.connected_at,
                "account": dict(conn.account),
            }
        # live refresh of the account snapshot (cheap local MCP call)
        try:
            res = await asyncio.to_thread(self._terminal_account)
            acct = res.get("account", {}) or {}
            term = res.get("terminal", {}) or {}
            connected = term.get("server_connected") is True
            conn.account = self._snapshot(acct, term)
            conn.last_seen = time_mod.time()
            if not connected:
                return {"status": "reconnecting", "login": conn.login,
                        "server": conn.server, "account": dict(conn.account)}
        except BrokerUnavailableError:
            return {"status": "reconnecting", "login": conn.login,
                    "server": conn.server, "account": dict(conn.account)}
        return {
            "status": "connected",
            "login": conn.login,
            "server": conn.server,
            "connected_at": conn.connected_at,
            "account": dict(conn.account),
        }

    # -------------------------------------------------------------- gate
    def get(self, user_id: str) -> BrokerConnection:
        conn = self._connections.get(user_id)
        if conn is None:
            raise NotConnectedError(
                "connect your MetaTrader 5 broker account first "
                "(⋮ → Connect broker)"
            )
        return conn

    def connected_users(self) -> int:
        return len(self._connections)
