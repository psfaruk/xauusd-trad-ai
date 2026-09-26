"""D-078 tests — the runtime bridge endpoint + staged diagnostics.

User report: "MetaTrader 5 terminal bridge unavailable: MT5 MCP HTTP 502"
— data offline, Exness connect failing. Root anatomy: the Railway backend
reaches the tunnel GATEWAY (alive) but the tunnel client on the user's PC
is offline — OR the free tunnel rotated its URL, leaving MT5_MCP_URL
pointing at a dead endpoint that only a redeploy could fix.

D-078 fixes:
  1. the endpoint is RUNTIME state — set_bridge_url() hot-swaps every live
     MT5TerminalClient (they re-resolve per RPC via the stamp check);
  2. the override persists in app_config (restarts keep it);
  3. diagnose_bridge() walks parse→dns→tcp→tls→http→mcp and returns the
     exact break stage + human remediation;
  4. the probe journal feeds the honest feed_status note and the Exness
     connect errors (broker_connect hint).

These tests run REAL local HTTP servers (ok / 502 / 401 / 404 modes) and
exercise the REAL client + REAL diagnose code paths — no monkeypatched
internals on the bridge itself.
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from sqlalchemy import text

from app.mt5 import bridge_config
from app.mt5.bridge_config import (
    clear_runtime_url,
    diagnose_bridge,
    get_bridge_url,
    hint_for_error,
    load_persisted_url,
    persist_url,
    set_bridge_url,
    url_stamp,
    validate_bridge_url,
)
from app.mt5.mcp import MT5TerminalClient

# ------------------------------------------------------------------ fake MCP

ACCOUNT_OK = {
    "account": {
        "login": 414350770, "server": "Exness-MT5Trial6", "balance": 1000.0,
        "equity": 1000.0, "currency": "USD",
    },
    "terminal": {"server_connected": True, "build": 6205, "mcp_trade_allowed": True},
}


class _FakeMCP(BaseHTTPRequestHandler):
    """A real local HTTP server speaking enough MCP for the client +
    diagnostics. mode: "ok" | "502" | "401" | "404"."""

    def log_message(self, *args):  # silence
        pass

    def do_POST(self):  # noqa: N802 — http.server API
        mode = getattr(self.server, "mode", "ok")
        hits = getattr(self.server, "hits", None)
        if hits is not None:
            hits.append(self.path)
        if mode == "502":
            self._raw(502, "Bad Gateway")
            return
        if mode == "401":
            self._raw(401, "Unauthorized")
            return
        if mode == "404":
            self._raw(404, "Not Found")
            return
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        method = payload.get("method")
        if method == "initialize":
            body = {
                "jsonrpc": "2.0", "id": payload.get("id"),
                "result": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fake-mt5", "version": "1.0"},
                },
            }
            self._json(200, body)
            return
        if method == "notifications/initialized":
            self._raw(202, "")
            return
        name = (payload.get("params") or {}).get("name")
        if name == "get_trading_account_info":
            self._json(200, {
                "jsonrpc": "2.0", "id": payload.get("id"),
                "result": {"content": [
                    {"type": "text", "text": json.dumps(ACCOUNT_OK)}
                ]},
            })
            return
        if name == "get_chart_ticks_history":
            self._json(200, {
                "jsonrpc": "2.0", "id": payload.get("id"),
                "result": {"content": [{"type": "text", "text": json.dumps({"history": []})}]},
            })
            return
        if name == "get_marketwatch_symbols":
            self._json(200, {
                "jsonrpc": "2.0", "id": payload.get("id"),
                "result": {"content": [{"type": "text", "text": json.dumps(
                    {"symbols": [{"symbol": "XAUUSDm"}, {"symbol": "BTCUSDm"},
                                 {"symbol": "USOIL"}, {"symbol": "USTEC"}]})}]},
            })
            return
        self._json(200, {
            "jsonrpc": "2.0", "id": payload.get("id"),
            "result": {"content": [{"type": "text", "text": "{}"}]},
        })

    def _json(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _raw(self, status: int, body: str) -> None:
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture()
def mcp_factory():
    servers: list[ThreadingHTTPServer] = []

    def _make(mode: str = "ok") -> tuple[ThreadingHTTPServer, str, list[str]]:
        srv = ThreadingHTTPServer(("127.0.0.1", 0), _FakeMCP)
        srv.mode = mode  # type: ignore[attr-defined]
        srv.hits = []  # type: ignore[attr-defined]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        servers.append(srv)
        port = srv.server_address[1]
        return srv, f"http://127.0.0.1:{port}/mcp", srv.hits  # type: ignore[attr-defined]

    yield _make
    for s in servers:
        s.shutdown()
        s.server_close()


@pytest.fixture(autouse=True)
def _clean_bridge_state(monkeypatch):
    """Isolate the process-global bridge state per test."""
    clear_runtime_url()
    bridge_config._probe = None  # noqa: SLF001 — test isolation
    monkeypatch.setenv("MT5_MCP_URL", "http://127.0.0.1:9/mcp")
    monkeypatch.setenv("MT5_MCP_KEY", "test-key")
    # fresh dynamic singleton per test (its stamp snapshot may be stale)
    import app.mt5.mcp as mcp_mod

    monkeypatch.setattr(mcp_mod, "_client", None)
    yield
    clear_runtime_url()
    bridge_config._probe = None  # noqa: SLF001


# ----------------------------------------------------------- url state store

class TestBridgeConfig:
    def test_precedence_env_then_runtime(self):
        assert get_bridge_url() == "http://127.0.0.1:9/mcp"  # env
        set_bridge_url("http://10.0.0.5:22346/mcp")
        assert get_bridge_url() == "http://10.0.0.5:22346/mcp"
        clear_runtime_url()
        assert get_bridge_url() == "http://127.0.0.1:9/mcp"

    def test_validation_rejects_junk(self):
        for bad in ("", "not a url", "ftp://host/mcp", "http://", "://x"):
            with pytest.raises(ValueError):
                validate_bridge_url(bad)

    def test_set_bumps_stamp(self):
        s0 = url_stamp()
        set_bridge_url("http://a.example:1/mcp")
        assert url_stamp() == s0 + 1
        clear_runtime_url()
        assert url_stamp() == s0 + 2

    def test_set_same_url_is_noop(self):
        set_bridge_url("http://same.example:2/mcp")
        s = url_stamp()
        set_bridge_url("http://same.example:2/mcp")
        assert url_stamp() == s

    def test_mask_hides_long_paths(self):
        masked = bridge_config.mask_url("https://h.example/mcp?XTransformPort=22346&x=1")
        assert masked.startswith("https://h.example/")

    def test_hint_for_error_classes(self):
        assert "tunnel client" in hint_for_error("MT5 MCP HTTP 502")
        assert "key mismatch" in hint_for_error("MT5 MCP HTTP 401")
        assert "path" in hint_for_error("MT5 MCP HTTP 404")
        assert "Settings" in hint_for_error("MT5 terminal unreachable: x")


# ------------------------------------------------------ dynamic client swap

class TestDynamicClient:
    def test_client_url_property_follows_runtime(self):
        c = MT5TerminalClient()  # dynamic
        assert c.url == "http://127.0.0.1:9/mcp"
        set_bridge_url("http://dyn.example:5/mcp")
        assert c.url == "http://dyn.example:5/mcp"
        clear_runtime_url()
        assert c.url == "http://127.0.0.1:9/mcp"

    def test_explicit_url_stays_frozen(self):
        c = MT5TerminalClient(url="http://fixed.example:7/mcp")
        set_bridge_url("http://other.example:8/mcp")
        assert c.url == "http://fixed.example:7/mcp"

    def test_hot_swap_redirects_live_client(self, mcp_factory):
        """THE fix: a live client switches to the NEW endpoint on its next
        call — the tunnel-rotation scenario with no restart/redeploy."""
        _, url_a, hits_a = mcp_factory("ok")
        _, url_b, hits_b = mcp_factory("ok")
        set_bridge_url(url_a)
        client = MT5TerminalClient(key="k")
        acct = client.account()
        assert acct["account"]["login"] == 414350770
        n_a = len(hits_a)
        # the tunnel rotates -> admin saves the new URL in-app
        set_bridge_url(url_b)
        acct2 = client.account()
        assert acct2["account"]["login"] == 414350770
        assert len(hits_b) >= 1
        assert len(hits_a) == n_a  # old endpoint sees NOTHING new

    def test_clone_inherits_dynamic_mode(self, mcp_factory):
        _, url_a, _ = mcp_factory("ok")
        set_bridge_url(url_a)
        client = MT5TerminalClient(key="k")
        clone = client.clone()
        assert clone._fixed_url is None  # noqa: SLF001 — dynamic mode inherited
        _, url_b, hits_b = mcp_factory("ok")
        set_bridge_url(url_b)
        clone.account()
        assert len(hits_b) >= 1


# ------------------------------------------------------------- diagnostics

class TestDiagnose:
    def test_parse_failure(self):
        d = diagnose_bridge(url="garbage")
        assert d["ok"] is False
        assert d["stages"][0]["stage"] == "parse"
        assert d["hint"]

    def test_dns_failure(self):
        d = diagnose_bridge(url="http://no-such-host-d078.invalid/mcp")
        stages = {s["stage"]: s for s in d["stages"]}
        assert stages["parse"]["ok"] and stages["dns"]["ok"] is False
        assert "rotate" in (stages["dns"]["hint"] or "")

    def test_tcp_refused(self):
        # port 9 (discard) is closed in the sandbox — conftest's dead URL
        d = diagnose_bridge(url="http://127.0.0.1:9/mcp")
        stages = {s["stage"]: s for s in d["stages"]}
        assert stages["dns"]["ok"] is True
        assert stages["tcp"]["ok"] is False

    def test_http_502_explains_tunnel_client(self, mcp_factory):
        _, url, _ = mcp_factory("502")
        d = diagnose_bridge(url=url)
        stages = {s["stage"]: s for s in d["stages"]}
        assert stages["http"]["ok"] is False
        assert "502" in stages["http"]["detail"]
        assert "tunnel client" in stages["http"]["hint"]
        assert d["ok"] is False
        assert "http" in d["verdict"]

    def test_http_401_explains_key(self, mcp_factory):
        _, url, _ = mcp_factory("401")
        d = diagnose_bridge(url=url)
        stages = {s["stage"]: s for s in d["stages"]}
        assert stages["http"]["ok"] is False
        assert "key mismatch" in stages["http"]["hint"]

    def test_http_404_explains_path(self, mcp_factory):
        _, url, _ = mcp_factory("404")
        d = diagnose_bridge(url=url)
        stages = {s["stage"]: s for s in d["stages"]}
        assert stages["http"]["ok"] is False
        assert "path" in (stages["http"]["hint"] or "")

    def test_full_chain_ok(self, mcp_factory):
        _, url, _ = mcp_factory("ok")
        d = diagnose_bridge(url=url)
        assert d["ok"] is True
        stages = {s["stage"]: s for s in d["stages"]}
        assert all(stages[k]["ok"] for k in ("parse", "dns", "tcp", "http", "mcp"))
        assert "414350770" in stages["mcp"]["detail"]


# ---------------------------------------------------- feed wiring (no mocks)

class TestFeedReattach:
    async def test_reattach_now_on_new_url_attaches_overlay(self, mcp_factory):
        """The exact production scenario, end to end with a REAL local MCP
        server: bridge dead (env port 9) -> admin saves the new tunnel URL
        -> reattach_now() attaches the real overlay and every symbol core
        rewires to the broker feed."""
        from app.mt5.live_source import MarketFeed

        feed = MarketFeed(enable_ws=False)
        try:
            assert feed.mcp is None
            # the honest pre-fix status: not attached + probe journal
            from app.mt5.mcp_market import mcp_market_available

            assert await asyncio.to_thread(mcp_market_available) is False
            probe = bridge_config.last_probe()
            assert probe is not None and probe["ok"] is False
            # feed_status surfaces the probe (D-078)
            from app.mt5.live_source import LiveDataSource

            src = LiveDataSource(enable_ws=False, market=feed)
            st = src.feed_status()
            assert st["mt5"]["bridge"] == "not_attached"
            assert st["mt5"]["probe"]["ok"] is False
            assert "Settings" in st["note"]
            await src.disconnect()

            _, url, _ = mcp_factory("ok")
            set_bridge_url(url)
            assert await feed.reattach_now() is True
            assert feed.mcp is not None
            for key, core in feed.feeds.items():
                assert core._mt5 is feed.mcp, key
        finally:
            await feed.stop()

    async def test_bridge_url_changed_wakes_watch_loop(self, mcp_factory):
        """The wake event beats the 20s cadence — an admin URL save probes
        within one beat (test uses 0.3s cadence, no wait)."""
        from app.mt5.live_source import MarketFeed

        feed = MarketFeed(enable_ws=False)
        feed.MCP_REATTACH_S = 30.0  # would NEVER fire within the test
        await feed.start()
        try:
            assert feed.mcp is None
            _, url, _ = mcp_factory("ok")
            set_bridge_url(url)
            feed.bridge_url_changed()
            for _ in range(60):  # ≤ ~3s
                if feed.mcp is not None:
                    break
                await asyncio.sleep(0.05)
            assert feed.mcp is not None, "wake event failed to trigger attach"
        finally:
            await feed.stop()


# ------------------------------------------------------------- persistence

class TestPersistence:
    def _sqlite_engine(self):
        import sqlalchemy as sa
        from sqlalchemy.pool import StaticPool

        eng = sa.create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        with eng.begin() as cx:
            cx.execute(text(
                "create table app_config ("
                "key text primary key, value text not null, updated_at text)"
            ))
        return eng

    async def test_app_config_roundtrip(self):
        eng = self._sqlite_engine()
        assert await load_persisted_url(eng) is None
        assert await persist_url(eng, "http://x.example:22346/mcp") is True
        assert await load_persisted_url(eng) == "http://x.example:22346/mcp"
        # update overwrites
        assert await persist_url(eng, "http://y.example:22346/mcp") is True
        assert await load_persisted_url(eng) == "http://y.example:22346/mcp"

    async def test_load_tolerates_missing_table(self):
        import sqlalchemy as sa
        from sqlalchemy.pool import StaticPool

        eng = sa.create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        assert await load_persisted_url(eng) is None


# ------------------------------------------------------------------- routes

VIEWER = {"id": "11111111-1111-1111-1111-111111111111",
          "email": "trader@example.com", "user_metadata": {}}
ADMIN = {"id": "22222222-2222-2222-2222-222222222222",
         "email": "boss@example.com", "user_metadata": {}}


@pytest.fixture()
def client(monkeypatch):
    import httpx
    from fastapi.testclient import TestClient

    from app import auth as auth_mod
    from app.auth import clear_auth_cache
    from app.config import Settings
    from app.main import app

    clear_auth_cache()

    def handler(request: httpx.Request) -> httpx.Response:
        authz = request.headers.get("authorization", "")
        if authz == "Bearer good-token":
            return httpx.Response(200, json=VIEWER)
        if authz == "Bearer admin-token":
            return httpx.Response(200, json=ADMIN)
        return httpx.Response(401, json={"message": "bad jwt"})

    mock = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0)
    settings = Settings(
        supabase_url="https://supabase.test",
        supabase_anon_key="anon-key-test",
        admin_emails="boss@example.com",
    )
    monkeypatch.setattr(auth_mod, "get_settings", lambda: settings)

    with TestClient(app) as c:
        real_http, real_settings = app.state.http, app.state.settings
        app.state.http, app.state.settings = mock, settings
        try:
            yield c
        finally:
            app.state.http, app.state.settings = real_http, real_settings
            clear_auth_cache()


GOOD = {"Authorization": "Bearer good-token"}
ADMIN_H = {"Authorization": "Bearer admin-token"}


class TestBridgeRoutes:
    def test_get_requires_admin(self, client):
        assert client.get("/api/mt5/bridge", headers=GOOD).status_code == 403

    def test_get_returns_state(self, client):
        r = client.get("/api/mt5/bridge", headers=ADMIN_H)
        assert r.status_code == 200
        body = r.json()
        assert body["source"] == "env"
        assert body["url"].startswith("http")
        assert "attached" in body

    def test_put_validates(self, client):
        r = client.put(
            "/api/mt5/bridge", headers=ADMIN_H, json={"url": "ftp://bad"}
        )
        assert r.status_code == 422

    def test_put_hot_swaps_and_diagnoses(self, client, mcp_factory):
        _, url, _ = mcp_factory("ok")
        r = client.put("/api/mt5/bridge", headers=ADMIN_H, json={"url": url})
        assert r.status_code == 200
        body = r.json()
        assert body["source"] == "runtime"
        assert body["url"] == url
        assert body["diagnosis"]["ok"] is True
        # the runtime state actually switched
        assert get_bridge_url() == url

    def test_diagnose_endpoint(self, client, mcp_factory):
        _, url, _ = mcp_factory("502")
        set_bridge_url(url)
        r = client.post("/api/mt5/bridge/diagnose", headers=ADMIN_H)
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] is False
        stages = {s["stage"]: s for s in d["stages"]}
        assert stages["http"]["ok"] is False

    def test_reset_returns_to_env(self, client, mcp_factory):
        _, url, _ = mcp_factory("ok")
        set_bridge_url(url)
        r = client.delete("/api/mt5/bridge", headers=ADMIN_H)
        assert r.status_code == 200
        assert r.json()["source"] == "env"
