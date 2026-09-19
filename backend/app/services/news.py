"""FMP economic-calendar news service (SPEC §8.2 rule 6, C6 graceful degrade).

Cached for 15 minutes. Any failure (no API key, timeout, HTTP error, parse
error) returns None — the caller then SKIPS the news check with a warning
instead of blocking trading signals.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

logger = logging.getLogger("xauusd.news")

CACHE_TTL = 15 * 60  # SPEC: cache 15 min


class NewsService:
    def __init__(
        self,
        http_factory: Callable[[], Awaitable] | None = None,
        api_key: str | None = None,
        timeout_s: float = 5.0,
    ) -> None:
        # http_factory() -> object with .get(url, params) (httpx.AsyncClient)
        self._http_factory = http_factory
        self._api_key = api_key
        self._timeout_s = timeout_s
        self._cache: dict[str, tuple[datetime, list[dict] | None]] = {}

    async def _fetch_window(
        self, start: datetime, end: datetime
    ) -> list[dict] | None:
        """High-impact USD events in [start, end]; None = unavailable."""
        if not self._api_key or self._http_factory is None:
            return None
        key = f"{start:%Y%m%d}-{end:%Y%m%d}"
        now = datetime.now(tz=start.tzinfo) if start.tzinfo else datetime.utcnow()
        cached = self._cache.get(key)
        if cached and (now - cached[0]).total_seconds() < CACHE_TTL:
            return cached[1]
        try:
            client = await self._http_factory()
            resp = await client.get(
                "https://financialmodelingprep.com/api/v3/economic_calendar",
                params={
                    "from": f"{start:%Y-%m-%d}",
                    "to": f"{end:%Y-%m-%d}",
                    "apikey": self._api_key,
                },
                timeout=self._timeout_s,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001 — C6: degrade gracefully
            logger.warning("FMP calendar unavailable: %s", exc)
            self._cache[key] = (now, None)
            return None

        events = [
            {
                "time": _parse(e.get("date")),
                "impact": e.get("impact", ""),
                "country": e.get("country", ""),
                "title": e.get("event", ""),
            }
            for e in data
            if e.get("country") == "US"
            and str(e.get("impact", "")).lower() == "high"
            and _parse(e.get("date")) is not None
        ]
        events = [e for e in events if start <= e["time"] <= end]
        self._cache[key] = (now, events)
        return events

    async def usd_high_impact(
        self, start: datetime, end: datetime
    ) -> list[dict] | None:
        return await self._fetch_window(start, end)

    async def next_usd_high_impact(self, now: datetime) -> str | None:
        """Human-readable distance to the next high-impact USD event (trace value)."""
        events = await self._fetch_window(now, now + timedelta(hours=24))
        if not events:
            return None
        upcoming = [e for e in events if e["time"] > now]
        if not upcoming:
            return None
        delta = min((e["time"] - now).total_seconds() for e in upcoming)
        hours = delta / 3600.0
        return f"{hours:.1f}h"


def _parse(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:

        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


class NullNewsService:
    """Always-unavailable service — news check is skipped with a warning."""

    async def usd_high_impact(
        self, start: datetime, end: datetime
    ) -> list[dict] | None:
        return None

    async def next_usd_high_impact(self, now: datetime) -> str | None:
        return None
