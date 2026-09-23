"""D-043/D-052 — Professional auto-drawing engine (user directive, Bengali):

"একজন প্রফেশনাল ট্রেডার যেভাবে তার চার্ট এনালাইসিস করার জন্য ড্রয়িং করে —
হরাইজন্টাল লাইন, ট্রেন্ড লাইন, fibonacchi… সব সময় ড্রয়িং করবে না।"

D-052 (user directive, Bengali): the drawings must look like the reference
screenshots the user supplied — labeled zone boxes ("Supply zone", "Demand
zone", "FVG"), liquidity-sweep lines with words, BOS / CHoCH labels,
channels with a median line, trend lines with full-word labels and
projection arrows. Everything is drawn on the ACTIVE timeframe's recent
80–150 candles ("এই ড্রয়িং গুলো রিসেন্ট 80 থেকে 150 ক্যান্ডেল এ দেখলেই হবে"),
and survives a timeframe switch because every timeframe gets its OWN
drawing set computed from its OWN frame ("টাইম ফ্রম পরিবর্তন করলেও ড্রয়িং
নষ্ট হবে না") — recomputed on every /api/analysis snapshot (~20s cache)
so the marks keep following the market ("কিছুক্ষণ পর পর এই ড্রয়িং গুলো চলতে
থাকবে মার্কেট এর সাথে").

Drawing primitives the chart renders on its overlay canvas:

    hline      — Support / Resistance / liquidity levels with FULL-WORD labels
    zone       — labeled supply/demand/order-block/FVG boxes (rect + label)
    trendline  — last two swing highs / lows, projected forward, broken flag
    channel    — parallel upper/lower lines + dashed median, direction label
    sweep      — "Liquidity Sweep High/Low" markers at swept pools
    structure  — BOS / CHoCH event chips anchored on the break candle
    arrow      — direction projection arrows at sweep / structure events
    fib        — retracement of the active leg + OTE (0.62–0.79) band
    setup      — the ENTRY SETUP box: zone + app-controlled SL/TP + factors

No short cryptic tags anywhere (user directive: "Full meaning লিখলে ভালো
হয়") — every label is a worded sentence a human trader would write.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import pandas as pd

from app.analysis import indicators as ind
from app.analysis import smc
from app.analysis.context import mtf_bias
from app.analysis.setup_geometry import setup_geometry

logger = logging.getLogger("xauusd.drawings")

#: hard cap — a pro chart never carries more than this many marks
MAX_DRAWINGS = 34
#: drawings live on the recent 80–150 candles of the ACTIVE timeframe
DRAW_WINDOW_BARS = 150
#: how close (in ATR units) price must be to a zone for the SETUP box
SETUP_NEAR_ATR = 0.75
#: a setup drawing expires once its zone origin is older than this
SETUP_MAX_AGE_MIN = 240
#: recent-signal window marking a forming setup as triggered
TRIGGER_WINDOW_MIN = 25

FIB_RATIOS = (0.236, 0.382, 0.5, 0.618, 0.786)

#: timeframe ladder used to pick the "one higher" HTF context per view
TF_ORDER = ("M1", "M5", "M15", "M30", "H1", "H4", "D1")


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


def _next_tf(tf: str) -> str | None:
    """The one-step-higher timeframe (None at the top of the ladder)."""
    try:
        i = TF_ORDER.index(tf)
    except ValueError:
        return "M15"
    return TF_ORDER[i + 1] if i + 1 < len(TF_ORDER) else None


def _in_window(t: Any, t_start: pd.Timestamp) -> bool:
    """Does a drawing anchor sit inside the visible 80–150 bar window?"""
    if t is None:
        return True
    try:
        ts = pd.Timestamp(t)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        return ts >= t_start
    except (TypeError, ValueError):
        return False


# ----------------------------------------------------------------- hlines


def _key_levels(
    snap: dict | None, snap_htf: dict | None, price: float,
    m1: pd.DataFrame | None = None, tf: str = "M1",
) -> list[dict]:
    """Previous-day H/L + POC + liquidity pools with FULL-WORD labels.

    These are the first lines a trader draws: the levels where resting
    liquidity sits. TPO time-at-price marks only intraday views (they are
    M1-derived micro levels — noise on H4).
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

    # time-at-price levels — the market PROVED these prices matter
    if m1 is not None and len(m1) >= 60 and tf in ("M1", "M5", "M15"):
        try:
            from app.analysis.tpo import tpo_profile

            prof = tpo_profile(m1)
            for lv in prof.get("levels") or []:
                if float(lv.get("minutes", 0)) < 25:
                    continue
                side_word = "Support" if lv["side"] == "support" else "Resistance"
                add(
                    lv["price"],
                    f"Time at Price · {side_word} {lv['minutes']:.0f}m",
                    "bull" if lv["side"] == "support" else "bear",
                    "solid",
                )
        except Exception:  # noqa: BLE001 — drawings must never break
            pass

    for src in (snap, snap_htf):
        if not src or not src.get("ok"):
            continue
        for lv in src.get("liquidity", {}).get("levels", []):
            tag = lv.get("tag")
            if tag == "PDH":
                label = "Previous Day High"
            elif tag == "PDL":
                label = "Previous Day Low"
            else:
                kind_word = (
                    "Buy Side Liquidity" if lv.get("kind") == "BSL"
                    else "Sell Side Liquidity"
                )
                label = kind_word + (
                    f" · swept {lv['hits']}x" if lv.get("hits", 1) > 1 else ""
                )
            add(
                lv["price"], label,
                "bull" if lv.get("kind") == "BSL" else "bear",
            )
    if snap and snap.get("ok"):
        poc = (snap.get("volume_profile") or {}).get("poc")
        if poc:
            add(poc, "Point of Control (POC)", "gold", "solid")
    # keep the ones a trader cares about: nearest above + below price,
    # PDH/PDL/POC + the strongest time-at-price marks
    above = sorted([d for d in out if d["price"] > price], key=lambda d: d["price"])[:4]
    below = sorted([d for d in out if d["price"] <= price], key=lambda d: -d["price"])[:4]
    keep = {id(d) for d in above + below}
    anchors = [
        d for d in out if d["label"] in
        ("Previous Day High", "Previous Day Low", "Point of Control (POC)")
    ]
    tpo_marks = [
        d for d in out if d["label"].startswith("Time at Price")
    ][:3]
    merged: list[dict] = []
    seen_ids: set[int] = set()
    for d in [d for d in out if id(d) in keep] + tpo_marks + anchors:
        if id(d) not in seen_ids:
            seen_ids.add(id(d))
            merged.append(d)
    return merged[:10]


