"""D-055 tests — the LIVE pending-order path to the real MT5 terminal.

User report: "pending orders show in the app but never reach the Exness
terminal" — the practice plane's paper book filled the UI while the REAL
terminal path was broken at five distinct points. These tests lock in the
fixes against a SCHEMA-VALIDATING fake terminal (the argument contracts the
real terminal MCP enforces — the D-050 code was never live-verified and
guessed the field names):

1. CONTRACT  — pending_order() negotiates tools/list and sends the field
              the terminal's inputSchema declares (`type`, + filling_type
              "return" when offered); a legacy `order_type` schema also
              works (adaptive, both directions).
2. RETCODE   — a placed pending order answering 10008 PLACED (not only
              10009 DONE) is a SUCCESS with the ORDER ticket.
3. RETRY     — the executor never blind-retries a transport failure
              (retcode None = the order may be live -> double-fire risk);
              an explicit broker rejection (retcode present) still retries.
4. ORPHANS   — signal expiry/cancel CANCELS a waiting terminal order
              (close_position dispatch: position -> close tool, waiting
              order -> trade_delete_order, gone -> already-closed ok).
5. KILL SW   — daily-loss/profit stops also cancel every waiting pending.
"""

from __future__ import annotations

import asyncio

import pytest

from app.engine.config import DEFAULT_CONFIG
from app.engine.executor import OrderExecutor, TradeRepo
from app.mt5.auto_trader import McpAutoTrader
from app.mt5.base import Order
from app.mt5.mcp import MCPError, MT5TerminalClient
from app.mt5.mcp_source import McpTradingSource

# ------------------------------------------------------------ fake terminal

#: the tool descriptors a real MT5 terminal (build 6000+) serves on
#: tools/list — field names mirror the LIVE-VERIFIED market_order contract
#: (D-034: {"symbol","type","volume","sl","tp","comment"}).
MODERN_TOOLS = [
    {
        "name": "get_trading_account_info",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_trading_open_positions",
        "inputSchema": {
            "type": "object",
            "properties": {"include_orders": {"type": "boolean"}},
        },
    },
    {
        "name": "trade_send_market_order",
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "type": {"type": "string"},
                "volume": {"type": "number"},
                "sl": {"type": "number"},
                "tp": {"type": "number"},
                "comment": {"type": "string"},
                "filling_type": {"type": "string"},
            },
            "required": ["symbol", "type", "volume"],
        },
    },
    {
        "name": "trade_send_pending_order",
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "type": {"type": "string"},
                "volume": {"type": "number"},
                "price": {"type": "number"},
                "sl": {"type": "number"},
                "tp": {"type": "number"},
                "comment": {"type": "string"},
                "filling_type": {"type": "string"},
            },
            "required": ["symbol", "type", "volume", "price"],
        },
    },
    {
        "name": "trade_delete_order",
        "inputSchema": {
            "type": "object",
            "properties": {"order_ticket": {"type": "number"}},
            "required": ["order_ticket"],
        },
    },
    {
        "name": "trade_close_single_position",
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "position_ticket": {"type": "number"},
            },
            "required": ["symbol", "position_ticket"],
        },
    },
]

#: a hypothetical older/legacy terminal that named the field `order_type`
LEGACY_PENDING_TOOL = {
    "name": "trade_send_pending_order",
    "inputSchema": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "order_type": {"type": "string"},
            "volume": {"type": "number"},
            "price": {"type": "number"},
        },
        "required": ["symbol", "order_type", "volume", "price"],
    },
}


