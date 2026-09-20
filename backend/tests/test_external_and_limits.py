"""Phase 4 tests — external free data APIs (fail-soft) + rate limits."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.services.external import ExternalMarketService


class _FakeResponse:
    def __init__(self, payload: dict, status: int = 200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict:
        return self._payload


def _url(url: str, params: dict | None) -> str:
    if not params:
        return url
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    return f"{url}?{qs}"


class _FakeClient:
    def __init__(self, routes: dict[str, dict]):
        self._routes = routes
        self.calls: list[str] = []

    async def get(self, url: str, params=None, timeout=None):
        full = _url(url, params)
        self.calls.append(full)
        for prefix, payload in self._routes.items():
            if full.startswith(prefix):
                return _FakeResponse(payload)
        raise RuntimeError("network down")


def _service(client: _FakeClient) -> ExternalMarketService:
    return ExternalMarketService(http_factory=lambda: _awaitable(client))


async def _awaitable(client: _FakeClient) -> _FakeClient:
    await asyncio.sleep(0)
    return client


BINANCE = {
    "lastPrice": "2651.23",
    "priceChangePercent": "0.42",
    "highPrice": "2660.00",
    "lowPrice": "2640.10",
    "closeTime": 1737000000000,
}
ECB_EUR = {"rates": {"USD": 1.0891}, "date": "2025-01-16"}
ECB_USD = {
    "rates": {
        "EUR": 0.918, "GBP": 0.821, "JPY": 156.4,
        "CAD": 1.438, "SEK": 10.97, "CHF": 0.911,
    },
    "date": "2025-01-16",
}


async def test_snapshot_aggregates_all_providers() -> None:
    client = _FakeClient(
        {
            # D-032: the PAXG fetch tries data-api.binance.vision FIRST
            "https://data-api.binance.vision/api/v3/ticker/24hr": BINANCE,
            "https://api.frankfurter.dev/v1/latest?base=EUR": ECB_EUR,
            "https://api.frankfurter.dev/v1/latest?base=USD": ECB_USD,
        }
    )
    snap = await _service(client).snapshot()
    assert snap["gold_reference"]["ok"] is True
    assert snap["gold_reference"]["price"] == pytest.approx(2651.23)
    assert snap["gold_reference"]["provider"] == "binance"
    assert snap["eur_usd"]["ok"] is True
    assert snap["eur_usd"]["rate"] == pytest.approx(1.0891)
    assert snap["usd_strength"]["ok"] is True
    assert snap["usd_strength"]["value"] > 0
    # every provider contacted exactly once (cached on top)
    assert len(client.calls) == 3


async def test_snapshot_fails_soft_when_provider_down() -> None:
    """C6: one dead API -> null field with ok:false, snapshot still valid."""
    client = _FakeClient(
        {"https://api.frankfurter.dev": ECB_EUR}
    )
    service = _service(client)  # ONE instance -> cache applies
    snap = await service.snapshot()
    assert snap["gold_reference"]["ok"] is False  # binance unreachable
    assert snap["eur_usd"]["ok"] is True  # frankfurter fine
    # second call within TTL hits the cache (no extra network attempts)
    before = len(client.calls)
    snap2 = await service.snapshot()
    assert len(client.calls) == before
    snap.pop("ts"), snap2.pop("ts")  # timestamps legitimately differ
    assert snap2 == snap


async def test_snapshot_all_down() -> None:
    client = _FakeClient({})
    snap = await _service(client).snapshot()
    assert snap["gold_reference"]["ok"] is False
    assert snap["eur_usd"]["ok"] is False
    assert snap["usd_strength"]["ok"] is False
    assert "ts" in snap  # still a valid payload


# ------------------------------------------------------------- rate limiting


def test_global_rate_limit_429() -> None:
    """SPEC §13: slowapi limits kick in with 429 + JSON detail on the real app."""
    from app.main import app as real_app

    with TestClient(real_app) as c:  # context manager runs the lifespan
        codes = []
        for _ in range(300):
            r = c.get("/api/health", headers={"X-Forwarded-For": "8.8.8.8"})
            codes.append(r.status_code)
            if r.status_code == 429:
                break
        assert 429 in codes, "rate limit never fired within 300 requests"
        assert codes[-1] == 429


async def test_trading_order_budget() -> None:
    """Money-moving endpoints: tight 30/min budget returns False on #31."""
    from app.api.routes_trading import _budget

    app = FastAPI()

    async def receive():
        return {}

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": [(b"x-forwarded-for", b"9.9.9.9")],
        "query_string": b"",
        "client": ("9.9.9.9", 1234),
        "app": app,
    }
    request = Request(scope, receive)

    blocked_at = None
    for i in range(35):
        if not await _budget(request, "order", 30):
            blocked_at = i
            break
    assert blocked_at == 30  # the 31st request (index 30) got blocked
