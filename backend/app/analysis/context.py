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
from app.analysis import smc, tpo

# timeframes the /api/analysis endpoint reports by default
# D-052 follow-up: M30 + D1 join the set — the frontend chart offers every
# TF from M1 to D1, and EACH needs its own drawing set so switching
# timeframes never blanks the overlay (user directive: "টাইম ফ্রম
# পরিবর্তন করলেও ড্রয়িং নষ্ট হবে না").
ANALYSIS_TFS: tuple[str, ...] = ("M1", "M5", "M15", "M30", "H1", "H4", "D1")


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
    whale_pulse_bars: int = 3,   # D-051 — real-time whale window (M1 bars)
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

    # 10 — D-044 cumulative delta confirmation (bonus, not gating): the
    # aggressive buy-vs-sell flow of the last 30 M1 bars agrees with the
    # trade direction (or is flat — never blocks)
    cd = of.cumulative_delta(base.iloc[-30:])
    cd_ok = (cd >= 0) if want_bull else (cd <= 0)
    factors.append({
        "name": "delta_confirms",
        "ok": bool(cd_ok),
        "detail": (
            f"30-bar delta {cd:+.0f} {'supports' if cd_ok else 'fades'}"
            f" the {direction.lower()}"
        ),
    })

    # 11 — D-044 active institutional participation (bonus, not gating):
    # the last hour's traded value runs above the 24h hourly average
    try:
        vol = base["v"].astype(float)
        tp_typ = (
            base["h"].astype(float) + base["l"].astype(float)
            + base["c"].astype(float)
        ) / 3.0
        usd = (vol * 100.0 * tp_typ).astype(float)
        usd_1h = float(usd.iloc[-60:].sum())
        usd_avg = float(usd.iloc[-1440:].sum()) / 24.0 if len(usd) >= 60 else usd_1h
        flow_ok = usd_avg <= 0 or usd_1h >= usd_avg
        detail = (
            f"1h flow ${usd_1h / 1e6:.1f}M vs ${usd_avg / 1e6:.1f}M/h avg"
            f" ({'active' if flow_ok else 'quiet'})"
        )
    except Exception:  # noqa: BLE001 — flow math must never break the pipeline
        flow_ok, detail = True, "flow unavailable"
    factors.append({
        "name": "flow_active",
        "ok": bool(flow_ok),
        "detail": detail,
    })

    # 12 — D-047 time-at-price level (bonus, not gating): the entry forms
    # AT a price level where the market SPENT TIME (TPO high-time node) —
    # support under a BUY, resistance over a SELL. Levels the market kept
    # returning to are where professionals expect a reaction.
    try:
        prof = tpo.tpo_profile_cached(base, lookback_minutes=1440)
        want_side = "support" if want_bull else "resistance"
        side_levels = [lv for lv in prof["levels"] if lv["side"] == want_side]
        near = tpo.nearest_level(side_levels, entry, max_dist=0.5 * a)
        tpo_ok = near is not None
        detail = (
            f"entry at {want_side} TPO {near['price']:.2f}"
            f" ({near['minutes']:.0f}m of time held there)"
            if near else
            f"no {want_side} time-at-price level within {0.5 * a:.2f}"
        )
    except Exception:  # noqa: BLE001 — TPO math must never break the pipeline
        tpo_ok, detail = True, "TPO unavailable"
    factors.append({
        "name": "at_tpo_level",
        "ok": bool(tpo_ok),
        "detail": detail,
    })

    # 13 — D-051 REAL-TIME whale pulse (TRUSTED, DOUBLE vote): a big
    # buyer/seller entered on one of the last few M1 bars (user directive:
    # "কখন বড় ভাইয়ার এন্ট্রি নিলো এই বিষয়টি রিয়েল টাইমে ধরা লাগবে" —
    # catching the big-player entry AS IT HAPPENS, not bars later).
    # Momentum/absorption events on the trigger side inside the window
    # count; sweeps count on the OPPOSITE side (a sell-side sweep IS
    # big-buyer evidence).
    try:
        win = max(int(whale_pulse_bars), 1)
        wlook = min(60, max(len(base) - 5, 6))
        evs = of.detect_whale_events(base.iloc[-(wlook + win):], lookback=wlook)
        win_from = base["time_utc"].iloc[-win]
        recent = [e for e in evs if e["t"] >= win_from]
        want_side = "buy" if want_bull else "sell"
        pulse_ok = any(
            e["side"] == want_side
            or (e["kind"] == "sweep" and e["side"] != want_side)
            for e in recent
        )
        if recent:
            e = recent[-1]
            wdetail = f"big {e['side']} {e['kind']} {e['vol_z']}z — {e['note']}"
        else:
            wdetail = f"no big-player entry in the last {win} M1 bars"
    except Exception:  # noqa: BLE001 — whale pulse must never break the pipeline
        pulse_ok, wdetail = False, "whale pulse unavailable"
    factors.append({
        "name": "whale_pulse",
        "ok": bool(pulse_ok),
        "detail": wdetail,
    })

    return factors