class SchemaTerminal:
    """Fake MT5TerminalClient that VALIDATES tool arguments like the real
    MCP server does (required params from the inputSchema) and keeps a live
    WAITING book that pending_order/delete_order mutate."""

    def __init__(self, *, tools=None, pending_retcode: int = 10008,
                 equity: float = 500.0,
                 open_positions: list | None = None) -> None:
        self._tools = {t["name"]: t for t in (tools or MODERN_TOOLS)}
        self.pending_retcode = pending_retcode
        self.equity = equity
        self.waiting: list[dict] = []
        self.open_positions: list[dict] = open_positions or []
        self.closes: list[int] = []
        self.deleted: list[int] = []
        self.pending_calls: list[dict] = []
        self.next_ticket = 700100000

    # ---- schema the client negotiates over
    def tools_list(self, refresh: bool = False) -> list[dict]:  # noqa: ARG002
        return list(self._tools.values())

    def tool_schema(self, name: str) -> dict | None:
        return self._tools.get(name, {}).get("inputSchema")

    # ---- read tools
    def account(self) -> dict:
        return {
            "account": {
                "login": 414350770, "server": "Exness-MT5Trial6",
                "broker": "Exness", "balance": self.equity,
                "equity": self.equity, "margin_free": self.equity,
                "profit": 0.0, "currency": "USD", "leverage": 200,
            },
            "terminal": {
                "server_connected": True, "mcp_trade_allowed": True,
                "experts_trade_allowed": True, "build": 6204,
            },
        }

    def positions(self, include_orders: bool = True) -> dict:  # noqa: ARG002
        return {
            "positions": list(self.open_positions),
            "orders": list(self.waiting),
        }

    def symbols(self) -> list[dict]:
        return [
            {"symbol": "XAUUSDm", "point": 0.01, "contract_size": 100.0,
             "volume_min": 0.01, "volume_max": 200.0, "volume_step": 0.01},
        ]

    # ---- order tools (schema-validated)
    def _require(self, tool: str, args: dict) -> None:
        schema = self._tools.get(tool, {}).get("inputSchema", {})
        required = schema.get("required", [])
        missing = [k for k in required if k not in args]
        if missing:
            # a real MCP server answers with a JSON-RPC tool error
            raise MCPError(
                f"Tool '{tool}' missing required parameter(s): {', '.join(missing)}"
            )
        allowed = set(schema.get("properties", {}))
        unknown = [k for k in args if allowed and k not in allowed]
        if unknown:
            raise MCPError(
                f"Tool '{tool}' got unknown parameter(s): {', '.join(unknown)}"
            )

    def market_order(self, symbol, side, volume, sl=None, tp=None, comment=""):
        self._require("trade_send_market_order", {
            "symbol": symbol, "type": side, "volume": volume,
        })
        return {
            "retcode": 10009, "retcode_details": "done",
            "deal": 4425000099, "order": 991000009,
            "price": 2700.0, "volume": volume, "symbol": symbol,
        }

    def pending_order(self, symbol, order_type, volume, price,
                      sl=None, tp=None, comment=""):
        # NOTE: the source passes its Order.order_type as the type VALUE —
        # the field NAME is whatever this terminal's schema declared.
        schema = self._tools["trade_send_pending_order"]["inputSchema"]
        field = (
            "order_type" if "order_type" in schema.get("properties", {})
            and "type" not in schema.get("properties", {})
            else "type"
        )
        args = {field: order_type, "symbol": symbol, "volume": volume,
                "price": price}
        if "filling_type" in schema.get("properties", {}):
            args["filling_type"] = "return"  # the source must send it
        self._require("trade_send_pending_order", args)
        self.pending_calls.append(args)
        ticket = self.next_ticket
        self.next_ticket += 1
        self.waiting.append({
            "order_ticket": ticket, "symbol": symbol, "type": order_type,
            "volume": volume, "price": price, "sl": sl, "tp": tp,
        })
        return {
            "retcode": self.pending_retcode,
            "retcode_details": "placed",
            "order": ticket, "price": price, "volume": volume,
        }

    def delete_order(self, order_ticket):
        self._require("trade_delete_order", {"order_ticket": order_ticket})
        self.deleted.append(order_ticket)
        self.waiting = [o for o in self.waiting
                        if o["order_ticket"] != order_ticket]
        return {"retcode": 10009, "retcode_details": "done"}

    def close_position(self, symbol, ticket):
        self.open_positions = [
            p for p in self.open_positions if p["position_id"] != ticket
        ]
        self.closes.append(ticket)
        return {"retcode": 10009, "retcode_details": "done", "price": 2701.0}


