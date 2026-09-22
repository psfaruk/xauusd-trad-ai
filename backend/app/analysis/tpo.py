"""TPO — Time-at-Price levels (D-047, user directive).

"একটি প্রাইস লেভেল এ মার্কেট কতক্ষণ স্টে করলে, এই ধরনের লেভেল চিহ্নিত হয়"
— when the market SPENDS TIME at a price level, that level is marked.

Classic market-profile logic rebuilt from M1 bars: every bar distributes its
minute across the price buckets its high-low range spans (overlap-weighted,
so a huge bar does not overweight a far tick). Price buckets where the
market accumulated the most minutes become S/R levels:

- POC            the single most-traded (longest-held) price — magnet;
- value area     contiguous buckets around the POC holding ~70% of time;
- strong levels  local maxima whose minutes exceed a floor — each becomes a
                 marked support (below price) or resistance (above price)
                 with a normalized strength for the confluence engine.

Everything is derived from REAL closed M1 bars the engine already holds —
no extra data source, no look-ahead (the forming bar is excluded upstream).
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

#: minutes a bucket must accumulate (inside the lookback) to be a "strong"
#: level — tuned for XAUUSD M1: ~15 min at one price over a day is a level
#: the market remembers.
LEVEL_MIN_MINUTES = 15.0
#: fraction of total lookback time the value area must cover
VALUE_AREA_TARGET = 0.70
#: default price-bucket size in price units (gold: $0.50 buckets)
DEFAULT_BUCKET = 0.50
#: how deep into history the profile reaches
DEFAULT_LOOKBACK_MIN = 1440  # 24h of M1 bars


def tpo_profile(
    df: pd.DataFrame,
    lookback_minutes: int = DEFAULT_LOOKBACK_MIN,
    bucket: float = DEFAULT_BUCKET,
    ref_price: float | None = None,
) -> dict[str, Any]:
    """Time-at-price profile over the last `lookback_minutes` M1 bars.

    Returns {poc, va_lo, va_hi, va_minutes, total_minutes, levels} where
    levels = [{price, minutes, side, strength}] sorted by minutes desc,
    side relative to `ref_price` (default: last close).
    """
    if df is None or len(df) < 5:
        return {"poc": None, "va_lo": None, "va_hi": None, "va_minutes": 0.0,
                "total_minutes": 0.0, "levels": []}
    tail = df.tail(int(lookback_minutes))
    hi = pd.to_numeric(tail["h"], errors="coerce").to_numpy(dtype=float)
    lo = pd.to_numeric(tail["l"], errors="coerce").to_numpy(dtype=float)
    close = pd.to_numeric(tail["c"], errors="coerce").to_numpy(dtype=float)
    ok = ~(~np.isfinite(hi) | ~np.isfinite(lo) | ~np.isfinite(close))
    hi, lo, close = hi[ok], lo[ok], close[ok]
    if len(hi) < 5:
        return {"poc": None, "va_lo": None, "va_hi": None, "va_minutes": 0.0,
                "total_minutes": 0.0, "levels": []}

    grid_lo = math.floor(float(lo.min()) / bucket) * bucket
    grid_hi = math.ceil(float(hi.max()) / bucket) * bucket
    n = int(round((grid_hi - grid_lo) / bucket)) + 1
    if n <= 1:
        # one flat bucket — degenerate market
        p = round(grid_lo, 2)
        mins = float(len(hi))
        return {"poc": p, "va_lo": p, "va_hi": p, "va_minutes": mins,
                "total_minutes": mins,
                "levels": [{"price": p, "minutes": mins,
                            "side": "support", "strength": 1.0}]}

    idx_lo = np.clip(((lo - grid_lo) / bucket).astype(int), 0, n - 1)
    idx_hi = np.clip(((hi - grid_lo) / bucket).astype(int), 0, n - 1)
    minutes = np.zeros(n, dtype=float)
    for k in range(len(hi)):
        a, b = int(idx_lo[k]), int(idx_hi[k])
        if b < a:
            a, b = b, a
        span = b - a + 1
        minutes[a:b + 1] += 1.0 / span  # overlap-weighted TPO minute

    total = float(minutes.sum())
    if total <= 0:
        return {"poc": None, "va_lo": None, "va_hi": None, "va_minutes": 0.0,
                "total_minutes": 0.0, "levels": []}

    poc_i = int(minutes.argmax())
    poc = round(grid_lo + poc_i * bucket, 2)

    # value area — grow around the POC until ~70% of time is covered
    va_sum = float(minutes[poc_i])
    lo_i = hi_i = poc_i
    while va_sum < VALUE_AREA_TARGET * total and (lo_i > 0 or hi_i < n - 1):
        below = minutes[lo_i - 1] if lo_i > 0 else -1.0
        above = minutes[hi_i + 1] if hi_i < n - 1 else -1.0
        if above >= below:
            hi_i += 1
            va_sum += minutes[hi_i]
        else:
            lo_i -= 1
            va_sum += minutes[lo_i]

    # strong levels: local maxima above the floor, merged with neighbors
    peak_min = max(LEVEL_MIN_MINUTES, 0.02 * total)
    peaks: list[int] = []
    for i in range(n):
        if minutes[i] < peak_min:
            continue
        left = minutes[i - 1] if i > 0 else 0.0
        right = minutes[i + 1] if i < n - 1 else 0.0
        if minutes[i] >= left and minutes[i] >= right:
            peaks.append(i)
    # keep peaks separated by >= 2 buckets (merge adjacent ties)
    merged: list[int] = []
    for i in peaks:
        if merged and i - merged[-1] <= 1:
            if minutes[i] > minutes[merged[-1]]:
                merged[-1] = i
        else:
            merged.append(i)

    ref = float(ref_price if ref_price is not None else close[-1])
    max_min = float(minutes.max()) or 1.0
    levels = []
    for i in merged:
        price = round(grid_lo + i * bucket, 2)
        m = float(minutes[i])
        levels.append({
            "price": price,
            "minutes": round(m, 1),
            "side": "support" if price <= ref else "resistance",
            "strength": round(min(1.0, m / max_min), 3),
        })
    levels.sort(key=lambda lv: lv["minutes"], reverse=True)

    return {
        "poc": poc,
        "va_lo": round(grid_lo + lo_i * bucket, 2),
        "va_hi": round(grid_lo + hi_i * bucket, 2),
        "va_minutes": round(va_sum, 1),
        "total_minutes": round(total, 1),
        "levels": levels[:8],
    }


def nearest_level(
    levels: list[dict[str, Any]], price: float,
    max_dist: float = 1.2,
) -> dict[str, Any] | None:
    """Nearest strong level within `max_dist` price units (ATR-scaled by
    the caller) — the engine's at-level probe."""
    best: dict[str, Any] | None = None
    best_d = max_dist
    for lv in levels or []:
        d = abs(float(lv["price"]) - price)
        if d <= best_d:
            best, best_d = lv, d
    return best
