"""PaperPlaneMixin (D-044) — practice-plane operations shared by the mock and
live sibling sources.

Every platform user now gets an auto-provisioned PRACTICE account (isolated
balance/positions/settings). The sibling sources already implement the paper
account core (place_order / get_positions / close_position priced off the real
market); this mixin adds what the institutional multi-user model still needs:

- restore_positions() — re-inject persisted positions after a restart
- set_balance()       — restore the persisted account balance
- check_stops()      — broker-like SL/TP execution: any position whose stop
                       or target is touched by the live tick is closed at
                       that level and the profit realised into the balance

check_stops is called from the plane poll loop (5s) — the app controls SL/TP
(D-039), so paper positions must respect them exactly like a broker would.
"""

from __future__ import annotations

from typing import Any

from app.mt5.base import Position


class PaperPlaneMixin:
    """Requires the host class to provide _positions/_account/_next_ticket,
    get_tick(), close_position() and _floating_profit()."""

    _positions: list[Position]
    _next_ticket: int

    # ------------------------------------------------------------ restore
    def restore_positions(
        self, positions: list[Position], next_ticket: int | None = None
    ) -> None:
        """Re-inject persisted open positions (D-044 plane restore)."""
        self._positions = list(positions)
        if next_ticket is not None:
            self._next_ticket = max(self._next_ticket, int(next_ticket))
        elif positions:
            self._next_ticket = max(self._next_ticket, max(p.ticket for p in positions))

    def set_balance(self, balance: float) -> None:
        """Restore the persisted account balance (D-044 plane restore)."""
        account = self._account  # type: ignore[attr-defined]
        account.balance = float(balance)
        account.equity = float(balance)

    # ------------------------------------------------------- SL/TP watcher
    async def check_stops(self) -> list[dict[str, Any]]:
        """Close paper positions whose SL/TP the live tick has touched.

        Returns [{ticket, kind: "sl"|"tp", price, profit, symbol, side,
        volume}] for the positions closed this pass (empty when none).
        """
        closed: list[dict[str, Any]] = []
        for p in list(self._positions):
            if p.sl is None and p.tp is None:
                continue
            try:
                tick = await self.get_tick(p.symbol)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 — feed hiccup: check again next pass
                continue
            kind: str | None = None
            level: float | None = None
            if p.side == "BUY":
                # close a BUY at bid; SL below, TP above
                if p.sl is not None and tick.bid <= p.sl:
                    kind, level = "sl", p.sl
                elif p.tp is not None and tick.bid >= p.tp:
                    kind, level = "tp", p.tp
            else:
                # close a SELL at ask; SL above, TP below
                if p.sl is not None and tick.ask >= p.sl:
                    kind, level = "sl", p.sl
                elif p.tp is not None and tick.ask <= p.tp:
                    kind, level = "tp", p.tp
            if kind is None or level is None:
                continue
            profit = self._floating_profit(p, tick.bid, tick.ask)  # type: ignore[attr-defined]
            res = await self.close_position(p.ticket)  # type: ignore[attr-defined]
            if res.ok:
                closed.append(
                    {
                        "ticket": p.ticket,
                        "kind": kind,
                        "price": float(level),
                        "profit": round(float(profit), 2),
                        "symbol": p.symbol,
                        "side": p.side,
                        "volume": p.volume,
                    }
                )
        return closed