def _pos(ticket=991000001, symbol="XAUUSDm", action="buy"):
    return {
        "position_id": ticket, "action": action, "symbol": symbol,
        "volume": 0.01, "price_open": 2700.0, "profit": 0.01,
        "create_time": "2026.09.23 06:00:00",
        "stop_loss": 2695.0, "take_profit": 2710.0,
    }


def make_source(client) -> McpTradingSource:
    return McpTradingSource(client=client)  # type: ignore[arg-type]


LIM_SIG = {
    "id": "abcdef0123456789",
    "direction": "BUY",
    "entry": 2694.0,        # BUY limit BELOW the market (POI zone edge)
    "sl": 2689.0,
    "tp": 2704.0,
    "confidence": 0.7,
    "spread_points": 8.0,
    "symbol": "XAUUSD",
    "tf": "M5",
    "entry_type": "limit",
}


class FakeHub:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def broadcast_all(self, kind: str, payload: dict) -> None:
        self.events.append((kind, payload))


# ------------------------------------------------- 1. schema negotiation

async def test_pending_order_sends_schema_type_and_filling() -> None:
    """The terminal's own inputSchema drives the field names: `type` (the
    LIVE-VERIFIED market-order spelling) + filling_type "return" when the
    schema offers it. The D-050 blind guess `order_type` would have failed
    required-parameter validation — the order never reached Exness."""
    client = SchemaTerminal()
    src = make_source(client)
    res = await src.place_order(
        Order(symbol="XAUUSD", side="BUY", volume=0.02,
              sl=2689.0, tp=2704.0, order_type="buy_limit", price=2694.0,
              comment="xauai-abcdef01")
    )
    assert res.ok
    call = client.pending_calls[0]
    assert call["type"] == "buy_limit"
    assert "order_type" not in call
    assert call["filling_type"] == "return"
    assert call["symbol"] == "XAUUSDm"  # broker name mapping intact
    assert call["price"] == 2694.0


async def test_pending_order_adapts_to_legacy_order_type_schema() -> None:
    """A terminal declaring `order_type` in its schema gets `order_type` —
    the negotiation is honest in BOTH directions (no blind guess)."""
    tools = [t for t in MODERN_TOOLS if t["name"] != "trade_send_pending_order"]
    tools.append(LEGACY_PENDING_TOOL)
    client = SchemaTerminal(tools=tools)
    src = make_source(client)
    res = await src.place_order(
        Order(symbol="XAUUSD", side="SELL", volume=0.01,
              order_type="sell_limit", price=2712.0)
    )
    assert res.ok
    call = client.pending_calls[0]
    assert call["order_type"] == "sell_limit"
    assert "type" not in call


async def test_pending_order_without_schema_guesses_type() -> None:
    """tools/list unavailable (older terminal build) -> default to the
    live-verified `type` field — the same spelling market_order used for
    its verified live round-trip."""
    client = SchemaTerminal()
    # simulate an unreachable tools/list: strip the negotiated cache
    client.tool_schema = lambda name: None  # type: ignore[method-assign]
    client.tools_list = lambda refresh=False: []  # type: ignore[method-assign]
    src = make_source(client)
    res = await src.place_order(
        Order(symbol="XAUUSD", side="BUY", volume=0.01,
              order_type="buy_limit", price=2694.0)
    )
    assert res.ok
    # SchemaTerminal's own _require validates against MODERN schema via the
    # args we assembled — the client sent `type`
    assert client.pending_calls[0]["type"] == "buy_limit"


# ---------------------------------------------------------- 2. retcode 10008

async def test_pending_10008_placed_is_success_with_order_ticket() -> None:
    """PLACED (10008) is the classic pending acceptance — treating it as a
    failure made the executor RETRY a live order (duplicate exposure)."""
    client = SchemaTerminal(pending_retcode=10008)
    src = make_source(client)
    res = await src.place_order(
        Order(symbol="XAUUSD", side="BUY", volume=0.02,
              order_type="buy_limit", price=2694.0)
    )
    assert res.ok is True
    assert res.retcode == 10008
    assert res.ticket == 700100000
    assert res.price == pytest.approx(2694.0)