#: D-051 — the TRUSTED core (user directive: "যদি কয়েকটি স্ট্যাটাজি মিলে
#: ভোট দেয়, বিশ্বাসযোগ্য কয়েকটি স্ট্রাটেজি হতে হবে"): when these
#: strategies vote together the signal fires even if the full 6-factor
#: ICT panel is short. The REAL-TIME whale pulse counts DOUBLE (a big
#: buyer/seller entry is the strongest single evidence there is).
TRUSTED_GATE = ("whale_pulse", "structure_m1", "liquidity_sweep", "zone")
#: how many votes each trusted factor carries
TRUSTED_WEIGHTS = {"whale_pulse": 2.0}


def trusted_score(factors: list[dict]) -> float:
    """D-051 — weighted votes of the TRUSTED core (whale counts double)."""
    return sum(
        TRUSTED_WEIGHTS.get(f["name"], 1.0)
        for f in factors
        if f["name"] in TRUSTED_GATE and f["ok"]
    )


CONFLUENCE_GATE = ("structure_m1", "htf_structure", "ob_retest",
                   "fvg_fill", "liquidity_sweep", "zone")
CONFLUENCE_BONUS = ("volume", "killzone", "whale_bias", "delta_confirms",
                    "flow_active", "at_tpo_level", "whale_pulse")

#: D-049 — the stop must survive at least this many spreads of noise
SPREAD_FLOOR_MULT = 2.5

#: D-049 — structural levels within this many ATRs of the ENTRY are the
#: market's CURRENT location, not barriers/targets (a zone minted by the
#: trigger's own move sits right on the entry and was vetoing sweeps at
#: 0.05R — the noise floor ignores it)
NOISE_FLOOR_ATR = 0.35


def confluence_score(factors: list[dict]) -> int:
    """How many GATING factors passed (0..6)."""
    return sum(1 for f in factors
               if f["name"] in CONFLUENCE_GATE and f["ok"])


def bonus_score(factors: list[dict]) -> int:
    """How many BONUS factors passed (0..5)."""
    return sum(1 for f in factors
               if f["name"] in CONFLUENCE_BONUS and f["ok"])


def _target_ladder(
    base: pd.DataFrame,
    direction: str,
    entry: float,
    risk: float,
    zones: list[dict] | None,
    tpo_levels: list[dict] | None,
) -> list[tuple[float, str]]:
    """Ordered (target_price, source) ladder ABOVE a BUY / BELOW a SELL.

    D-049 — every structural object the engine already computes becomes a
    take-profit candidate: opposing POI zones (near edge), buy/sell-side
    liquidity pools (equal highs/lows + PDH/PDL) and opposing TPO
    time-at-price levels. The market is drawn to these — the TP belongs
    just IN FRONT of the nearest one, not at an arbitrary fixed multiple.

    Levels the market is ALREADY trading at (within NOISE_FLOOR_ATR of
    the entry) are dropped: a zone minted by the trigger's own move sits
    right on top of the entry — it is the current location, not a
    barrier (the sweep's breakdown origin was vetoing every sweep setup
    at 0.05R).
    """
    ladder: list[tuple[float, str]] = []
    want_bull = direction == "BUY"
    noise = NOISE_FLOOR_ATR * (ind.atr(base, 14) or 1e-9)

    # opposing POI zones — the near edge is where reaction starts
    for z in zones or []:
        if want_bull and z.get("side") == "supply":
            ladder.append((float(z["lo"]), f"supply zone {z['lo']:.2f}"))
        elif not want_bull and z.get("side") == "demand":
            ladder.append((float(z["hi"]), f"demand zone {z['hi']:.2f}"))

    # liquidity pools the trade runs toward (BSL above for BUY, SSL below)
    liq = smc.detect_liquidity(base.iloc[-160:])
    for lv in liq.get("levels", []):
        if want_bull and lv["kind"] == "BSL":
            tag = lv.get("tag") or "BSL"
            ladder.append((float(lv["price"]), f"{tag} liquidity"))
        elif not want_bull and lv["kind"] == "SSL":
            tag = lv.get("tag") or "SSL"
            ladder.append((float(lv["price"]), f"{tag} liquidity"))

    # opposing TPO time-at-price levels
    for lv in tpo_levels or []:
        p = float(lv.get("price", 0.0))
        side = lv.get("side")
        if want_bull and side == "resistance":
            ladder.append((p, f"TPO resistance {p:.2f}"))
        elif not want_bull and side == "support":
            ladder.append((p, f"TPO support {p:.2f}"))

    # keep only targets genuinely in the trade's direction (and beyond
    # the noise floor around the entry)
    fwd = [t for t in ladder
           if (t[0] > entry if want_bull else t[0] < entry)
           and abs(t[0] - entry) > noise]
    fwd.sort(key=lambda t: abs(t[0] - entry))
    return fwd


