"""AnalysisService (D-042/D-043) — cached multi-TF ICT/SMC snapshots.

Backs GET /api/analysis: pulls closed bars for M1..D1 through
the live DataSource, runs the analysis package (structure, order
    blocks, FVG, liquidity, supply/demand, whales, classic indicators) per
TF and aggregates the MTF bias. Results are cached ~20s per symbol —
the analysis only changes on bar close, so the chart overlay + the
live-analysis strip poll cheaply.

D-043: every snapshot also carries `drawings` — the professional
auto-drawing layer (hlines, trendlines, fib+OTE, notes, entry setups)
built from the same frames, recomputed on each cache miss so the chart
keeps re-drawing as the market moves.

D-044: the snapshot is a full MARKET INTELLIGENCE packet —
  flow  — order-flow statistics (USD traded value, delta, tape
          velocity, whale entry zones)
  news  — upcoming high-impact USD economic events
  cot   — weekly CFTC institutional positioning (gold)
"""

from __future__ import annotations

import logging
import time as time_mod
from datetime import UTC, datetime
from typing import Any

from app.analysis.context import ANALYSIS_TFS, analyze_frame, mtf_bias
from app.analysis.drawings import build_drawings
from app.analysis.orderflow import flow_stats

logger = logging.getLogger("xauusd.analysis")

CACHE_TTL_S = 20.0
# D-052 — every chart timeframe (M1..D1) gets a snapshot + its own
# drawing set; M30/D1 carry a bit less depth (bars cost fetch time).
BARS_PER_TF = {"M1": 260, "M5": 200, "M15": 160, "M30": 140, "H1": 140,
                "H4": 120, "D1": 120}


class AnalysisService:
    def __init__(
        self,
        ttl_s: float = CACHE_TTL_S,
        news_service: Any | None = None,
        cot_service: Any | None = None,
    ) -> None:
        self._ttl = float(ttl_s)
        self._news = news_service
        self._cot = cot_service
        self._cache: dict[str, tuple[float, dict]] = {}

    def invalidate(self, symbol: str | None = None) -> None:
        if symbol is None:
            self._cache.clear()
        else:
            self._cache.pop(symbol, None)

    async def get(
        self,
        source: Any,
        symbol: str,
        recent_signals: list[dict] | None = None,
    ) -> dict:
        """Snapshot for one symbol (cached). Never raises — degrades to
        an error payload so the chart keeps working when the feed blips."""
        hit = self._cache.get(symbol)
        now = time_mod.monotonic()
        if hit is not None and now - hit[0] < self._ttl:
            return hit[1]
        snapshots: dict[str, dict] = {}
        frames: dict[str, Any] = {}
        errors: list[str] = []
        for tf in ANALYSIS_TFS:
            try:
                df = await source.get_rates(symbol, tf, BARS_PER_TF.get(tf, 160))
            except Exception as exc:  # noqa: BLE001 — one TF failing must not kill all
                errors.append(f"{tf}: {type(exc).__name__}")
                continue
            if df is None or len(df) < 10:
                errors.append(f"{tf}: no data")
                continue
            frames[tf] = df
            snapshots[tf] = analyze_frame(df)
        price = await self._live_price(source, symbol, frames)
        payload = {
            "symbol": symbol,
            "updated_at": time_mod.time(),
            "per_tf": snapshots,
            "mtf": mtf_bias(snapshots),
            "errors": errors,
        }
        # D-052 — professional auto-drawings PER TIMEFRAME: every view
        # (M1/M5/M15/H1/H4) gets its own drawing set anchored on its OWN
        # recent 80–150 candles, so switching timeframes never destroys
        # the overlay — each TF simply re-draws its own marks from the
        # same 20s-cached snapshot (user directive: "টাইম ফ্রম পরিবর্তন
        # করলেও ড্রয়িং নষ্ট হবে না").
        if price > 0:
            payload["drawings_by_tf"] = {
                tf: build_drawings(frames, snapshots, price, recent_signals, tf=tf)
                for tf in frames
            }
        else:
            payload["drawings_by_tf"] = {}
        # backward-compatible default view (M1 marks) for older clients
        payload["drawings"] = payload["drawings_by_tf"].get("M1", [])
        # D-044 — order-flow statistics off the M1 tape
        payload["flow"] = flow_stats(frames["M1"]) if frames.get("M1") is not None else {}
        # D-047 — time-at-price profile: where the market SPENT TIME becomes
        # marked S/R levels (POC / value area / strong nodes)
        try:
            from app.analysis.tpo import tpo_profile

            payload["tpo"] = (
                tpo_profile(frames["M1"])
                if frames.get("M1") is not None else
                {"poc": None, "va_lo": None, "va_hi": None,
                 "va_minutes": 0.0, "total_minutes": 0.0, "levels": []}
            )
        except Exception:  # noqa: BLE001 — TPO must never break the snapshot
            payload["tpo"] = {"poc": None, "va_lo": None, "va_hi": None,
                              "va_minutes": 0.0, "total_minutes": 0.0, "levels": []}
        # D-048 — unified POI zone list (the zone-retest trigger's source):
        # supply/demand bases, order blocks, FVGs, TPO levels and PDH/PDL
        # ranked by hard-coded quality — chart overlay + AI panel + engine
        try:
            from app.analysis.poi import poi_zones

            payload["poi"] = {
                "zones": (
                    poi_zones(frames["M1"],
                              {tf: f for tf, f in frames.items() if tf != "M1"},
                              max_zones=8)
                    if frames.get("M1") is not None else []
                ),
            }
        except Exception:  # noqa: BLE001 — POI must never break the snapshot
            payload["poi"] = {"zones": []}
        # D-044 — news events + weekly institutional positioning
        payload["news"] = await self._news_block()
        payload["cot"] = await self._cot_block()
        if snapshots:
            self._cache[symbol] = (now, payload)
        return payload

    async def _news_block(self) -> dict:
        if self._news is None:
            return {"events": []}
        try:
            events = await self._news.upcoming(hours=48)
        except Exception:  # noqa: BLE001 — panel must never break the snapshot
            events = None
        if not events:
            return {"events": [], "available": bool(events)}
        now = datetime.now(tz=UTC)
        blackout = False
        for e in events:
            try:
                when = datetime.fromisoformat(e["time"])
                mins = abs((when - now).total_seconds()) / 60.0
                if e.get("impact") == "high" and mins <= 30:
                    blackout = True
                    break
            except (ValueError, KeyError):
                continue
        return {"events": events, "blackout_now": blackout, "available": True}

    async def _cot_block(self) -> dict:
        if self._cot is None:
            return {}
        try:
            snap = await self._cot.snapshot()
        except Exception:  # noqa: BLE001
            snap = None
        return snap or {}

    async def _live_price(
        self, source: Any, symbol: str, frames: dict[str, Any]
    ) -> float:
        """Best live price: real tick mid, else last closed M1 close."""
        try:
            tick = getattr(source, "get_tick", None)
            if tick is not None:
                t = tick(symbol)
                if hasattr(t, "__await__"):
                    t = await t
                if t is not None:
                    mid = (float(t.bid) + float(t.ask)) / 2.0
                    if mid > 0:
                        return mid
        except Exception:  # noqa: BLE001 — price fallback below
            pass
        m1 = frames.get("M1")
        try:
            if m1 is not None and len(m1):
                return float(m1["c"].iloc[-1])
        except Exception:  # noqa: BLE001
            pass
        return 0.0