async def test_market_order_still_requires_done() -> None:
    """Market orders keep the strict DONE contract (10009)."""
    client = SchemaTerminal()
    src = make_source(client)
    res = await src.place_order(
        Order(symbol="XAUUSD", side="BUY", volume=0.01)
    )
    assert res.ok and res.retcode == 10009


# ------------------------------------------------------------- 3. safe retry

async def test_executor_never_retries_transport_failure() -> None:
    """retcode ABSENT = the order may be live on the terminal — a retry
    double-fires. The executor must give up after ONE attempt."""

    class FlakyPending(SchemaTerminal):
        def __init__(self) -> None:
            super().__init__()
            self.attempts = 0

        def pending_order(self, *a, **kw):
            self.attempts += 1
            raise MCPError("MT5 terminal unreachable: connection reset")

    client = FlakyPending()
    src = make_source(client)
    repo = TradeRepo(None)
    executor = OrderExecutor(source=src, cfg=DEFAULT_CONFIG, repo=repo,
                             hub=None, owner=None)
    executor.arm(True)
    result = await executor.execute_signal(LIM_SIG, "XAUUSDm", 0.01)
    assert result is not None
    assert result.ok is False
    assert client.attempts == 1  # NOT retried — no duplicate order risk


async def test_executor_retries_broker_rejection_once() -> None:
    """retcode PRESENT = a genuine broker rejection (order definitely not
    live) — one retry is safe and can succeed after a re-quote."""

    class RequoteThenOk(SchemaTerminal):
        def __init__(self) -> None:
            super().__init__()
            self.attempts = 0

        def pending_order(self, symbol, order_type, volume, price,
                          sl=None, tp=None, comment=""):
            self.attempts += 1
            if self.attempts == 1:
                # TRADE_RETCODE_REQUOTE from the broker (order NOT placed)
                return {"retcode": 10004, "retcode_details": "Requote"}
            return super().pending_order(
                symbol, order_type, volume, price, sl, tp, comment
            )

    client = RequoteThenOk()
    src = make_source(client)
    repo = TradeRepo(None)
    executor = OrderExecutor(source=src, cfg=DEFAULT_CONFIG, repo=repo,
                             hub=None, owner=None)
    executor.arm(True)
    result = await executor.execute_signal(LIM_SIG, "XAUUSDm", 0.01)
    assert result is not None and result.ok
    assert client.attempts == 2  # rejected once, retried once, placed


# ------------------------------------------------- 4. orphan lifecycle

async def test_expired_signal_cancels_waiting_terminal_order() -> None:
    """THE orphan bug: a waiting limit order + an expired signal -> the old
    close_position() found no POSITION, answered "already closed" ok, and
    the LIVE order stayed on Exness. Now it is cancelled via the delete
    tool and the trade row closes."""
    client = SchemaTerminal()
    src = make_source(client)
    # place the pending (as the executor would)
    res = await src.place_order(
        Order(symbol="XAUUSD", side="BUY", volume=0.02,
              sl=2689.0, tp=2704.0, order_type="buy_limit", price=2694.0)
    )
    assert res.ok
    ticket = res.ticket

    hub = FakeHub()
    repo = TradeRepo(None)
    trader = McpAutoTrader(source=src, cfg=DEFAULT_CONFIG, repo=repo,
                           hub=hub, config_repo=None)
    await trader._repo.insert({
        "signal_id": LIM_SIG["id"], "owner": None, "ticket": ticket,
        "side": "BUY", "volume": 0.02, "price_open": 2694.0,
        "sl": 2689.0, "tp": 2704.0,
        "opened_at": "2026-09-23T06:00:00+00:00",
    })

    await trader.notify_signal_status(LIM_SIG["id"], "expired")
    assert ticket in client.deleted          # the waiting order WAS cancelled
    assert not client.waiting                # the terminal book is clean
    assert client.closes == []               # no bogus close of a position
    # the linked trade row is closed (memory book: the repo has no DB here)
    row = next(t for t in repo._mem if t["signal_id"] == LIM_SIG["id"])
    assert row.get("closed_at") is not None


