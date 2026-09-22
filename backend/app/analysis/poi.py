"""POI — Point-of-Interest zone engine (D-048, user directive).

"Best POI ZONE, SUPPLY ZONE, DEMAND ZONE — এই গুলো তে সিগন্যাল দিতে হবে,
মিস করা যাবে না" — signals must fire AT these zones; none may be missed.

Unifies every zone concept the platform computes into ONE ranked list of
points of interest:

  source "sd"   supply/demand base zones (swing base + displacement)
  source "ob"   order blocks (institutional entry footprints)
  source "fvg"  fair value gaps (unfilled / freshly-filled inefficiencies)
  source "tpo"  time-at-price levels (D-047 — prices where the market
                SPENT TIME become marked S/R)
  source "pd"   prior-day high/low (classic liquidity draws)

Each zone carries a normalized 0..1 QUALITY built from hard-coded
weights: impulse strength of the origin move, freshness (unretested
full, FIRST retest near-full — that is the entry), age decay,
time-at-price reinforcement and HTF origin (M15 structure = bigger
players). The signal engine's zone-retest trigger (app/engine/zones.py)
consumes this list: price returning to a QUALITY POI zone and rejecting
IS the entry — no other strategy must agree at that moment (user
directive: "সব গুলো স্ট্রাটেজি একই সময়ে AGREE নাও থাকতে পারে").

Pure functions on closed bars only — no lookahead, no I/O.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from app.analysis import smc
from app.analysis.indicators import atr as atr_last
from app.analysis.tpo import tpo_profile

#: base-TF bars scanned for zone origins (mirrors the confluence window)
POI_WINDOW = 160
#: zone age (minutes) up to which quality stays full
AGE_FULL_MIN = 240.0
#: zone age (minutes) at which the age component bottoms out (24h)
AGE_ZERO_MIN = 1440.0
#: TPO minutes inside the band that count as full reinforcement
TPO_FULL_MINUTES = 30.0
#: TPO minutes below this add nothing (noise)
TPO_MIN_MINUTES = 8.0
#: FVG fills older than this (minutes) stop being POIs
FVG_FILL_WINDOW_MIN = 20.0
#: hard-coded quality weights (sum of the first four = 1.0; HTF is a bonus)
W_IMPULSE = 0.35
W_FRESH = 0.30
W_AGE = 0.15
W_TPO = 0.10
W_HTF_BONUS = 0.10
#: freshness component values
FRESH_UNTOUCHED = 1.0
FRESH_FIRST_TOUCH = 0.85  # the retest happening RIGHT NOW — the ICT entry
FRESH_STALE = 0.45
#: a close this far beyond the far edge (in ATRs) breaks the zone
BROKEN_TOL_ATR = 0.10


def _clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else (1.0 if x > 1.0 else x)


def _age_q(t: Any, last_t: Any) -> float:
    """Age component: full inside 4h, decaying to 0.15 by 24h."""
    try:
        age = (pd.Timestamp(last_t) - pd.Timestamp(t)).total_seconds() / 60.0
    except (TypeError, ValueError):
        return 0.0
    if age <= AGE_FULL_MIN:
        return 1.0
    if age >= AGE_ZERO_MIN:
        return 0.15
    return 1.0 - 0.85 * (age - AGE_FULL_MIN) / (AGE_ZERO_MIN - AGE_FULL_MIN)


def _impulse_q(impulse_atr: float | None) -> float:
    """Impulse component: 1.0x ATR -> 0.0, 3.0x ATR -> 1.0."""
    if not impulse_atr or impulse_atr <= 0:
        return 0.25  # sources without displacement (TPO/PD) get a neutral fill
    return _clamp01((impulse_atr - 1.0) / 2.0)


def _freshness_q(zone: dict, recent_from: Any) -> float:
    """Freshness component from mitigation/fill timestamps."""
    if not zone.get("mitigated") and not zone.get("filled"):
        return FRESH_UNTOUCHED
    mt = zone.get("mit_t") or zone.get("fill_t")
    if mt is None:
        return FRESH_STALE
    try:
        if pd.Timestamp(mt) >= pd.Timestamp(recent_from):
            return FRESH_FIRST_TOUCH
    except (TypeError, ValueError):
        pass
    return FRESH_STALE


def _tpo_minutes(levels: list[dict], lo: float, hi: float) -> float:
    """How many TPO minutes sit inside the [lo, hi] band."""
    total = 0.0
    for lv in levels or []:
        p = float(lv.get("price", 0.0))
        if lo - 0.5 <= p <= hi + 0.5:
            total += float(lv.get("minutes", 0.0))
    return total


def zone_quality(
    *,
    impulse_atr: float | None,
    fresh_q: float,
    age_q: float,
    tpo_minutes: float,
    htf: bool,
) -> float:
    """0..1 — the hard-coded POI quality model (D-048).

    impulse 0.35 — displacement that created the zone (institutional size)
    fresh   0.30 — untested zones + the FIRST retest score highest
    age     0.15 — fresh origins matter; day-old zones decay
    tpo     0.10 — the market also SPENT TIME at this level (reinforcement)
    htf    +0.10 — zone originates on M15 structure (bigger timeframe)
    """
    tpo_q = 0.0
    if tpo_minutes >= TPO_MIN_MINUTES:
        tpo_q = _clamp01(tpo_minutes / TPO_FULL_MINUTES)
    q = (
        W_IMPULSE * _impulse_q(impulse_atr)
        + W_FRESH * _clamp01(fresh_q)
        + W_AGE * _clamp01(age_q)
        + W_TPO * tpo_q
    )
    if htf:
        q += W_HTF_BONUS
    return round(_clamp01(q), 3)


def _broken(
    closes: np.ndarray,
    side: str,
    lo: float,
    hi: float,
    tol: float,
    start_i: int,
) -> bool:
    """A CLOSE beyond the far edge (± tolerance) invalidates the zone.

    Vectorized (D-048): this scan runs for every candidate zone on every
    bar close — the pandas .iloc loop it replaced dominated backtest time.
    """
    if start_i >= len(closes):
        return False
    seg = closes[start_i:]
    if side == "demand":
        return bool((seg < lo - tol).any())
    return bool((seg > hi + tol).any())


def _origin_index(frame: pd.DataFrame, t: Any) -> int:
    """Bar index of the zone origin (+2: the move needs the next bar)."""
    idx = frame.index.get_indexer([pd.Timestamp(t)])
    return int(idx[0]) + 2 if idx[0] >= 0 else 0


def poi_zones(
    base: pd.DataFrame,
    htf: dict[str, pd.DataFrame] | None = None,
    max_zones: int = 24,
) -> list[dict]:
    """Ranked POI zone list (highest quality first), all sides mixed.

    `base` = closed bars of the trigger TF (M1). `htf` optional — when it
    carries an "M15" frame its S/D zones + order blocks join the list as
    HTF zones (quality bonus, institutional timeframe). Zones are deduped
    (same-side overlaps keep the higher-quality band) and broken zones are
    dropped — a close beyond the far edge means the zone is invalidated.
    """
    out: list[dict] = []
    if base is None or len(base) < 30:
        return out
    frame = base.iloc[-POI_WINDOW:].reset_index(drop=True)
    atr_val = atr_last(frame, 14) or 1e-9
    last_t = frame["time_utc"].iloc[-1]
    closes_np = frame["c"].to_numpy(dtype=float)
    broken_tol = BROKEN_TOL_ATR * atr_val
    # the "first touch" window: a mitigation in the last 3 bars is the
    # retest happening RIGHT NOW (the classic zone entry)
    recent_from = frame["time_utc"].iloc[-3] if len(frame) >= 3 else last_t

    # TPO profile once — reinforcement minutes for every zone band
    try:
        prof = tpo_profile(base, lookback_minutes=1440)
        tpo_levels: list[dict] = prof.get("levels", [])
    except Exception:  # noqa: BLE001 — reinforcement is best-effort
        tpo_levels = []

    def _add(
        side: str,
        source: str,
        t: Any,
        lo: float,
        hi: float,
        impulse: float | None,
        htf_origin: bool,
        mitigated: bool = False,
        mit_t: Any = None,
        filled: bool = False,
        fill_t: Any = None,
    ) -> None:
        if _broken(closes_np, side, float(lo), float(hi), broken_tol,
                   _origin_index(frame, t)):
            return
        zone = {
            "side": side,
            "source": source,
            "t": t,
            "lo": float(lo),
            "hi": float(hi),
            "impulse": float(impulse) if impulse else None,
            "mitigated": mitigated,
            "mit_t": mit_t,
            "filled": filled,
            "fill_t": fill_t,
            "htf": htf_origin,
        }
        zone["quality"] = zone_quality(
            impulse_atr=impulse,
            fresh_q=_freshness_q(zone, recent_from),
            age_q=_age_q(t, last_t),
            tpo_minutes=_tpo_minutes(tpo_levels, float(lo), float(hi)),
            htf=htf_origin,
        )
        out.append(zone)

    # --- 1/2. base-TF supply/demand zones + order blocks
    for z in smc.detect_supply_demand(frame):
        _add(z["side"], "sd", z["t"], z["lo"], z["hi"],
             z.get("impulse"), False)
    for ob in smc.detect_order_blocks(frame):
        side = "demand" if ob["side"] == "bullish" else "supply"
        _add(side, "ob", ob["t"], ob["lo"], ob["hi"],
             ob.get("impulse"), False, ob["mitigated"], ob.get("mit_t"))

    # --- 3. fair value gaps: unfilled or FRESHLY filled only
    for g in smc.detect_fvg(frame):
        if g["filled"]:
            fill_t = g.get("fill_t")
            try:
                age = (pd.Timestamp(last_t) - pd.Timestamp(fill_t)
                       ).total_seconds() / 60.0
            except (TypeError, ValueError):
                age = 1e9
            if age > FVG_FILL_WINDOW_MIN:
                continue
        side = "demand" if g["side"] == "bullish" else "supply"
        gap_atr = (g["hi"] - g["lo"]) / atr_val
        _add(side, "fvg", g["t"], g["lo"], g["hi"],
             1.0 + 2.0 * _clamp01(gap_atr), False,
             g["filled"], g.get("fill_t"))

    # --- 4. TPO time-at-price levels (both sides, band = ±half bucket)
    if tpo_levels:
        last_close = float(frame["c"].iloc[-1])
        for lv in tpo_levels:
            if float(lv.get("minutes", 0.0)) < 12.0:
                continue
            p = float(lv["price"])
            side = "demand" if p <= last_close else "supply"
            _add(side, "tpo", last_t, p - 0.25, p + 0.25, None, False)

    # --- 5. prior-day high/low liquidity draws
    liq = smc.detect_liquidity(frame)
    for lv in liq.get("levels", []):
        if lv.get("tag") not in ("PDH", "PDL"):
            continue
        p = float(lv["price"])
        side = "supply" if lv["kind"] == "BSL" else "demand"
        _add(side, "pd", lv.get("t", last_t), p - 0.30, p + 0.30, None, False)

    # --- 6. HTF (M15) zones — institutional timeframe, quality bonus
    m15 = (htf or {}).get("M15")
    if m15 is not None and len(m15) >= 30:
        htf_frame = m15.iloc[-POI_WINDOW:].reset_index(drop=True)
        htf_closes = htf_frame["c"].to_numpy(dtype=float)
        htf_atr = atr_last(htf_frame, 14) or atr_val
        htf_tol = BROKEN_TOL_ATR * htf_atr
        for z in smc.detect_supply_demand(htf_frame):
            if _broken(htf_closes, z["side"], float(z["lo"]), float(z["hi"]),
                       htf_tol, _origin_index(htf_frame, z["t"])):
                continue
            _add(z["side"], "sd", z["t"], z["lo"], z["hi"],
                 z.get("impulse"), True)
        for ob in smc.detect_order_blocks(htf_frame):
            side = "demand" if ob["side"] == "bullish" else "supply"
            if _broken(htf_closes, side, float(ob["lo"]), float(ob["hi"]),
                       htf_tol, _origin_index(htf_frame, ob["t"])):
                continue
            _add(side, "ob", ob["t"], ob["lo"], ob["hi"],
                 ob.get("impulse"), True, ob["mitigated"], ob.get("mit_t"))

    # dedupe: same-side overlapping bands keep the higher-quality zone
    ranked = sorted(out, key=lambda z: z["quality"], reverse=True)
    kept: list[dict] = []
    for z in ranked:
        clash = any(
            o["side"] == z["side"]
            and z["lo"] <= o["hi"] and z["hi"] >= o["lo"]
            for o in kept
        )
        if not clash:
            kept.append(z)
    return kept[:max_zones]
