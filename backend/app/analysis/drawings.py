"""D-043 — Professional auto-drawing engine (user directive, Bengali):

"একজন প্রফেশনাল ট্রেডার যেভাবে তার চার্ট এনালাইসিস করার জন্য ড্রয়িং করে —
হরাইজন্টাল লাইন, ট্রেন্ড লাইন, fibonacchi… সব সময় ড্রয়িং করবে না। যখন মনে
হবে এখানে এন্ট্রি নেওয়ার জন্য ভালো একটা সেটআপ পাইছি তখন অ্যাপ নিজে থেকেই
চার্ট এর উপর ড্রইং করবে… চার্ট এ ছোট করে নোট লিখে রাখবে (fvg, choch, SL)…
এন্ট্রি নেওয়ার আগে চার্ট এর মধ্যে এন্ট্রি সেটাপ ড্রয়িং করতে হবে।"

build_drawings() turns the already-computed analysis frames/snapshots into
JSON drawing primitives the chart renders on its overlay canvas:

    hline      — PDH/PDL, POC, key liquidity (BSL/SSL) with tags
    trendline  — last two swing highs / lows, projected forward, broken flag
    fib        — retracement of the active leg + OTE (0.618–0.79) band
    note       — small text chips (BOS/CHoCH, sweeps, whale/manipulation)
    setup      — the ENTRY SETUP box: zone + app-controlled SL/TP + factors,
                 drawn BEFORE entry while the setup is forming, and marked
                 "triggered" once the engine actually fired the signal

Drawings are RE-COMPUTED on every /api/analysis snapshot (~20s cache) so
they follow the market — never a fixed picture (user directive: "ড্রয়িং টি
ফিক্স থাকবে না… কিছুক্ষণ পর পর ড্রয়িং করতে থাকবে").
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import pandas as pd

from app.analysis import indicators as ind
from app.analysis import smc
from app.analysis.context import mtf_bias

logger = logging.getLogger("xauusd.drawings")

MAX_DRAWINGS = 26
#: how close (in ATR units) price must be to a zone for the SETUP box
SETUP_NEAR_ATR = 0.75
#: a setup drawing expires once its zone origin is older than this
SETUP_MAX_AGE_MIN = 240
#: recent-signal window marking a forming setup as triggered
TRIGGER_WINDOW_MIN = 25

FIB_RATIOS = (0.236, 0.382, 0.5, 0.618, 0.786)


def _iso(t: Any) -> str | None:
    """Timestamp/datetime -> ISO string (None-safe)."""
    if t is None:
        return None
    try:
        ts = pd.Timestamp(t)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        return ts.isoformat()
    except (TypeError, ValueError):
        return None


def _age_min(t: Any, now: datetime) -> float:
    try:
        ts = pd.Timestamp(t)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        return (now - ts.to_pydatetime()).total_seconds() / 60.0
    except (TypeError, ValueError):
        return 1e9


def _killzone_note(now: datetime) -> str | None:
    kz = smc.kill_zone(now)
    if kz["in"]:
        return f"{kz['name']} killzone"
    return None


# ----------------------------------------------------------------- hlines


def _key_levels(
    m15: dict | None, h1: dict | None, price: float,
    m1: pd.DataFrame | None = None,
) -> list[dict]:
    """PDH/PDL + POC + the liquidity pools a trader would mark first.

    D-047: strong time-at-price levels join the mark — every place the
    market SPENT >= 15 minutes becomes a labeled S/R line ("TPO·S 42m").
    """
    out: list[dict] = []
    seen: set[float] = set()

    def add(price_level: float, label: str, tone: str, style: str = "dash") -> None:
        if not price_level or not (1e-9 < price_level < 1e9):
            return
        for s in seen:  # dedupe near-identical levels
            if abs(s - price_level) < 0.05:
                return
        seen.add(price_level)
        out.append({
            "kind": "hline", "price": round(float(price_level), 2),
            "label": label, "tone": tone, "style": style,
        })

    # D-047 — time-at-price levels FIRST (they are the strongest marks:
    # the market itself proved these prices matter by spending time there)
    if m1 is not None and len(m1) >= 60:
        try:
            from app.analysis.tpo import tpo_profile

            prof = tpo_profile(m1)
            for lv in prof.get("levels") or []:
                if float(lv.get("minutes", 0)) < 20:
                    continue  # only meaningful marks make the chart
                side_tag = "S" if lv["side"] == "support" else "R"
                add(
                    lv["price"], f"TPO·{side_tag} {lv['minutes']:.0f}m",
                    "bull" if lv["side"] == "support" else "bear",
                    "solid",
                )
        except Exception:  # noqa: BLE001 — drawings must never break
            pass

    for snap in (h1, m15):
        if not snap or not snap.get("ok"):
            continue
        for lv in snap.get("liquidity", {}).get("levels", []):
            tag = lv.get("tag") or lv.get("kind", "")
            is_bsl = lv.get("kind") == "BSL"
            add(
                lv["price"],
                f"{tag}{' ×' + str(lv['hits']) if lv.get('hits', 1) > 1 else ''}",
                "bull" if is_bsl else "bear",
            )
    if m15 and m15.get("ok"):
        poc = (m15.get("volume_profile") or {}).get("poc")
        if poc:
            add(poc, "POC", "gold", "solid")
    # keep the ones a trader cares about: nearest above + below price,
    # PDH/PDL/POC + the strongest TPO marks
    above = sorted([d for d in out if d["price"] > price], key=lambda d: d["price"])[:4]
    below = sorted([d for d in out if d["price"] <= price], key=lambda d: -d["price"])[:4]
    keep = {id(d) for d in above + below}
    pdh_pdl_poc = [d for d in out if d["label"] in ("PDH", "PDL", "POC")]
    tpo_marks = [d for d in out if d["label"].startswith("TPO·")][:4]
    merged: list[dict] = []
    seen_ids: set[int] = set()
    for d in [d for d in out if id(d) in keep] + tpo_marks + pdh_pdl_poc:
        if id(d) not in seen_ids:
            seen_ids.add(id(d))
            merged.append(d)
    return merged[:10]


# -------------------------------------------------------------- trendlines


def _trendlines(m5: pd.DataFrame | None, atr5: float) -> list[dict]:
    """Last-two-swings trendlines (highs + lows), projected forward."""
    if m5 is None or len(m5) < 30 or atr5 <= 0:
        return []
    pts = ind.swings(m5, 2, 2)
    out: list[dict] = []
    for kind, tone, name in (("high", "bear", "TL·H"), ("low", "bull", "TL·L")):
        arr = [p for p in pts if p["kind"] == kind][-2:]
        if len(arr) < 2:
            continue
        p1, p2 = arr[0], arr[1]
        if p1["t"] == p2["t"]:
            continue
        t1 = pd.Timestamp(p1["t"])
        t2 = pd.Timestamp(p2["t"])
        dt = (t2 - t1).total_seconds()
        if dt <= 0:
            continue
        slope = (p2["price"] - p1["price"]) / dt  # price per second
        last_t = pd.Timestamp(m5["time_utc"].iloc[-1])
        proj = p2["price"] + slope * (last_t - t2).total_seconds()
        last_close = float(m5["c"].iloc[-1])
        broken = (
            last_close > proj + 0.25 * atr5 if kind == "high"
            else last_close < proj - 0.25 * atr5
        )
        out.append({
            "kind": "trendline",
            "t1": _iso(p1["t"]), "p1": round(float(p1["price"]), 2),
            "t2": _iso(p2["t"]), "p2": round(float(p2["price"]), 2),
            "label": name + (" ·broken" if broken else ""),
            "tone": tone, "broken": broken,
        })
    return out


# --------------------------------------------------------------------- fib


def _fib(m15: pd.DataFrame | None, pd_state: dict | None) -> dict | None:
    """Fibonacci retracement of the ACTIVE leg + ICT OTE band."""
    if m15 is None or len(m15) < 40:
        return None
    pts = ind.swings(m15, 2, 2)
    if len(pts) < 2:
        return None
    hi_i = int(m15["h"].iloc[-90:].idxmax()) if len(m15) >= 90 else int(m15["h"].idxmax())
    lo_i = int(m15["l"].iloc[-90:].idxmin()) if len(m15) >= 90 else int(m15["l"].idxmin())
    leg_up = hi_i > lo_i
    # anchor on confirmed swings nearest those extremes
    highs = [p for p in pts if p["kind"] == "high"]
    lows = [p for p in pts if p["kind"] == "low"]
    if leg_up:
        a = lows[-1] if lows else None
        b = highs[-1] if highs else None
    else:
        a = highs[-1] if highs else None
        b = lows[-1] if lows else None
    if a is None or b is None or a["t"] == b["t"]:
        return None
    p0, p1 = float(a["price"]), float(b["price"])
    if abs(p1 - p0) <= 1e-9:
        return None
    levels = []
    for r in FIB_RATIOS:
        price = p1 - r * (p1 - p0) if leg_up else p1 + r * (p0 - p1)
        levels.append({"ratio": r, "price": round(price, 2)})
    ote = (pd_state or {}).get("ote") if (pd_state or {}).get("ote") else None
    return {
        "kind": "fib",
        "t0": _iso(a["t"]), "p0": round(p0, 2),
        "t1": _iso(b["t"]), "p1": round(p1, 2),
        "dir": "up" if leg_up else "down",
        "levels": levels,
        "ote": [round(float(ote["lo"]), 2), round(float(ote["hi"]), 2)]
        if isinstance(ote, dict) and ote.get("lo") is not None else None,
        "tone": "gold",
    }


# ------------------------------------------------------------------- notes


def _notes(m1: pd.DataFrame | None, s1: dict | None, s5: dict | None,
           now: datetime) -> list[dict]:
    """Small chart annotations: BOS/CHoCH, sweeps, whale/manipulation."""
    out: list[dict] = []
    # structure events (M1 latest + M5 latest)
    for snap, weight in ((s1, "M1"), (s5, "M5")):
        if not snap or not snap.get("ok"):
            continue
        ev = (snap.get("structure") or {}).get("last_event")
        if ev and _age_min(ev.get("t"), now) < 180:
            arrow = "↑" if ev.get("dir") == "up" else "↓"
            txt = f"{ev.get('kind', 'BOS')}{arrow}"
            if ev.get("kind") == "CHoCH":
                txt += " trend shift"
            out.append({
                "kind": "note", "t": _iso(ev.get("t")),
                "price": round(float(ev.get("level", 0.0) or 0.0), 2),
                "text": f"{weight} {txt}", "tone": "bull" if ev.get("dir") == "up" else "bear",
            })
    # liquidity sweeps on the last M1 bars (manipulation)
    if s1 and s1.get("ok"):
        for sw in (s1.get("liquidity") or {}).get("sweeps", [])[-2:]:
            out.append({
                "kind": "note", "t": _iso(sw.get("t")),
                "price": round(float(sw.get("price", 0.0) or 0.0), 2),
                "text": f"{sw.get('kind')} SWEEP — manipulation",
                "tone": "violet",
            })
        # whale events (institutional entries / stop hunts / absorption)
        for ev in ((s1.get("whales") or {}).get("events") or [])[-3:]:
            if _age_min(ev.get("t"), now) > 120:
                continue
            tag = (
                "WHALE BUY" if ev.get("kind") == "momentum" and ev.get("side") == "buy"
                else "WHALE SELL" if ev.get("kind") == "momentum"
                else "STOP HUNT" if ev.get("kind") == "sweep"
                else "ABSORPTION"
            )
            out.append({
                "kind": "note", "t": _iso(ev.get("t")),
                "price": round(float(ev.get("price", 0.0) or 0.0), 2),
                "text": f"{tag} z{ev.get('vol_z', '?')}",
                "tone": "violet" if ev.get("kind") != "momentum"
                else ("bull" if ev.get("side") == "buy" else "bear"),
            })
    return out[-8:]


# ------------------------------------------------------------------- setup


def _candidate_zones(
    s5: dict | None, s15: dict | None, direction: str, price: float, atr5: float
) -> list[tuple[str, float, float, Any]]:
    """(tag, lo, hi, origin_t) zones that support `direction`."""
    zones: list[tuple[str, float, float, Any]] = []
    if atr5 <= 0:
        return zones
    want_bull = direction == "BUY"
    for snap in (s5, s15):
        if not snap or not snap.get("ok"):
            continue
        for z in snap.get("zones") or []:
            if (z.get("side") == "demand") == want_bull:
                zones.append((f"{z['side']} zone", float(z["lo"]), float(z["hi"]), z.get("t")))
        for ob in snap.get("order_blocks") or []:
            if (ob.get("side") == "bullish") == want_bull:
                zones.append(("OB retest", float(ob["lo"]), float(ob["hi"]), ob.get("t")))
        for g in snap.get("fvgs") or []:
            if g.get("filled"):
                continue
            if (g.get("side") == "bullish") == want_bull:
                zones.append(("FVG", float(g["lo"]), float(g["hi"]), g.get("t")))
    return zones


def _setup(
    frames: dict[str, pd.DataFrame],
    snaps: dict[str, dict],
    bias: dict,
    price: float,
    now: datetime,
    recent_signals: list[dict],
) -> dict | None:
    """The entry-setup drawing — the professional 'I'd take this trade' box.

    Fires only when a real confluence exists: MTF bias + price sitting in /
    near a supporting zone (demand/OB/FVG/OTE). That is exactly the user's
    directive — draw WHEN a good setup appears, not always.
    """
    m1, m5 = frames.get("M1"), frames.get("M5")
    s1, s5, s15 = snaps.get("M1"), snaps.get("M5"), snaps.get("M15")
    if m1 is None or m5 is None or s1 is None or not s1.get("ok"):
        return None
    atr5 = float(s5.get("atr") or 0.0) if s5 and s5.get("ok") else 0.0
    if atr5 <= 0:
        atr5 = float(s1.get("atr") or 0.0)
    if atr5 <= 0:
        return None

    b = bias.get("bias")
    s15_trend = (s15 or {}).get("structure", {}).get("trend") if s15 else None
    if b == "bullish":
        direction = "BUY"
    elif b == "bearish":
        direction = "SELL"
    elif s15_trend in ("bullish", "bearish"):
        direction = "BUY" if s15_trend == "bullish" else "SELL"
    else:
        return None

    zones = _candidate_zones(s5, s15, direction, price, atr5)
    # OTE band as an extra candidate zone
    pd_state = (s15 or {}).get("premium_discount") if s15 else None
    ote = (pd_state or {}).get("ote") if pd_state else None
    if isinstance(ote, dict) and ote.get("lo") is not None:
        leg = (pd_state or {}).get("leg")
        if (leg == "up") == (direction == "BUY"):
            zones.append(("OTE 0.62–0.79", float(ote["lo"]), float(ote["hi"]), None))

    # nearest supporting zone by distance to price
    best: tuple[float, str, float, float, Any] | None = None
    near = SETUP_NEAR_ATR * atr5
    for tag, lo, hi, t0 in zones:
        if hi < lo:
            lo, hi = hi, lo
        dist = 0.0 if (lo <= price <= hi) else min(abs(price - lo), abs(price - hi))
        if dist > near:
            continue
        if best is None or dist < best[0]:
            best = (dist, tag, lo, hi, t0)
    if best is None:
        return None
    _, tag, lo, hi, t0 = best
    if t0 is not None and _age_min(t0, now) > SETUP_MAX_AGE_MIN:
        t0 = m1["time_utc"].iloc[-1]
    entry = round((lo + hi) / 2.0, 2)

    # app-controlled SL: below/above the zone, protected beyond liquidity
    levels = ((s5 or {}).get("liquidity") or {}).get("levels") or []
    if direction == "BUY":
        sl = lo - 0.35 * atr5
        ssl_below = [lv["price"] for lv in levels
                     if lv.get("kind") == "SSL" and lo - 1.6 * atr5 < lv["price"] < lo]
        if ssl_below:
            sl = min(ssl_below) - 0.15 * atr5
        risk = entry - sl
        if risk <= 0:
            return None
        bsl_above = [lv["price"] for lv in levels
                     if lv.get("kind") == "BSL" and lv["price"] > entry + 0.5 * risk]
        tp = (min(bsl_above) - 0.15 * atr5) if bsl_above else entry + 1.5 * risk
        if (tp - entry) / risk < 0.8:
            tp = entry + 1.2 * risk
    else:
        sl = hi + 0.35 * atr5
        bsl_above = [lv["price"] for lv in levels
                     if lv.get("kind") == "BSL" and hi < lv["price"] < hi + 1.6 * atr5]
        if bsl_above:
            sl = max(bsl_above) + 0.15 * atr5
        risk = sl - entry
        if risk <= 0:
            return None
        ssl_below = [lv["price"] for lv in levels
                     if lv.get("kind") == "SSL" and lv["price"] < entry - 0.5 * risk]
        tp = (max(ssl_below) + 0.15 * atr5) if ssl_below else entry - 1.5 * risk
        if (entry - tp) / risk < 0.8:
            tp = entry - 1.2 * risk

    rr = round(abs(tp - entry) / risk, 2)

    # confluence factor tags (the "why" the trader writes on the chart)
    factors: list[str] = [tag]
    if s15 and s15.get("ok"):
        factors.append(f"M15 {s15.get('structure', {}).get('trend', '?')}")
    h1s = snaps.get("H1")
    if h1s and h1s.get("ok"):
        factors.append(f"H1 {h1s.get('structure', {}).get('trend', '?')}")
    whales = (s1.get("whales") or {})
    if whales.get("bias") == ("buy" if direction == "BUY" else "sell"):
        factors.append("whale flow agrees")
    sweeps = (s1.get("liquidity") or {}).get("sweeps") or []
    if sweeps:
        factors.append("recent sweep — manipulation")
    kz = _killzone_note(now)
    if kz:
        factors.append(kz)

    # triggered? an engine signal for this direction fired recently
    status = "forming"
    sig_note = None
    for sig in recent_signals or []:
        try:
            st = pd.Timestamp(sig.get("ts"))
            if st.tzinfo is None:
                st = st.tz_localize("UTC")
            age = (now - st.to_pydatetime()).total_seconds() / 60.0
        except (TypeError, ValueError):
            continue
        if 0 <= age <= TRIGGER_WINDOW_MIN and sig.get("direction") == direction:
            status = "triggered"
            sig_note = f"entry taken @ {sig.get('entry')}"
            break

    pd_state_txt = "range"
    if s15 and s15.get("ok"):
        pd_state_txt = (s15.get("premium_discount") or {}).get("state") or "range"
    note = f"{direction} setup — {tag} in {pd_state_txt}"
    side_word = "below" if direction == "BUY" else "above"
    note += f" · SL {side_word} zone · TP at liquidity · RR {rr}"
    if sig_note:
        note += f" · {sig_note}"

    return {
        "kind": "setup",
        "dir": direction,
        "zone": [round(lo, 2), round(hi, 2)],
        "entry": entry,
        "sl": round(sl, 2),
        "tp": round(tp, 2),
        "rr": rr,
        "t0": _iso(t0 if t0 is not None else m1["time_utc"].iloc[-1]),
        "status": status,
        "factors": factors[:6],
        "note": note,
    }


# ------------------------------------------------------------------- entry


def build_drawings(
    frames: dict[str, pd.DataFrame],
    snaps: dict[str, dict],
    price: float,
    recent_signals: list[dict] | None = None,
) -> list[dict]:
    """All chart drawings for one symbol snapshot (bounded, relevant-only)."""
    try:
        return _build(frames, snaps, price, recent_signals or [])
    except Exception:  # noqa: BLE001 — drawings must NEVER break /api/analysis
        logger.exception("drawings build failed")
        return []


def _build(
    frames: dict[str, pd.DataFrame],
    snaps: dict[str, dict],
    price: float,
    recent_signals: list[dict],
) -> list[dict]:
    m1 = frames.get("M1")
    if m1 is None or len(m1) < 20 or not (price and price > 0):
        return []
    now = datetime.now(UTC)
    s1 = snaps.get("M1")
    s5 = snaps.get("M5")
    s15 = snaps.get("M15")
    h1 = snaps.get("H1")
    if s1 is None or not s1.get("ok"):
        return []

    out: list[dict] = []

    setup = _setup(frames, snaps, mtf_bias(snaps), price, now, recent_signals)
    if setup is not None:
        out.append(setup)

    out.extend(_key_levels(s15, h1, price, m1=m1))
    atr5 = float(s5.get("atr") or 0.0) if s5 and s5.get("ok") else 0.0
    out.extend(_trendlines(frames.get("M5"), atr5))
    fib = _fib(frames.get("M15"), (s15 or {}).get("premium_discount") if s15 else None)
    if fib is not None:
        out.append(fib)
    out.extend(_notes(m1, s1, s5, now))

    return out[:MAX_DRAWINGS]
