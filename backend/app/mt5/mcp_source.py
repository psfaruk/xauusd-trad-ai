"""McpTradingSource — DataSource adapter over the REAL MT5 terminal (D-036).

Implements the TRADING half of the DataSource interface (app.mt5.base) on top
of the terminal's MCP bridge: account_info, get_positions, symbol_info,
place_order, close_position. This lets the existing §9 OrderExecutor (kill
switches, lot sizing, idempotency, retry, emergency stop) run UNCHANGED
against the real Exness account — orders are always executed by the genuine
MetaTrader 5 terminal, never by a direct broker connection.

Market-data methods are intentionally NOT wired (the platform market feed is
LiveDataSource with the MT5-first overlay, D-035): get_rates returns empty and
get_tick raises — this source exists for EXECUTION only.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

import pandas as pd

from app.mt5.base import (
    DataSource,
    DataSourceError,
    Order,
    OrderResult,
    Position,
    SymbolInfo,
    Tick,
    empty_rates,
)
from app.mt5.mcp import (
    RET_DONE,
    RET_PLACED,
    TRADING_NOT_PERMITTED_HINT,
    MCPError,
    MT5TerminalClient,
    terminal_client,
)
from app.mt5.mcp_market import DEFAULT_MAP

logger = logging.getLogger("xauusd.mcpsource")

#: known fallback specs (refined by the terminal's Market Watch discovery)
FALLBACK_SPECS: dict[str, SymbolInfo] = {
    "XAUUSD": SymbolInfo(
        name="XAUUSDm", point=0.01, contract_size=100.0,
        volume_min=0.01, volume_max=100.0, volume_step=0.01,
    ),
    "BTCUSD": SymbolInfo(
        name="BTCUSDm", point=0.01, contract_size=1.0,
        volume_min=0.01, volume_max=200.0, volume_step=0.01,
    ),
}

SYMBOLS_TTL_S = 600.0  # Market Watch discovery cache
RET_MARKET_CLOSED = 10018
#: D-055 — ok retcodes for a PENDING placement: DONE (request completed)
#: or PLACED (the order is registered and waiting for its trigger price).
_PEND_OK = frozenset({RET_DONE, RET_PLACED})


def _parse_time(s: Any) -> Any:
    """Terminal time '2026.09.20 18:01:06' -> datetime (best-effort)."""
    from datetime import UTC, datetime

    if not s or not isinstance(s, str):
        return datetime.now(tz=UTC)
    for fmt in ("%Y.%m.%d %H:%M:%S", "%Y.%m.%d %H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            raw = s.split(".")[0] if fmt.endswith("%f") else s
            return datetime.strptime(raw, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return datetime.now(tz=UTC)


def _map_symbol(platform: str, names: list[str]) -> str | None:
    """Platform symbol -> broker symbol (exact, then shortest prefix)."""
    exact = [n for n in names if n == platform]
    if exact:
        return exact[0]
    prefix = [n for n in names if n.startswith(platform)]
    if prefix:
        return min(prefix, key=len)
    return None


class McpTradingSource(DataSource):
    """Execution-only DataSource backed by the MetaTrader 5 terminal (MCP).

    All MCP calls are blocking HTTP — every method offloads to a worker
    thread via `asyncio.to_thread` so the event loop is never blocked.
    """

    def __init__(self, client: MT5TerminalClient | None = None) -> None:
        self._client = client or terminal_client()
        self._symbol_map: dict[str, str] = {}
        self._specs: dict[str, SymbolInfo] = {}
        self._names_at = 0.0

    # ------------------------------------------------------------ discovery

    async def _refresh_symbols(self, force: bool = False) -> None:
        """Market Watch discovery: broker names + full trading specs."""
        now = time.monotonic()
        if not force and self._symbol_map and now - self._names_at < SYMBOLS_TTL_S:
            return
        try:
            rows = await asyncio.to_thread(self._client.symbols)
        except (MCPError, OSError) as exc:
            logger.warning("terminal symbol discovery failed (%s) — defaults", exc)
            return
        names = [str(r.get("symbol", "")) for r in rows if isinstance(r, dict)]
        self._symbol_map = {}
        self._specs = {}
        for plat, default in DEFAULT_MAP.items():
            broker = _map_symbol(plat, names) or default
            self._symbol_map[plat] = broker
            spec_row = next(
                (r for r in rows if isinstance(r, dict) and r.get("symbol") == broker),
                None,
            )
            if spec_row is not None:
                self._specs[plat] = SymbolInfo(
                    name=broker,
                    point=float(spec_row.get("point") or 0.01),
                    contract_size=float(spec_row.get("contract_size") or 100.0),
                    volume_min=float(spec_row.get("volume_min") or 0.01),
                    volume_max=float(spec_row.get("volume_max") or 100.0),
                    volume_step=float(spec_row.get("volume_step") or 0.01),
                )
        self._names_at = now
        logger.info("terminal symbols mapped: %s", self._symbol_map)

    def broker_symbol(self, platform: str) -> str | None:
        """Platform symbol -> broker symbol (XAUUSD -> XAUUSDm)."""
        if platform in self._symbol_map:
            return self._symbol_map[platform]
        if platform in DEFAULT_MAP:  # discovery not run yet
            return DEFAULT_MAP[platform]
        # broker name passed through (e.g. manual XAUUSDm)
        return platform

    async def abroker_symbol(self, platform: str) -> str | None:
        await self._refresh_symbols()
        return self.broker_symbol(platform)

    # ------------------------------------------------------ DataSource (acct)

    async def connect(self, creds: dict) -> dict:  # noqa: ARG002 — no-op
        """The terminal is always-on; 'connect' = verify it answers."""
        info = await self.account_info()
        if info is None:
            raise DataSourceError("MT5 terminal bridge unavailable")
        return info

    async def disconnect(self) -> None:  # nothing to tear down
        return None

    async def is_connected(self) -> bool:
        try:
            return await asyncio.to_thread(self._client.available)
        except (MCPError, OSError):
            return False

    async def account_info(self) -> dict | None:
        try:
            res = await asyncio.to_thread(self._client.account)
        except (MCPError, OSError) as exc:
            logger.warning("terminal account_info failed: %s", exc)
            return None
        acct = res.get("account", {})
        term = res.get("terminal", {})
        if not acct or not term.get("server_connected"):
            return None
        return {
            "login": str(acct.get("login", "")),
            "server": acct.get("server"),
            "broker": acct.get("broker"),
            "balance": float(acct.get("balance") or 0.0),
            "equity": float(acct.get("equity") or 0.0),
            "margin_free": float(acct.get("margin_free") or 0.0),
            "profit": float(acct.get("profit") or 0.0),
            "currency": acct.get("currency", "USD"),
            "leverage": acct.get("leverage"),
            "trade_allowed": bool(term.get("mcp_trade_allowed"))
            and bool(term.get("experts_trade_allowed")),
        }

    async def get_positions(self) -> list[Position]:
        try:
            res = await asyncio.to_thread(self._client.positions)
        except (MCPError, OSError) as exc:
            logger.warning("terminal positions failed: %s", exc)
            return []
        out: list[Position] = []
        for p in res.get("positions", []):
            try:
                action = str(p.get("action", "buy")).lower()
                out.append(
                    Position(
                        ticket=int(p["position_id"]),
                        symbol=str(p.get("symbol", "")),
                        side="BUY" if action == "buy" else "SELL",
                        volume=float(p.get("volume") or 0.0),
                        price_open=float(p.get("price_open") or 0.0),
                        sl=p.get("stop_loss") or None,
                        tp=p.get("take_profit") or None,
                        profit=float(p.get("profit") or 0.0),
                        time=_parse_time(p.get("create_time")),
                    )
                )
            except (KeyError, TypeError, ValueError):
                logger.warning("skipping malformed terminal position: %r", p)
        return out

    # ------------------------------------------------- D-055 pending book

    async def pending_orders(self) -> list[dict]:
        """The terminal's WAITING limit orders — D-054 contract parity.

        The admin AI tab + trading_account broadcast only ever saw the PAPER
        planes' pending book; the institution executor's live waiting orders
        were invisible. The terminal exposes them in the SAME
        get_trading_open_positions response (orders array) — the field
        mapping is tolerant because the terminal serializes orders with
        its own names (order_ticket | ticket | order, type | order_type,
        price_open | price ...).
        """
        try:
            res = await asyncio.to_thread(self._client.positions)
        except (MCPError, OSError) as exc:
            logger.warning("terminal pending orders failed: %s", exc)
            return []
        return self._parse_pending(res)

    def _parse_pending(self, res: dict) -> list[dict]:
        out: list[dict] = []
        for o in res.get("orders", []) or []:
            if not isinstance(o, dict):
                continue
            try:
                ticket = next(
                    int(o[k]) for k in ("order_ticket", "ticket", "order", "order_id")
                    if o.get(k) is not None
                )
            except (TypeError, ValueError, StopIteration):
                logger.warning("skipping malformed terminal order: %r", o)
                continue
            raw_type = str(
                o.get("order_type") or o.get("type") or o.get("action") or ""
            ).lower()
            side = "BUY" if "buy" in raw_type else "SELL"
            price = next(
                (
                    o.get(k) for k in ("price_open", "price", "open_price")
                    if o.get(k) is not None
                ),
                None,
            )
            volume = next(
                (
                    o.get(k) for k in ("volume_current", "volume", "volume_initial")
                    if o.get(k) is not None
                ),
                None,
            )
            out.append(
                {
                    "ticket": ticket,
                    "symbol": str(o.get("symbol", "")),
                    "side": side,
                    "order_type": raw_type or "pending",
                    "volume": float(volume) if volume is not None else None,
                    "price": float(price) if price is not None else None,
                    "sl": o.get("stop_loss") or o.get("sl") or None,
                    "tp": o.get("take_profit") or o.get("tp") or None,
                }
            )
        return out

    async def delete_pending_order(self, ticket: int) -> OrderResult:
        """D-055 — cancel a WAITING terminal order (signal expiry path)."""
        try:
            res = await asyncio.to_thread(self._client.delete_order, ticket)
        except (MCPError, OSError) as exc:
            comment = (
                TRADING_NOT_PERMITTED_HINT
                if "not permitted" in str(exc)
                else f"terminal: {exc}"
            )
            return OrderResult(ok=False, retcode=None, comment=comment)
        retcode = res.get("retcode")
        return OrderResult(
            ok=retcode in _PEND_OK,
            retcode=int(retcode) if retcode is not None else None,
            comment=str(res.get("retcode_details") or ""),
        )

    async def cancel_all_pending(self) -> list[OrderResult]:
        """D-055 — kill-switch hygiene: remove every WAITING order.

        Daily-loss/profit stop used to close POSITIONS only; live limit
        orders survived the kill switch and could fill afterwards — silent
        re-exposure AFTER the safety limit fired.
        """
        pendings = await self.pending_orders()
        results: list[OrderResult] = []
        for o in pendings:
            try:
                results.append(await self.delete_pending_order(int(o["ticket"])))
            except (TypeError, ValueError):
                continue
        return results

    def symbol_info(self, symbol: str) -> SymbolInfo | None:
        """Real terminal spec (contract size, volume limits) — cached."""
        plat = self._platform_for(symbol)
        if plat in self._specs:
            info = self._specs[plat]
            return SymbolInfo(name=self.broker_symbol(symbol) or info.name, point=info.point,
                              contract_size=info.contract_size,
                              volume_min=info.volume_min, volume_max=info.volume_max,
                              volume_step=info.volume_step)
        fb = FALLBACK_SPECS.get(plat)
        if fb is not None:
            return fb
        return None  # unknown symbol -> executor §9 defaults

    def _platform_for(self, symbol: str) -> str:
        """Broker or platform name -> platform key (XAUUSDm -> XAUUSD)."""
        if symbol in DEFAULT_MAP or symbol in FALLBACK_SPECS:
            return symbol
        for plat, broker in self._symbol_map.items():
            if broker == symbol:
                return plat
        for plat in DEFAULT_MAP:
            if symbol.startswith(plat):
                return plat
        return symbol

    # ---------------------------------------------------- DataSource (orders)

    async def place_order(self, order: Order) -> OrderResult:
        broker = self.broker_symbol(order.symbol) or order.symbol
        is_pending = order.order_type in ("buy_limit", "sell_limit") and order.price
        try:
            # D-050/D-055 — pending limit orders ride their own terminal tool;
            # SL/TP are attached server-side exactly like market orders.
            if is_pending:
                res = await asyncio.to_thread(
                    self._client.pending_order,
                    broker,
                    order.order_type,
                    order.volume,
                    order.price,
                    order.sl,
                    order.tp,
                    order.comment,
                )
            else:
                res = await asyncio.to_thread(
                    self._client.market_order,
                    broker,
                    order.side.lower(),
                    order.volume,
                    order.sl,
                    order.tp,
                    order.comment,
                )
        except (MCPError, OSError) as exc:
            logger.warning("terminal order failed: %s", exc)
            # D-040: the terminal-side permission refusal carries the fix hint
            comment = (
                TRADING_NOT_PERMITTED_HINT
                if "not permitted" in str(exc)
                else f"terminal: {exc}"
            )
            return OrderResult(ok=False, retcode=None, comment=comment)
        retcode = res.get("retcode")
        # D-055 — a pending order is successfully REGISTERED with either
        # 10009 DONE or 10008 PLACED ("order placed", the classic pending
        # acceptance). Treating 10008 as failure made the executor retry a
        # LIVE order -> duplicate pending exposure on the real account.
        ok = retcode in (_PEND_OK if is_pending else {RET_DONE})
        ticket = res.get("order") or res.get("deal")
        return OrderResult(
            ok=ok,
            ticket=int(ticket) if ticket else None,
            price=float(res["price"]) if res.get("price") else order.price,
            retcode=int(retcode) if retcode is not None else None,
            comment=str(res.get("retcode_details") or ""),
            volume=float(res["volume"]) if res.get("volume") else None,
        )

    async def close_position(self, ticket: int, deviation: int = 30) -> OrderResult:  # noqa: ARG002
        """Close by ticket — D-055 dispatch:

        1. ticket is an OPEN POSITION  -> trade_close_single_position;
        2. ticket is a WAITING ORDER  -> trade_delete_order (cancel);
        3. neither                      -> already closed broker-side (fine).

        The expiry path used to only check positions: a waiting limit order
        was "already closed"-ok and stayed live on the account forever.
        """
        positions = await self.get_positions()
        pos = next((p for p in positions if p.ticket == ticket), None)
        if pos is not None:
            try:
                res = await asyncio.to_thread(
                    self._client.close_position, pos.symbol, ticket
                )
            except (MCPError, OSError) as exc:
                comment = (
                    TRADING_NOT_PERMITTED_HINT
                    if "not permitted" in str(exc)
                    else f"terminal: {exc}"
                )
                return OrderResult(ok=False, retcode=None, comment=comment)
            retcode = res.get("retcode")
            return OrderResult(
                ok=retcode == RET_DONE,
                retcode=int(retcode) if retcode is not None else None,
                price=float(res["price"]) if res.get("price") else None,
                comment=str(res.get("retcode_details") or ""),
            )
        # not an open position: a WAITING order? -> cancel it (D-055)
        pendings = await self.pending_orders()
        if any(o["ticket"] == ticket for o in pendings):
            return await self.delete_pending_order(ticket)
        return OrderResult(ok=True, retcode=RET_DONE, comment="already closed")

    # -------------------------------------------------- DataSource (mkt data)

    async def get_rates(self, symbol: str, tf: str, count: int) -> pd.DataFrame:  # noqa: ARG002
        return empty_rates()  # market data lives on LiveDataSource (D-035)

    async def get_tick(self, symbol: str) -> Tick:  # execution-only source
        raise DataSourceError(
            "McpTradingSource is execution-only — market data comes from the"
            " live feed (D-035)"
        )

    def subscribe_ticks(self, symbol: str) -> AsyncIterator:  # type: ignore[override]
        async def _empty() -> AsyncIterator:
            return
            yield  # pragma: no cover — execution-only source
        return _empty()

    def discover_symbols(self, pattern: str = "*XAUUSD*") -> list[str]:
        return list(self._symbol_map.keys()) or list(DEFAULT_MAP.keys())
