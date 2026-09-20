"""External free market-data aggregation (user req #6, C6 graceful degrade).

Aggregates FREE, key-less public APIs as reference data alongside the MT5
feed so users can cross-check the platform's XAUUSD pricing:

- Frankfurter.app  — ECB reference rates: USD-strength context (DXY proxy
  via EUR/USD), updated daily. Free, no key, no rate limit issues.
- Binance public API — PAXG/USDT (tokenized gold, 1 PAXG = 1 fine troy oz)
  spot + 24h stats. Free, no key. Close proxy for spot gold.

Every provider fails soft (C6): an unavailable API degrades to a `null`
field with an `ok: false` marker — never a 5xx, never blocking the UI.
Responses are cached 60s to respect the free tiers.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

logger = logging.getLogger("xauusd.external")

CACHE_TTL_S = 60.0


class ExternalMarketService:
    def __init__(self, http_factory: Callable[[], Awaitable] | None = None) -> None:
        self._http_factory = http_factory
        self._cache: dict[str, tuple[float, dict]] = {}

    async def _cached(self, key: str, fetch) -> dict:
        now = datetime.now(tz=UTC).timestamp()
        hit = self._cache.get(key)
        if hit and now - hit[0] < CACHE_TTL_S:
            return hit[1]
        try:
            data = await fetch()
        except Exception as exc:  # noqa: BLE001 — C6 degrade
            logger.debug("external %s unavailable: %s", key, exc)
            data = {"ok": False, "error": "unavailable"}
        self._cache[key] = (now, data)
        return data

    # ------------------------------------------------------------- providers

    async def _fetch_paxg(self) -> dict:
        client = await self._http_factory()
        resp = await client.get(
            "https://api.binance.com/api/v3/ticker/24hr",
            params={"symbol": "PAXGUSDT"},
            timeout=5.0,
        )
        resp.raise_for_status()
        d = resp.json()
        return {
            "ok": True,
            "provider": "binance",
            "symbol": "PAXGUSDT",
            "price": float(d["lastPrice"]),
            "change_24h_pct": float(d["priceChangePercent"]),
            "high_24h": float(d["highPrice"]),
            "low_24h": float(d["lowPrice"]),
            "quote_time": d.get("closeTime"),
        }

    async def _fetch_eurusd(self) -> dict:
        client = await self._http_factory()
        resp = await client.get(
            "https://api.frankfurter.app/latest",
            params={"from": "EUR", "to": "USD"},
            timeout=5.0,
        )
        resp.raise_for_status()
        d = resp.json()
        return {
            "ok": True,
            "provider": "frankfurter (ECB)",
            "pair": "EUR/USD",
            "rate": float(d["rates"]["USD"]),
            "date": d.get("date"),
        }

    async def _fetch_dxy_proxy(self) -> dict:
        """USD index proxy from ECB majors (free Frankfurter series)."""
        client = await self._http_factory()
        resp = await client.get(
            "https://api.frankfurter.app/latest",
            params={"from": "USD", "to": "EUR,GBP,JPY,CAD,SEK,CHF"},
            timeout=5.0,
        )
        resp.raise_for_status()
        rates = resp.json()["rates"]
        # Inverse of the DXY basket legs (approximate: DXY uses FX quoting
        # conventions; we expose the geometric-ish mean of USD strength)
        legs = {
            "EUR": 1.0 / float(rates["EUR"]),
            "GBP": 1.0 / float(rates["GBP"]),
            "JPY": float(rates["JPY"]),
            "CAD": float(rates["CAD"]),
            "SEK": float(rates["SEK"]),
            "CHF": float(rates["CHF"]),
        }
        strength = sum(legs.values()) / len(legs)
        return {
            "ok": True,
            "provider": "frankfurter (ECB)",
            "name": "USD strength (DXY proxy, 6-currency mean)",
            "value": round(strength, 5),
            "legs": {k: round(v, 5) for k, v in legs.items()},
            "date": resp.json().get("date"),
        }

    # ---------------------------------------------------------------- public

    async def snapshot(self) -> dict:
        """Full external reference snapshot — every field fails soft (C6)."""
        paxg = await self._cached("paxg", self._fetch_paxg)
        eurusd = await self._cached("eurusd", self._fetch_eurusd)
        dxy = await self._cached("dxy", self._fetch_dxy_proxy)
        return {
            "ts": datetime.now(tz=UTC).isoformat(),
            "gold_reference": paxg,
            "eur_usd": eurusd,
            "usd_strength": dxy,
        }
