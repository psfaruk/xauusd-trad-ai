"""AnalysisService (D-042/D-043) — cached multi-TF ICT/SMC snapshots.

Backs GET /api/analysis: pulls closed bars for M1/M5/M15/H1/H4 through
the live DataSource, runs the analysis package (structure, order
    blocks, FVG, liquidity, supply/demand, whales, classic indicators) per
TF and aggregates the MTF bias. Results are cached ~20s per symbol —
the analysis only changes on bar close, so the chart overlay + the
live-analysis strip poll cheaply.

D-043: every snapshot also carries `drawings` — the professional
auto-drawing layer (hlines, trendlines, fib+OTE, notes, entry setups)
built from the same frames, recomputed on each cache miss so the chart
keeps re-drawing as the market moves.
"""

from __future__ import annotations

import logging
import time as time_mod
from typing import Any

from app.analysis.context import ANALYSIS_TFS, analyze_frame, mtf_bias
from app.analysis.drawings import build_drawings

logger = logging.getLogger("xauusd.analysis")

CACHE_TTL_S = 20.0
BARS_PER_TF = {"M1": 260, "M5": 200, "M15": 160, "H1": 140, "H4": 120}


class AnalysisService:
    def __init__(self, ttl_s: float = CACHE_TTL_S) -> None:
        self._ttl = float(ttl_s)
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
        # D-043 — professional auto-drawings (hlines / trendlines / fib /
        # notes / entry setups), rebuilt on every snapshot
        if frames.get("M1") is not None and price > 0:
            payload["drawings"] = build_drawings(
                frames, snapshots, price, recent_signals
            )
        else:
            payload["drawings"] = []
        if snapshots:
            self._cache[symbol] = (now, payload)
        return payload

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
