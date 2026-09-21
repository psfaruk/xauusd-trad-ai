"""MCP bridge to the REAL MetaTrader 5 terminal (D-034).

The MT5 terminal (build 6000+) runs a built-in MCP server
(http://127.0.0.1:22346/mcp, bearer-key auth) exposing account info,
positions, history, market data and ORDER EXECUTION. In the sandbox the
terminal runs under user-space Wine (see scripts/watchdog.sh); on any
Windows host the same terminal MCP endpoint is used — the platform never
talks to the broker directly, only through MetaTrader 5 (user requirement).

Config:
  MT5_MCP_URL      endpoint (default http://127.0.0.1:22346/mcp)
  MT5_MCP_KEY      bearer key (overrides key file)
  MT5_MCP_KEY_FILE path to a file containing the key
                   (default /home/z/mt5stack/mcp_key.txt)
"""
from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from datetime import UTC
from typing import Any

DEFAULT_URL = "http://127.0.0.1:22346/mcp"
DEFAULT_KEY_FILE = "/home/z/mt5stack/mcp_key.txt"
_TIMEOUT = 60  # order ops can take a few seconds; info ops are fast

#: D-040 — honest hint when the terminal refuses trading tools. The native
#: MT5 MCP gates trade tools on BOTH the terminal AutoTrading button AND
#: Tools → Options → AI Assistant → Trading ("Manual confirmation" blocks
#: headless clients). Live-verified 2026-09-21 (d040 round-trip 10009).
TRADING_NOT_PERMITTED_HINT = (
    "Terminal trading not permitted — in the MetaTrader terminal enable BOTH:"
    " the AutoTrading button (Ctrl+E) and Tools → Options → AI Assistant →"
    " Trading = Enabled"
)


class MCPError(RuntimeError):
    """Raised when the terminal MCP is unreachable or refuses a call."""


def _cfg_url() -> str:
    return os.environ.get("MT5_MCP_URL", DEFAULT_URL).rstrip("/")


