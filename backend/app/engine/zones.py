"""POI zone-retest trigger (D-048, D-049 rework — user directive).

"Best POI ZONE, SUPPLY ZONE, DEMAND ZONE এই গুলো তে সিগন্যাল দিতে হবে,
মিস করা যাবে না" — when price RETURNS to a quality point-of-interest
zone and REJECTS, that IS the entry. The trigger does not wait for the
other strategies to agree (user directive: "সব গুলো স্ট্রাটেজি একই সময়
AGREE নাও থাকতে পারে") — the location + the rejection carry the setup,
everything else only shapes confidence.

D-049 hard-coded retest rules (DEMAND / BUY — SUPPLY mirrors everything):

  1. ENGAGEMENT — the trigger bar traded INTO the band (low <= zone.hi),
     OR a bar inside `zone_retest_window` entered it AND the trigger bar
     is still CLOSE to the zone (close <= zone.hi + near_atr*ATR) — a
     bounce that already ran away is a MISSED entry, not a late one;
  2. REJECTION — the trigger bar CLOSED back above the zone high with a
     real rejection:
       * lower wick >= 35% of the bar range AND close above the zone
         MID (the zone gave way past its middle = weak), or
       * close in the top 30% of the bar range (strong body rejection), or
       * SWEEP + RECLAIM — the bar wicked THROUGH the zone's far edge
         (low < zone.lo) and closed back above the zone high: the
         classic stop-hunt reversal, rejection forced to >= 0.90;
  3. ENTRY PROXIMITY — a WINDOW-entered retest (the dip happened on an
     earlier bar) still counts only while the close stays near the band
     (<= zone.hi + near_atr*ATR): a bounce that already ran away is a
     MISSED entry, not a late one. A trigger bar that ITSELF traded into
     the zone needs no distance cap — its wick proves the engagement and
     a far closing price is strength (V-reversal), not chasing;
  4. ZONE INTACT — no close beyond the far edge since formation
     (broken zones are dropped upstream in poi_zones);
  5. QUALITY GATE — zone quality >= cfg.min_zone_quality when the zone
     direction agrees with (or is neutral to) the D-049 direction bias;
     a counter-trend reversal needs quality >= cfg.counter_trend_quality
     (D-049: 0.70 -> 0.58 — the old gate made BUY signals practically
     impossible during H1 downtrends, which is why the platform only
     ever sold) AND a strong rejection.
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
REJECT_BODY_POS = 0.70
#: sweep+reclaim rejection floor (the strongest zone entry)
SWEEP_RECLAIM_REJ = 0.90
#: counter-trend retests additionally need this rejection quality
COUNTER_REJECT_MIN = 0.50
#: D-049 — a window-entered zone still counts only if the trigger bar
#: stayed this close (in ATRs) to the band (current-bar entries are
#: exempt: the wick inside the zone IS the engagement)
NEAR_ZONE_ATR = 0.90
#: widened RSI windows for zone entries (D-049 — reversal buys off
#: demand zones print momentum fast; only absolute extremes are avoided)
ZONE_RSI_WINDOWS: dict[str, tuple[float, float]] = {
    "BUY": (20.0, 70.0),
    "SELL": (30.0, 80.0),
}


@dataclass(frozen=True)
class ZoneRetestSignal:
    """A POI zone retest that fired — the D-048/D-049 trigger payload."""

    direction: str  # "BUY" (demand) | "SELL" (supply)
    entry: float  # close of the trigger bar
    zone: dict  # the POI zone dict (side/source/lo/hi/quality/...)
    rejection: float  # 0..1 — how hard the trigger bar rejected
    quality: float  # 0..1 — the zone's POI quality
    atr: float
    counter_trend: bool  # zone direction against the direction bias
    sweep_reclaim: bool = False  # D-049 — stop-hunt through the zone
    sweep_extreme: float | None = None  # D-049 — the wick that swept


def rejection_quality(
    wick_share: float, body_pos: float, sweep_reclaim: bool = False
) -> float:
    """0..1 — trigger-bar rejection strength (wick + close position)."""
    base = max(0.0, min(1.0, 0.5 * wick_share / 0.6 + 0.5 * body_pos))
    if sweep_reclaim:
        base = max(base, SWEEP_RECLAIM_REJ)
    return base


def detect_zone_retest(
    base: pd.DataFrame,
    zones: list[dict],
    cfg: EngineConfig,
    bias_verdict: str,  # D-049 direction bias ("BUY" | "SELL" | "NEUTRAL")
) -> ZoneRetestSignal | None:
    """Scan the ranked POI zones for a live retest + rejection.

    Returns the BEST candidate (quality-first, with-bias preferred) or
    None. Deliberately does NOT write trace entries on the negative path
    — the arbitration in evaluate() may still fire sfp/pullback on the
    same bar and their trace must not carry a red zone entry.
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
        zmid = (zlo + zhi) / 2.0

        if z["side"] == "demand":
            entered = low <= zhi
            entered_prev = bool((prev["l"].astype(float) <= zhi).any())
            near_zone = c <= zhi + NEAR_ZONE_ATR * atr_val
            closed_back = c > zhi
            wick_share = (min(o, c) - low) / rng
            body_pos = (c - low) / rng
            swept = low < zlo  # wicked through the far edge
            rejected = (
                (wick_share >= REJECT_WICK_SHARE and c > zmid)
                or body_pos >= REJECT_BODY_POS
                or (swept and closed_back)
            )
            if not (entered or (entered_prev and near_zone)):
                continue  # price never reached this zone — not a miss
            if not closed_back or not rejected:
                continue  # forming / no rejection — not a signal (yet)
            counter = bias_verdict == "SELL"
            d = "BUY"
        else:
            entered = h >= zlo
            entered_prev = bool((prev["h"].astype(float) >= zlo).any())
            near_zone = c >= zlo - NEAR_ZONE_ATR * atr_val
            closed_back = c < zlo
            wick_share = (h - max(o, c)) / rng
            body_pos = (h - c) / rng
            swept = h > zhi  # wicked through the far edge
            rejected = (
                (wick_share >= REJECT_WICK_SHARE and c < zmid)
                or body_pos >= REJECT_BODY_POS
                or (swept and closed_back)
            )
            if not (entered or (entered_prev and near_zone)):
                continue
            if not closed_back or not rejected:
                continue
            counter = bias_verdict == "BUY"
            d = "SELL"

        gate = cfg.counter_trend_quality if counter else cfg.min_zone_quality
        if q < gate:
            continue
        sweep_reclaim = bool(swept and closed_back)
        rej = rejection_quality(wick_share, body_pos, sweep_reclaim)
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
                sweep_reclaim=sweep_reclaim,
                sweep_extreme=(low if z["side"] == "demand" else h)
                if sweep_reclaim else None,
            )

    return best


