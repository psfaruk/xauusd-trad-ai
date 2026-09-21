"""Executor + risk management (SPEC §9, Phase 4).

Pure functions (lot sizing, kill-switch evaluation) + an `OrderExecutor` that
runs only when `auto_trade == true` AND a data source is connected. Every
order is guarded by the three kill switches, idempotent per signal_id, and
persisted into the `trades` table with the signal linkage.

The same `size_lot()` math is reused by the backtest risk simulation so live
and simulated sizing can never drift apart (single source of truth).
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.engine.config import EngineConfig
from app.mt5.base import DataSource, Order, OrderResult, SymbolInfo

logger = logging.getLogger("xauusd.executor")

# XAUUSD standard: 1 lot = 100 oz (SPEC §9 fallback when symbol_info missing)
DEFAULT_CONTRACT_SIZE = 100.0
DEFAULT_VOLUME_MIN = 0.01
DEFAULT_VOLUME_MAX = 100.0
DEFAULT_VOLUME_STEP = 0.01


@dataclass(frozen=True)
class LotDecision:
    """Result of the lot-sizing formula (SPEC §9)."""

    lots: float
    risk_amount: float
    sl_distance: float
    raw_lots: float  # before volume_step rounding (diagnostics)
    clamped: bool  # hit volume_min/volume_max bounds


def size_lot(
    cfg: EngineConfig,
    equity: float,
    sl_distance: float,
    info: SymbolInfo | None = None,
) -> LotDecision:
    """`lots = risk_amount / (sl_distance x contract_size)`, floored to
    volume_step, clamped to [volume_min, volume_max] (SPEC §9).

    Unit-tested math — the executor and the backtest share this function.
    """
    info = info or SymbolInfo(name="XAUUSD")
    contract = info.contract_size or DEFAULT_CONTRACT_SIZE
    risk_amount = (
        equity * cfg.risk_percent / 100.0
        if cfg.risk_mode == "percent"
        else None
    )
    if risk_amount is None:  # fixed-lot mode
        lots = cfg.fixed_lot
        return LotDecision(
            lots=min(max(lots, info.volume_min), info.volume_max),
            risk_amount=0.0,
            sl_distance=sl_distance,
            raw_lots=lots,
            clamped=True,
        )

    if sl_distance <= 0:
        # Degenerate SL — safest possible size (never expected: SFP always
        # produces a positive buffer above the swing point).
        lots = info.volume_min
        return LotDecision(lots, risk_amount, sl_distance, lots, True)

    raw = risk_amount / (sl_distance * contract)
    step = info.volume_step or DEFAULT_VOLUME_STEP
    stepped = math.floor(raw / step) * step
    # guard float noise (0.30000000000000004 -> 0.30)
    stepped = round(stepped, 8)
    clamped = False
    if stepped < info.volume_min:
        stepped, clamped = info.volume_min, True
    if stepped > info.volume_max:
        stepped, clamped = info.volume_max, True
    return LotDecision(stepped, risk_amount, sl_distance, raw, clamped)


@dataclass(frozen=True)
class KillSwitchVerdict:
    """Outcome of the pre-order checks (SPEC §9 kill switches)."""

    allowed: bool
    reason: str = ""
    kill_daily_loss: bool = False  # triggers close-all + auto_trade=false


async def evaluate_kill_switches(
    cfg: EngineConfig,
    source: DataSource,
    symbol: str,
    point_size: float,
    spread_points: float,
    day_start_equity: float | None = None,
    positions: list | None = None,
) -> KillSwitchVerdict:
    """Check all three kill switches before EVERY order (SPEC §9).

    The daily-loss emergency is evaluated FIRST — even when max_positions
    would skip the order, a breached daily loss must still close everything
    and disarm (a bleeding open position must never be left unhandled).

    1. daily realized+floating loss >= daily_max_loss_pct -> close all,
       auto_trade=false, CRITICAL log (handled by the caller via the flag)
    2. max_positions reached -> skip
    3. current spread > max_spread_points -> skip
    """
    if positions is None:
        positions = await source.get_positions()

    info = source.account_info()
    if asyncio.iscoroutine(info):
        info = await info
    if info is not None and day_start_equity:
        equity = float(info.get("equity", 0.0))
        loss_pct = (day_start_equity - equity) / day_start_equity * 100.0
        if loss_pct >= cfg.daily_max_loss_pct:
            return KillSwitchVerdict(
                False,
                f"daily loss {loss_pct:.2f}% >= {cfg.daily_max_loss_pct}%",
                kill_daily_loss=True,
            )

    if len(positions) >= cfg.max_positions:
        return KillSwitchVerdict(False, f"max_positions {cfg.max_positions} reached")

    if spread_points > cfg.max_spread_points:
        return KillSwitchVerdict(
            False, f"spread {spread_points:.0f} > {cfg.max_spread_points} points"
        )
    return KillSwitchVerdict(True)


class TradeRepo:
    """`trades` table persistence with in-memory fallback (C6)."""

    def __init__(self, db_engine: Any) -> None:
        self._db = db_engine
        self._mem: list[dict] = []

    async def has_trade_for_signal(self, signal_id: str, owner: str | None = None) -> bool:
        if self._db is not None:
            try:
                from sqlalchemy import text

                async with self._db.connect() as conn:
                    if owner:
                        row = await conn.execute(
                            text("select 1 from trades where signal_id = :s and owner = :o"),
                            {"s": signal_id, "o": owner},
                        )
                    else:
                        row = await conn.execute(
                            text("select 1 from trades where signal_id = :s"),
                            {"s": signal_id},
                        )
                    return row.first() is not None
            except Exception:  # noqa: BLE001 — fall through to memory
                pass
        if owner:
            return any(
                t.get("signal_id") == signal_id and t.get("owner") == owner
                for t in self._mem
            )
        return any(t.get("signal_id") == signal_id for t in self._mem)

    async def insert(self, trade: dict) -> None:
        self._mem.append(trade)
        if self._db is None:
            return
        try:
            from sqlalchemy import text

            async with self._db.begin() as conn:
                await conn.execute(
                    text(
                        """
                        insert into trades
                            (signal_id, owner, ticket, side, volume, price_open,
                             sl, tp, opened_at)
                        values
                            (:signal_id, :owner, :ticket, :side, :volume, :price_open,
                             :sl, :tp, :opened_at)
                        """
                    ),
                    {
                        "signal_id": trade.get("signal_id"),
                        "owner": trade.get("owner"),
                        "ticket": trade.get("ticket"),
                        "side": trade["side"],
                        "volume": trade["volume"],
                        "price_open": trade["price_open"],
                        "sl": trade.get("sl"),
                        "tp": trade.get("tp"),
                        "opened_at": trade.get("opened_at"),
                    },
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("trade persist failed (kept in memory): %s", exc)

    async def open_trade_for_signal(
        self, signal_id: str, owner: str | None = None
    ) -> dict | None:
        """The still-open trade linked to a signal (D-036 exit sync)."""
        if self._db is not None:
            try:
                from sqlalchemy import text

                async with self._db.connect() as conn:
                    if owner:
                        row = (
                            await conn.execute(
                                text(
                                    "select * from trades where signal_id = :s"
                                    " and owner = :o and closed_at is null"
                                    " order by opened_at desc limit 1"
                                ),
                                {"s": signal_id, "o": owner},
                            )
                        ).mappings().first()
                    else:
                        row = (
                            await conn.execute(
                                text(
                                    "select * from trades where signal_id = :s"
                                    " and closed_at is null"
                                    " order by opened_at desc limit 1"
                                ),
                                {"s": signal_id},
                            )
                        ).mappings().first()
                if row is not None:
                    d = dict(row)
                    d["opened_at"] = (
                        d["opened_at"].isoformat() if d.get("opened_at") else None
                    )
                    return d
            except Exception:  # noqa: BLE001 — fall through to memory
                pass
        for t in reversed(self._mem):
            if (
                t.get("signal_id") == signal_id
                and (owner is None or t.get("owner") == owner)
                and not t.get("closed_at")
            ):
                return t
        return None

    async def mark_closed(
        self,
        signal_id: str,
        owner: str | None = None,
        price_close: float | None = None,
        profit: float | None = None,
    ) -> None:
        """Record the exit of a linked trade (best-effort, D-036)."""
        for t in reversed(self._mem):
            if (
                t.get("signal_id") == signal_id
                and (owner is None or t.get("owner") == owner)
                and not t.get("closed_at")
            ):
                t["closed_at"] = datetime.now(tz=UTC).isoformat()
                if price_close is not None:
                    t["price_close"] = price_close
                if profit is not None:
                    t["profit"] = profit
                break
        if self._db is None:
            return
        try:
            from sqlalchemy import text

            async with self._db.begin() as conn:
                await conn.execute(
                    text(
                        """
                        update trades set closed_at = now(),
                            price_close = coalesce(:pc, price_close),
                            profit = coalesce(:p, profit)
                         where signal_id = :s and closed_at is null
                           and (:o is null or owner = :o)
                        """
                    ),
                    {"s": signal_id, "o": owner, "pc": price_close, "p": profit},
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("trade close persist failed: %s", exc)

    async def list_for_owner(self, owner: str, limit: int = 100) -> list[dict]:
        if self._db is not None:
            try:
                from sqlalchemy import text

                async with self._db.connect() as conn:
                    rows = (
                        await conn.execute(
                            text(
                                "select t.*, s.direction as signal_direction,"
                                " s.status as signal_status"
                                " from trades t left join signals s on s.id = t.signal_id"
                                " where t.owner = :o order by t.opened_at desc limit :lim"
                            ),
                            {"o": owner, "lim": limit},
                        )
                    ).mappings().all()
                out = []
                for r in rows:
                    d = dict(r)
                    d["opened_at"] = d["opened_at"].isoformat() if d.get("opened_at") else None
                    d["closed_at"] = d["closed_at"].isoformat() if d.get("closed_at") else None
                    out.append(d)
                return out
            except Exception:  # noqa: BLE001
                pass
        mine = [t for t in self._mem if t.get("owner") == owner]
        return list(reversed(mine[-limit:]))


class OrderExecutor:
    """SPEC §9 executor — runs ONLY when auto_trade is true and connected."""

    def __init__(
        self,
        source: DataSource,
        cfg: EngineConfig,
        repo: TradeRepo,
        hub: Any = None,
        owner: str | None = None,
        day_start_equity: float | None = None,
    ) -> None:
        self._source = source
        self._cfg = cfg
        self._repo = repo
        self._hub = hub
        self._owner = owner  # None = platform admin account
        self._day_start_equity = day_start_equity
        self._lock = asyncio.Lock()
        self.auto_trade = False  # armed by the owner of the plane
        self.last_skip_reason: str | None = None  # D-036 — surfaced to the UI

    async def apply_config(self, cfg: EngineConfig) -> None:
        self._cfg = cfg

    def arm(self, enabled: bool) -> None:
        self.auto_trade = enabled
        self.last_skip_reason = None
        if enabled:
            # reset the daily-loss anchor on arming
            self._day_start_equity = None

    def set_owner(self, owner: str | None) -> None:
        """D-036 — re-scope the trades-table linkage (live plane: the arming
        admin's profile id) without rebuilding the executor."""
        self._owner = owner

    async def _ensure_day_anchor(self) -> None:
        if self._day_start_equity is None:
            info = self._source.account_info()
            if asyncio.iscoroutine(info):
                info = await info
            if info:
                self._day_start_equity = float(info.get("equity", 0.0))

    async def execute_signal(
        self, signal: dict, symbol: str, point_size: float
    ) -> OrderResult | None:
        """Send one market order for `signal` after all §9 guards.

        Returns None when skipped (kill switch / cooldown / duplicate);
        logs the reason via engine_log + the logs table.
        """
        async with self._lock:
            if not self.auto_trade:
                self.last_skip_reason = "auto-trade disarmed"
                return None
            signal_id = signal.get("id")
            if signal_id and await self._repo.has_trade_for_signal(signal_id, self._owner):
                self.last_skip_reason = f"duplicate signal {str(signal_id)[:8]} (idempotency)"
                logger.info("duplicate signal %s skipped (idempotency)", str(signal_id)[:8])
                return None

            await self._ensure_day_anchor()
            positions = await self._source.get_positions()
            verdict = await evaluate_kill_switches(
                self._cfg, self._source, symbol, point_size,
                spread_points=float(signal.get("spread_points", 0.0)),
                day_start_equity=self._day_start_equity,
                positions=positions,
            )
            if not verdict.allowed:
                self.last_skip_reason = verdict.reason
                if verdict.kill_daily_loss:
                    await self._emergency_stop()
                else:
                    await self._notify(verdict)
                return None

            info = self._source.account_info()
            if asyncio.iscoroutine(info):
                info = await info
            equity = float(info.get("equity", 0.0)) if info else 0.0

            sym_info = self._source.symbol_info(symbol)
            if asyncio.iscoroutine(sym_info):
                sym_info = await sym_info
            sl_distance = abs(signal["entry"] - signal["sl"])
            lot = size_lot(self._cfg, equity, sl_distance, sym_info)

            order = Order(
                symbol=symbol,
                side=signal["direction"],
                volume=lot.lots,
                sl=signal["sl"],
                tp=signal["tp"],
                deviation=30,
                magic=self._cfg.magic,
                comment=f"xauai-{str(signal_id)[:8] if signal_id else 'manual'}",
            )
            result = await self._place_with_retry(order)
            if result.ok:
                await self._repo.insert(
                    {
                        "signal_id": signal_id,
                        "owner": self._owner,
                        "ticket": result.ticket,
                        "side": order.side,
                        "volume": order.volume,
                        "price_open": result.price,
                        "sl": order.sl,
                        "tp": order.tp,
                        "opened_at": datetime.now(tz=UTC).isoformat(),
                    }
                )
            await self._log(
                "info" if result.ok else "warning",
                f"order {order.side} {order.volume} {symbol} @ {result.price} "
                f"retcode={result.retcode} — {result.comment}",
            )
            return result

    async def _place_with_retry(self, order: Order) -> OrderResult:
        """Send; failed orders retried max 1x (SPEC §9)."""
        result = await self._source.place_order(order)
        if not result.ok:
            logger.warning(
                "order failed (retcode=%s) — retrying once", result.retcode
            )
            result = await self._source.place_order(order)
        return result

    async def _emergency_stop(self) -> None:
        """Daily-loss kill switch: close all positions, disarm, CRITICAL log."""
        logger.critical("DAILY LOSS KILL SWITCH — closing all positions, disarming auto-trade")
        positions = await self._source.get_positions()
        for p in positions:
            try:
                res = await self._source.close_position(p.ticket)
                logger.info("kill-switch close %s -> %s", p.ticket, res.comment)
            except Exception:  # noqa: BLE001 — keep closing the rest
                logger.exception("kill-switch close failed for %s", p.ticket)
        self.arm(False)
        await self._log(
            "critical",
            "KILL SWITCH: daily loss limit reached — auto-trade disarmed, all positions closed",
        )

    async def _notify(self, verdict: KillSwitchVerdict) -> None:
        level = "critical" if verdict.kill_daily_loss else "info"
        await self._log(level, f"order skipped: {verdict.reason}")

    async def _log(self, level: str, message: str) -> None:
        logger.log(getattr(logging, level.upper(), logging.INFO), "%s", message)
        if self._hub is None:
            return
        try:
            # D-044 — a USER plane's order/skip events go ONLY to that user;
            # only the platform executor (owner=None) broadcasts globally.
            if self._owner is not None:
                await self._hub.broadcast_user(
                    self._owner,
                    "mt5_auto",
                    {
                        "event": "log",
                        "level": level,
                        "ts": datetime.now(tz=UTC).isoformat(),
                        "message": message,
                    },
                )
            else:
                await self._hub.broadcast_all(
                    "engine_log", {"level": level, "message": message}
                )
        except Exception:  # noqa: BLE001
            pass
