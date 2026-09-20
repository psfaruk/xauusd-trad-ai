"""MT5 Wine-Bridge — Linux-side access to the real MetaTrader5 terminal.

The Windows-only `MetaTrader5` pip package runs INSIDE wine (gateway.py on
127.0.0.1:8765, started by the sandbox watchdog / mt5env stack). This module
mirrors the package's Python API over HTTP so `MT5DataSource` (written against
the real package) works unchanged on Linux.

Bridge URL resolution: MT5_BRIDGE_URL env (default http://127.0.0.1:8765).
"""
from __future__ import annotations

import json
import os
from types import SimpleNamespace
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

DEFAULT_URL = "http://127.0.0.1:8765"
_TIMEOUT = 90  # initialize can take ~10s; order ops stay well under this


def bridge_url() -> str:
    return os.environ.get("MT5_BRIDGE_URL", DEFAULT_URL).rstrip("/")


def bridge_available(timeout: float = 2.0) -> bool:
    """Fast health probe — is the wine gateway up?"""
    try:
        with urlopen(f"{bridge_url()}/health", timeout=timeout) as r:
            return json.load(r).get("ok") is True
    except Exception:  # noqa: BLE001 — any failure means unavailable
        return False


class BridgeError(RuntimeError):
    pass


def _call(fn: str, *args: Any, **kwargs: Any) -> Any:
    body = json.dumps({"fn": fn, "args": list(args), "kwargs": kwargs}).encode()
    req = Request(
        f"{bridge_url()}/call", data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(req, timeout=_TIMEOUT) as r:
            resp = json.load(r)
    except URLError as e:
        raise BridgeError(f"MT5 bridge unreachable: {e}") from e
    except Exception as e:  # noqa: BLE001
        raise BridgeError(f"MT5 bridge error: {e}") from e
    if not resp.get("ok"):
        raise BridgeError(resp.get("error", "unknown bridge error"))
    return resp.get("data")


def _ns(d: dict | None) -> SimpleNamespace | None:
    return SimpleNamespace(**d) if d else None


class MetaTrader5Shim:
    """Drop-in stand-in for the `MetaTrader5` module (attributes match the
    subset MT5DataSource + executor use)."""

    # ---- constants (mirror MQL5 enums, same values as the real package)
    TRADE_ACTION_DEAL = 1
    TRADE_ACTION_PENDING = 5
    TRADE_ACTION_SLTP = 6
    TRADE_ACTION_MODIFY = 7
    TRADE_ACTION_REMOVE = 8
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_TYPE_BUY_LIMIT = 2
    ORDER_TYPE_SELL_LIMIT = 3
    ORDER_TYPE_BUY_STOP = 4
    ORDER_TYPE_SELL_STOP = 5
    ORDER_TIME_GTC = 0
    ORDER_FILLING_FOK = 0
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_RETURN = 2
    SYMBOL_FILLING_FOK = 1
    SYMBOL_FILLING_IOC = 2
    POSITION_TYPE_BUY = 0
    POSITION_TYPE_SELL = 1

    # ---- lifecycle
    def initialize(self, path=None, login=None, password=None, server=None, **kw):
        kwargs = {k: v for k, v in dict(
            login=login, password=password, server=server, **kw).items() if v is not None}
        if isinstance(path, str) and path:
            kwargs.setdefault("path", path)
        return bool(_call("initialize", **kwargs))

    def shutdown(self):
        _call("shutdown")
        return None

    def last_error(self):
        data = _call("last_error")
        return tuple(data) if isinstance(data, list) else data

    def version(self):
        data = _call("version")
        return tuple(data) if isinstance(data, list) else data

    # ---- info
    def terminal_info(self):
        return _ns(_call("terminal_info"))

    def account_info(self):
        return _ns(_call("account_info"))

    def symbols_get(self, group=None):
        data = _call("symbols_get", **({"group": group} if group else {})) or []
        return [_ns(d) for d in data]

    def symbol_info(self, symbol):
        return _ns(_call("symbol_info", symbol))

    def symbol_info_tick(self, symbol):
        return _ns(_call("symbol_info_tick", symbol))

    def symbol_select(self, symbol, enable=True):
        return bool(_call("symbol_select", symbol, enable))

    # ---- market data (rates come back as list[dict] — MT5DataSource treats
    # them exactly like the numpy structured rows: len()/[-1]/df construction)
    def copy_rates_from_pos(self, symbol, timeframe, start_pos, count):
        return _call("copy_rates_from_pos", symbol, timeframe, start_pos, count)

    def copy_rates_from(self, symbol, timeframe, date_from, count):
        return _call("copy_rates_from", symbol, timeframe, date_from, count)

    def copy_ticks_from(self, symbol, date_from, count, flags):
        return _call("copy_ticks_from", symbol, date_from, count, flags)

    # ---- trading state
    def positions_get(self, **kw):
        data = _call("positions_get", **kw) or []
        return [_ns(d) for d in data]

    def orders_get(self, **kw):
        data = _call("orders_get", **kw) or []
        return [_ns(d) for d in data]

    def order_send(self, request):
        return _ns(_call("order_send", request))

    def order_check(self, request):
        return _ns(_call("order_check", request))

    # ---- history (epoch ints per the gateway contract)
    def history_deals_get(self, date_from, date_to, **kw):
        data = _call("history_deals_get", date_from, date_to, **kw) or []
        return [_ns(d) for d in data]

    def history_orders_get(self, date_from, date_to, **kw):
        data = _call("history_orders_get", date_from, date_to, **kw) or []
        return [_ns(d) for d in data]

    def history_deals_total(self, date_from, date_to):
        return int(_call("history_deals_total", date_from, date_to) or 0)


#: module-level singleton mirroring `import MetaTrader5`
mt5_shim = MetaTrader5Shim()


def import_mt5_module() -> MetaTrader5Shim | None:
    """Return the shim when the bridge is reachable, else None (fail-closed)."""
    if bridge_available():
        return mt5_shim
    return None
