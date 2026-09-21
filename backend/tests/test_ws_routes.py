"""WebSocket + Phase 2/3 REST route integration tests (full app, mock source).

Runs the REAL app with DATA_SOURCE=mock — the mock auto-connects at startup
(D-018) so /api/candles, /ws streaming and /api/mt5/status all work live.
Supabase auth is faked via httpx MockTransport on app.state.http.
"""

from __future__ import annotations

import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import auth as auth_mod
from app.auth import clear_auth_cache
from app.config import Settings
from app.main import app

VIEWER = {"id": "11111111-1111-1111-1111-111111111111",
          "email": "trader@example.com", "user_metadata": {}}
ADMIN = {"id": "22222222-2222-2222-2222-222222222222",
         "email": "boss@example.com", "user_metadata": {}}


@pytest.fixture()
def client(monkeypatch):
    clear_auth_cache()

    def handler(request: httpx.Request) -> httpx.Response:
        authz = request.headers.get("authorization", "")
        if authz == "Bearer good-token":
            return httpx.Response(200, json=VIEWER)
        if authz == "Bearer admin-token":
            return httpx.Response(200, json=ADMIN)
        return httpx.Response(401, json={"message": "bad jwt"})

    mock = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), timeout=5.0
    )
    settings = Settings(
        supabase_url="https://supabase.test",
        supabase_anon_key="anon-key-test",
        admin_emails="boss@example.com",
    )
    monkeypatch.setattr(auth_mod, "get_settings", lambda: settings)

    with TestClient(app) as c:  # runs lifespan -> mock auto-connect (D-018)
        real_http, real_settings = app.state.http, app.state.settings
        app.state.http, app.state.settings = mock, settings
        try:
            yield c
        finally:
            app.state.http, app.state.settings = real_http, real_settings
            clear_auth_cache()


GOOD = {"Authorization": "Bearer good-token"}
ADMIN_H = {"Authorization": "Bearer admin-token"}