def zone_retest_note(sig: ZoneRetestSignal) -> str:
    """Human-readable trace line for a FIRED zone retest."""
    tag = "counter-trend" if sig.counter_trend else "with-trend"
    sweep = ", SWEEP+RECLAIM" if sig.sweep_reclaim else ""
    return (
        f"{sig.direction} at {sig.zone['side'].upper()} POI "
        f"{sig.zone['lo']:.2f}-{sig.zone['hi']:.2f} "
        f"({sig.zone['source']}, quality {sig.quality:.2f}, {tag}, "
        f"rejection {sig.rejection:.2f}{sweep})"
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

    A sweep+reclaim entry anchors the stop beyond the SWEEP extreme
    (the actual invalidation), not just the zone edge. The floor/cap/
    TPO-anchor/liquidity-snap pass happens in smart_targets() exactly
    as for the other triggers.
    """
    z = sig.zone
    if sig.direction == "BUY":
        sl_base = float(z["lo"]) - cfg.sl_buffer_atr * sig.atr
        if sig.sweep_reclaim and sig.sweep_extreme is not None:
            # the stop-hunt wick IS the invalidation — anchor beyond it
            sl_base = min(sl_base, sig.sweep_extreme - cfg.sl_buffer_atr * sig.atr)
    else:
        sl_base = float(z["hi"]) + cfg.sl_buffer_atr * sig.atr
        if sig.sweep_reclaim and sig.sweep_extreme is not None:
            sl_base = max(sl_base, sig.sweep_extreme + cfg.sl_buffer_atr * sig.atr)
    return sig.entry, sl_base


def zone_trigger_quality(sig: ZoneRetestSignal) -> float:
    """0..1 — confidence contribution of a zone setup (location + rejection)."""
    return max(0.0, min(1.0, 0.55 * sig.quality + 0.45 * sig.rejection))
