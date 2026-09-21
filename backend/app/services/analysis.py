"""AnalysisService (D-042) — cached multi-TF ICT/SMC snapshots.

Backs GET /api/analysis: pulls closed bars for M1/M5/M15/H1/H4 through
the live DataSource, runs the analysis package (structure, order
blocks, FVG, liquidity, supply/demand, whales, classic indicators) per
TF and aggregates the MTF bias. Results are cached ~20s per symbol —
the analysis only changes on bar close, so the chart overlay + the
live-analysis strip poll cheaply.
"""

from __future__ import annotations

import logging
import time as time_mod
from typing import Any

from app.analysis.context import ANALYSIS_TFS, analyze_frame, mtf_bias

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

    async def get(self, source: Any, symbol: str) -> dict:
        """Snapshot for one symbol (cached). Never raises — degrades to
        an error payload so the chart keeps working when the feed blips."""
        hit = self._cache.get(symbol)
        now = time_mod.monotonic()
        if hit is not None and now - hit[0] < self._ttl:
            return hit[1]
        snapshots: dict[str, dict] = {}
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
            snapshots[tf] = analyze_frame(df)
        payload = {
            "symbol": symbol,
            "updated_at": time_mod.time(),
            "per_tf": snapshots,
            "mtf": mtf_bias(snapshots),
            "errors": errors,
        }
        if snapshots:
            self._cache[symbol] = (now, payload)
        return payload