def smart_targets(
    base: pd.DataFrame,
    direction: str,
    entry: float,
    sl_base: float,          # structural SL candidate (beyond sweep/swing)
    rr: float,               # fallback TP multiple when no target is found
    min_sl_atr: float,
    max_sl_atr: float,
    tpo_levels: list[dict] | None = None,  # D-047 time-at-price levels
    zones: list[dict] | None = None,  # D-049 ranked POI zones (TP ladder)
    spread_price: float = 0.0,  # D-049 current spread in price units
    min_rr: float = 1.2,    # D-049 geometry floor — skip worse trades
    max_tp_r: float = 3.0,  # D-049 beyond the furthest useful target
) -> tuple[float, float, float | None, str]:
    """Structure-aware (entry, sl, tp, note) — app-controlled exits.

    D-049 user directive: "SL ও TP সমান রেশিও দিচ্ছে কেনো? ... হিসাব করে
    প্রেডিকশন করতে হবে, মার্কেট এই প্রাইস লেভেল এ গেলে SL অথবা TP হিট
    করবে" — exits are PREDICTED from the market structure, not fixed:

    SL: the structural invalidation (sweep extreme / swing / zone edge),
    floored at min_sl_atr*ATR and at 2.5x the current spread (a stop the
    spread can eat half of is not a stop), capped at max_sl_atr*ATR.
    D-047: when a STRONG time-at-price level sits beyond the structural
    SL the stop extends JUST PAST the NEAREST one (D-049 bug fix — the
    old code anchored past the FURTHEST level, ballooning the stop).
    TP: the nearest structural target (opposing POI zone edge, liquidity
    pool, TPO level) at >= min_rr x risk, parked 0.10 ATR in front of
    it; rr x risk when no target exists inside max_tp_r. When the
    nearest structure stands BEFORE min_rr x risk the trade has no room
    — tp=None tells the caller to SKIP (poor geometry, predicted to lose
    the race between the barrier and the profit).
    """
    a = ind.atr(base, 14) or 1e-9
    want_bull = direction == "BUY"
    # --- stop loss: structural, spread-floored, ATR-floored and capped
    spread_floor = SPREAD_FLOOR_MULT * max(spread_price, 0.0)
    min_risk = max(min_sl_atr * a, spread_floor)
    if want_bull:
        sl = min(sl_base, entry - min_risk)
        sl = max(sl, entry - max_sl_atr * a)      # cap: never risk more
        risk = entry - sl
    else:
        sl = max(sl_base, entry + min_risk)
        sl = min(sl, entry + max_sl_atr * a)
        risk = sl - entry
    if risk <= 0:  # pathological (cap below floor) — fall back to floor
        sl = entry - min_risk if want_bull else entry + min_risk
        risk = min_risk

    # --- D-047/D-049 TPO anchoring: extend the stop just past the
    # NEAREST strong level that sits beyond it (BUY: supports below sl)
    if tpo_levels:
        strong = [lv for lv in tpo_levels if float(lv.get("strength", 0)) >= 0.5]
        pad = 0.15 * a
        if want_bull:
            below = [float(lv["price"]) for lv in strong if lv["price"] < sl]
            if below:
                cand = max(below) - pad  # NEAREST level under the stop
                if entry - cand <= max_sl_atr * a:
                    sl = cand
                    risk = entry - sl
        else:
            above = [float(lv["price"]) for lv in strong if lv["price"] > sl]
            if above:
                cand = min(above) + pad  # NEAREST level over the stop
                if cand - entry <= max_sl_atr * a:
                    sl = cand
                    risk = sl - entry

    # --- take profit: PREDICTED from the structure (D-049)
    ladder = _target_ladder(base, direction, entry, risk, zones, tpo_levels)
    park = 0.10 * a  # park just in front of the level (avoid the shave)
    tp: float | None = None
    note = ""
    if ladder:
        nearest_dist = abs(ladder[0][0] - entry)
        if nearest_dist < min_rr * risk:
            # a structural barrier stands before the profit can develop —
            # the market is predicted to hit the barrier first: skip
            return entry, float(sl), None, (
                f"poor geometry — {ladder[0][1]} at "
                f"{nearest_dist / risk:.2f}R (< {min_rr:.1f}R min)"
            )
        for target, src in ladder:
            dist = (target - entry) if want_bull else (entry - target)
            if min_rr * risk <= dist <= max_tp_r * risk:
                tp = (target - park) if want_bull else (target + park)
                note = f"{src} @ {target:.2f} ({dist / risk:.2f}R)"
                break
    if tp is None:
        fallback = min(rr, max_tp_r)
        tp = entry + fallback * risk if want_bull else entry - fallback * risk
        note = f"no target within {max_tp_r:.0f}R — rr {fallback:.1f}R"
    return entry, float(sl), float(tp), note
