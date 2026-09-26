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

import http.client
import json
import logging
import os
import ssl
import threading
import time
import urllib.parse
from datetime import UTC
from typing import Any

DEFAULT_URL = "http://127.0.0.1:22346/mcp"
DEFAULT_KEY_FILE = "/home/z/mt5stack/mcp_key.txt"
_TIMEOUT = 60  # order ops can take a few seconds; info ops are fast

# D-078 — the endpoint is RUNTIME state (see bridge_config.py): a client
# constructed WITHOUT an explicit URL resolves the effective endpoint on
# EVERY call, so an in-app URL update (tunnel rotation) hot-swaps every
# live client without a redeploy. No import cycle: bridge_config only
# pulls _cfg_key lazily inside diagnose_bridge().
from app.mt5.bridge_config import get_bridge_url, url_stamp  # noqa: E402

#: D-055 — MT5 return codes. A pending order is ACCEPTED by the terminal
#: with TRADE_RETCODE_PLACED (10008, "order placed") just as often as with
#: TRADE_RETCODE_DONE (10009) depending on server/bridge; treating 10008 as
#: failure used to make the executor RETRY a live pending order (duplicate
#: exposure) and log the placement as rejected.
RET_DONE = 10009
RET_PLACED = 10008
#: D-047 — TLS handshake / connect budget. The PUBLIC bridge (HTTPS tunnel)
#: occasionally stalls a fresh handshake for the FULL socket timeout, which
#: used to freeze tick polling for 60s+ ("candles stop updating"). Connecting
#: with a short budget fails fast and retries on a FRESH connection instead.
_CONNECT_TIMEOUT_S = float(os.environ.get("MT5_MCP_CONNECT_TIMEOUT_S", "8"))
#: D-047 — keep-alive idle limit. Tunnel gateways silently drop idle
#: connections (often ~60s); the next request on a dead socket fails with
#: RemoteDisconnected. Reconnecting proactively after this idle window makes
#: stale-connection failures rare instead of routine.
_CONN_MAX_IDLE_S = float(os.environ.get("MT5_MCP_CONN_MAX_IDLE_S", "25"))

log = logging.getLogger("xauusd.mcp")

#: D-041 — read-only tools that are safe to retry once on a transport error
#: (a gateway blip must not flip the platform to "disconnected"). Order
#: tools are deliberately absent: a duplicate order is worse than a miss.
_IDEMPOTENT_TOOLS = frozenset(
    {
        "get_trading_account_info",
        "get_trading_open_positions",
        "get_trading_history_positions",
        "get_marketwatch_symbols",
        "get_chart_ticks_history",
        "get_chart_history",
    }
)

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


