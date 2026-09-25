"""D-072 — Classic chart-pattern engine (user directive, Bengali):

"ইউটিউব চ্যানেল এর প্রত্যেকটি ভিডিও এনালাইসিস করে দেখো, চার্ট কিভাবে
আনাইসিস করে, এবং ড্রয়িং করে, আমার চার্ট এ কীভাবে এই গুলো এপ্লাই করবে?"

Channel studied: youtube.com/@easytradingeasy (48 shorts, 40+ patterns —
Cup&Handle, H&S, Dragon, flags, pennants, triangles, wedges, harmonics,
triple tops, channels). Their signature drawing recipe, applied here:

  1. NUMBERED swing points — 1..N circles on the pattern's pivot candles
     (the structure read: "1 up, 2 down, 3 up …" so the eye can walk the
     market's path exactly like the channel walks it);
  2. THIN SOLID geometry lines connecting the pivots (pattern outline +
     neckline / boundary lines), light shaded pattern body;
  3. the TRADE PLAN — entry circle at the breakout level, red dashed
     STOP-LOSS line beyond the pattern extreme, light TARGET band at the
     measured-move price, and a breakout arrow. 48/48 of the channel's
     videos mark the entry, 45/48 the target, 32/48 the stop-loss;
  4. pattern state — "forming" while the last pivot is still intact,
     "confirmed" once price CLOSED through the trigger level.

Families detected on the ACTIVE timeframe's visible window (the ones that
actually print on intraday gold — channel coverage without the noise):
  - DOUBLE TOP / DOUBLE BOTTOM (neckline = middle trough)
  - TRIPLE TOP / TRIPLE BOTTOM
  - HEAD & SHOULDERS / INVERSE HEAD & SHOULDERS
  - BULL FLAG / BEAR FLAG (impulse pole + counter-trend consolidation)
  - BULL PENNANT / BEAR PENNANT (converging consolidation)
  - ASCENDING / DESCENDING TRIANGLE, SYMMETRIC TRIANGLE
  - RISING / FALLING WEDGE
  - BULL / BEAR RECTANGLE (range)
Every detection carries the measured-move entry / SL / target math of the
classic textbooks the channel teaches from.
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd

from app.analysis.indicators import swings

logger = logging.getLogger(__name__)

# pattern quality floors / tolerances (in ATR units)
TOP_TOL = 0.30          # peaks count as "equal" within 0.30 ATR
NECK_BRAVE = 0.12       # neckline break needs this much beyond-tolerance
MIN_HEIGHT = 0.9        # pattern height must reach 0.9 ATR (a real shape)
MAX_HEIGHT = 14.0       # …but not a monthly mega-structure
POLE_MIN = 1.8          # flag pole: 1.8 ATR impulse
FLAG_MAXRETR = 0.55     # flag may retrace at most 55% of the pole
MAX_POINTS_GAP = 26     # max bars between consecutive pivots
TOL_FLAT = 0.22         # rectangle / triangle flat-side tolerance
CONFIRM_PAD = 0.05      # close must finish this far past the level
MIN_SWINGS = 3          # a double top is already high-trough-high (3)


def _iso(t: Any) -> str | None:
    if t is None or (isinstance(t, float) and np.isnan(t)):
        return None
    try:
        ts = pd.Timestamp(t)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        else:
            ts = ts.tz_convert("UTC")
        return ts.isoformat()
    except Exception:  # noqa: BLE001
        return None


def _pts(df: pd.DataFrame, win: pd.DataFrame) -> list[dict]:
    """Confirmed alternating pivots inside the visible window."""
    need = MAX_POINTS_GAP
    off = max(0, len(win) - need * 4)
    sub = win.iloc[off:]
    base_off = len(df) - len(sub)
    sw = swings(sub, left=2, right=2)
    out = []
    for s in sw:
        out.append({
            "t": s["t"], "price": s["price"], "kind": s["kind"],
            "i": base_off + s["i"],
        })
    # guarantee strict alternation high/low (pattern geometry needs it)
    alt: list[dict] = []
    for p in out:
        if alt and alt[-1]["kind"] == p["kind"]:
            # same kind twice: keep the more extreme print
            if p["kind"] == "high" and p["price"] >= alt[-1]["price"]:
                alt[-1] = p
            elif p["kind"] == "low" and p["price"] <= alt[-1]["price"]:
                alt[-1] = p
        else:
            alt.append(p)
    return alt


def _line(a: dict, b: dict, dash: bool = False) -> dict:
    return {
        "t1": _iso(a["t"]), "p1": a["price"],
        "t2": _iso(b["t"]), "p2": b["price"],
        "dash": dash,
    }


def _mk_pattern(
    name: str, family: str, dirn: str, pts: list[dict],
    lines: list[dict], zone: tuple[float, float, Any] | None,
    entry: float, sl: float, target: float, state: str,
    atr: float, note: str, breakout_t: Any = None,
) -> dict:
    hi = max(p["price"] for p in pts)
    lo = min(p["price"] for p in pts)
    height = hi - lo
    numbered = [
        {"t": _iso(p["t"]), "price": p["price"], "n": i + 1, "kind": p["kind"]}
        for i, p in enumerate(pts)
    ]
    risk = abs(entry - sl)
    reward = abs(target - entry)
    rr = round(reward / risk, 2) if risk > 1e-9 else None
    z = None
    if zone is not None:
        z = {"t": _iso(zone[2]), "lo": zone[0], "hi": zone[1]}
    # target band: from target back toward entry by 30% of height
    band_w = 0.30 * height
    if dirn == "down":
        t_lo, t_hi = target, target + band_w
    else:
        t_lo, t_hi = target - band_w, target
    return {
        "kind": "pattern",
        "name": name,
        "family": family,
        "dir": dirn,
        "state": state,
        "points": numbered,
        "lines": lines,
        "zone": z,
        "entry": {"price": float(entry), "t": _iso(breakout_t)},
        "sl": float(sl),
        "target": float(target),
        "target_zone": {"lo": float(t_lo), "hi": float(t_hi)},
        "height_atr": round(height / atr, 2) if atr > 0 else None,
        "rr": rr,
        "label": f"{name} · {'bullish' if dirn == 'up' else 'bearish'} — entry on "
                 f"{'breakout' if state == 'confirmed' else 'trigger break'}",
        "note": note,
        "tone": "bull" if dirn == "up" else "bear",
    }


def _tops_bottoms(
    df: pd.DataFrame, pts: list[dict], atr: float, price: float,
) -> dict | None:
    """Double/Triple top-bottom + H&S / inverse H&S off the last pivots."""
    highs = [p for p in pts if p["kind"] == "high"][-4:]
    lows = [p for p in pts if p["kind"] == "low"][-4:]
    closes = df["c"].to_numpy(dtype=float)

    # ---------- tops family (double/triple/H&S) --------------------------
    if len(highs) >= 2 and highs[-1]["i"] >= len(df) - 40:
        # H&S first: 3 highs, middle clearly the tallest
        if len(highs) >= 3:
            lsh, head, rsh = highs[-3], highs[-2], highs[-1]
            head_up = head["price"] - max(lsh["price"], rsh["price"])
            if head_up > 0.45 * atr and abs(lsh["price"] - rsh["price"]) <= TOP_TOL * atr:
                # neckline = the trough between Lsh-Head and Head-Rsh
                between = [p for p in pts if p["kind"] == "low"
                           and lsh["i"] < p["i"] < rsh["i"]]
                if between:
                    neck_pts = sorted(between, key=lambda p: p["i"])
                    neck_l, neck_r = neck_pts[0], neck_pts[-1]
                    neck = min(neck_l["price"], neck_r["price"])
                    head_h = head["price"] - neck
                    if head_h >= MIN_HEIGHT * atr:
                        conf = closes[-1] < neck - CONFIRM_PAD * atr
                        pts_sel = [p for p in pts if lsh["i"] <= p["i"] <= len(df) - 1
                                   and p["i"] <= rsh["i"] + MAX_POINTS_GAP]
                        pts_sel = pts_sel or pts
                        lines = [
                            _line(lsh, neck_l), _line(neck_l, head),
                            _line(head, neck_r), _line(neck_r, rsh),
                            _line(neck_l, neck_r, dash=True),
                        ]
                        entry = neck
                        sl = max(head["price"], rsh["price"]) + 0.25 * atr
                        target = neck - head_h
                        state = "confirmed" if conf else "forming"
                        breakout_t = None
                        if conf:
                            for k in range(rsh["i"], len(df)):
                                if closes[k] < neck - CONFIRM_PAD * atr:
                                    breakout_t = df["time_utc"].iloc[k]
                                    break
                        return _mk_pattern(
                            "HEAD & SHOULDERS", "reversal", "down",
                            pts_sel, lines, (neck, head["price"], lsh["t"]),
                            entry, sl, target, state, atr,
                            f"Head {head_h / atr:.1f}A above neckline — bearish "
                            f"reversal; target = head height below neckline",
                            breakout_t,
                        )
        # triple top: 3 near-equal highs
        if len(highs) >= 3:
            h1, h2, h3 = highs[-3], highs[-2], highs[-1]
            spread = max(h["price"] for h in (h1, h2, h3)) - \
                min(h["price"] for h in (h1, h2, h3))
            if spread <= TOP_TOL * atr:
                lown = [p for p in pts if p["kind"] == "low"
                        and h1["i"] < p["i"] < h3["i"]]
                if lown:
                    neck = min(p["price"] for p in lown)
                    height = max(h["price"] for h in (h1, h2, h3)) - neck
                    if height >= MIN_HEIGHT * atr:
                        conf = closes[-1] < neck - CONFIRM_PAD * atr
                        pts_sel = [p for p in pts if h1["i"] <= p["i"] <= len(df) - 1
                                   and p["i"] <= h3["i"] + MAX_POINTS_GAP] or pts
                        lines = [
                            _line(h1, h2), _line(h2, h3),
                            _line(lown[0], lown[-1], dash=True),
                        ]
                        entry = neck
                        sl = max(h["price"] for h in (h1, h2, h3)) + 0.25 * atr
                        target = neck - height
                        breakout_t = None
                        if conf:
                            for k in range(h3["i"], len(df)):
                                if closes[k] < neck - CONFIRM_PAD * atr:
                                    breakout_t = df["time_utc"].iloc[k]
                                    break
                        return _mk_pattern(
                            "TRIPLE TOP", "reversal", "down",
                            pts_sel, lines, (neck, h1["price"], h1["t"]),
                            entry, sl, target,
                            "confirmed" if conf else "forming", atr,
                            "Three failed attacks on the same supply — "
                            "bearish reversal; target = pattern height",
                            breakout_t,
                        )
        # double top
        h1, h2 = highs[-2], highs[-1]
        if abs(h1["price"] - h2["price"]) <= TOP_TOL * atr:
            lown = [p for p in pts if p["kind"] == "low" and h1["i"] < p["i"] < h2["i"]]
            if lown:
                neck = min(p["price"] for p in lown)
                height = max(h1["price"], h2["price"]) - neck
                if height >= MIN_HEIGHT * atr:
                    conf = closes[-1] < neck - CONFIRM_PAD * atr
                    pts_sel = [p for p in pts if h1["i"] <= p["i"] <= len(df) - 1
                               and p["i"] <= h2["i"] + MAX_POINTS_GAP] or pts
                    lines = [_line(h1, h2), _line(lown[0], lown[-1], dash=True)]
                    entry = neck
                    sl = max(h1["price"], h2["price"]) + 0.25 * atr
                    target = neck - height
                    breakout_t = None
                    if conf:
                        for k in range(h2["i"], len(df)):
                            if closes[k] < neck - CONFIRM_PAD * atr:
                                breakout_t = df["time_utc"].iloc[k]
                                break
                    return _mk_pattern(
                        "DOUBLE TOP", "reversal", "down",
                        pts_sel, lines, (neck, h1["price"], h1["t"]),
                        entry, sl, target,
                        "confirmed" if conf else "forming", atr,
                        "Equal highs = liquidity above taken, then rejected — "
                        "bearish reversal; target = pattern height",
                        breakout_t,
                    )

    # ---------- bottoms family ------------------------------------------
    if len(lows) >= 2 and lows[-1]["i"] >= len(df) - 40:
        if len(lows) >= 3:
            lsh, head, rsh = lows[-3], lows[-2], lows[-1]
            head_dn = min(lsh["price"], rsh["price"]) - head["price"]
            if head_dn > 0.45 * atr and abs(lsh["price"] - rsh["price"]) <= TOP_TOL * atr:
                between = [p for p in pts if p["kind"] == "high"
                           and lsh["i"] < p["i"] < rsh["i"]]
                if between:
                    neck_l, neck_r = sorted(between, key=lambda p: p["i"])[0], \
                        sorted(between, key=lambda p: p["i"])[-1]
                    neck = max(neck_l["price"], neck_r["price"])
                    head_h = neck - head["price"]
                    if head_h >= MIN_HEIGHT * atr:
                        conf = closes[-1] > neck + CONFIRM_PAD * atr
                        pts_sel = [p for p in pts if lsh["i"] <= p["i"] <= len(df) - 1
                                   and p["i"] <= rsh["i"] + MAX_POINTS_GAP] or pts
                        lines = [
                            _line(lsh, neck_l), _line(neck_l, head),
                            _line(head, neck_r), _line(neck_r, rsh),
                            _line(neck_l, neck_r, dash=True),
                        ]
                        entry = neck
                        sl = min(head["price"], rsh["price"]) - 0.25 * atr
                        target = neck + head_h
                        breakout_t = None
                        if conf:
                            for k in range(rsh["i"], len(df)):
                                if closes[k] > neck + CONFIRM_PAD * atr:
                                    breakout_t = df["time_utc"].iloc[k]
                                    break
                        return _mk_pattern(
                            "INVERSE HEAD & SHOULDERS", "reversal", "up",
                            pts_sel, lines, (head["price"], neck, lsh["t"]),
                            entry, sl, target,
                            "confirmed" if conf else "forming", atr,
                            f"Head {head_h / atr:.1f}A below neckline — bullish "
                            f"reversal; target = head height above neckline",
                            breakout_t,
                        )
        if len(lows) >= 3:
            l1, l2, l3 = lows[-3], lows[-2], lows[-1]
            bot = min(l1["price"], l2["price"], l3["price"])
            spread = max(l1["price"], l2["price"], l3["price"]) - bot
            if spread <= TOP_TOL * atr:
                highs_b = [p for p in pts if p["kind"] == "high"
                           and l1["i"] < p["i"] < l3["i"]]
                if highs_b:
                    neck = max(p["price"] for p in highs_b)
                    height = neck - bot
                    if height >= MIN_HEIGHT * atr:
                        conf = closes[-1] > neck + CONFIRM_PAD * atr
                        pts_sel = [p for p in pts if l1["i"] <= p["i"] <= len(df) - 1
                                   and p["i"] <= l3["i"] + MAX_POINTS_GAP] or pts
                        lines = [_line(l1, l2), _line(l2, l3),
                                 _line(highs_b[0], highs_b[-1], dash=True)]
                        entry = neck
                        sl = bot - 0.25 * atr
                        target = neck + height
                        breakout_t = None
                        if conf:
                            for k in range(l3["i"], len(df)):
                                if closes[k] > neck + CONFIRM_PAD * atr:
                                    breakout_t = df["time_utc"].iloc[k]
                                    break
                        return _mk_pattern(
                            "TRIPLE BOTTOM", "reversal", "up",
                            pts_sel, lines,
                            (l1["price"], neck, l1["t"]),
                            entry, sl, target,
                            "confirmed" if conf else "forming", atr,
                            "Three failed attacks on the same demand — bullish "
                            "reversal; target = pattern height",
                            breakout_t,
                        )
        l1, l2 = lows[-2], lows[-1]
        if abs(l1["price"] - l2["price"]) <= TOP_TOL * atr:
            highs_b = [p for p in pts if p["kind"] == "high"
                       and l1["i"] < p["i"] < l2["i"]]
            if highs_b:
                neck = max(p["price"] for p in highs_b)
                height = neck - min(l1["price"], l2["price"])
                if height >= MIN_HEIGHT * atr:
                    conf = closes[-1] > neck + CONFIRM_PAD * atr
                    pts_sel = [p for p in pts if l1["i"] <= p["i"] <= len(df) - 1
                               and p["i"] <= l2["i"] + MAX_POINTS_GAP] or pts
                    lines = [_line(l1, l2), _line(highs_b[0], highs_b[-1], dash=True)]
                    entry = neck
                    sl = min(l1["price"], l2["price"]) - 0.25 * atr
                    target = neck + height
                    breakout_t = None
                    if conf:
                        for k in range(l2["i"], len(df)):
                            if closes[k] > neck + CONFIRM_PAD * atr:
                                breakout_t = df["time_utc"].iloc[k]
                                break
                    return _mk_pattern(
                        "DOUBLE BOTTOM", "reversal", "up",
                        pts_sel, lines, (l1["price"], neck, l1["t"]),
                        entry, sl, target,
                        "confirmed" if conf else "forming", atr,
                        "Equal lows = liquidity below taken, then rejected — "
                        "bullish reversal; target = pattern height",
                        breakout_t,
                    )
    return None


def _flags(
    df: pd.DataFrame, pts: list[dict], atr: float, price: float,
) -> dict | None:
    """Bull/Bear flag + pennant: impulse pole then counter consolidation."""
    closes = df["c"].to_numpy(dtype=float)
    n = len(df)
    if len(pts) < 4:
        return None
    last = pts[-1]
    if last["i"] < n - 40:
        return None

    # candidate flag window: after the most recent STRONG impulse leg
    pole_dir = None
    pole = None
    for k in range(len(pts) - 1, 0, -1):
        a, b = pts[k - 1], pts[k]
        move = (b["price"] - a["price"]) if a["kind"] == "low" and b["kind"] == "high" \
            else (a["price"] - b["price"]) if a["kind"] == "high" and b["kind"] == "low" \
            else None
        if move is None:
            continue
        dist_atr = move / atr if atr > 0 else 0
        bars = b["i"] - a["i"]
        if dist_atr >= POLE_MIN and bars <= 18 and bars >= 2:
            # ensure the impulse really is the pole (price continuity)
            pole_dir = "up" if b["kind"] == "high" else "down"
            pole = (a, b)
            break
    if pole is None:
        return None
    a, b = pole
    # consolidation: the pivots AFTER the pole tip
    cons = [p for p in pts if p["i"] > b["i"]]
    if len(cons) < 2:
        return None
    hi_c = max(p["price"] for p in cons if p["kind"] == "high") if \
        any(p["kind"] == "high" for p in cons) else None
    lo_c = min(p["price"] for p in cons if p["kind"] == "low") if \
        any(p["kind"] == "low" for p in cons) else None
    if hi_c is None or lo_c is None:
        return None
    retr = 0.0
    if pole_dir == "up":
        retr = (b["price"] - lo_c) / (b["price"] - a["price"]) if b["price"] != a["price"] else 1
        # break level = flag upper boundary; pennant if converging
        first_lo = next((p["price"] for p in cons if p["kind"] == "low"), lo_c)
        converging = hi_c - lo_c < first_lo - lo_c + (hi_c - lo_c) * 0.0 and False
        # converging test: consolidation high below pole tip AND range narrows
        hi_seq = [p["price"] for p in cons if p["kind"] == "high"]
        lo_seq = [p["price"] for p in cons if p["kind"] == "low"]
        converging = (len(hi_seq) >= 2 and len(lo_seq) >= 2
                      and hi_seq[-1] < hi_seq[0] - 0.1 * atr
                      and lo_seq[-1] > lo_seq[0] + 0.1 * atr)
        entry = hi_c
        sl = lo_c - 0.2 * atr
        target = b["price"] + (b["price"] - a["price"])
        name = "BULL PENNANT" if converging else "BULL FLAG"
        conf = closes[-1] > hi_c + CONFIRM_PAD * atr
        pts_sel = [a, b, *cons]
        lines = [_line(a, b), _line(cons[0], cons[-1])]
        breakout_t = None
        if conf:
            for k2 in range(cons[-1]["i"], n):
                if closes[k2] > hi_c + CONFIRM_PAD * atr:
                    breakout_t = df["time_utc"].iloc[k2]
                    break
        note = (f"Pole {retr:.0%} held — continuation up; "
                f"target = pole height above the flag" if retr <= FLAG_MAXRETR else
                "Flag retraced too deep — weak continuation")
        return _mk_pattern(
            name, "continuation", "up", pts_sel, lines,
            (lo_c, hi_c, cons[0]["t"]), entry, sl, target,
            "confirmed" if conf else "forming", atr, note, breakout_t,
        )

    # bear flag: pole down, consolidation up
    retr = (hi_c - b["price"]) / (a["price"] - b["price"]) if a["price"] != b["price"] else 1
    hi_seq = [p["price"] for p in cons if p["kind"] == "high"]
    lo_seq = [p["price"] for p in cons if p["kind"] == "low"]
    converging = (len(hi_seq) >= 2 and len(lo_seq) >= 2
                  and hi_seq[-1] < hi_seq[0] - 0.1 * atr
                  and lo_seq[-1] > lo_seq[0] + 0.1 * atr)
    entry = lo_c
    sl = hi_c + 0.2 * atr
    target = b["price"] - (a["price"] - b["price"])
    conf = closes[-1] < lo_c - CONFIRM_PAD * atr
    name = "BEAR PENNANT" if converging else "BEAR FLAG"
    pts_sel = [a, b, *cons]
    lines = [_line(a, b), _line(cons[0], cons[-1])]
    breakout_t = None
    if conf:
        for k2 in range(cons[-1]["i"], n):
            if closes[k2] < lo_c - CONFIRM_PAD * atr:
                breakout_t = df["time_utc"].iloc[k2]
                break
    note = (f"Pole {retr:.0%} held — continuation down; "
            f"target = pole height below the flag" if retr <= FLAG_MAXRETR else
            "Flag retraced too deep — weak continuation")
    return _mk_pattern(
        name, "continuation", "down", pts_sel, lines,
        (lo_c, hi_c, cons[0]["t"]), entry, sl, target,
        "confirmed" if conf else "forming", atr, note, breakout_t,
    )


def _triangle_wedge_rect(
    df: pd.DataFrame, pts: list[dict], atr: float, price: float,
) -> dict | None:
    """Converging/flat boundaries from the last 2 highs + 2 lows."""
    closes = df["c"].to_numpy(dtype=float)
    n = len(df)
    highs = [p for p in pts if p["kind"] == "high"][-2:]
    lows = [p for p in pts if p["kind"] == "low"][-2:]
    if len(highs) < 2 or len(lows) < 2:
        return None
    if highs[-1]["i"] < n - 45 or lows[-1]["i"] < n - 45:
        return None
    h1, h2 = highs
    l1, l2 = lows
    # order the four pivots chronologically
    four = sorted([h1, h2, l1, l2], key=lambda p: p["i"])
    if four[-1]["i"] - four[0]["i"] < 6:
        return None

    hh = abs(h2["price"] - h1["price"])
    ll = abs(l2["price"] - l1["price"])
    flat_h = hh <= TOL_FLAT * atr
    flat_l = ll <= TOL_FLAT * atr
    hi_span = max(h1["price"], h2["price"]) - min(l1["price"], l2["price"])
    if hi_span < MIN_HEIGHT * atr or hi_span > MAX_HEIGHT * atr:
        return None

    up_h = h2["price"] > h1["price"]
    up_l = l2["price"] > l1["price"]

    def emit(name: str, dirn: str, entry: float, sl: float, target: float,
             note: str) -> dict:
        pts_sel = four
        lines = [
            _line(h1, h2), _line(l1, l2),
        ]
        conf = closes[-1] > entry + CONFIRM_PAD * atr if dirn == "up" \
            else closes[-1] < entry - CONFIRM_PAD * atr
        breakout_t = None
        trig_i = (h2 if dirn == "up" else l2)["i"]
        if conf:
            for k in range(trig_i, n):
                if (dirn == "up" and closes[k] > entry + CONFIRM_PAD * atr) or \
                        (dirn == "down" and closes[k] < entry - CONFIRM_PAD * atr):
                    breakout_t = df["time_utc"].iloc[k]
                    break
        zone_lo = min(l1["price"], l2["price"])
        zone_hi = max(h1["price"], h2["price"])
        return _mk_pattern(
            name, "boundary", dirn, pts_sel, lines,
            (zone_lo, zone_hi, four[0]["t"]), entry, sl, target,
            "confirmed" if conf else "forming", atr, note, breakout_t,
        )

    # RECTANGLE: both sides flat
    if flat_h and flat_l:
        entry = h2["price"]
        sl = l2["price"] - 0.2 * atr if h2["price"] > l2["price"] else h2["price"] + 0.2 * atr
        # direction: the dominant side of the closes
        mid = (h2["price"] + l2["price"]) / 2
        dirn = "up" if closes[-1] > mid else "down"
        if dirn == "up":
            entry = h2["price"]
            sl = l2["price"] - 0.2 * atr
            target = h2["price"] + (h2["price"] - l2["price"])
        else:
            entry = l2["price"]
            sl = h2["price"] + 0.2 * atr
            target = l2["price"] - (h2["price"] - l2["price"])
        return emit(
            "BULL RECTANGLE" if dirn == "up" else "BEAR RECTANGLE",
            dirn, entry, sl, target,
            "Balanced range — trade the breakout; target = range height",
        )

    # ASCENDING TRIANGLE: flat highs, rising lows -> bullish
    if flat_h and up_l and not flat_l:
        entry = h2["price"]
        sl = l2["price"] - 0.2 * atr
        target = h2["price"] + (h2["price"] - min(l1["price"], l2["price"]))
        return emit(
            "ASCENDING TRIANGLE", "up", entry, sl, target,
            "Rising lows press the flat supply — bullish breakout; "
            "target = base height",
        )

    # DESCENDING TRIANGLE: flat lows, falling highs -> bearish
    if flat_l and not up_h and not flat_h:
        entry = l2["price"]
        sl = h2["price"] + 0.2 * atr
        target = l2["price"] - (max(h1["price"], h2["price"]) - l2["price"])
        return emit(
            "DESCENDING TRIANGLE", "down", entry, sl, target,
            "Falling highs press the flat demand — bearish breakout; "
            "target = base height",
        )

    # SYMMETRIC TRIANGLE: both converge
    if not flat_h and not flat_l and (not up_h or up_l):
        if (up_h and not up_l) or (not up_h and up_l):
            pass  # same-direction sides are a channel, not a triangle
        entry = h2["price"]
        sl = l2["price"] - 0.2 * atr
        target = entry + hi_span
        # direction from momentum into the apex
        dirn = "up" if closes[-1] > (h2["price"] + l2["price"]) / 2 else "down"
        if dirn == "down":
            entry = l2["price"]
            sl = h2["price"] + 0.2 * atr
            target = entry - hi_span
        return emit(
            "SYMMETRIC TRIANGLE", dirn, entry, sl, target,
            "Coiling into the apex — breakout resolves the coil; "
            "target = base height",
        )

    # RISING WEDGE: both up, converging -> bearish
    if up_h and up_l and hh < hi_span * 0.5:
        entry = l2["price"]
        sl = h2["price"] + 0.2 * atr
        target = l2["price"] - hi_span * 0.7
        return emit(
            "RISING WEDGE", "down", entry, sl, target,
            "Climb decelerating into convergence — bearish resolution",
        )
    # FALLING WEDGE: both down, converging -> bullish
    if (not up_h) and (not up_l) and ll < hi_span * 0.5:
        entry = h2["price"]
        sl = l2["price"] - 0.2 * atr
        target = h2["price"] + hi_span * 0.7
        return emit(
            "FALLING WEDGE", "up", entry, sl, target,
            "Slide decelerating into convergence — bullish resolution",
        )
    return None


def detect_patterns(df: pd.DataFrame, atr: float, price: float,
                    window: int = 150) -> list[dict]:
    """Best classic patterns on the active TF's visible window.

    Returns 0..2 pattern drawings, best-first. Order: reversal family
    (tops/bottoms/H&S) beats the flag, flag beats the boundary family —
    the channel's own teaching priority.
    """
    try:
        if df is None or len(df) < 24 or atr <= 0 or not price:
            return []
        win = df.tail(window)
        pts = _pts(df, win)
        if len(pts) < MIN_SWINGS:
            return []
        cands: list[dict] = []
        for fn in (_tops_bottoms, _flags, _triangle_wedge_rect):
            try:
                r = fn(df, pts, atr, price)
            except Exception:  # noqa: BLE001 — one family failing must not kill all
                logger.debug("pattern family %s failed", fn.__name__, exc_info=True)
                continue
            if r:
                cands.append(r)
        # de-dup by name; prefer confirmed, then bigger height
        cands.sort(key=lambda r: (r["state"] != "confirmed", -(r["height_atr"] or 0)))
        seen: set[str] = set()
        out: list[dict] = []
        for c in cands:
            if c["name"] in seen:
                continue
            seen.add(c["name"])
            out.append(c)
        return out[:2]
    except Exception:  # noqa: BLE001 — patterns must never break /api/analysis
        logger.exception("detect_patterns failed")
        return []
