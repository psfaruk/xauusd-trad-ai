"""Economic-calendar news service (SPEC §8.2 rule 6; D-044 multi-source).

Sources (tried in order, merged + deduped):
1. ForexFactory weekly JSON (FREE, no key) — thisweek + nextweek
2. FMP economic_calendar (when FMP_API_KEY is set)

Cached 10 minutes. Any failure of a source is skipped; when ALL sources
fail the news check is SKIPPED with a warning (C6 graceful degrade) —
never blocks trading signals silently.

Interfaces:
- usd_high_impact(start, end)   — engine blackout gate (rule 6)
- upcoming(hours, impacts)      — frontend "Economic Events" panel
- next_usd_high_impact(now)     — human-readable distance (trace)
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

logger = logging.getLogger("xauusd.news")

CACHE_TTL = 10 * 60  # 10 min
FF_URLS = (
    "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
    "https://nfs.faireconomy.media/ff_calendar_nextweek.json",
)


class NewsService:
    def __init__(
        self,
        http_factory: Callable[[], Awaitable] | None = None,
        api_key: str | None = None,
        timeout_s: float = 6.0,
    ) -> None:
        # http_factory() -> object with .get(url, params) (httpx.AsyncClient)
        self._http_factory = http_factory
        self._api_key = api_key
        self._timeout_s = timeout_s
        self._cache: tuple[datetime, list[dict] | None] | None = None

    # ------------------------------------------------------------ internals
    async def _fetch_all(self) -> list[dict] | None:
        """Merged high+medium impact calendar from every healthy source."""
        now = datetime.now(tz=UTC)
        if self._cache is not None and (now - self._cache[0]).total_seconds() < CACHE_TTL:
            return self._cache[1]
        events: list[dict] = []
        ff = await self._fetch_forexfactory()
        if ff is not None:
            events.extend(ff)
        fmp = await self._fetch_fmp(now - timedelta(hours=12), now + timedelta(days=10))
        if fmp is not None:
            events.extend(fmp)
        if not events and ff is None and fmp is None:
            result = None  # every source failed
        else:
            # dedupe by (title, time) — FF first (richer impact tagging)
            seen: set[tuple[str, datetime]] = set()
            result = []
            for e in sorted(events, key=lambda x: x["time"]):
                key = (e["title"].strip().lower(), e["time"])
                if key in seen:
                    continue
                seen.add(key)
                result.append(e)
        self._cache = (now, result)
        return result

    async def _fetch_forexfactory(self) -> list[dict] | None:
        """ForexFactory weekly JSON — free, no key (D-044)."""
        if self._http_factory is None:
            return None
        out: list[dict] = []
        ok = False
        try:
            client = await self._http_factory()
            for url in FF_URLS:
                resp = await client.get(url, timeout=self._timeout_s)
                if resp.status_code != 200:
                    continue
                ok = True
                for e in resp.json():
                    if str(e.get("country", "")).upper() not in ("USD", "ALL"):
                        continue
                    impact = str(e.get("impact", "")).strip().lower()
                    if impact not in ("high", "medium"):
                        continue
                    when = _parse(e.get("date"))
                    if when is None:
                        continue
                    out.append(
                        {
                            "time": when,
                            "impact": "high" if impact == "high" else "medium",
                            "country": "USD",
                            "title": str(e.get("title", "")).strip(),
                            "forecast": e.get("forecast") or None,
                            "previous": e.get("previous") or None,
                            "source": "calendar",
                        }
                    )
        except Exception as exc:  # noqa: BLE001 — degrade to next source
            logger.warning("ForexFactory calendar unavailable: %s", exc)
            return None
        return out if ok else None

    async def _fetch_fmp(self, start: datetime, end: datetime) -> list[dict] | None:
        """FMP fallback (needs key) — high-impact USD events."""
        if not self._api_key or self._http_factory is None:
            return None
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
            return None
        return [
            {
                "time": when,
                "impact": "high",
                "country": "USD",
                "title": str(e.get("event", "")).strip(),
                "forecast": e.get("estimate") or None,
                "previous": e.get("previous") or None,
                "source": "calendar",
            }
            for e in data
            if e.get("country") == "US"
            and str(e.get("impact", "")).lower() == "high"
            and (when := _parse(e.get("date"))) is not None
        ]

    # ------------------------------------------------------------- public API
    async def usd_high_impact(
        self, start: datetime, end: datetime
    ) -> list[dict] | None:
        """High-impact USD events in [start, end]; None = sources unavailable."""
        events = await self._fetch_all()
        if events is None:
            return None
        return [
            e
            for e in events
            if e["impact"] == "high" and start <= e["time"] <= end
        ]

    async def upcoming(
        self, hours: float = 48, impacts: tuple[str, ...] = ("high", "medium")
    ) -> list[dict] | None:
        """Next USD events for the frontend panel (ISO times, sorted)."""
        now = datetime.now(tz=UTC)
        events = await self._fetch_all()
        if events is None:
            return None
        out = [
            {
                "time": e["time"].isoformat(),
                "impact": e["impact"],
                "title": e["title"],
                "forecast": e.get("forecast"),
                "previous": e.get("previous"),
            }
            for e in events
            if e["time"] >= now - timedelta(minutes=30)
            and e["time"] <= now + timedelta(hours=hours)
            and e["impact"] in impacts
        ]
        return out[:12]

    async def next_usd_high_impact(self, now: datetime) -> str | None:
        """Human-readable distance to the next high-impact USD event."""
        events = await self._fetch_all()
        if not events:
            return None
        upcoming = [e for e in events if e["time"] > now and e["impact"] == "high"]
        if not upcoming:
            return None
        delta = min((e["time"] - now).total_seconds() for e in upcoming)
        return f"{delta / 3600.0:.1f}h"


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
