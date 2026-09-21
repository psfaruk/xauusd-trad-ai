"""COT institutional positioning service (D-044).

Weekly CFTC Commitments-of-Traders data for COMEX gold futures — the
closest public view of WHERE THE BIG INSTITUTIONS stand:

- Large speculators (non-commercial: hedge funds / leveraged funds)
- Commercial hedgers (banks / producers — the "smart money" side of the
  gold market)
- Small traders (non-reportable)

Source: publicreporting.cftc.gov Socrata API (free, no key, no auth).
Cached 6 hours (the report updates once per week — Friday 15:30 ET).
Every failure degrades to None (C6 spirit: never block trading/panels).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger("xauusd.cot")

CACHE_TTL = 6 * 3600
GOLD_MARKET = "GOLD - COMMODITY EXCHANGE INC."
BASE_URL = (
    "https://publicreporting.cftc.gov/resource/6dca-aqww.json"
    "?$select=market_and_exchange_names,report_date_as_yyyy_mm_dd,"
    "open_interest_all,noncomm_positions_long_all,noncomm_positions_short_all,"
    "comm_positions_long_all,comm_positions_short_all,"
    "nonrept_positions_long_all,nonrept_positions_short_all"
    "&$where=market_and_exchange_names = 'GOLD - COMMODITY EXCHANGE INC.'"
    "&$order=report_date_as_yyyy_mm_dd DESC&$limit=60"
)


class CotService:
    """Weekly gold COT snapshot with 60-week percentile context."""

    def __init__(
        self,
        http_factory: Callable[[], Awaitable] | None = None,
        timeout_s: float = 8.0,
    ) -> None:
        self._http_factory = http_factory
        self._timeout_s = timeout_s
        self._cache: tuple[float, dict | None] | None = None

    async def snapshot(self) -> dict[str, Any] | None:
        """{report_date, open_interest, large_spec{...}, commercial{...},
        small{...}, net_spec, net_spec_change, percentile, bias} or None."""
        now = time_now()
        if self._cache is not None and now - self._cache[0] < CACHE_TTL:
            return self._cache[1]
        data = await self._fetch()
        self._cache = (now, data)
        return data

    async def _fetch(self) -> dict[str, Any] | None:
        if self._http_factory is None:
            return None
        try:
            client = await self._http_factory()
            resp = await client.get(BASE_URL, timeout=self._timeout_s)
            resp.raise_for_status()
            rows = resp.json()
        except Exception as exc:  # noqa: BLE001 — degrade gracefully
            logger.warning("CFTC COT unavailable: %s", exc)
            return None
        if not isinstance(rows, list) or not rows:
            return None
        try:
            return self._build(rows)
        except Exception as exc:  # noqa: BLE001 — schema drift: no panel, no crash
            logger.warning("CFTC COT parse failed: %s", exc)
            return None

    # ------------------------------------------------------------------ parse
    @staticmethod
    def _build(rows: list[dict]) -> dict[str, Any]:
        # rows are ordered newest-first by the query
        parsed = []
        for r in rows:
            date = _parse_date(r.get("report_date_as_yyyy_mm_dd"))
            if date is None:
                continue
            parsed.append(
                {
                    "date": date,
                    "open_interest": _i(r.get("open_interest_all")),
                    "spec_long": _i(r.get("noncomm_positions_long_all")),
                    "spec_short": _i(r.get("noncomm_positions_short_all")),
                    "comm_long": _i(r.get("comm_positions_long_all")),
                    "comm_short": _i(r.get("comm_positions_short_all")),
                    "small_long": _i(r.get("nonrept_positions_long_all")),
                    "small_short": _i(r.get("nonrept_positions_short_all")),
                }
            )
        if not parsed:
            return {}
        latest = parsed[0]
        prev = parsed[1] if len(parsed) > 1 else None
        net = latest["spec_long"] - latest["spec_short"]
        net_prev = (
            prev["spec_long"] - prev["spec_short"] if prev is not None else net
        )
        # 60-week percentile of net speculative positioning
        nets = [p["spec_long"] - p["spec_short"] for p in parsed]
        rank = sum(1 for n in nets if n <= net)
        percentile = round(100.0 * rank / max(len(nets), 1), 1)
        if percentile >= 85:
            bias = "extreme_long"  # crowded long — squeeze risk
        elif percentile <= 15:
            bias = "extreme_short"  # crowded short — squeeze fuel
        elif net > net_prev:
            bias = "accumulating_long"
        elif net < net_prev:
            bias = "accumulating_short"
        else:
            bias = "neutral"
        return {
            "report_date": latest["date"].date().isoformat(),
            "open_interest": latest["open_interest"],
            "large_speculators": {
                "long": latest["spec_long"],
                "short": latest["spec_short"],
                "net": net,
                "net_change": net - net_prev,
            },
            "commercial_hedgers": {
                "long": latest["comm_long"],
                "short": latest["comm_short"],
                "net": latest["comm_long"] - latest["comm_short"],
            },
            "small_traders": {
                "long": latest["small_long"],
                "short": latest["small_short"],
                "net": latest["small_long"] - latest["small_short"],
            },
            "net_percentile_52w": percentile,
            "bias": bias,
            "note": _NOTE[bias],
        }


_NOTE = {
    "extreme_long": (
        "Large speculators are extremely long vs the past year — crowded "
        "trade, watch for long squeezes"
    ),
    "extreme_short": (
        "Large speculators are extremely short vs the past year — short "
        "squeeze fuel, reversals often start from here"
    ),
    "accumulating_long": "Institutions added longs week-over-week",
    "accumulating_short": "Institutions added shorts week-over-week",
    "neutral": "Positioning near the yearly average",
}


def _i(v: Any) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _parse_date(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def time_now() -> float:
    import time

    return time.monotonic()
