"""D-057 — ONE geometry for the chart drawing AND the engine signal.

User directive (Bengali): "চার্ট এর মধ্যে যে ড্রয়িং হচ্ছে, আমি চাই সিগন্যাল
গুলো এই ড্রয়িং ফলো করে আসবে ... SL TP ENTRY সব কিছু এই চার্ট ফলো করে
হবে। কারণ এই সিগন্যাল গুলো বেশি সঠিক হচ্ছে।"

Before D-057 the chart's entry-setup box and the engine's signal computed
the SAME trade twice through two different pipelines:

    chart   (drawings.py)  -> smc snapshots; entry = zone midpoint,
                              SL beyond the zone / liquidity, TP at the
                              opposing liquidity pool
    engine  (engine.py)    -> poi_zones + poi_pending_entry + smart_targets

The numbers disagreed, so the order the broker received was never the
trade the chart was showing. D-057 routes both sides through THIS module
so the geometry is computed once, from the same per-TF snapshots:

    ENTRY — the drawn zone's midpoint (the level the setup box marks)
    SL    — beyond the zone, protected past the nearest liquidity pool
    TP    — at the drawn liquidity target, parked just in front of it

`setup_snapshot()` mirrors the exact slices `analyze_frame()` uses for
the drawing layer (services/analysis.py BARS_PER_TF), so the engine sees
the very zones/liquidity the chart draws.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from app.analysis import indicators as ind
from app.analysis import smc

# Per-TF geometry windows that make the engine's frames == the drawing
# layer's frames (services/analysis.py BARS_PER_TF): same slice -> same
# smc detections -> the same zones/liquidity the chart draws.
# D-058 — deepened with BARS_PER_TF (more history = cleaner
# swing/liquidity detection for BOTH the drawing and the order).
GEOMETRY_BARS = {"M5": 300, "M15": 240}

# how close (in M5-ATR units) price must be to a zone for the setup to
# exist — identical to drawings.SETUP_NEAR_ATR (the box the user sees)
SETUP_NEAR_ATR = 0.75

# SL/TP geometry pads (identical to the D-052 drawing formulas)
SL_PAD_ATR = 0.35       # SL this many ATRs beyond the zone edge
LIQ_PROTECT_ATR = 0.15  # parked this far past the liquidity pool
LIQ_WINDOW_ATR = 1.6    # pools this far beyond the zone protect the SL
TP_MIN_RR = 0.8         # below this the TP falls back to the RR floor
TP_FLOOR_RR = 1.2       # the RR floor itself
TP_MAX_RR = 1.8         # D-057 — drawn targets beyond this are swing-scale
MAX_RISK_ATR = 1.6      # D-057 — drawn stops wider than this many M5-ATRs
                        # are swing trades, not the short-time profile


def setup_snapshot(df: pd.DataFrame | None, tf: str) -> dict | None:
    """Light per-TF snapshot with EXACTLY the fields the trade geometry
    needs — the same smc calls `analyze_frame()` runs, over the same
    bar window, so the engine and the chart see the same levels.

    Returns None when the frame is too short to mean anything.
    """
    if df is None or len(df) < 10:
        return None
    frame = df.iloc[-GEOMETRY_BARS.get(tf, 200):]
    return {
        "ok": True,
        "atr": round(ind.atr(frame, 14) or 0.0, 3),
        "order_blocks": smc.detect_order_blocks(frame),
        "fvgs": smc.detect_fvg(frame),
        "liquidity": smc.detect_liquidity(frame),
        "zones": smc.detect_supply_demand(frame),
        "premium_discount": smc.premium_discount(frame),
    }


def candidate_zones(
    s5: dict | None, s15: dict | None, direction: str
) -> list[tuple[str, float, float, Any]]:
    """(tag, lo, hi, origin_t) zones that support `direction`.

    Demand zones / bullish OBs / bullish FVGs support a BUY; the supply
    mirror supports a SELL. M5 zones come first (near-term reaction),
    M15 zones follow (the higher-TF context the chart also draws).
    """
    zones: list[tuple[str, float, float, Any]] = []
    want_bull = direction == "BUY"
    for snap in (s5, s15):
        if not snap or not snap.get("ok"):
            continue
        for z in snap.get("zones") or []:
            if (z.get("side") == "demand") == want_bull:
                zones.append((f"{z['side']} zone", float(z["lo"]),
                              float(z["hi"]), z.get("t")))
        for ob in snap.get("order_blocks") or []:
            if (ob.get("side") == "bullish") == want_bull:
                zones.append(("OB retest", float(ob["lo"]),
                              float(ob["hi"]), ob.get("t")))
        for g in snap.get("fvgs") or []:
            if g.get("filled"):
                continue
            if (g.get("side") == "bullish") == want_bull:
                zones.append(("FVG", float(g["lo"]), float(g["hi"]), g.get("t")))
    return zones


def setup_geometry(
    s5: dict | None,
    s15: dict | None,
    direction: str,
    price: float,
    near_atr: float = SETUP_NEAR_ATR,
    atr_fallback: float = 0.0,
    entry_anchor: str = "near",
    max_rr: float = TP_MAX_RR,
    max_risk_atr: float = MAX_RISK_ATR,
) -> dict | None:
    """THE chart-true trade contract (D-057).

    Returns {tag, lo, hi, t0, entry, sl, tp, rr} for the nearest
    supporting zone within `near_atr` M5-ATRs of `price`, or None when
    the chart would draw no setup box there.

    The numbers are the entry-setup box's own formulas (D-052/D-057):
      entry = the zone's NEAR edge ("near" — the first-touch level the
              retest rejects at; the drawn boundary itself) or its
              midpoint ("mid" — the D-052 box style). "near" is the
              default: mid-entry pendings only fill when price trades
              DEEP into the zone, which is adverse selection (the zone
              only trades that deep when it is breaking — the D-056
              backtest autopsy).
      SL    = zone far edge ± SL_PAD_ATR, protected past the deepest
              same-side liquidity pool within LIQ_WINDOW_ATR
      TP    = nearest opposing liquidity pool parked LIQ_PROTECT_ATR in
              front of it, floored at TP_FLOOR_RR x risk
    """
    if not price or price <= 0:
        return None
    atr5 = float((s5 or {}).get("atr") or 0.0)
    if atr5 <= 0:
        atr5 = float(atr_fallback or 0.0)
    if atr5 <= 0:
        return None

    zones = candidate_zones(s5, s15, direction)
    # OTE band as an extra candidate zone (the fib the chart draws)
    pd_state = (s15 or {}).get("premium_discount") if s15 else None
    ote = (pd_state or {}).get("ote") if pd_state else None
    if isinstance(ote, dict) and ote.get("lo") is not None:
        leg = (pd_state or {}).get("leg")
        if (leg == "up") == (direction == "BUY"):
            zones.append(("OTE 0.62–0.79", float(ote["lo"]),
                          float(ote["hi"]), None))

    # nearest supporting zone by distance to price (band distance: 0
    # when price sits inside the zone — same rule the box uses)
    best: tuple[float, str, float, float, Any] | None = None
    near = near_atr * atr5
    for tag, lo, hi, t0 in zones:
        if hi < lo:
            lo, hi = hi, lo
        dist = 0.0 if (lo <= price <= hi) else min(abs(price - lo),
                                                   abs(price - hi))
        if dist > near:
            continue
        if best is None or dist < best[0]:
            best = (dist, tag, lo, hi, t0)
    if best is None:
        return None
    _, tag, lo, hi, t0 = best
    # entry anchor: the zone's NEAR edge is the level a retest touches
    # FIRST (the drawn boundary line) — pendings there fill on the first
    # return, not only when the zone breaks; "mid" keeps the classic
    # D-052 box style
    if entry_anchor == "mid":
        entry = round((lo + hi) / 2.0, 2)
    else:
        entry = round(hi if direction == "BUY" else lo, 2)

    levels = ((s5 or {}).get("liquidity") or {}).get("levels") or []
    # D-057 — TP candidates: every level the CHART DRAWS in the trade's
    # direction (liquidity lines + opposing zone boxes). The TP is the
    # NEAREST drawn target beyond TP_MIN_RR x risk (parked just in front),
    # never the far pools the old box chased — "TP যেনো হিট বেশি হয়".
    # No drawn target within TP_MAX_RR -> None: the chart offers no
    # short-time trade here and the caller falls back to the legacy
    # ladder (which parks at its own nearest structure).
    def _tp_candidates(want_bull: bool, entry: float, risk: float
                       ) -> list[tuple[float, str]]:
        cands: list[tuple[float, str]] = []
        min_fwd = TP_MIN_RR * risk
        for lv in levels:
            kind, p = lv.get("kind"), float(lv["price"])
            if want_bull and kind == "BSL" and p > entry + min_fwd:
                cands.append((p, "BSL liquidity line"))
            elif not want_bull and kind == "SSL" and p < entry - min_fwd:
                cands.append((p, "SSL liquidity line"))
        for snap in (s5, s15):
            for z in (snap or {}).get("zones") or []:
                zlo, zhi = float(z["lo"]), float(z["hi"])
                if want_bull and z.get("side") == "supply" \
                        and zlo > entry + min_fwd:
                    cands.append((zlo, "supply zone edge"))
                elif not want_bull and z.get("side") == "demand" \
                        and zhi < entry - min_fwd:
                    cands.append((zhi, "demand zone edge"))
        return cands

    if direction == "BUY":
        sl = lo - SL_PAD_ATR * atr5
        ssl_below = [lv["price"] for lv in levels
                     if lv.get("kind") == "SSL"
                     and lo - LIQ_WINDOW_ATR * atr5 < lv["price"] < lo]
        if ssl_below:
            sl = min(ssl_below) - LIQ_PROTECT_ATR * atr5
        risk = entry - sl
        if risk <= 0:
            return None
        cands = _tp_candidates(True, entry, risk)
        if not cands:
            return None
        # D-057 tradeability gate — the FINAL drawn stop (liquidity-
        # protected) wider than max_risk_atr M5-ATRs is a swing trade,
        # not the short-time profile (ATR-relative: scales across
        # mock / gold / BTC); checked here so the box and the engine
        # reject the SAME contracts
        if risk > max_risk_atr * atr5:
            return None
        tp = min(p for p, _ in cands) - LIQ_PROTECT_ATR * atr5
        if (tp - entry) / risk > max_rr:
            return None  # nearest drawn target is swing-scale — not ours
        if (tp - entry) / risk < TP_MIN_RR:
            tp = entry + TP_FLOOR_RR * risk
        tp_src = next(src for p, src in cands
                      if p == min(q for q, _ in cands))
    else:
        sl = hi + SL_PAD_ATR * atr5
        bsl_above = [lv["price"] for lv in levels
                     if lv.get("kind") == "BSL"
                     and hi < lv["price"] < hi + LIQ_WINDOW_ATR * atr5]
        if bsl_above:
            sl = max(bsl_above) + LIQ_PROTECT_ATR * atr5
        risk = sl - entry
        if risk <= 0:
            return None
        cands = _tp_candidates(False, entry, risk)
        if not cands:
            return None
        if risk > max_risk_atr * atr5:
            return None  # the liquidity-protected stop is swing-scale
        tp = max(p for p, _ in cands) + LIQ_PROTECT_ATR * atr5
        if (entry - tp) / risk > max_rr:
            return None
        if (entry - tp) / risk < TP_MIN_RR:
            tp = entry - TP_FLOOR_RR * risk
        tp_src = next(src for p, src in cands
                      if p == max(q for q, _ in cands))

    return {
        "tag": tag,
        "lo": round(lo, 2),
        "hi": round(hi, 2),
        "t0": t0,
        "atr5": atr5,
        "entry": entry,
        "sl": round(float(sl), 2),
        "tp": round(float(tp), 2),
        "rr": round(abs(tp - entry) / risk, 2),
        "tp_source": tp_src,
    }
