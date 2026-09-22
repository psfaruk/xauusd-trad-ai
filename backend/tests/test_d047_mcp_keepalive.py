"""D-047 — MT5TerminalClient keep-alive transport tests.

A REAL local HTTP server (threading) stands in for the MCP bridge so the
per-thread persistent connection machinery is exercised end-to-end:

1. connections are REUSED across calls (server counts handshakes),
2. a server-side close of the keep-alive socket is survived (reconnect),
3. connect-phase failures fail FAST (connect budget, no 60s stall),
4. order tools NEVER get a response-phase retry (no double-fire risk),
5. idle connections are proactively rebuilt (stale-socket avoidance).
"""

from __future__ import annotations

import http.server
import json
import threading
import time

import pytest

from app.mt5.mcp import MCPError, MT5TerminalClient


class _Bridge(http.server.BaseHTTPRequestHandler):
    """Mimics the MCP bridge: initialize + tools/call + SSE-style bodies."""

    protocol_version = "HTTP/1.1"  # keep-alive

    #: class-level counters shared across handler instances
    connections = 0
    requests = 0
    close_next = 0  # how many of the next responses should close the conn

    def do_POST(self):  # noqa: N802 — http.server API
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or b"{}")
        MT5BridgeState.requests += 1
        if payload.get("method") == "initialize":
            self._respond({"jsonrpc": "2.0", "id": payload.get("id"),
                           "result": {"serverInfo": {"name": "fake"}}})
            return
        if payload.get("method") == "notifications/initialized":
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        name = (payload.get("params") or {}).get("name", "")
        if name:
            MT5BridgeState.call_requests += 1
        if name in MT5BridgeState.explode:
            # bridge dies mid-response -> client sees a connection reset
            raise ConnectionResetError("bridge exploded")
        # normal read tool
        self._respond({
            "jsonrpc": "2.0", "id": payload.get("id"),
            "result": {"content": [{"type": "text", "text": json.dumps({"ok": name})}]},
        })

    def _respond(self, obj: dict) -> None:
        body = json.dumps(obj).encode()
        close = MT5BridgeState.close_next > 0
        if close:
            MT5BridgeState.close_next -= 1
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if close:  # simulate a gateway dropping the keep-alive socket
            self.close_connection = True
        self.end_headers()
        self.wfile.write(body)

    def setup(self) -> None:
        MT5BridgeState.connections += 1
        super().setup()

    def log_message(self, *args) -> None:  # silence
        pass


class MT5BridgeState:
    connections = 0
    requests = 0
    call_requests = 0  # tools/call POSTs only (initialize/notification excluded)
    close_next = 0
    explode: set[str] = set()


@pytest.fixture()
def bridge():
    MT5BridgeState.connections = 0
    MT5BridgeState.requests = 0
    MT5BridgeState.call_requests = 0
    MT5BridgeState.close_next = 0
    MT5BridgeState.explode = set()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Bridge)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    host, port = server.server_address[:2]
    yield f"http://127.0.0.1:{port}/mcp"
    server.shutdown()
    server.server_close()


def _client(url: str) -> MT5TerminalClient:
    return MT5TerminalClient(url=url, key="test-key")


def test_connection_reused_across_calls(bridge: str) -> None:
    """3 calls from the same thread = ONE TCP connection (the D-047 goal)."""
    c = _client(bridge)
    for _ in range(3):
        assert c.account() == {"ok": "get_trading_account_info"}
    assert MT5BridgeState.connections == 1
    assert MT5BridgeState.requests >= 3 + 2  # calls + initialize handshake


def test_server_closed_socket_is_survived(bridge: str) -> None:
    """Gateway drops the keep-alive conn -> next call reconnects transparently."""
    c = _client(bridge)
    assert c.account() == {"ok": "get_trading_account_info"}
    MT5BridgeState.close_next = 1  # server will close after next response
    assert c.account() == {"ok": "get_trading_account_info"}
    # connection was closed server-side; the FOLLOWING call must still work
    assert c.account() == {"ok": "get_trading_account_info"}
    assert MT5BridgeState.connections >= 2  # at least one reconnect happened


def test_idempotent_read_tool_gets_retry_ladder(bridge: str) -> None:
    """A read tool failing in the response phase is retried (2 in _post + 1 outer)."""
    c = _client(bridge)
    MT5BridgeState.explode = {"get_marketwatch_symbols"}
    MT5BridgeState.call_requests = 0
    with pytest.raises(MCPError):
        c.symbols()  # get_marketwatch_symbols is idempotent
    # the tools/call reached the bridge MULTIPLE times: in-_post retry + the
    # outer _call retry (fresh connections between attempts)
    assert MT5BridgeState.call_requests >= 3


def test_order_tool_never_response_retry(bridge: str) -> None:
    """Order tools must NOT retry response-phase failures (double-fire risk)."""
    c = _client(bridge)
    MT5BridgeState.explode = {"trade_send_market_order"}
    MT5BridgeState.call_requests = 0
    with pytest.raises(MCPError):
        c.market_order("XAUUSDm", "buy", 0.01)
    # exactly ONE tools/call reached the bridge — no retry of any kind
    assert MT5BridgeState.call_requests == 1


def test_idle_connection_is_proactively_rebuilt(bridge: str) -> None:
    """Idle > MT5_MCP_CONN_MAX_IDLE_S -> fresh connection on next use."""
    c = _client(bridge)
    assert c.account() == {"ok": "get_trading_account_info"}
    assert MT5BridgeState.connections == 1
    # simulate an idle window far beyond the 25s default
    c._local.used_at = time.monotonic() - 60.0  # noqa: SLF001 — test hook
    assert c.account() == {"ok": "get_trading_account_info"}
    assert MT5BridgeState.connections == 2  # stale conn dropped + rebuilt


def test_connect_failure_is_fast(bridge: str) -> None:
    """Connect-phase failure raises MCPError (fast budget, no long stall)."""
    c = MT5TerminalClient(url="http://127.0.0.1:9/mcp", key="k")
    t0 = time.monotonic()
    with pytest.raises(MCPError):
        c.account()
    elapsed = time.monotonic() - t0
    assert elapsed < 20.0  # 2 connect attempts @ short budget, not 60s each