# ------------------------------------------------------------------- zones


def _zone_drawings(
    snaps: dict[str, dict], tf: str, t_start: pd.Timestamp, price: float
) -> list[dict]:
    """Labeled zone boxes — exactly like the reference screenshots.

    Active-TF supply/demand zones, order blocks and unfilled FVGs, plus
    the one-step-higher timeframe's zones tagged "· HTF" (the grey HTF
    boxes from the reference images). Full-word labels only.
    """
    out: list[dict] = []
    sources = [tf]
    htf = _next_tf(tf)
    if htf:
        sources.append(htf)

    for source_tf in sources:
        s = snaps.get(source_tf)
        if not s or not s.get("ok"):
            continue
        is_htf = source_tf != tf
        tag = " · higher timeframe" if is_htf else ""

        for z in (s.get("zones") or [])[-4:]:
            if not _in_window(z.get("t"), t_start):
                continue
            side = z.get("side")
            label = ("Supply Zone" if side == "supply" else "Demand Zone") + tag
            out.append({
                "kind": "zone", "side": side or "demand",
                "lo": round(float(z["lo"]), 2), "hi": round(float(z["hi"]), 2),
                "t": _iso(z.get("t")), "label": label,
                "tone": "bear" if side == "supply" else "bull",
                "source_tf": source_tf,
            })
        for ob in (s.get("order_blocks") or [])[-3:]:
            if not _in_window(ob.get("t"), t_start):
                continue
            side = ob.get("side")
            label = (
                "Bullish Order Block" if side == "bullish" else "Bearish Order Block"
            ) + tag + (" · tested" if ob.get("mitigated") else "")
            out.append({
                "kind": "zone",
                "side": "ob_bull" if side == "bullish" else "ob_bear",
                "lo": round(float(min(ob["lo"], ob["hi"])), 2),
                "hi": round(float(max(ob["lo"], ob["hi"])), 2),
                "t": _iso(ob.get("t")), "label": label,
                "tone": "bull" if side == "bullish" else "bear",
                "source_tf": source_tf,
                # D-053 — mitigated blocks render THIN (user directive:
                # "মোছে যাওয়া অঙ্কনগুলোর লেখা চিকন")
                "state": "faded" if ob.get("mitigated") else "active",
            })
        for g in (s.get("fvgs") or [])[-3:]:
            if g.get("filled"):
                continue
            if not _in_window(g.get("t"), t_start):
                continue
            side = g.get("side")
            label = (
                "Fair Value Gap · bullish" if side == "bullish"
                else "Fair Value Gap · bearish"
            ) + tag
            out.append({
                "kind": "zone",
                "side": "fvg_bull" if side == "bullish" else "fvg_bear",
                "lo": round(float(min(g["lo"], g["hi"])), 2),
                "hi": round(float(max(g["lo"], g["hi"])), 2),
                "t": _iso(g.get("t")), "label": label,
                "tone": "bull" if side == "bullish" else "bear",
                "source_tf": source_tf,
            })
    return out