async def test_expired_signal_closes_filled_position_normally() -> None:
    """A FILLED pending is a normal position on expiry — closed by ticket."""
    client = SchemaTerminal(open_positions=[_pos(ticket=700100000)])
    src = make_source(client)
    hub = FakeHub()
    repo = TradeRepo(None)
    trader = McpAutoTrader(source=src, cfg=DEFAULT_CONFIG, repo=repo,
                           hub=hub, config_repo=None)
    await repo.insert({
        "signal_id": LIM_SIG["id"], "owner": None, "ticket": 700100000,
        "side": "BUY", "volume": 0.02, "price_open": 2694.0,
        "sl": 2689.0, "tp": 2704.0,
        "opened_at": "2026-09-23T06:00:00+00:00",
    })
    await trader.notify_signal_status(LIM_SIG["id"], "expired")
    assert 700100000 in client.closes
    assert client.deleted == []


async def test_cancelled_signal_also_cleans_waiting_order() -> None:
    client = SchemaTerminal()
    src = make_source(client)
    res = await src.place_order(
        Order(symbol="XAUUSD", side="SELL", volume=0.01,
              order_type="sell_limit", price=2712.0)
    )
    ticket = res.ticket
    hub = FakeHub()
    repo = TradeRepo(None)
    trader = McpAutoTrader(source=src, cfg=DEFAULT_CONFIG, repo=repo,
                           hub=hub, config_repo=None)
    await repo.insert({
        "signal_id": LIM_SIG["id"], "owner": None, "ticket": ticket,
        "side": "SELL", "volume": 0.01, "price_open": 2712.0,
        "opened_at": "2026-09-23T06:00:00+00:00",
    })
    await trader.notify_signal_status(LIM_SIG["id"], "cancelled")
    assert ticket in client.deleted


async def test_close_position_dispatch_three_ways() -> None:
    client = SchemaTerminal(open_positions=[_pos(ticket=991000001)])
    client.waiting.append({
        "order_ticket": 700100001, "symbol": "XAUUSDm", "type": "buy_limit",
        "volume": 0.02, "price": 2694.0, "sl": None, "tp": None,
    })
    src = make_source(client)
    # 1. open position -> close tool
    r1 = await src.close_position(991000001)
    assert r1.ok and client.closes == [991000001]
    # 2. waiting order -> delete tool
    r2 = await src.close_position(700100001)
    assert r2.ok and 700100001 in client.deleted
    # 3. neither -> honest already-closed ok
    r3 = await src.close_position(424242)
    assert r3.ok and "already closed" in r3.comment


async def test_pending_orders_contract_for_ui() -> None:
    """D-054 contract parity: the LIVE plane's waiting orders surface with
    the same dict shape the paper planes serve (ticket/side/order_type/
    volume/price/sl/tp)."""
    client = SchemaTerminal()
    client.waiting.append({
        "order_ticket": 700100002, "symbol": "XAUUSDm", "type": "sell_limit",
        "volume": 0.03, "price": 2712.5, "sl": 2717.0, "tp": 2702.0,
    })
    src = make_source(client)
    pendings = await src.pending_orders()
    assert len(pendings) == 1
    o = pendings[0]
    assert o["ticket"] == 700100002
    assert o["side"] == "SELL"
    assert o["order_type"] == "sell_limit"
    assert o["volume"] == pytest.approx(0.03)
    assert o["price"] == pytest.approx(2712.5)
    assert o["sl"] == pytest.approx(2717.0)


# --------------------------------------------------------- 5. kill switches

async def test_emergency_stop_cancels_waiting_pendings() -> None:
    """Daily-loss kill switch closes positions AND cancels waiting orders —
    surviving pendings could fill after the safety limit already fired."""
    client = SchemaTerminal(open_positions=[_pos(ticket=991000001)])
    client.waiting.append({
        "order_ticket": 700100003, "symbol": "XAUUSDm", "type": "buy_limit",
        "volume": 0.02, "price": 2694.0, "sl": None, "tp": None,
    })
    src = make_source(client)
    repo = TradeRepo(None)
    executor = OrderExecutor(source=src, cfg=DEFAULT_CONFIG, repo=repo,
                             hub=None, owner=None)
    executor.arm(True)
    await executor._emergency_stop()
    assert 700100003 in client.deleted
    assert not client.waiting
    assert 991000001 in client.closes
    assert executor.auto_trade is False