class TestMt5Routes:
    def test_status_connected(self, client):
        r = client.get("/api/mt5/status", headers=GOOD)
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "connected"
        assert body["symbol"] == "XAUUSDm"
        assert body["account"]["currency"] == "USD"
        assert body["engine_running"] is True

    def test_status_requires_auth(self, client):
        assert client.get("/api/mt5/status").status_code == 401

    def test_trading_routes_require_broker_connection_428(self, client):
        """D-037: without a broker connection the trading routes answer
        428 (connect required) — honest, never another user's account."""
        r = client.get("/api/mt5/account", headers=GOOD)
        assert r.status_code == 428
        r = client.get("/api/mt5/positions", headers=GOOD)
        assert r.status_code == 428
        r = client.get("/api/mt5/history", headers=GOOD)
        assert r.status_code == 428
        r = client.post(
            "/api/mt5/order", headers=GOOD,
            json={"symbol": "XAUUSDm", "side": "buy", "volume": 0.01},
        )
        assert r.status_code == 428

    def test_auto_trade_arm_requires_connection_428(self, client):
        """D-037: non-admin without a broker connection cannot arm."""
        r = client.post(
            "/api/mt5/auto-trade", headers=GOOD,
            json={"enabled": True, "confirm": "ENABLE"},
        )
        assert r.status_code == 428

    def test_connect_any_user_d037(self, client):
        """D-037: any authenticated user connects THEIR OWN broker account
        (mock demo plane accepts entered credentials and binds the user)."""
        r = client.post(
            "/api/mt5/connect", headers=GOOD,
            json={"server": "Exness-MT5Trial", "login": "123", "password": "x"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "connected"
        assert body["login"] == "123"
        # per-user status carries the broker block
        r = client.get("/api/mt5/status", headers=GOOD)
        assert r.status_code == 200
        assert r.json()["broker"]["status"] == "connected"
        assert r.json()["broker"]["login"] == "123"
        # a DIFFERENT user is NOT connected (per-user isolation)
        r = client.get("/api/mt5/status", headers=ADMIN_H)
        assert r.json()["broker"]["status"] == "disconnected"

    def test_disconnect_connect_cycle_admin(self, client):
        r = client.post("/api/mt5/disconnect", headers=ADMIN_H)
        assert r.status_code == 200
        assert r.json()["status"] == "disconnected"
        # D-037: the platform market plane keeps running for everyone else
        r = client.get("/api/candles", headers=GOOD, params={"tf": "M15"})
        assert r.status_code == 200
        # reconnect (mock accepts anything; platform already streaming so
        # the response carries the broker block + engine flag, no re-discovery)
        r = client.post(
            "/api/mt5/connect", headers=ADMIN_H,
            json={"server": "Exness-MT5Trial", "login": "123", "password": "pw"},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "connected"
        assert r.json()["login"] == "123"
        assert r.json()["engine_running"] is True


class TestCandlesRoute:
    def test_candles_shape(self, client):
        r = client.get("/api/candles", headers=GOOD,
                       params={"tf": "M15", "limit": 50})
        assert r.status_code == 200
        body = r.json()
        assert body["symbol"] == "XAUUSDm"
        assert body["tf"] == "M15"
        assert len(body["candles"]) == 50
        first = body["candles"][0]
        assert set(first) == {"t", "o", "h", "l", "c", "v"}
        # closed bars only, ascending
        ts = [c["t"] for c in body["candles"]]
        assert ts == sorted(ts)
        now_bucket = int(time.time()) // 900 * 900
        assert ts[-1] < now_bucket

    def test_candles_bad_tf(self, client):
        r = client.get("/api/candles", headers=GOOD, params={"tf": "M99"})
        assert r.status_code == 400

    def test_candles_all_tfs(self, client):
        for tf in ("M1", "M5", "M15", "M30", "H1", "H4", "D1"):
            r = client.get("/api/candles", headers=GOOD,
                           params={"tf": tf, "limit": 10})
            assert r.status_code == 200, tf
            assert len(r.json()["candles"]) == 10

    def test_positions_empty(self, client):
        r = client.get("/api/positions", headers=GOOD)
        assert r.status_code == 200
        assert r.json() == {"positions": []}


class TestConfigRoutes:
    def test_get_config_defaults(self, client):
        r = client.get("/api/config", headers=GOOD)
        assert r.status_code == 200
        body = r.json()
        assert body["auto_trade"] is False  # global kill switch OFF by default
        assert body["config"]["timeframe"] == "M1"
        assert body["config"]["rr"] == 0.9
        assert body["config"]["magic"] == 234000

    def test_put_config_valid(self, client):
        r = client.get("/api/config", headers=GOOD)
        cfg = r.json()["config"]
        cfg["min_atr"] = 1.2
        r = client.put("/api/config", headers=ADMIN_H, json=cfg)
        assert r.status_code == 200
        assert r.json()["config"]["min_atr"] == 1.2
        # persisted
        r = client.get("/api/config", headers=GOOD)
        assert r.json()["config"]["min_atr"] == 1.2
        # restore default
        cfg["min_atr"] = 0.15
        client.put("/api/config", headers=ADMIN_H, json=cfg)

    def test_put_config_invalid_rejected(self, client):
        r = client.get("/api/config", headers=GOOD)
        cfg = r.json()["config"]
        cfg["rr"] = -1  # invalid
        r = client.put("/api/config", headers=ADMIN_H, json=cfg)
        assert r.status_code == 422
        cfg["rr"] = 2.0
        cfg["bogus_field"] = 1  # extra=forbid
        r = client.put("/api/config", headers=ADMIN_H, json=cfg)
        assert r.status_code == 422

    def test_put_config_requires_admin(self, client):
        r = client.get("/api/config", headers=GOOD)
        r = client.put("/api/config", headers=GOOD, json=r.json()["config"])
        assert r.status_code == 403

    def test_auto_trade_typed_confirmation(self, client):
        r = client.post("/api/config/auto-trade", headers=ADMIN_H,
                        json={"enabled": True})
        assert r.status_code == 400  # missing confirm: "ENABLE"
        r = client.post("/api/config/auto-trade", headers=ADMIN_H,
                        json={"enabled": True, "confirm": "ENABLE"})
        assert r.status_code == 200 and r.json()["auto_trade"] is True
        # disable needs no confirmation
        r = client.post("/api/config/auto-trade", headers=ADMIN_H,
                        json={"enabled": False})
        assert r.status_code == 200 and r.json()["auto_trade"] is False


class TestSignalsRoutes:
    def test_list_empty_then_shape(self, client):
        r = client.get("/api/signals", headers=GOOD)
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == len(body["signals"])

    def test_get_missing_signal_404(self, client):
        r = client.get("/api/signals/00000000-0000-0000-0000-000000000000",
                       headers=GOOD)
        assert r.status_code == 404


class TestStatsRoute:
    def test_stats_shape(self, client):
        r = client.get("/api/stats", headers=GOOD)
        assert r.status_code == 200
        body = r.json()
        for key in ("win_rate", "expectancy", "profit_factor",
                    "max_drawdown_r", "by_session", "total_signals"):
            assert key in body


class TestWebSocket:
    def test_ws_rejects_missing_token(self, client):
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws"):
                pass

    def test_ws_market_stream(self, client):
        """AC (mock): the chart streams — ticks + bar events over /ws."""
        with client.websocket_connect(
            "/ws?token=good-token"
        ) as ws:
            ws.send_text(json.dumps({
                "type": "subscribe", "channel": "market",
                "symbol": "XAUUSDm", "tf": "M15",
            }))
            got_subscribed = got_tick = got_bar = False
            deadline = time.time() + 20
            while time.time() < deadline and not (got_tick and got_bar):
                msg = json.loads(ws.receive_text())
                if msg["type"] == "subscribed":
                    got_subscribed = True
                elif msg["type"] == "tick":
                    got_tick = True
                    assert "bid" in msg and "ask" in msg and "ts" in msg
                elif msg["type"] in ("bar_open", "bar_update", "bar_close"):
                    got_bar = True
                    assert msg["tf"] == "M15"
                    assert set(msg["candle"]) == {"t", "o", "h", "l", "c", "v"}
            assert got_subscribed and got_tick and got_bar

    def test_ws_global_events_reach_all_clients(self, client):
        """mt5_status broadcast goes to clients without a market subscription."""
        with client.websocket_connect("/ws?token=good-token") as ws:
            ws.send_text(json.dumps({"type": "ping"}))
            msg = json.loads(ws.receive_text())
            assert msg["type"] == "heartbeat"
            # trigger a status broadcast via admin disconnect (D-037: the
            # platform stays connected; the broker block goes disconnected)
            client.post("/api/mt5/disconnect", headers=ADMIN_H)
            deadline = time.time() + 10
            got_status = False
            while time.time() < deadline:
                msg = json.loads(ws.receive_text())
                if msg["type"] == "mt5_status":
                    assert msg["status"] == "connected"
                    assert msg["broker"]["status"] == "disconnected"
                    got_status = True
                    break
            assert got_status
            client.post("/api/mt5/connect", headers=ADMIN_H,
                        json={"server": "s", "login": "1", "password": "x"})

    def test_ws_bad_token_rejected(self, client):
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws?token=garbage"):
                pass