def _effective_url(fixed: str | None) -> str:
    """D-078 — fixed endpoint, or the runtime-resolved one when dynamic."""
    return fixed if fixed is not None else get_bridge_url()


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
        # D-078 — url=None means RUNTIME-RESOLVED: every call takes the
        # current bridge_config endpoint (admin hot-swap, no redeploy). An
        # explicit url (tests, explicit hosts) stays frozen as before.
        self._fixed_url: str | None = url
        self._url_stamp_seen = url_stamp()
        self._key = key  # None -> resolve lazily (key file may appear later)
        self._session: str | None = None
        self._id = 0
        self._lock = threading.Lock()
        # D-047 — per-thread KEEP-ALIVE connection pool. urllib.urlopen opened
        # a fresh TCP+TLS connection for EVERY call; the public HTTPS bridge
        # intermittently stalled those handshakes (SSL timeout every few
        # minutes in production logs) and each stall froze a tick worker for
        # the whole socket timeout. A persistent per-thread connection
        # handshakes ONCE and reuses; on any transport error it is discarded
        # and rebuilt (fresh path through the tunnel).
        self._local = threading.local()

    @property
    def url(self) -> str:
        """Effective endpoint — dynamic unless fixed at construction."""
        return _effective_url(self._fixed_url)

    # ------------------------------------------------------- connection pool

    def _conn_parts(self) -> urllib.parse.SplitResult:
        return urllib.parse.urlsplit(self.url)

    def _drop_local_conn(self) -> None:
        conn = getattr(self._local, "conn", None)
        self._local.conn = None
        self._local.conn_at = 0.0
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 — closing a dead socket
                pass

    def _acquire_conn(self) -> http.client.HTTPConnection:
        """This thread's warm connection, proactively rebuilt when stale."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            idle = time.monotonic() - getattr(self._local, "used_at", 0.0)
            if idle > _CONN_MAX_IDLE_S:
                self._drop_local_conn()
            else:
                return conn
        parts = self._conn_parts()
        if parts.scheme == "https":
            conn = http.client.HTTPSConnection(
                parts.hostname, parts.port or 443,
                timeout=_CONNECT_TIMEOUT_S,
                context=ssl.create_default_context(),
            )
        else:
            conn = http.client.HTTPConnection(
                parts.hostname, parts.port or 80, timeout=_CONNECT_TIMEOUT_S
            )
        conn.connect()  # handshake failures surface HERE (fast, no request sent)
        self._local.conn = conn
        self._local.used_at = time.monotonic()
        return conn

    # ------------------------------------------------------------------ rpc
    def _post(
        self, payload: dict[str, Any], timeout: int = _TIMEOUT,
        idempotent: bool = False,
    ) -> dict[str, Any]:
        """POST one JSON-RPC message over the warm keep-alive connection.

        D-047 retry ladder (the "hard-code the feed" directive):
        - connect/handshake failure  -> request NEVER left the box: safe to
          retry on a fresh connection for EVERY tool (orders included);
        - send/response failure      -> request may have reached the terminal:
          retry ONLY idempotent read tools (orders must never double-fire);
        - every retry drops the thread's cached connection first so the next
          attempt takes a brand-new path through the tunnel.
        """
        # D-078 — hot-swap checkpoint: when bridge_config's stamp moved
        # (admin updated the endpoint), drop this thread's pooled connection
        # AND the MCP session so the very next request takes the NEW url.
        # _post is always called under self._lock (see _call_once), so the
        # session mutation is race-free; each worker thread drops its own
        # pooled conn here on its next call.
        stamp = url_stamp()
        if stamp != self._url_stamp_seen:
            self._url_stamp_seen = stamp
            self._drop_local_conn()
            self._session = None
        key = self._key if self._key is not None else _cfg_key()
        if not key:
            raise MCPError("MT5 MCP key not configured (MT5_MCP_KEY / key file)")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {key}",
            "Connection": "keep-alive",
        }
        if self._session:
            headers["Mcp-Session-Id"] = self._session
        # D-047 HOTFIX: the PUBLIC bridge URL carries a query string
        # (…/mcp?XTransformPort=22346) — urlsplit().path DROPS it and the
        # tunnel gateway answers 404. Rebuild path + query exactly.
        parts = self._conn_parts()
        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"
        data = json.dumps(payload).encode()
        body = ""
        for attempt in (1, 2):
            if attempt > 1:
                self._drop_local_conn()
            try:
                conn = self._acquire_conn()
            except (OSError, ssl.SSLError) as e:
                if attempt >= 2:
                    self._session = None
                    raise MCPError(f"MT5 terminal unreachable: {e}") from e
                time.sleep(0.25)
                continue
            try:
                conn.sock.settimeout(float(timeout))
                conn.request("POST", path, body=data, headers=headers)
                r = conn.getresponse()
                raw = r.read()
                status = r.status
                sid = r.getheader("Mcp-Session-Id")
                reusable = not r.will_close
            except (http.client.HTTPException, OSError, ssl.SSLError) as e:
                self._drop_local_conn()
                # response-phase failure: the request MAY have been processed —
                # only idempotent tools may take the second attempt
                if attempt >= 2 or not idempotent:
                    self._session = None
                    raise MCPError(f"MT5 terminal unreachable: {e}") from e
                time.sleep(0.5)
                continue
            finally:
                used = getattr(self._local, "used_at", None)
                if used is not None:
                    self._local.used_at = time.monotonic()
            if not reusable:
                self._drop_local_conn()
            if sid:
                self._session = sid
            if status >= 400:
                if status in (401, 403, 404):
                    self._session = None
                raise MCPError(f"MT5 MCP HTTP {status}")
            body = raw.decode(errors="replace")
            break
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
        # D-041/D-047 — idempotent read tools get the transport retry ladder
        # (fresh connection per attempt inside _post, plus this outer retry
        # for one more total chance). Order tools never retry above the
        # send-phase-only safety retry inside _post.
        idempotent = name in _IDEMPOTENT_TOOLS
        try:
            return self._call_once(name, arguments, timeout, idempotent=idempotent)
        except MCPError as first:
            if not idempotent:
                raise
            log.warning("MCP %s failed (%s) — one retry", name, first)
            time.sleep(1.0)
            with self._lock:
                self._session = None
            return self._call_once(name, arguments, timeout, idempotent=idempotent)

    def _call_once(self, name: str, arguments: dict[str, Any] | None,
                   timeout: int, idempotent: bool = False) -> Any:
        with self._lock:
            if self._session is None:
                self._connect_locked()
            self._id += 1
            rid = self._id
            resp = self._post(
                {"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                 "params": {"name": name, "arguments": arguments or {}}},
                timeout=timeout, idempotent=idempotent,
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
                        timeout=timeout, idempotent=idempotent,
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

    # ------------------------------------------------------ D-055 capability

    def tools_list(self, refresh: bool = False) -> list[dict[str, Any]]:
        """tools/list — the terminal's OWN tool descriptors (name + schema).

        The pending-order contract was written blind in D-050 (never
        live-verified: field `order_type`, no filling_type). Instead of
        guessing again, D-055 NEGOTIATES: read the actual inputSchema of
        every tool from the terminal and adapt the arguments to it.
        """
        with self._lock:
            if self._session is None:
                self._connect_locked()
            self._id += 1
            resp = self._post(
                {"jsonrpc": "2.0", "id": self._id, "method": "tools/list"},
                timeout=15,
            )
        if "error" in resp:
            raise MCPError(str(resp["error"].get("message", resp["error"])))
        result = resp.get("result", {})
        tools = result.get("tools", []) if isinstance(result, dict) else []
        if isinstance(tools, list):
            self._tools_schema = {str(t.get("name")): t for t in tools if isinstance(t, dict)}
        return tools

    def tool_schema(self, name: str) -> dict[str, Any] | None:
        """inputSchema of one tool (cached; None = unknown tool)."""
        cached = getattr(self, "_tools_schema", None)
        if cached is None:
            try:
                self.tools_list()
            except (MCPError, OSError):
                return None
            cached = self._tools_schema
        t = (cached or {}).get(name)
        if not isinstance(t, dict):
            return None
        schema = t.get("inputSchema") or t.get("input_schema")
        return schema if isinstance(schema, dict) else None

    @staticmethod
    def _schema_props(schema: dict[str, Any] | None) -> set[str]:
        if not isinstance(schema, dict):
            return set()
        props = schema.get("properties")
        if isinstance(props, dict):
            return {str(k) for k in props}
        return set()

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

    def clone(self) -> MT5TerminalClient:
        """D-042 — a fresh client (own MCP session, same endpoint/key).

        The tick-poller runs one clone per worker so slow bridge round
        trips pipeline instead of serializing behind the single lock.
        D-078 — clones inherit the DYNAMIC mode (url=None) so an in-app
        endpoint update reaches every worker on its next call.
        """
        return MT5TerminalClient(url=self._fixed_url, key=self._key)

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

    def pending_order(self, symbol: str, order_type: str, volume: float,
                      price: float, sl: float | None = None,
                      tp: float | None = None,
                      comment: str = "") -> dict[str, Any]:
        """D-050/D-055 — place a PENDING limit order on the real terminal.

        `order_type`: "buy_limit" | "sell_limit" (an entry BELOW the market
        for a BUY / ABOVE it for a SELL — the POI zone pending entry). SL/TP
        ride with the order so the exit stays server-side (Exness executes
        them even if the platform goes down, same guarantee market orders
        have). The fill happens when the market retraces to `price`.

        D-055 — the arguments are ADAPTIVE to the terminal's own tool
        schema (tools/list negotiation):
        - the order-type field is sent as `type` (matching the LIVE-VERIFIED
          market_order contract, D-034) unless the terminal's schema
          explicitly declares `order_type` instead;
        - `filling_type: "return"` is attached when the schema offers it —
          pending orders on many symbols are only valid with the RETURN
          filling policy (FOK/IOC pendings get TRADE_RETCODE_INVALID_FILL);
          live-verified community reports on MT5 builds 5955+.
        """
        schema = self.tool_schema("trade_send_pending_order")
        props = self._schema_props(schema)
        type_field = "order_type" if ("order_type" in props and "type" not in props) else "type"
        args: dict[str, Any] = {
            "symbol": symbol,
            type_field: order_type,
            "volume": volume,
            "price": price,
        }
        if "filling_type" in props:
            args["filling_type"] = "return"
        if sl:
            args["sl"] = sl
        if tp:
            args["tp"] = tp
        if comment:
            args["comment"] = comment[:31]
        if type_field != "type":
            log.info(
                "terminal pending tool uses field %r (schema-adaptive, D-055)",
                type_field,
            )
        res = self._call("trade_send_pending_order", args, timeout=120)
        if isinstance(res, str):
            raise MCPError(res)
        return res

    def delete_order(self, order_ticket: int) -> dict[str, Any]:
        """D-055 — cancel a WAITING pending order on the real terminal.

        Signal expiry used to call close_position() which only searches OPEN
        POSITIONS: a still-waiting limit order was reported "already closed"
        and left LIVE on the account — it could fill hours later with no
        signal linkage (untracked real-money exposure). This tool
        (trade_delete_order) actually removes the waiting order.
        """
        schema = self.tool_schema("trade_delete_order")
        props = self._schema_props(schema)
        ticket_field = next(
            (k for k in ("order_ticket", "ticket", "order", "order_id") if k in props),
            "order_ticket",
        )
        res = self._call("trade_delete_order", {ticket_field: order_ticket}, timeout=120)
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