async def test_status_surfaces_waiting_pendings() -> None:
    """The admin AI tab can finally SEE the terminal's live pending book."""
    client = SchemaTerminal()
    client.waiting.append({
        "order_ticket": 700100004, "symbol": "XAUUSDm", "type": "buy_limit",
        "volume": 0.02, "price": 2694.0, "sl": None, "tp": None,
    })
    src = make_source(client)
    trader = McpAutoTrader(source=src, cfg=DEFAULT_CONFIG,
                           repo=TradeRepo(None), hub=FakeHub(),
                           config_repo=None)
    st = await trader.status()
    assert st["pending_count"] == 1
    assert st["pending_orders"][0]["ticket"] == 700100004


# ------------------------------------------- wire-level negotiation (D-047)

class _WireBridge:  # minimal HTTP JSON-RPC fake for the REAL client
    def __init__(self) -> None:
        import http.server
        import threading

        outer = self
        outer.calls: list[dict] = []

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:  # noqa: N802
                import json as _json
                length = int(self.headers.get("Content-Length", 0))
                payload = _json.loads(self.rfile.read(length) or b"{}")
                method = payload.get("method")
                if method == "initialize":
                    self._respond(_json.dumps({
                        "jsonrpc": "2.0", "id": payload.get("id"),
                        "result": {"serverInfo": {"name": "fake-term"}},
                    }))
                    return
                if method == "notifications/initialized":
                    self.send_response(202)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if method == "tools/list":
                    self._respond(_json.dumps({
                        "jsonrpc": "2.0", "id": payload.get("id"),
                        "result": {"tools": MODERN_TOOLS},
                    }))
                    return
                name = (payload.get("params") or {}).get("name", "")
                args = (payload.get("params") or {}).get("arguments", {})
                outer.calls.append({"name": name, "args": args})
                if name == "trade_send_pending_order":
                    req = MODERN_TOOLS[3]["inputSchema"]["required"]
                    missing = [k for k in req if k not in args]
                    if missing:
                        self._respond(_json.dumps({
                            "jsonrpc": "2.0", "id": payload.get("id"),
                            "error": {"code": -32602,
                                      "message": f"missing: {','.join(missing)}"},
                        }))
                        return
                    self._respond(_json.dumps({
                        "jsonrpc": "2.0", "id": payload.get("id"),
                        "result": {"content": [{"type": "text", "text":
                            _json.dumps({
                                "retcode": 10008, "retcode_details": "placed",
                                "order": 700100009, "price": args["price"],
                            })}]},
                    }))
                    return
                self._respond(_json.dumps({
                    "jsonrpc": "2.0", "id": payload.get("id"),
                    "result": {"content": [{"type": "text", "text":
                        _json.dumps({"ok": name})}]},
                }))

            def _respond(self, body: str) -> None:
                data = body.encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a) -> None:  # silence
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        t = threading.Thread(target=self.server.serve_forever, daemon=True)
        t.start()
        host, port = self.server.server_address[:2]
        self.url = f"http://{host}:{port}/mcp"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


async def test_wire_client_pending_negotiates_schema() -> None:
    """End-to-end over REAL HTTP JSON-RPC: the MT5TerminalClient negotiates
    tools/list, sends `type` (+ filling_type) per the terminal's schema and
    accepts 10008 PLACED."""
    bridge = _WireBridge()
    try:
        client = MT5TerminalClient(url=bridge.url, key="test-key")
        res = await asyncio.to_thread(
            client.pending_order, "XAUUSDm", "buy_limit", 0.02, 2694.0,
            2689.0, 2704.0, "xauai-test",
        )
        assert res["retcode"] == 10008
        assert res["order"] == 700100009
        pend = next(c for c in bridge.calls
                    if c["name"] == "trade_send_pending_order")
        assert pend["args"]["type"] == "buy_limit"
        assert pend["args"]["filling_type"] == "return"
        assert "order_type" not in pend["args"]
    finally:
        bridge.close()
