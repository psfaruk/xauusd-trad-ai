"""Analysis context (D-042) — per-TF snapshot, MTF bias, engine confluence.

`analyze_frame(df)` builds the full per-timeframe snapshot (structure,
zones, liquidity, whales, classic indicators) used by /api/analysis.
`build_confluence()` is THE shared gate the signal engine (live AND
backtest) calls on every bar close — it answers "which ICT/SMC factors
confirm this trade" with human-readable trace lines that surface
directly in the app's Signal Analysis panel.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from app.analysis import indicators as ind
from app.analysis import orderflow as of
from app.analysis import smc

# timeframes the /api/analysis endpoint reports by default
ANALYSIS_TFS: tuple[str, ...] = ("M1", "M5", "M15", "H1", "H4")


# ------------------------------------------------------------ per-TF view


def analyze_frame(df: pd.DataFrame, bars: int = 240) -> dict:
    """Full analysis snapshot of one timeframe (closed bars only)."""
    if df is None or len(df) < 10:
        return {"ok": False, "bars": 0 if df is None else len(df)}
    frame = df.iloc[-bars:]
    closes = frame["c"]
    last = float(closes.iloc[-1])
    structure = smc.detect_structure(frame)
    a = ind.atr(frame, 14)
    return {
        "ok": True,
        "bars": len(frame),
        "last": round(last, 2),
        "atr": round(a, 3),
        "structure": structure,
        "order_blocks": smc.detect_order_blocks(frame),
        "fvgs": smc.detect_fvg(frame),
        "liquidity": smc.detect_liquidity(frame),
        "zones": smc.detect_supply_demand(frame),
        "premium_discount": smc.premium_discount(frame),
        "whales": of.whale_summary(frame),
        "indicators": {
            "rsi": round(float(ind.rsi_series(closes).iloc[-1]), 1),
            "macd": {k: round(v, 3) for k, v in ind.macd(closes).items()},
            "stoch": {k: round(v, 1) for k, v in ind.stochastic(frame).items()},
            "adx": {k: round(v, 1) for k, v in ind.adx(frame).items()},
            "bollinger": {k: round(v, 3) for k, v in ind.bollinger(closes).items()},
            "vwap": round(ind.vwap(frame), 2),
            "vwap_rel": ("above" if last > ind.vwap(frame) else
                         ("below" if last < ind.vwap(frame) else "at")),
            "cci": round(ind.cci(frame), 1),
            "momentum": round(ind.momentum(closes), 4),
            "vol_z": round(ind.volume_zscore(frame), 2),
        },
        "volume_profile": of.volume_profile(frame.iloc[-120:]),
        "delta": round(of.cumulative_delta(frame.iloc[-30:]), 1),
    }


def mtf_bias(snapshots: dict[str, dict]) -> dict:
    """Aggregate per-TF structure + EMA trend into one bias verdict."""
    votes: list[float] = []
    notes: list[str] = []
    for tf in ("H4", "H1", "M15", "M5"):
        snap = snapshots.get(tf)
        if not snap or not snap.get("ok"):
            continue
        trend = snap.get("structure", {}).get("trend", "balanced")
        v = 1.0 if trend == "bullish" else (-1.0 if trend == "bearish" else 0.0)
        # higher TFs weigh more
        votes.append(v * {"H4": 4.0, "H1": 3.0, "M15": 2.0, "M5": 1.0}[tf])
        notes.append(f"{tf}: {trend}")
    if not votes:
        return {"bias": "unknown", "score": 0.0, "notes": notes}
    denom = max(sum(abs(v) for v in votes), 1e-9)  # D-043: all-balanced guard
    score = sum(votes) / denom
    bias = "bullish" if score > 0.2 else ("bearish" if score < -0.2 else "mixed")
    return {"bias": bias, "score": round(score, 2), "notes": notes}


# -------------------------------------------------------- engine factors


def _near(price: float, lo: float, hi: float, tol: float) -> bool:
    """Price within `tol` of the [lo, hi] band (outside edges count)."""
    return lo - tol <= price <= hi + tol


def build_confluence(
    base: pd.DataFrame,          # closed M1 (trigger TF) bars
    htf: dict[str, pd.DataFrame],  # closed higher-TF frames (H1, M5, M15, H4...)
    direction: str,              # "BUY" | "SELL"
    entry: float,                # candidate entry price
    bar_time: datetime,          # close time of the trigger bar
    max_zone_atr: float = 0.9,   # entry-to-zone proximity in ATRs
) -> list[dict]:
    """ICT/SMC confirmation factors for one candidate trade.

    Returns a list of {name, ok, detail} — the caller gates on how many
    are ok and folds them into the trace + confidence score.
    """
    factors: list[dict] = []
    want_bull = direction == "BUY"
    a = ind.atr(base, 14) or 1e-9
    tol = max_zone_atr * a

    # 1 — M1 market structure in the trade direction
    st = smc.detect_structure(base.iloc[-160:])
    le = st.get("last_event") or {}
    struct_ok = (
        (st["trend"] == "bullish" and want_bull)
        or (st["trend"] == "bearish" and not want_bull)
        or (le.get("dir") == ("up" if want_bull else "down")
            and le.get("kind") in ("BOS", "CHoCH"))
    )
    factors.append({
        "name": "structure_m1",
        "ok": bool(struct_ok),
        "detail": (
            f"M1 {st['trend']}"
            + (f", last {le.get('kind')} {le.get('dir')} @ {le.get('level'):.2f}"
               if le else "")
        ),
    })

    # 2 — higher-TF structure bias (trend TF + bias TFs, majority vote)
    agreed, total = 0, 0
    detail_tfs: list[str] = []
    for tf in ("H4", "H1", "M15", "M5"):
        frame = htf.get(tf)
        if frame is None or len(frame) < 25:
            continue
        hst = smc.detect_structure(frame.iloc[-160:])
        total += 1
        if (hst["trend"] == "bullish" and want_bull) or (
            hst["trend"] == "bearish" and not want_bull
        ):
            agreed += 1
            detail_tfs.append(f"{tf}={hst['trend'][:4]}")
        else:
            detail_tfs.append(f"{tf}={hst['trend'][:4]}")
    htf_ok = total > 0 and agreed >= max(1, (total + 1) // 2)
    factors.append({
        "name": "htf_structure",
        "ok": bool(htf_ok),
        "detail": f"structure bias {agreed}/{total} agree ({', '.join(detail_tfs) or 'no data'})",
    })

    # 3 — order-block retest on the trigger TF (fresh touches count: the
    # FIRST retest of an OB is the ICT entry, so a mitigation happening at
    # the trigger bar or the bar before is exactly what we want to reward)
    obs = smc.detect_order_blocks(base.iloc[-160:])
    side = "bullish" if want_bull else "bearish"
    recent_from = base["time_utc"].iloc[-2]
    ob_hit = next(
        (ob for ob in reversed(obs)
         if ob["side"] == side
         and _near(entry, ob["lo"], ob["hi"], tol)
         and (not ob["mitigated"] or ob.get("mit_t") is not None
              and ob["mit_t"] >= recent_from)),
        None,
    )
    factors.append({
        "name": "ob_retest",
        "ok": ob_hit is not None,
        "detail": (
            f"retest of {side} OB {ob_hit['lo']:.2f}-{ob_hit['hi']:.2f}"
            f" ({ob_hit['impulse']}x ATR impulse)"
            if ob_hit else
            f"no fresh {side} order block within {tol:.2f}"
        ),
    })

    # 4 — fair-value-gap fill on the trigger TF (filling NOW counts —
    # price trading into an unfilled gap and rejecting is the entry)
    gaps = smc.detect_fvg(base.iloc[-120:])
    fvg_hit = next(
        (g for g in reversed(gaps)
         if g["side"] == side
         and _near(entry, g["lo"], g["hi"], tol)
         and (not g["filled"] or g.get("fill_t") is not None
              and g["fill_t"] >= recent_from)),
        None,
    )
    factors.append({
        "name": "fvg_fill",
        "ok": fvg_hit is not None,
        "detail": (
            f"price filling {side} FVG {fvg_hit['lo']:.2f}-{fvg_hit['hi']:.2f}"
            if fvg_hit else
            f"no fresh {side} fair value gap near entry"
        ),
    })

    # 5 — liquidity sweep into the trade (manipulation confirming intent)
    liq = smc.detect_liquidity(base.iloc[-160:])
    want_sweep = "SSL" if want_bull else "BSL"  # BUY hunts sell-side first
    sweep_ok = any(s["kind"] == want_sweep for s in liq.get("sweeps", []))
    lvl_note = ", ".join(
        f"{lv['kind']}{('@' + lv['tag']) if lv.get('tag') else ''} {lv['price']:.2f}"
        for lv in liq.get("levels", [])[:3]
    ) or "none mapped"
    factors.append({
        "name": "liquidity_sweep",
        "ok": bool(sweep_ok),
        "detail": (
            f"{want_sweep} swept then rejected (stop hunt into the trade)"
            if sweep_ok else
            f"no {want_sweep} sweep on the trigger bar; pools: {lvl_note}"
        ),
    })

    # 6 — supply/demand zone confluence
    zones = smc.detect_supply_demand(base.iloc[-160:])
    zside = "demand" if want_bull else "supply"
    z_hit = next(
        (z for z in reversed(zones)
         if z["side"] == zside and _near(entry, z["lo"], z["hi"], tol)),
        None,
    )
    factors.append({
        "name": "zone",
        "ok": z_hit is not None,
        "detail": (
            f"entry at {zside} zone {z_hit['lo']:.2f}-{z_hit['hi']:.2f}"
            if z_hit else
            f"no fresh {zside} zone near entry"
        ),
    })

    # 7 — institutional volume on the trigger bar (whale participation)
    vol_z = ind.volume_zscore(base)
    factors.append({
        "name": "volume",
        "ok": bool(vol_z >= 0.8),
        "detail": f"trigger-bar volume z={vol_z:.1f} (institutions active >= 0.8)",
    })

    # 8 — ICT kill zone timing (bonus, not gating)
    kz = smc.kill_zone(bar_time)
    factors.append({
        "name": "killzone",
        "ok": bool(kz["in"]),
        "detail": f"{kz['name']} kill zone" if kz["in"] else "outside kill zones",
    })

    # 9 — whale bias from the order-flow proxy (bonus, not gating)
    wh = of.whale_summary(base.iloc[-90:], lookback=45)
    wh_ok = (
        wh["bias"] == ("buy" if want_bull else "sell")
        or wh["bias"] == "neutral"
    )
    factors.append({
        "name": "whale_bias",
        "ok": bool(wh_ok),
        "detail": (
            f"order-flow bias {wh['bias']}"
            + (f", last: {wh['last']['note']}" if wh.get("last") else "")
        ),
    })

    return factors


CONFLUENCE_GATE = ("structure_m1", "htf_structure", "ob_retest",
                   "fvg_fill", "liquidity_sweep", "zone")
CONFLUENCE_BONUS = ("volume", "killzone", "whale_bias")


def confluence_score(factors: list[dict]) -> int:
    """How many GATING factors passed (0..6)."""
    return sum(1 for f in factors
               if f["name"] in CONFLUENCE_GATE and f["ok"])


def bonus_score(factors: list[dict]) -> int:
    """How many BONUS factors passed (0..3)."""
    return sum(1 for f in factors
               if f["name"] in CONFLUENCE_BONUS and f["ok"])


def smart_targets(
    base: pd.DataFrame,
    direction: str,
    entry: float,
    sl_base: float,          # structural SL candidate (beyond sweep/swing)
    rr: float,
    min_sl_atr: float,
    max_sl_atr: float,
) -> tuple[float, float, float]:
    """Zone-aware (entry, sl, tp) — app-controlled exits (user directive).

    SL: the structural invalidation (sweep extreme / swing / zone edge),
    floored at min_sl_atr*ATR and capped at max_sl_atr*ATR from entry.
    TP: rr * risk, snapped just in front of the nearest opposing
    liquidity pool when one sits inside [0.75, 1.6]x risk (ICT: price
    is drawn to liquidity).
    """
    a = ind.atr(base, 14) or 1e-9
    want_bull = direction == "BUY"
    # --- stop loss: structural, floored and capped
    if want_bull:
        sl = min(sl_base, entry - min_sl_atr * a)
        sl = max(sl, entry - max_sl_atr * a)      # cap: never risk more
        risk = entry - sl
    else:
        sl = max(sl_base, entry + min_sl_atr * a)
        sl = min(sl, entry + max_sl_atr * a)
        risk = sl - entry
    if risk <= 0:  # pathological (cap below floor) — fall back to floor
        sl = entry - min_sl_atr * a if want_bull else entry + min_sl_atr * a
        risk = min_sl_atr * a

    # --- take profit: rr * risk, liquidity-snapped
    tp = entry + rr * risk if want_bull else entry - rr * risk
    liq = smc.detect_liquidity(base.iloc[-160:])
    opposing = [
        lv["price"] for lv in liq.get("levels", [])
        if (lv["kind"] == "BSL" and want_bull) or (lv["kind"] == "SSL" and not want_bull)
    ]
    for target in sorted(opposing, key=lambda p: abs(p - entry)):
        dist = (target - entry) if want_bull else (entry - target)
        if 0.75 * risk <= dist <= 1.6 * risk:
            # park the TP just in front of the pool (avoid the shave)
            tp = target - 0.05 * risk if want_bull else target + 0.05 * risk
            break
    return entry, float(sl), float(tp)
