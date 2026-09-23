"""McpAutoTrader — AI signal -> auto-order on the REAL MT5 terminal (D-036).

Wires the platform signal engines (XAUUSD + BTCUSD, SPEC §8) to REAL order
execution through the MetaTrader 5 terminal MCP bridge:

    engine.on_bar_close -> signal -> UserTradingManager.relay_signal
        -> McpAutoTrader.on_signal
            -> pre-guards (armed, terminal up, broker market OPEN)
            -> OrderExecutor.execute_signal   [unchanged §9 risk core:
                 idempotency per signal_id, daily-loss / max-positions /
                 spread kill switches, size_lot on REAL equity, retry,
                 emergency close-all + disarm]
            -> terminal market_order(symbol, side, lots, SL, TP, comment)

Safety properties (user-visible honesty, never silent):
- A SEPARATE explicit live arm (`auto_trade_live`), distinct from the paper
  auto_trade kill switch; admin-only + typed confirmation "ENABLE".
- Orders are NEVER queued: if the terminal is down or the broker market is
  closed (weekend/holiday — the forex-closed logic), the signal is kept and
  the skip is broadcast with the honest reason.
- Broker-side SL/TP ride with every order (Exness executes exits server-side
  even if the platform goes down). Signal EXPIRY (§8 tracker) closes the
  linked terminal position; won/lost are reconciled in the trades table.
- Every action broadcasts a structured `mt5_auto` WS event + engine_log line.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from app.engine.config import EngineConfig
from app.engine.executor import OrderExecutor, TradeRepo
from app.mt5.mcp_source import McpTradingSource

logger = logging.getLogger("xauusd.autolive")

#: callable(platform_symbol) -> (broker_market_open, detail) — injected by
#: main.py from the live feed's MT5 overlay (weekend/holiday detection).
MarketState = Callable[[str], tuple[bool, str]]

#: statuses from the signal tracker that mean the position is gone
CLOSED_STATUSES = {"won", "lost", "expired", "cancelled"}


class ArmError(RuntimeError):
    """Raised when arming is refused (terminal down / trading not allowed)."""


class McpAutoTrader:
    """Owns the live arm + the §9 executor bound to the real terminal."""

    def __init__(
        self,
        source: McpTradingSource,
        cfg: EngineConfig,
        repo: TradeRepo,
        hub: Any = None,
        config_repo: Any = None,
        db_engine: Any = None,
        market_state: MarketState | None = None,
    ) -> None:
        self._source = source
        self._hub = hub
        self._config_repo = config_repo
        self._db = db_engine
        self._market_state = market_state
        self._repo = repo
        self._cfg = cfg
        self._executor = OrderExecutor(
            source=source, cfg=cfg, repo=repo, hub=hub, owner=None
        )
        self._owner: str | None = None  # arming admin (trades-table linkage)
        self._armed_at: str | None = None
        # D-054 — the arming admin's money-management window, merged over
        # the global config on every apply_config (survives PUT /api/config
        # and the boot restore). None = institution runs on global defaults.
        self._money_window: dict | None = None

    # ------------------------------------------------------------------ arm

    @property
    def armed(self) -> bool:
        return self._executor.auto_trade

    @property
    def owner(self) -> str | None:
        return self._owner

    async def arm(self, enabled: bool, owner: str | None = None) -> dict:
        """Arm/disarm live auto-execution (admin action, typed-confirmed).

        D-054 — arming anchors the USD money window on the terminal's REAL
        equity (the typed day_start_balance is a prefill hint only): the old
        path never anchored, so `_ensure_day_anchor` fell back to
        first-order equity — correct — but with the GLOBAL config's window
        values (0 = off) unless apply_money_window ran, the admin's window
        was a placebo that never guarded the institution executor.
        """
        if enabled:
            info = await self._source.account_info()
            if info is None:
                raise ArmError(
                    "MT5 terminal bridge unavailable — cannot arm live trading"
                )
            if not info.get("trade_allowed"):
                raise ArmError(
                    "the MetaTrader terminal does not allow automated trading"
                    " — enable the AutoTrading button (Ctrl+E) and"
                    " Tools → Options → AI Assistant → Trading = Enabled"
                )
            self._owner = owner
            self._armed_at = datetime.now(tz=UTC).isoformat()
            equity = float(info.get("equity", 0.0))
            self._executor.arm(True, day_start_equity=equity or None)
        else:
            self._executor.arm(False)
        self._executor.set_owner(self._owner)
        if self._config_repo is not None:
            await self._config_repo.save_auto_live(
                self._db, enabled, owner if enabled else None
            )
        level = "critical" if enabled else "info"
        msg = (
            f"LIVE AUTO-TRADE ARMED by {owner or '?'} — every AI signal now"
            f" places a REAL order on {self._account_label()}"
            if enabled
            else "live auto-trade DISARMED — AI signals will not place orders"
        )
        await self._emit(level, msg, {"event": "armed" if enabled else "disarmed",
                                      "armed": enabled})
        return {"armed": enabled, "owner": owner}

    async def apply_money_window(self, settings: dict) -> None:
        """D-054 — the arming ADMIN's money window governs this executor.

        The money-management modal used to save into user settings while
        this arm path bound the institution executor to the GLOBAL config
        (daily_loss_usd / daily_profit_usd / fixed_lot at their defaults) —
        the admin's typed limits never reached the executor that actually
        trades. This merges the admin's window over the global cfg and
        re-anchors the day on the terminal's real equity.
        """
        window = {
            k: settings[k]
            for k in (
                "risk_mode", "fixed_lot", "max_positions",
                "max_trades_per_day", "daily_loss_usd", "daily_profit_usd",
                "day_start_balance",
            )
            if settings.get(k) is not None
        }
        self._money_window = window or None
        await self._apply_window_to_executor()

    async def _apply_window_to_executor(self) -> None:
        if self._money_window:
            data = self._cfg.model_dump()
            data.update(self._money_window)
            try:
                cfg = EngineConfig(**data)
            except Exception as exc:  # noqa: BLE001 — bad values: keep global
                logger.warning("admin money window invalid: %s", exc)
                return
            await self._executor.apply_config(cfg)
            # re-anchor the day on real equity (fallback: typed balance)
            info = await self._source.account_info()
            equity = float((info or {}).get("equity", 0.0))
            if equity <= 0:
                equity = float(self._money_window.get("day_start_balance") or 0.0)
            if self.armed and equity > 0:
                self._executor.arm(True, day_start_equity=equity)

    async def restore(self, owner: str | None = None) -> bool:
        """Boot-time restore of the persisted arm (CRITICAL log when armed)."""
        if self._config_repo is None:
            return False
        armed, armed_by = await self._config_repo.load_auto_live(self._db)
        if not armed:
            return False
        self._owner = owner or armed_by
        self._executor.set_owner(self._owner)
        self._executor.arm(True)
        logger.critical(
            "LIVE AUTO-TRADE armed at boot (persisted state) — real orders"
            " will be placed by AI signals on the MT5 terminal"
        )
        return True

    async def apply_config(self, cfg: EngineConfig) -> None:
        """PUT /api/config reaches the live executor (risk params stay live).

        D-054 — the admin's money window is re-merged AFTER the global
        config lands, so a config save can no longer silently wipe the
        USD stop-loss / target / fixed-lot the admin armed with.
        """
        self._cfg = cfg
        await self._executor.apply_config(cfg)
        await self._apply_window_to_executor()

    # -------------------------------------------------------------- signal in

    async def on_signal(self, signal: dict, symbol: str, point_size: float) -> None:
        """Relay hook — one AI signal in, at most one REAL order out."""
        signal_id = str(signal.get("id") or "")
        if not self.armed:
            return  # disarmed: nothing to do (no noise)

        # terminal reachable?
        if not await self._source.is_connected():
            await self._skip(
                signal_id, symbol, "MT5 terminal unreachable — order NOT placed"
            )
            return
        info = await self._source.account_info()
        if info is None or not info.get("trade_allowed"):
            await self._skip(
                signal_id, symbol,
                "terminal not allowing automated trading (enable AutoTrading"
                " Ctrl+E + Options → AI Assistant → Trading = Enabled)"
                " — order NOT placed",
            )
            return

        # broker market open? (forex weekend/holiday logic — never queue)
        if self._market_state is not None:
            try:
                open_, detail = self._market_state(symbol)
            except Exception:  # noqa: BLE001 — check must never block trading
                open_, detail = True, "market state unknown"
            if not open_:
                await self._skip(
                    signal_id, symbol,
                    f"broker market closed ({detail}) — signal kept, order skipped",
                )
                return

        broker = await self._source.abroker_symbol(symbol)
        if not broker:
            await self._skip(
                signal_id, symbol,
                f"no broker symbol for {symbol} on this terminal — order skipped",
            )
            return

        result = await self._executor.execute_signal(signal, broker, point_size)
        if result is None:
            reason = self._executor.last_skip_reason or "skipped by risk guards"
            await self._skip(signal_id, symbol, f"order skipped: {reason}")
            return
        if result.ok:
            await self._emit(
                "info",
                f"AI AUTO-ORDER {signal.get('direction')} {broker}"
                f" {result.volume} lots @ {result.price}"
                f" (signal {signal_id[:8]})",
                {
                    "event": "order",
                    "ok": True,
                    "signal_id": signal_id,
                    "symbol": broker,
                    "side": signal.get("direction"),
                    "volume": result.volume,
                    "price": result.price,
                    "ticket": result.ticket,
                    "retcode": result.retcode,
                },
            )
        else:
            note = "market closed (weekend/holiday)" if result.retcode == 10018 else (
                result.comment or f"retcode {result.retcode}"
            )
            blocked = "not permitted" in note
            label = (
                "BLOCKED by terminal trading permissions"
                if blocked
                else "REJECTED by broker"
            )
            await self._emit(
                "warning",
                f"AI auto-order {label}: {note} (signal {signal_id[:8]})",
                {
                    "event": "order",
                    "ok": False,
                    "signal_id": signal_id,
                    "symbol": broker,
                    "side": signal.get("direction"),
                    "retcode": result.retcode,
                    "detail": note,
                },
            )

    # ------------------------------------------------------------ exit sync

    async def notify_signal_status(self, signal_id: str, status: str) -> None:
        """Tracker outcome -> keep the linked REAL position in step.

        won/lost : the broker SL/TP already closed it — reconcile the record.
        expired  : §8 expiry — CLOSE the terminal position now.
        """
        if status not in CLOSED_STATUSES:
            return
        try:
            trade = await self._repo.open_trade_for_signal(signal_id, self._owner)
        except Exception:  # noqa: BLE001
            trade = None
        if trade is None:
            return  # signal traded virtually only (not auto-executed)
        ticket = trade.get("ticket")

        if status == "expired" and ticket:
            res = await self._source.close_position(int(ticket))
            await self._repo.mark_closed(
                signal_id, self._owner, price_close=res.price
            )
            await self._emit(
                "info" if res.ok else "warning",
                f"signal {signal_id[:8]} expired — terminal position #{ticket} "
                + (f"closed @ {res.price}" if res.ok else f"close FAILED: {res.comment}"),
                {"event": "close", "ok": res.ok, "signal_id": signal_id,
                 "ticket": ticket, "price": res.price, "reason": "signal expired"},
            )
        elif status in ("won", "lost"):
            # broker-side SL/TP executed the exit
            await self._repo.mark_closed(signal_id, self._owner)
            await self._emit(
                "info",
                f"signal {signal_id[:8]} {status.upper()} — broker SL/TP closed"
                f" terminal position #{ticket}",
                {"event": "close", "ok": True, "signal_id": signal_id,
                 "ticket": ticket, "reason": f"signal {status}"},
            )

    # --------------------------------------------------------------- status

    async def status(self) -> dict:
        info = await self._source.account_info()
        cfg = self._cfg
        balance = info.get("balance") if info else None

        # D-039: per-symbol broker market state (forex weekend/holiday logic)
        markets: dict[str, dict] = {}
        if self._market_state is not None:
            for sym in ("XAUUSD", "BTCUSD"):
                try:
                    open_, detail = self._market_state(sym)
                except Exception:  # noqa: BLE001
                    open_, detail = True, "market state unknown"
                markets[sym] = {"open": bool(open_), "detail": detail}

        # D-039: one-line honest diagnosis — WHY is the AI not trading right
        # now? Surfaced in the AI Trading tab so the answer is never a guess.
        why: dict[str, str] | None = None
        if not self.armed:
            why = {
                "code": "not_armed",
                "text": "AI auto-trade is OFF — arm it in the AI Trading tab (type ENABLE).",
            }
        elif info is None:
            why = {
                "code": "terminal_down",
                "text": "MT5 terminal bridge unreachable — retrying automatically.",
            }
        elif not info.get("trade_allowed"):
            why = {
                "code": "trade_not_allowed",
                "text": (
                    "The MetaTrader terminal is blocking automated trading —"
                    " enable the AutoTrading button (Ctrl+E) and"
                    " Tools → Options → AI Assistant → Trading = Enabled."
                ),
            }
        elif balance is not None and float(balance) <= 0:
            why = {
                "code": "no_balance",
                "text": (
                    "Broker balance is 0.00 — top up your Exness account "
                    "before AI orders can execute."
                ),
            }
        elif markets.get("XAUUSD", {}).get("open") is False and markets.get(
            "BTCUSD", {}
        ).get("open") is False:
            why = {
                "code": "market_closed",
                "text": (
                    "Broker markets are closed (weekend/holiday) — signals"
                    " stay, orders resume at open."
                ),
            }
        else:
            why = {
                "code": "ready",
                "text": "Armed and ready — every AI signal places a real order.",
            }

        return {
            "armed": self.armed,
            "armed_at": self._armed_at,
            "armed_by": self._owner,
            "terminal": {
                "available": info is not None,
                "trade_allowed": bool(info and info.get("trade_allowed")),
                "server": info.get("server") if info else None,
                "login": info.get("login") if info else None,
                "balance": balance,
                "equity": info.get("equity") if info else None,
                "currency": info.get("currency") if info else None,
            },
            "markets": markets,
            "why": why,
            "risk": {
                "risk_mode": cfg.risk_mode,
                "risk_percent": cfg.risk_percent,
                "fixed_lot": cfg.fixed_lot,
                "max_positions": cfg.max_positions,
                "daily_max_loss_pct": cfg.daily_max_loss_pct,
                "max_spread_points": cfg.max_spread_points,
                "timeframe": cfg.timeframe,
            },
            "last_skip_reason": self._executor.last_skip_reason,
        }

    # -------------------------------------------------------------- helpers

    def _account_label(self) -> str:
        return "the MetaTrader 5 terminal account"

    async def _skip(self, signal_id: str, symbol: str, reason: str) -> None:
        await self._emit(
            "warning", f"AI signal {signal_id[:8]} ({symbol}): {reason}",
            {"event": "skip", "signal_id": signal_id, "symbol": symbol,
             "reason": reason},
        )

    async def _emit(self, level: str, message: str, event: dict) -> None:
        """Structured `mt5_auto` WS event + engine log line."""
        logger.log(getattr(logging, level.upper(), logging.INFO), "%s", message)
        if self._hub is None:
            return
        try:
            await self._hub.broadcast_all(
                "mt5_auto", {**event, "ts": datetime.now(tz=UTC).isoformat(),
                             "message": message, "level": level}
            )
        except Exception:  # noqa: BLE001
            pass