# -------------------------------------------------------------- trendlines


def _trendlines(df: pd.DataFrame | None, atr: float) -> list[dict]:
    """Last-two-swing trendlines with FULL-WORD labels, projected forward."""
    if df is None or len(df) < 30 or atr <= 0:
        return []
    pts = ind.swings(df, 2, 2)
    out: list[dict] = []
    for kind, tone, name in (
        ("high", "bear", "Bearish Trend Line"),
        ("low", "bull", "Bullish Trend Line"),
    ):
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
        last_t = pd.Timestamp(df["time_utc"].iloc[-1])
        proj = p2["price"] + slope * (last_t - t2).total_seconds()
        last_close = float(df["c"].iloc[-1])
        broken = (
            last_close > proj + 0.25 * atr if kind == "high"
            else last_close < proj - 0.25 * atr
        )
        out.append({
            "kind": "trendline",
            "t1": _iso(p1["t"]), "p1": round(float(p1["price"]), 2),
            "t2": _iso(p2["t"]), "p2": round(float(p2["price"]), 2),
            "label": name + (" · broken" if broken else ""),
            "tone": tone, "broken": broken,
            # D-053 — broken lines render THIN + faded
            "state": "faded" if broken else "active",
        })
    return out


def _channel(df: pd.DataFrame | None) -> dict | None:
    """Ascending/descending channel: upper + lower parallel lines + median.

    Drawn from the last TWO swing highs and TWO swing lows of the active
    timeframe — the reference screenshots' descending channels with the
    dashed mid-line.
    """
    if df is None or len(df) < 40:
        return None
    pts = ind.swings(df, 3, 3)
    highs = [p for p in pts if p["kind"] == "high"][-2:]
    lows = [p for p in pts if p["kind"] == "low"][-2:]
    if len(highs) < 2 or len(lows) < 2:
        return None
    h1, h2 = highs
    l1, l2 = lows

    def line(a: dict, b: dict) -> tuple[str, float, str, float] | None:
        if a["t"] == b["t"]:
            return None
        t1 = pd.Timestamp(a["t"])
        t2 = pd.Timestamp(b["t"])
        if (t2 - t1).total_seconds() <= 0:
            return None
        return (
            _iso(a["t"]), round(float(a["price"]), 2),
            _iso(b["t"]), round(float(b["price"]), 2),
        )

    upper = line(h1, h2)
    lower = line(l1, l2)
    if not upper or not lower:
        return None
    # direction from the upper line slope (both should agree for a channel)
    up_slope = h2["price"] > h1["price"] and l2["price"] > l1["price"]
    down_slope = h2["price"] < h1["price"] and l2["price"] < l1["price"]
    if not up_slope and not down_slope:
        return None
    label = "Ascending Channel" if up_slope else "Descending Channel"
    # median through the midpoint of the two line midpoints
    m1 = (h1["price"] + l1["price"]) / 2.0
    m2 = (h2["price"] + l2["price"]) / 2.0
    median = {
        "t1": _iso(h1["t"]), "p1": round(float(m1), 2),
        "t2": _iso(h2["t"]), "p2": round(float(m2), 2),
    }
    return {
        "kind": "channel", "dir": "up" if up_slope else "down",
        "label": label, "tone": "bull" if up_slope else "bear",
        "upper": {"t1": upper[0], "p1": upper[1], "t2": upper[2], "p2": upper[3]},
        "lower": {"t1": lower[0], "p1": lower[1], "t2": lower[2], "p2": lower[3]},
        "median": median,
    }


# ------------------------------------------------------ sweeps / structure


