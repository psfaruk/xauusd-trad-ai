"""Signal lifecycle tracking (SPEC §8.4, D-050 pending orders).

Live tracker: ticks drive pending-order FILLS, then SL/TP touches;
engine-TF bar closes drive expiry.
- PENDING (D-050, entry_type="limit"): a BUY limit fills when the ask
  trades down to the entry, a SELL limit when the bid trades up to it;
  after `pending_expiry_bars` engine-TF bars without a fill the signal
  expires UNFILLED (result_r=None — a missed trade, never a loss).
- BUY exits on bid (SL: bid <= sl; TP: bid >= tp)
- SELL exits on ask (SL: ask >= sl; TP: ask <= tp)
- after `expiry_bars` engine-TF closes: expired with result_r =
  (close-entry)/risk signed by direction.

The backtest uses bar-based simulation instead (see backtest.py) with the
pessimistic both-touched -> SL-first convention.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

logger = logging.getLogger("xauusd.tracker")


@dataclass
class TrackedSignal:
    id: str
    direction: str
    entry: float
    sl: float
    tp: float
    risk: float  # |entry - sl|
    created_bar_time: datetime  # UTC open time of the sweep bar
    confidence: float
    trace: dict
    bars_seen: int = 0
    status: str = "active"  # pending | active | won | lost | expired | cancelled
    result_r: float | None = None
    closed_at: datetime | None = None
    # D-050 — pending-limit order fields
    entry_type: str = "market"  # "market" | "limit"
    market_ref: float | None = None  # market price at signal time
    filled_at: datetime | None = None  # when the pending order filled
    bars_pending: int = 0  # engine-TF bars spent waiting for a fill

    def to_signal_dict(self, symbol: str, tf: str) -> dict:
        return {
            "id": self.id,
            "ts": self.created_bar_time.isoformat(),
            "symbol": symbol,
            "tf": tf,
            "direction": self.direction,
            "entry": self.entry,
            "sl": self.sl,
            "tp": self.tp,
            "confidence": self.confidence,
            "trace": self.trace,
            "status": self.status,
            "result_r": self.result_r,
            "closed_at": self.closed_at.isoformat() if self.closed_at else None,
            "entry_type": self.entry_type,
            "market_ref": self.market_ref,
            "filled_at": self.filled_at.isoformat() if self.filled_at else None,
        }


StatusCallback = Callable[[TrackedSignal], Awaitable[None]]


class SignalTracker:
    """Tracks active signals against ticks and bar closes."""

    def __init__(self) -> None:
        self._active: dict[str, TrackedSignal] = {}
        self._lock = asyncio.Lock()
        self.on_status: StatusCallback | None = None

    @property
    def active(self) -> list[TrackedSignal]:
        return list(self._active.values())

    def has_active(self, direction: str | None = None) -> bool:
        if direction:
            return any(s.direction == direction for s in self._active.values())
        return bool(self._active)

    async def register(self, sig: TrackedSignal) -> None:
        async with self._lock:
            self._active[sig.id] = sig

    async def cancel_all(self, reason: str = "cancelled") -> None:
        async with self._lock:
            for sig in self._active.values():
                sig.status = "cancelled"
                sig.closed_at = datetime.now(UTC)
                await self._emit(sig)
            self._active.clear()

    async def on_tick(self, bid: float, ask: float) -> None:
        async with self._lock:
            done: list[str] = []
            for sig in self._active.values():
                # D-050 — pending limit orders fill first (a BUY limit
                # fills when the ask trades down to it, a SELL limit when
                # the bid trades up); only FILLED signals track SL/TP.
                if sig.status == "pending":
                    self._maybe_fill(sig, bid, ask)
                    if sig.status == "pending":
                        continue
                if sig.direction == "BUY":
                    if bid <= sig.sl:
                        sig.status, sig.result_r = "lost", -1.0
                    elif bid >= sig.tp:
                        sig.status, sig.result_r = "won", sig.risk and round(
                            (sig.tp - sig.entry) / sig.risk, 4
                        )
                else:  # SELL exits on ask
                    if ask >= sig.sl:
                        sig.status, sig.result_r = "lost", -1.0
                    elif ask <= sig.tp:
                        sig.status, sig.result_r = "won", sig.risk and round(
                            (sig.entry - sig.tp) / sig.risk, 4
                        )
                if sig.status not in ("active", "pending"):
                    sig.closed_at = datetime.now(UTC)
                    await self._emit(sig)
                    done.append(sig.id)
            for sid in done:
                self._active.pop(sid, None)

    @staticmethod
    def _maybe_fill(sig: TrackedSignal, bid: float, ask: float) -> None:
        """D-050 — fill a pending limit on a tick touch (in place).

        A BUY limit fills when the ASK trades at/below the entry (buys
        fill at the ask); a SELL limit when the BID trades at/above it.
        The fill price is the limit level (or better — kept conservative).
        """
        if sig.direction == "BUY" and ask <= sig.entry:
            sig.status = "active"
            sig.filled_at = datetime.now(UTC)
        elif sig.direction == "SELL" and bid >= sig.entry:
            sig.status = "active"
            sig.filled_at = datetime.now(UTC)

    async def on_bar_close(
        self,
        cfg_expiry_bars: int,
        closed_bar_close_time: datetime,
        close_price: float,
        pending_expiry_bars: int | None = None,
        bar_low: float | None = None,
        bar_high: float | None = None,
    ) -> None:
        """Count a closed engine-TF bar per signal; expire when full.

        D-050 — PENDING signals: the bar's low/high acts as a fill fallback
        (the tick path is authoritative, but a missed tick burst must not
        strand a limit that clearly traded through), and after
        `pending_expiry_bars` bars without a fill the signal expires
        UNFILLED — result_r None (a missed trade, never a loss).
        """
        async with self._lock:
            done: list[str] = []
            for sig in self._active.values():
                if sig.status == "pending":
                    sig.bars_pending += 1
                    if (
                        bar_low is not None and sig.direction == "BUY"
                        and bar_low <= sig.entry
                    ) or (
                        bar_high is not None and sig.direction == "SELL"
                        and bar_high >= sig.entry
                    ):
                        sig.status = "active"
                        sig.filled_at = closed_bar_close_time
                        continue
                    limit = pending_expiry_bars or cfg_expiry_bars
                    if sig.bars_pending >= limit:
                        sig.status = "expired"
                        sig.result_r = None  # never filled — no trade, no R
                        sig.closed_at = closed_bar_close_time
                        await self._emit(sig)
                        done.append(sig.id)
                    continue
                sig.bars_seen += 1
                if sig.bars_seen >= cfg_expiry_bars:
                    if sig.direction == "BUY":
                        raw = (close_price - sig.entry) / sig.risk
                    else:
                        raw = (sig.entry - close_price) / sig.risk
                    sig.status = "expired"
                    sig.result_r = round(raw, 4)
                    sig.closed_at = closed_bar_close_time
                    await self._emit(sig)
                    done.append(sig.id)
            for sid in done:
                self._active.pop(sid, None)

    async def _emit(self, sig: TrackedSignal) -> None:
        if self.on_status is not None:
            try:
                await self.on_status(sig)
            except Exception:  # noqa: BLE001 — tracking must never break the stream
                logger.exception("status callback failed for signal %s", sig.id)


def make_tracked(
    direction: str,
    entry: float,
    sl: float,
    tp: float,
    confidence: float,
    trace: dict,
    bar_time: datetime,
    signal_id: str | None = None,
    entry_type: str = "market",
    market_ref: float | None = None,
) -> TrackedSignal:
    risk = abs(entry - sl)
    if risk <= 0:
        raise ValueError("signal risk must be positive")
    return TrackedSignal(
        id=signal_id or str(uuid.uuid4()),
        direction=direction,
        entry=entry,
        sl=sl,
        tp=tp,
        risk=risk,
        created_bar_time=bar_time,
        confidence=confidence,
        trace=trace,
        status="pending" if entry_type == "limit" else "active",  # D-050
        entry_type=entry_type,
        market_ref=market_ref,
    )