def _cfg_key() -> str:
    key = os.environ.get("MT5_MCP_KEY", "")
    if key:
        return key.strip()
    path = os.environ.get("MT5_MCP_KEY_FILE", DEFAULT_KEY_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


class MT5TerminalClient:
    """Thread-safe JSON-RPC client for the terminal's MCP server."""

    def __init__(self, url: str | None = None, key: str | None = None) -> None:
        self.url = url or _cfg_url()
        self._key = key  # None -> resolve lazily (key file may appear later)
        self._session: str | None = None
        self._id = 0
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ rpc
    def _post(self, payload: dict[str, Any], timeout: int = _TIMEOUT) -> dict[str, Any]:
        key = self._key if self._key is not None else _cfg_key()
        if not key:
            raise MCPError("MT5 MCP key not configured (MT5_MCP_KEY / key file)")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {key}",
        }
        if self._session:
            headers["Mcp-Session-Id"] = self._session
        req = urllib.request.Request(
            self.url, data=json.dumps(payload).encode(), headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                sid = r.headers.get("Mcp-Session-Id")
                if sid:
                    self._session = sid
                body = r.read().decode(errors="replace")
        except urllib.error.HTTPError as e:
            # 401 -> key rotated/stale session; drop session and surface code
            self._session = None
            raise MCPError(f"MT5 MCP HTTP {e.code}") from e
        except (urllib.error.URLError, OSError) as e:
            self._session = None
            raise MCPError(f"MT5 terminal unreachable: {e}") from e
        if not body.strip():
            return {}
        if body.startswith("event:") or body.startswith("data:"):
            for line in body.splitlines():
                if line.startswith("data:"):
                    body = line[5:].strip()
                    break
        try:
            return json.loads(body)
        except json.JSONDecodeError as e:
            raise MCPError(f"MT5 MCP bad response: {body[:120]}") from e

    def _call(self, name: str, arguments: dict[str, Any] | None = None,
              timeout: int = _TIMEOUT) -> Any:
        with self._lock:
            if self._session is None:
                self._connect_locked()
            self._id += 1
            rid = self._id
            resp = self._post(
                {"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                 "params": {"name": name, "arguments": arguments or {}}},
                timeout=timeout,
            )
        if "error" in resp:
            # session might be stale — one transparent retry
            err = resp["error"]
            if self._session is not None:
                with self._lock:
                    self._session = None
                    self._connect_locked()
                    resp = self._post(
                        {"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                         "params": {"name": name, "arguments": arguments or {}}},
                        timeout=timeout,
                    )
                if "error" in resp:
                    raise MCPError(str(resp["error"].get("message", resp["error"])))
            else:
                raise MCPError(str(err.get("message", err)))
        return self._content(resp.get("result", {}))

    def _connect_locked(self) -> None:
        self._id += 1
        resp = self._post(
            {"jsonrpc": "2.0", "id": self._id, "method": "initialize",
             "params": {
                 "protocolVersion": "2025-06-18",
                 "capabilities": {},
                 "clientInfo": {"name": "xauusd-platform", "version": "0.1.0"},
             }}
        )
        if "error" in resp:
            raise MCPError(f"initialize failed: {resp['error']}")
        # notification (202, empty body is fine)
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    @staticmethod
    def _content(result: dict[str, Any]) -> Any:
        content = result.get("content", [])
        if content and isinstance(content, list) and content[0].get("type") == "text":
            text = content[0].get("text", "")
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
        return result

    # -------------------------------------------------------------- public
    def available(self) -> bool:
        """Cheap liveness probe for UI badges."""
        try:
            info = self.account()
            return bool(info.get("terminal", {}).get("server_connected"))
        except MCPError:
            return False

    def account(self) -> dict[str, Any]:
        return self._call("get_trading_account_info")  # type: ignore[return-value]

    def positions(self, include_orders: bool = True) -> dict[str, Any]:
        return self._call("get_trading_open_positions", {"include_orders": include_orders})  # type: ignore[return-value]

    def history(self, days: int = 30, symbol: str | None = None) -> dict[str, Any]:
        from datetime import datetime, timedelta

        to = datetime.now(UTC)
        frm = to - timedelta(days=days)
        args: dict[str, Any] = {
            "datetime_from": frm.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "datetime_to": to.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        if symbol:
            args["symbol"] = symbol
        return self._call("get_trading_history_positions", args)  # type: ignore[return-value]

    def symbols(self) -> list[dict[str, Any]]:
        res = self._call("get_marketwatch_symbols", timeout=30)
        return res.get("symbols", []) if isinstance(res, dict) else []

    # ------------------------------------------------- market data (D-035)
    def ticks(self, symbol: str, dt_from: str, dt_to: str) -> list[dict[str, Any]]:
        """Recent tick history: [{time_ms, bid, ask}, ...] (terminal format)."""
        res = self._call(
            "get_chart_ticks_history",
            {"datetime_from": dt_from, "datetime_to": dt_to,
             "symbol": symbol, "limit": 100000},
            timeout=10,
        )
        if isinstance(res, dict):
            return res.get("history", []) or []
        return []

    def bars(self, symbol: str, period: str, dt_from: str, dt_to: str,
             limit: int = 1000) -> list[dict[str, Any]]:
        """Chart history bars: [{time, open, high, low, close, tick_volume}, ...]."""
        res = self._call(
            "get_chart_history",
            {"datetime_from": dt_from, "datetime_to": dt_to,
             "symbol": symbol, "period": period, "limit": limit},
            timeout=25,
        )
        if isinstance(res, dict):
            return res.get("history", []) or []
        return []

    def market_order(self, symbol: str, side: str, volume: float,
                     sl: float | None = None, tp: float | None = None,
                     comment: str = "") -> dict[str, Any]:
        args: dict[str, Any] = {"symbol": symbol, "type": side.lower(), "volume": volume}
        if sl:
            args["sl"] = sl
        if tp:
            args["tp"] = tp
        if comment:
            args["comment"] = comment[:31]
        res = self._call("trade_send_market_order", args, timeout=120)
        if isinstance(res, str):
            raise MCPError(res)
        return res

    def close_position(self, symbol: str, ticket: int) -> dict[str, Any]:
        res = self._call("trade_close_single_position",
                         {"symbol": symbol, "position_ticket": ticket}, timeout=120)
        if isinstance(res, str):
            raise MCPError(res)
        return res


#: process-wide singleton (the terminal serves one account)
_client: MT5TerminalClient | None = None
_client_lock = threading.Lock()


def terminal_client() -> MT5TerminalClient:
    global _client
    with _client_lock:
        if _client is None:
            _client = MT5TerminalClient()
        return _client