def _sweeps(snap: dict | None) -> list[dict]:
    """Liquidity sweep markers — "stop hunt" lines from the reference shots."""
    if not snap or not snap.get("ok"):
        return []
    out = []
    for sw in (snap.get("liquidity") or {}).get("sweeps", [])[-2:]:
        is_high = sw.get("kind") == "BSL"
        out.append({
            "kind": "sweep", "t": _iso(sw.get("t")),
            "price": round(float(sw.get("price", 0.0) or 0.0), 2),
            "side": "high" if is_high else "low",
            "label": "Liquidity Sweep High" if is_high else "Liquidity Sweep Low",
            "tone": "bear" if is_high else "bull",
        })
    return out


def _structure_events(snap: dict | None, t_start: pd.Timestamp) -> list[dict]:
    """BOS / CHoCH chips anchored on the break candle — full words."""
    if not snap or not snap.get("ok"):
        return []
    out = []
    events = (snap.get("structure") or {}).get("events") or []
    for ev in events[-4:]:
        if not _in_window(ev.get("t"), t_start):
            continue
        up = ev.get("dir") == "up"
        word = "Break of Structure" if ev.get("kind") == "BOS" else "Change of Character"
        kind = "BOS" if ev.get("kind") == "BOS" else "CHoCH"
        arrow = "↑" if up else "↓"
        out.append({
            "kind": "structure", "t": _iso(ev.get("t")),
            "price": round(float(ev.get("level", 0.0) or 0.0), 2),
            "dir": "up" if up else "down",
            "label": f"{kind} {arrow} — {word}",
            "tone": "bull" if up else "bear",
        })
    return out


def _arrows(snap: dict | None, price: float) -> list[dict]:
    """Direction projection arrows at the freshest sweeps / structure events.

    A swept BSL (highs taken) projects DOWN (reversal), a swept SSL projects
    UP; the last BOS/CHoCH projects in its own direction (continuation).
    """
    out: list[dict] = []
    if not snap or not snap.get("ok"):
        return out
    liq = snap.get("liquidity") or {}
    for sw in liq.get("sweeps", [])[-1:]:
        out.append({
            "kind": "arrow", "t": _iso(sw.get("t")),
            "price": round(float(sw.get("price", 0.0) or 0.0), 2),
            "dir": "down" if sw.get("kind") == "BSL" else "up",
            "label": (
                "Sweep reversal · sell" if sw.get("kind") == "BSL"
                else "Sweep reversal · buy"
            ),
            "tone": "bear" if sw.get("kind") == "BSL" else "bull",
        })
    ev = (snap.get("structure") or {}).get("last_event")
    if ev is not None:
        up = ev.get("dir") == "up"
        out.append({
            "kind": "arrow", "t": _iso(ev.get("t")),
            "price": round(float(ev.get("level", 0.0) or 0.0), 2),
            "dir": "up" if up else "down",
            "label": "Structure continuation · buy" if up else "Structure continuation · sell",
            "tone": "bull" if up else "bear",
        })
    return out[:2]


# --------------------------------------------------------------------- fib


def _fib(df: pd.DataFrame | None, pd_state: dict | None) -> dict | None:
    """Fibonacci retracement of the ACTIVE leg + ICT OTE band."""
    if df is None or len(df) < 40:
        return None
    pts = ind.swings(df, 2, 2)
    if len(pts) < 2:
        return None
    hi_i = int(df["h"].iloc[-90:].idxmax()) if len(df) >= 90 else int(df["h"].idxmax())
    lo_i = int(df["l"].iloc[-90:].idxmin()) if len(df) >= 90 else int(df["l"].idxmin())
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


