"""POI zone-retest trigger (D-048, user directive — "hard coding").

"Best POI ZONE, SUPPLY ZONE, DEMAND ZONE এই গুলো তে সিগন্যাল দিতে হবে,
মিস করা যাবে না" — when price RETURNS to a quality point-of-interest
zone and REJECTS, that IS the entry. The trigger does not wait for the
other strategies to agree (user directive: "সব গুলো স্ট্রাটেজি একই সময়
AGREE নাও থাকতে পারে") — the location + the rejection carry the setup,
everything else only shapes confidence.

Hard-coded retest rules (DEMAND / BUY — SUPPLY mirrors everything):

  1. ENTRY INTO THE ZONE — the trigger bar, or one of the
     `zone_retest_window` bars before it, traded INTO the band
     (low <= zone.hi);
  2. REJECTION — the trigger bar CLOSED back above the zone high;
  3. REJECTION QUALITY — lower wick >= 35% of the bar range, or the
     close sits in the top 40% of the range;
  4. ZONE INTACT — no close beyond the far edge since formation
     (broken zones are dropped upstream in poi_zones);
  5. QUALITY GATE — zone quality >= cfg.min_zone_quality with the H1
     trend; a counter-trend reversal (demand zone in an H1 downtrend)
     needs quality >= cfg.counter_trend_quality AND a strong rejection.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from app.engine.config import EngineConfig
from app.engine.indicators import atr as atr_calc
from app.engine.trace import Trace

#: rejection wick as a share of the bar range (hard gate)
REJECT_WICK_SHARE = 0.35
#: close position inside the bar range that alone counts as rejection
REJECT_BODY_POS = 0.60
#: counter-trend retests additionally need this rejection quality
COUNTER_REJECT_MIN = 0.50
#: widened RSI windows for zone entries (deep retracements are the setup;
#: only absolute extremes are avoided)
ZONE_RSI_WINDOWS: dict[str, tuple[float, float]] = {
    "BUY": (25.0, 65.0),
    "SELL": (35.0, 75.0),
}


@dataclass(frozen=True)
class ZoneRetestSignal:
    """A POI zone retest that fired — the D-048 trigger payload."""

    direction: str  # "BUY" (demand) | "SELL" (supply)
    entry: float  # close of the trigger bar
    zone: dict  # the POI zone dict (side/source/lo/hi/quality/...)
    rejection: float  # 0..1 — how hard the trigger bar rejected
    quality: float  # 0..1 — the zone's POI quality
    atr: float
    counter_trend: bool  # zone direction against the H1 trend


def rejection_quality(
    wick_share: float, body_pos: float
) -> float:
    """0..1 — trigger-bar rejection strength (wick + close position)."""
    return max(0.0, min(1.0, 0.5 * wick_share / 0.6 + 0.5 * body_pos))


def detect_zone_retest(
    base: pd.DataFrame,
    zones: list[dict],
    cfg: EngineConfig,
    direction: str,  # H1 trend direction ("BUY" | "SELL")
) -> ZoneRetestSignal | None:
    """Scan the ranked POI zones for a live retest + rejection.

    Returns the BEST candidate (quality-first, with-trend preferred) or
    None. Deliberately does NOT write trace entries on the negative path
    — the arbitration in evaluate() may still fire sfp/pullback on the
    same bar and their trace must not carry a red zone entry (a zone
    that never formed is not a failed check, and "price at zone, no
    rejection yet" is a FORMING setup, not a rejection of the trade).
    """
    if len(base) < 5:
        return None
    atr_val = atr_calc(base, cfg.atr_period)
    if atr_val <= 0:
        return None

    last = base.iloc[-1]
    o = float(last["o"])
    h = float(last["h"])
    low = float(last["l"])
    c = float(last["c"])
    rng = max(h - low, 1e-9)
    win = min(max(int(cfg.zone_retest_window), 1), 5)
    prev = base.iloc[-(win + 1) : -1]  # the bars before the trigger
    if len(prev) == 0:
        return None

    best: ZoneRetestSignal | None = None
    best_score = -1.0

    for z in zones:
        q = float(z.get("quality", 0.0))
        zlo, zhi = float(z["lo"]), float(z["hi"])

        if z["side"] == "demand":
            entered = low <= zhi
            entered_prev = bool((prev["l"].astype(float) <= zhi).any())
            closed_back = c > zhi
            wick_share = (min(o, c) - low) / rng
            body_pos = (c - low) / rng
            rejected = (
                wick_share >= REJECT_WICK_SHARE or body_pos >= REJECT_BODY_POS
            )
            if not (entered or entered_prev):
                continue  # price never reached this zone — not a miss
            if not closed_back or not rejected:
                continue  # forming / no rejection — not a signal (yet)
            counter = direction == "SELL"
            d = "BUY"
        else:
            entered = h >= zlo
            entered_prev = bool((prev["h"].astype(float) >= zlo).any())
            closed_back = c < zlo
            wick_share = (h - max(o, c)) / rng
            body_pos = (h - c) / rng
            rejected = (
                wick_share >= REJECT_WICK_SHARE or body_pos >= REJECT_BODY_POS
            )
            if not (entered or entered_prev):
                continue
            if not closed_back or not rejected:
                continue
            counter = direction == "BUY"
            d = "SELL"

        gate = cfg.counter_trend_quality if counter else cfg.min_zone_quality
        if q < gate:
            continue
        rej = rejection_quality(wick_share, body_pos)
        if counter and rej < COUNTER_REJECT_MIN:
            continue

        score = q + 0.25 * rej - (0.05 if counter else 0.0)
        if score > best_score:
            best_score = score
            best = ZoneRetestSignal(
                direction=d,
                entry=c,
                zone=z,
                rejection=round(rej, 3),
                quality=q,
                atr=atr_val,
                counter_trend=counter,
            )

    return best


def zone_retest_note(sig: ZoneRetestSignal) -> str:
    """Human-readable trace line for a FIRED zone retest."""
    tag = "counter-trend" if sig.counter_trend else "with-trend"
    return (
        f"{sig.direction} at {sig.zone['side'].upper()} POI "
        f"{sig.zone['lo']:.2f}-{sig.zone['hi']:.2f} "
        f"({sig.zone['source']}, quality {sig.quality:.2f}, {tag}, "
        f"rejection {sig.rejection:.2f})"
    )


def check_rsi_zone(
    base: pd.DataFrame, cfg: EngineConfig, trace: Trace
) -> tuple[bool, float]:
    """RSI check with the widened zone windows (direction set upstream)."""
    from app.engine.indicators import rsi

    value = rsi(base["c"], cfg.rsi_period)
    lo, hi = ZONE_RSI_WINDOWS[trace.direction or "BUY"]
    passed = lo <= value <= hi
    trace.add(
        "rsi", passed,
        f"{value:.1f} (zone window {lo:.0f}-{hi:.0f})",
    )
    return passed, value


def build_levels_zone(
    sig: ZoneRetestSignal, cfg: EngineConfig
) -> tuple[float, float]:
    """(entry, structural SL candidate) — SL beyond the zone's far edge.

    The floor/cap/TPO-anchor/liquidity-snap pass happens in
    smart_targets() exactly as for the other triggers.
    """
    z = sig.zone
    if sig.direction == "BUY":
        sl_base = float(z["lo"]) - cfg.sl_buffer_atr * sig.atr
    else:
        sl_base = float(z["hi"]) + cfg.sl_buffer_atr * sig.atr
    return sig.entry, sl_base


def zone_trigger_quality(sig: ZoneRetestSignal) -> float:
    """0..1 — confidence contribution of a zone setup (location + rejection)."""
    return max(0.0, min(1.0, 0.55 * sig.quality + 0.45 * sig.rejection))