# ------------------------------------------------------------------- setup


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

    D-057: the ENTRY/SL/TP geometry comes from the SHARED module
    (app.analysis.setup_geometry) — the exact same numbers the signal
    engine places its order at, so the box the user watches IS the trade
    the broker receives. Once a signal fires, the box mirrors the
    signal's actual levels verbatim ("সিগন্যাল গুলো এই ড্রয়িং ফলো করে
    আসবে ... SL TP ENTRY সব কিছু এই চার্ট ফলো করে হবে").
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

    # D-057 — the SHARED geometry: the same entry/SL/TP the signal engine
    # places its order at (setup_geometry is the single source of truth)
    geo = setup_geometry(s5, s15, direction, price, atr_fallback=atr5)
    if geo is None:
        return None
    tag, lo, hi, t0 = geo["tag"], geo["lo"], geo["hi"], geo["t0"]
    entry, sl, tp, rr = geo["entry"], geo["sl"], geo["tp"], geo["rr"]
    if t0 is not None and _age_min(t0, now) > SETUP_MAX_AGE_MIN:
        t0 = m1["time_utc"].iloc[-1]

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

    # triggered? an engine signal for this direction fired recently:
    # the box then mirrors the FIRED signal's actual levels — the box IS
    # the trade contract, so after the trigger it shows exactly what the
    # broker holds (entry/SL/TP of the live order)
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
            try:
                entry = round(float(sig["entry"]), 2)
                sl = round(float(sig["sl"]), 2)
                tp = round(float(sig["tp"]), 2)
                rr = round(float(sig.get("rr") or rr), 2)
                sig_note = f"order live @ {entry}"
            except (KeyError, TypeError, ValueError):
                sig_note = f"entry taken @ {sig.get('entry')}"
            break

    pd_state_txt = "range"
    if s15 and s15.get("ok"):
        pd_state_txt = (s15.get("premium_discount") or {}).get("state") or "range"
    note = f"{direction} setup — {tag} in {pd_state_txt}"
    side_word = "below" if direction == "BUY" else "above"
    note += f" · SL {side_word} zone · TP at drawn target · RR {rr}"
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
    tf: str = "M1",
) -> list[dict]:
    """All chart drawings for one symbol snapshot (bounded, relevant-only).

    D-052: `tf` selects the ACTIVE view's drawing set — every mark is
    anchored on that timeframe's recent DRAW_WINDOW_BARS candles, so the
    overlay looks right on M1, M5, M15, H1 and H4 alike and survives
    timeframe switches (each TF gets its own set from analysis.py).
    """
    try:
        return _build(frames, snaps, price, recent_signals or [], tf)
    except Exception:  # noqa: BLE001 — drawings must NEVER break /api/analysis
        logger.exception("drawings build failed")
        return []


def _build(
    frames: dict[str, pd.DataFrame],
    snaps: dict[str, dict],
    price: float,
    recent_signals: list[dict],
    tf: str,
) -> list[dict]:
    base = frames.get(tf)
    if base is None or len(base) < 20 or not (price and price > 0):
        return []
    now = datetime.now(UTC)
    snap = snaps.get(tf)
    if snap is None or not snap.get("ok"):
        return []

    # the visible window: last DRAW_WINDOW_BARS candles of THIS timeframe
    win = base.tail(DRAW_WINDOW_BARS)
    t_start = pd.Timestamp(win["time_utc"].iloc[0])
    htf = _next_tf(tf)
    snap_htf = snaps.get(htf) if htf else None

    atr = float(snap.get("atr") or 0.0)
    if atr <= 0:
        atr = float((snap_htf or {}).get("atr") or 0.0)
    if atr <= 0:
        atr = float(base["c"].tail(20).std() or 0.0)

    out: list[dict] = []

    # 1. the entry-setup box (engine-level, M1-driven — only when a real
    #    confluence exists; drawn BEFORE entry, marked once triggered)
    setup = _setup(frames, snaps, mtf_bias(snaps), price, now, recent_signals)
    if setup is not None:
        out.append(setup)

    # 2. key horizontal levels (Support/Resistance/liquidity/POC)
    out.extend(_key_levels(snap, snap_htf, price, m1=frames.get("M1"), tf=tf))

    # 3. labeled zone boxes (S/D + OB + FVG, active TF + one HTF)
    out.extend(_zone_drawings(snaps, tf, t_start, price))

    # 4. channel (upper + lower + median)
    channel = _channel(base)
    if channel is not None:
        out.append(channel)

    # 5. trendlines projected forward
    out.extend(_trendlines(base, atr))

    # 6. fibonacci retracement — HTF context for intraday views, the
    #    active TF itself for M15+
    fib_src = base if tf in ("M15", "H1", "H4", "D1") else frames.get("M15")
    fib = _fib(fib_src, (snap.get("premium_discount") if snap else None))
    if fib is not None:
        out.append(fib)

    # 7. liquidity sweeps + 8. BOS/CHoCH structure chips + 9. arrows
    out.extend(_sweeps(snap))
    out.extend(_structure_events(snap, t_start))
    out.extend(_arrows(snap, price))

    return out[:MAX_DRAWINGS]
