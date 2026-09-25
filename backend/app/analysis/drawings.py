"""D-043/D-052/D-058 — Professional auto-drawing engine (user directive,
Bengali):

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
from app.analysis.setup_geometry import setup_geometry, setup_snapshot

logger = logging.getLogger("xauusd.drawings")

#: hard cap — a pro chart never carries more than this many marks
#: D-058 — 42: the momentum ribbon / swing labels / kill-zone bands add
#: marks, and every one of them is 2-4px thin on the canvas
#: D-061 — 45: +3 for the AMD cycle set (range / manipulation /
#: distribution) that labels the institutional trap on the chart
#: D-064 — 48: +3 for the structure set (REST boxes, pullback magnets,
#: the leg-count ladder badge)
#: D-067 — 52: +4 for the candle-battle set (the war badge + the last
#: decisive wick rejections)
#: D-071 — 60: +8 for the liquidity set (up to 5 pool life-cycle marks +
#: the DIRECTION outlook + the draw path)
MAX_DRAWINGS = 60
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


# ------------------------------------------------- D-058 momentum / structure


def _ema_ribbon(df: pd.DataFrame | None) -> dict | None:
    """kind: "ema" — the MOMENTUM ribbon a pro keeps under price.

    EMA 9 / 21 / 50 sampled across the drawing window (payload stays
    light: ~60 points per line). The label carries the live verdict —
    which EMA stack is in charge and whether momentum agrees with the
    last close (the "momentum" axis of the user's Setup/Level/Zone/
    Structure/Momentum checklist).
    """
    if df is None or len(df) < 55:
        return None
    win = df.tail(DRAW_WINDOW_BARS + 60)
    lines: list[dict] = []
    series = {
        9: ind.ema(win["c"], 9),
        21: ind.ema(win["c"], 21),
        50: ind.ema(win["c"], 50),
    }
    for period, tone in ((9, "gold"), (21, "violet"), (50, "neutral")):
        s = series[period]
        pts: list[dict] = []
        times = win["time_utc"].tolist()
        for i in range(len(win)):
            v = s.iloc[i]
            if pd.notna(v):
                pts.append({
                    "t": _iso(times[i]),
                    "p": round(float(v), 2),
                })
        if len(pts) < 2:
            continue
        step = max(1, len(pts) // 60)
        lines.append({
            "period": period, "tone": tone,
            "label": f"EMA {period}",
            "points": pts[::step],
        })
    if len(lines) < 2:
        return None
    e9 = float(series[9].iloc[-1])
    e21 = float(series[21].iloc[-1])
    e50 = float(series[50].iloc[-1])
    close = float(win["c"].iloc[-1])
    if e9 > e21 > e50:
        verdict = "Momentum · bullish stack — EMA 9 > 21 > 50"
    elif e9 < e21 < e50:
        verdict = "Momentum · bearish stack — EMA 9 < 21 < 50"
    elif e9 > e21:
        verdict = "Momentum · turning up — EMA 9 crossing above 21"
    else:
        verdict = "Momentum · turning down — EMA 9 crossing below 21"
    tone = "bull" if e9 > e21 else "bear"
    return {
        "kind": "ema", "lines": lines,
        "label": verdict, "tone": tone,
        "above": bool(close > e9),
    }


def _swing_labels(df: pd.DataFrame | None, atr: float) -> list[dict]:
    """kind: "swing" — HH / HL / LH / LL structure reads at confirmed
    swings. This is the exact map a price-action trader keeps in their
    head: higher highs and higher lows = uptrend alive, etc.
    """
    if df is None or len(df) < 40:
        return []
    win = df.tail(DRAW_WINDOW_BARS + 20)
    pts = ind.swings(win, 2, 2)
    out: list[dict] = []
    last_high = last_low = None
    for p in pts:
        if p["kind"] == "high":
            if last_high is not None:
                tag = "HH" if p["price"] > last_high else "LH"
                out.append({
                    "kind": "swing", "t": _iso(p["t"]),
                    "price": round(float(p["price"]), 2),
                    "tag": tag, "side": "high",
                    "label": (
                        "Higher High — bullish structure" if tag == "HH"
                        else "Lower High — weakening structure"
                    ),
                    "tone": "bull" if tag == "HH" else "bear",
                })
            last_high = float(p["price"])
        else:
            if last_low is not None:
                tag = "HL" if p["price"] > last_low else "LL"
                out.append({
                    "kind": "swing", "t": _iso(p["t"]),
                    "price": round(float(p["price"]), 2),
                    "tag": tag, "side": "low",
                    "label": (
                        "Higher Low — bullish structure" if tag == "HL"
                        else "Lower Low — bearish structure"
                    ),
                    "tone": "bull" if tag == "HL" else "bear",
                })
            last_low = float(p["price"])
    return out[-6:]


def _sessions(df: pd.DataFrame | None, tf: str) -> list[dict]:
    """kind: "session" — ICT kill-zone bands (Asia / London / NY).

    The WHEN layer: institutions move size inside these UTC windows, so
    the pro marks them before marking anything else. Bands merge into
    contiguous runs of same-session bars inside the drawing window and
    render as whisper-quiet vertical shading — visible only on the
    intraday views where they mean something.
    """
    if df is None or len(df) < 30 or tf not in ("M1", "M5", "M15"):
        return []
    win = df.tail(DRAW_WINDOW_BARS)
    name_map = {
        "asia": "Asia Kill Zone",
        "london": "London Kill Zone",
        "ny-am": "New York AM Kill Zone",
        "ny-pm": "New York PM Kill Zone",
    }
    out: list[dict] = []
    cur_name: str | None = None
    band_start: Any = None
    band_end: Any = None
    for row_t in win["time_utc"].tolist():
        kz = smc.kill_zone(pd.Timestamp(row_t))
        name = kz["name"] if kz["in"] else None
        if name != cur_name:
            if cur_name is not None and band_start is not None \
                    and band_end is not None:
                out.append({
                    "kind": "session", "t0": _iso(band_start),
                    "t1": _iso(band_end),
                    "name": name_map.get(cur_name, cur_name),
                    "label": name_map.get(cur_name, cur_name),
                    "tone": "gold" if cur_name in ("london", "ny-am")
                    else "neutral",
                })
            cur_name = name
            band_start = row_t if name else None
            band_end = row_t if name else None
        else:
            band_end = row_t
    if cur_name is not None and band_start is not None and band_end is not None:
        out.append({
            "kind": "session", "t0": _iso(band_start), "t1": _iso(band_end),
            "name": name_map.get(cur_name, cur_name),
            "label": name_map.get(cur_name, cur_name),
            "tone": "gold" if cur_name in ("london", "ny-am") else "neutral",
        })
    return out


# ------------------------------------------------------- D-061 AMD marks


def _amd_marks(base: pd.DataFrame, tf: str) -> list[dict]:
    """kind: "amd" — the institutional cycle drawn the way ICT teaches it.

    User directive (D-061, Bengali): the sell filled into the sweep and
    the market reversed — "তোমি ফাদ বুজতে পারো নাই" (you did not see the
    trap). The chart now labels the trap anatomy ON the bars where it
    happened:

    - the ACCUMULATION range box (the coil before the false break),
    - the MANIPULATION marker on the swept-and-reclaimed level,
    - the DISTRIBUTION marker on the displacement leg.

    One compact mark per element, full-word labels, only when the phase
    structure actually exists (a pro does not label noise).
    """
    if base is None or len(base) < 40 or tf not in ("M1", "M5", "M15", "M30"):
        return []
    try:
        from app.analysis.manipulation import amd_state

        st = amd_state(base)
    except Exception:  # noqa: BLE001 — AMD marks must never break the set
        return []
    if st.get("phase") == "none" or not st.get("range"):
        return []
    out: list[dict] = []
    rng = st["range"]
    out.append({
        "kind": "amd",
        "element": "range",
        "t0": _iso(rng.get("t0")),
        "t1": _iso(base["time_utc"].iloc[-1]),
        "zone": [rng.get("lo"), rng.get("hi")],
        "phase": st.get("phase"),
        "label": f"Accumulation Range — {st.get('phase')}",
        "note": st.get("note"),
        "tone": "gold",
    })
    sweep = st.get("sweep")
    if sweep:
        out.append({
            "kind": "amd",
            "element": "manipulation",
            "t": _iso(sweep.get("t")),
            "price": sweep.get("level"),
            "side": sweep.get("side"),
            "depth_atr": sweep.get("depth_atr"),
            "bars_ago": sweep.get("bars_ago"),
            "label": (
                f"Manipulation — {sweep.get('side')} swept & reclaimed "
                f"({sweep.get('depth_atr')} ATR deep)"
            ),
            "tone": "bear" if sweep.get("side") == "BSL" else "bull",
        })
    disp = st.get("displacement")
    if disp:
        out.append({
            "kind": "amd",
            "element": "distribution",
            "t": _iso(disp.get("t")),
            "price": disp.get("price"),
            "dir": disp.get("dir"),
            "run": disp.get("run"),
            "strength_atr": disp.get("strength_atr"),
            "label": (
                f"Distribution — {disp.get('run')} institutional bodies "
                f"{'up' if disp.get('dir') == 'up' else 'down'} "
                f"({disp.get('strength_atr')} ATR)"
            ),
            "tone": "bull" if disp.get("dir") == "up" else "bear",
        })
    return out[:3]


# ----------------------------------------------------- D-064 structure set


def _structure_marks(base: pd.DataFrame, tf: str, price: float) -> list[dict]:
    """kind: "rest" / "magnet" / "ladder" — the market's REST anatomy.

    User directive (D-064, Bengali): "মার্কেট কোথায় গিয়ে রেস্ট করে বা
    একটু বিশ্রাম নেয়, বিশ্রাম নিয়ে একটু উপরের দিকে যায়, তার পর আবার
    ডাউন এ যায়" — the chart now shows exactly that anatomy:

    - REST boxes: the compressed pauses where the market actually rested
      (a pro marks these — the expansion leaves from them);
    - REST MAGNET lines: WHERE the market is expected to rest NEXT
      (EMA 21/50, equilibrium, unfilled FVG, OTE, prior rest);
    - the LADDER badge: the live leg count + phase + honest reversal
      odds ("LEG 3 lower — rest due, p(reversal) 55%").

    The measured facts behind the words (scripts/measure_legs.py): a
    pause between same-direction legs is ~2.3 ATR deep and 10-16 bars
    long inside a <=0.62-compressed range; 87% of runs end at leg 3.
    """
    if base is None or len(base) < 60 or tf not in (
        "M1", "M5", "M15", "M30"
    ):
        return []
    try:
        from app.analysis.structure import LEGS_EXHAUST, structure_read

        st = structure_read(base, price)
    except Exception:  # noqa: BLE001 — structure marks must never break
        return []
    out: list[dict] = []

    # 1. REST boxes — where the market rested (most recent first)
    for z in (st.get("rests") or [])[:2]:
        out.append({
            "kind": "rest",
            "t0": _iso(z.get("t0")),
            "t1": _iso(z.get("t1")),
            "lo": z.get("lo"),
            "hi": z.get("hi"),
            "bars": z.get("bars"),
            "compress": z.get("compress"),
            "label": f"REST — {z.get('bars')} bars, compressed range",
            "note": (
                "the market paused here; expansions leave from rests"
            ),
            "tone": "neutral",
        })

    # 2. REST MAGNETS — where the market is expected to rest next
    for m in (st.get("magnets") or [])[:2]:
        out.append({
            "kind": "magnet",
            "price": m.get("price"),
            "source": m.get("kind"),
            "dist_atr": m.get("dist_atr"),
            "side": "up" if st.get("run_dir") == "down" else "down",
            "label": (
                f"REST MAGNET — {m.get('kind')} · "
                f"{m.get('dist_atr')} ATR away"
            ),
            "note": m.get("note"),
            "tone": "gold",
        })

    # 3. the LADDER badge — the live "কত বার LL/LH" count + verdict
    run = int(st.get("run") or 0)
    rdir = st.get("run_dir")
    if run > 0 and rdir:
        phase = st.get("phase") or "leg"
        word = "lower" if rdir == "down" else "higher"
        if phase == "reversal-confirmed":
            badge = (
                f"STRUCTURE SHIFT {'↑' if rdir == 'up' else '↓'} — "
                f"reversal confirmed, new leg 1"
            )
            tone = "bull" if rdir == "up" else "bear"
        elif phase == "resting":
            badge = "RESTING — compressed range, wait for the break"
            tone = "neutral"
        else:
            due = (
                "REST DUE" if run >= LEGS_EXHAUST else "run intact"
            )
            badge = (
                f"LEG {run} {word} · {due} · "
                f"p(reversal) {round(100 * float(st.get('p_reversal') or 0))}%"
            )
            tone = "bear" if rdir == "down" else "bull"
        out.append({
            "kind": "ladder",
            "t": _iso(base["time_utc"].iloc[-1]),
            "price": round(float(price), 2),
            "run": run,
            "run_dir": rdir,
            "phase": phase,
            "p_reversal": st.get("p_reversal"),
            "label": badge,
            "action": st.get("action"),
            "tone": tone,
        })
    return out[:5]


def _battle_marks(base: pd.DataFrame, tf: str) -> list[dict]:
    """kind: "battle" / "reject" — the candle-by-candle WAR (D-067).

    User directive (Bengali): "কারা কাদের কে ডোমেনেট করছে, কারা জিতেছে,
    লাস্ট কয়েক টি ক্যান্ডেল এর ভিতর কি ঘটেছে" — the chart now shows:

    - the BATTLE badge: who dominates the last 6 candles of THIS
      timeframe (volume-weighted flow split), who won how many, the
      net displacement in ATR and the winning streak;
    - REJECT marks: the last decisive wick rejections — WHERE sellers
      rejected the high / buyers absorbed the dip inside those
      candles ("what happened INSIDE the last few candles").

    The RUNNING candle's live verdict is rendered by the UI from the
    forming bar on every tick — the backend only labels closes.
    """
    if base is None or len(base) < 60 or tf not in (
        "M1", "M5", "M15", "M30"
    ):
        return []
    try:
        from app.analysis.orderflow import battle_read

        b = battle_read(base, n=6)
    except Exception:  # noqa: BLE001 — battle marks must never break
        return []
    if not b or not b.get("n"):
        return []
    out: list[dict] = []

    # 1. the war badge — who owns the flow right now
    state = b.get("state")
    if state in ("buyers", "sellers"):
        pct = b.get("buy_pct" if state == "buyers" else "sell_pct")
        wins = b.get("wins") or {}
        n_won = wins.get(state, 0)
        streak = b.get("streak") or {}
        s_len = int(streak.get("len") or 0)
        label = (
            f"BATTLE · {state.upper()} {round(pct)}% · "
            f"won {n_won}/{b.get('n')} · {b.get('net_atr'):+.1f} ATR"
            + (f" · {s_len} streak" if s_len >= 2 else "")
        )
        out.append({
            "kind": "battle",
            "t": _iso(base["time_utc"].iloc[-1]),
            "price": round(float(base["c"].iloc[-1]), 2),
            "state": state,
            "buy_pct": b.get("buy_pct"),
            "sell_pct": b.get("sell_pct"),
            "net_atr": b.get("net_atr"),
            "label": label,
            "note": b.get("verdict"),
            "tone": "bull" if state == "buyers" else "bear",
        })

    # 2. the decisive moments INSIDE the candles — wick rejections
    for e in (b.get("events") or []):
        if e.get("kind") != "rejection":
            continue
        out.append({
            "kind": "reject",
            "t": e.get("t"),
            "price": e.get("price"),
            "side": e.get("side"),
            "depth_atr": e.get("depth_atr"),
            "label": (
                f"{str(e.get('side')).upper()} REJECTED · "
                f"{e.get('depth_atr')} ATR wick"
            ),
            "note": e.get("note"),
            "tone": "bull" if e.get("side") == "buyers" else "bear",
        })
    return out[:4]


# ------------------------------------------------------ D-071 liquidity set


def _liquidity_marks(
    base: pd.DataFrame, tf: str, price: float, flow: dict,
) -> list[dict]:
    """kind: "liq" — every pool's LIFE-CYCLE state (D-071).

    User directive (Bengali): "কোনো লেভেল বা zone এর বা কোনো একটি
    ক্যান্ডেল এর লিকুডিটি নিলো, কি নিল না। নেওয়ার পরে লিকুডিটি রান
    করে নাকি সুয়েপ করবে" — each pool renders with its live verdict:

    - UNTOUCHED — the draw: resting liquidity price is pulled toward
      ("$$ · 1.2 ATR away");
    - SWEPT — the stops were harvested and REJECTED: expect REVERSAL
      away from the pool (the ICT stop-hunt);
    - RUN — the pool was consumed and price closed through: expect
      CONTINUATION toward the next pool.

    The tone carries the EXPECTED DIRECTION (green = up, red = down) —
    swept BSL is bear (reversal down), run BSL is bull (continuation
    up), untouched pools keep the side's own color.
    """

    if base is None or len(base) < 40 or not (price and price > 0):
        return []
    pools = (flow or {}).get("pools") or []
    draw = (flow or {}).get("draw") or {}
    if not pools:
        return []

    draw_prices = {
        p.get("price") for p in (draw.get("above"), draw.get("below")) if p
    }

    def rank(p: dict) -> tuple:
        # draws first, then fresh events (nearest first), then the rest
        if p.get("price") in draw_prices:
            return (0, 0)
        if p.get("state") != "untouched":
            bars_ago = 99
            if p.get("i_event") is not None:
                bars_ago = max(0, len(base) - 1 - int(p["i_event"]))
            return (1, min(bars_ago, 40))
        return (2, p.get("dist_atr") or 99)

    ranked = sorted(pools, key=rank)[:5]
    out: list[dict] = []
    for p in ranked:
        side = p.get("kind")
        state = p.get("state")
        side_word = "Buy Side" if side == "BSL" else "Sell Side"
        if state == "untouched":
            d = p.get("dist_atr")
            label = (
                f"{side_word} Liquidity — untouched draw"
                + (f" · {d} ATR away" if d is not None else "")
            )
            note = (
                "resting liquidity the market is drawn toward — "
                "price moves from pool to pool"
            )
            tone = "bull" if side == "BSL" else "bear"
        elif state == "swept":
            rev = "down" if side == "BSL" else "up"
            disp = p.get("disp_atr")
            label = (
                f"{side_word} Liquidity — SWEPT · reversal {rev}"
                + (f" ({disp} ATR so far)" if disp is not None else "")
            )
            note = (
                "stops harvested then rejected (stop-hunt) — "
                "expect the move AWAY from the pool"
            )
            tone = "bear" if side == "BSL" else "bull"
        else:
            cont = "up" if side == "BSL" else "down"
            disp = p.get("disp_atr")
            label = (
                f"{side_word} Liquidity — RUN · continuation {cont}"
                + (f" ({disp} ATR so far)" if disp is not None else "")
            )
            note = (
                "pool consumed, price closed through — expect the move "
                "to continue toward the next pool"
            )
            tone = "bull" if side == "BSL" else "bear"
        out.append({
            "kind": "liq",
            "side": side,
            "price": p.get("price"),
            "t": _iso(p.get("t")),
            "state": state,
            "t_event": _iso(p.get("t_event")) if p.get("t_event") else None,
            "disp_atr": p.get("disp_atr"),
            "dist_atr": p.get("dist_atr"),
            "source": p.get("source"),
            "label": label,
            "note": note,
            "tone": tone,
        })
    return out


def _outlook_mark(
    frames: dict, snaps: dict, base: pd.DataFrame, price: float, flow: dict,
) -> dict | None:
    """kind: "outlook" — the DIRECTION synthesis (D-071).

    User directive (Bengali): "চার্ট এর ড্রয়িং আরও বিস্তারিত করতে হবে।
    যেনো আমি বুঝতে পারি মার্কেট কোন দিকে যাবে" — one honest verdict,
    built from the three directional engines the app already has:

    1. FRESH liquidity event (strongest, shortest term): a sweep says
       reversal, a run says continuation;
    2. the DRAW-ON-LIQUIDITY: the nearest untouched pool the market is
       pulled toward (price moves pool to pool);
    3. the higher-timeframe context (D-068 regime label + MTF bias)
       as confirmation, never as the driver.

    The verdict is drawn as three compact lines on the chart canvas —
    direction, the why, and the two-sided pool map.
    """

    if base is None or len(base) < 40 or not (price and price > 0):
        return None
    draw = (flow or {}).get("draw") or {}

    label = None
    vol = None
    try:
        from app.analysis.regime import regime_read

        rg = regime_read(frames.get("M1") if isinstance(frames, dict) else None,
                         frames, price) or {}
        label = rg.get("label")
        vol = (rg.get("vol") or {}).get("state")
    except Exception:  # noqa: BLE001 — outlook must never break the set
        label = None
    try:
        bias = (mtf_bias(snaps) or {}).get("bias") if snaps else None
    except Exception:  # noqa: BLE001
        bias = None

    fresh = draw.get("fresh")
    draw_dir = draw.get("dir")
    direction = None
    why = "no directional edge yet"
    if fresh:
        direction = fresh.get("dir")
        word = "sweep" if fresh.get("state") == "swept" else "run"
        exp = "reversal" if fresh.get("state") == "swept" else "continuation"
        why = (
            f"fresh {word} of {fresh.get('side')} "
            f"{fresh.get('bars_ago')} bars ago — {exp}"
        )
    elif draw_dir:
        direction = draw_dir
        tgt = draw.get("above") if draw_dir == "up" else draw.get("below")
        if tgt:
            why = (
                f"drawn to untouched {tgt.get('kind')} "
                f"{tgt.get('price')} ({tgt.get('dist_atr')} ATR)"
            )
    elif label and str(label).endswith("UP"):
        direction = "up"
        why = f"{label} regime"
    elif label and str(label).endswith("DOWN"):
        direction = "down"
        why = f"{label} regime"

    arrow = "↑" if direction == "up" else "↓" if direction == "down" else "—"
    line1 = f"DIRECTION {arrow} · {why}"
    ctx_bits = []
    if label:
        ctx_bits.append(str(label))
    if vol:
        ctx_bits.append(f"vol {vol}")
    if bias:
        ctx_bits.append(f"bias {bias}")
    line2 = "regime " + (" · ".join(ctx_bits) if ctx_bits else "unreadable")
    line3_parts = []
    for side, p in (("BSL above", draw.get("above")),
                    ("SSL below", draw.get("below"))):
        if p:
            line3_parts.append(
                f"{side} {p.get('price')} ({p.get('dist_atr')} ATR)"
            )
    line3 = " · ".join(line3_parts) if line3_parts else "no untouched pools in view"

    return {
        "kind": "outlook",
        "dir": direction,
        "regime": label,
        "vol": vol,
        "bias": bias,
        "draw_dir": draw_dir,
        "draw_side": (draw.get("above") or {}).get("kind")
        if direction == "up" else (draw.get("below") or {}).get("kind"),
        "draw_price": (draw.get("above") or {}).get("price") if direction == "up"
        else (draw.get("below") or {}).get("price"),
        "fresh": fresh,
        "label": f"Market direction — {direction or 'neutral'} ({why})",
        "lines": [line1, line2, line3],
        "note": "synthesis: fresh liquidity event > draw-on-liquidity > regime",
        "tone": "bull" if direction == "up" else "bear" if direction == "down"
        else "gold",
    }


def _path_mark(price: float, flow: dict, direction: str | None) -> dict | None:
    """kind: "path" — the dotted draw-path from live price to the target
    pool (D-071): the visual answer to "মার্কেট কোন দিকে যাবে"."""

    if not direction or not (price and price > 0):
        return None
    draw = (flow or {}).get("draw") or {}
    tgt = draw.get("above") if direction == "up" else draw.get("below")
    if not tgt:
        return None
    return {
        "kind": "path",
        "dir": direction,
        "from_price": round(float(price), 2),
        "to_price": tgt.get("price"),
        "dist_atr": tgt.get("dist_atr"),
        "label": f"draw path → {tgt.get('kind')} {tgt.get('price')}",
        "note": "the projected pull toward the nearest untouched pool",
        "tone": "bull" if direction == "up" else "bear",
    }


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
        # D-058 — ICT equilibrium: the 50% of the dealing range. The line
        # every premium/discount read hangs off ("price is expensive
        # above it, cheap below it").
        pd_state = snap.get("premium_discount") or {}
        eq = pd_state.get("eq")
        if eq is None and pd_state.get("range_lo") is not None \
                and pd_state.get("range_hi") is not None:
            try:
                eq = (float(pd_state["range_lo"]) +
                      float(pd_state["range_hi"])) / 2.0
            except (TypeError, ValueError):
                eq = None
        if eq:
            add(float(eq), "Equilibrium · 50% of dealing range",
                "gold", "dash")
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
            # D-069 — the label states the gap's measured importance
            # (size in the frame's own ATRs + fill depth) so the chart
            # shows WHY one gap matters and the next is noise
            size_note = ""
            try:
                gap_atr = float(g.get("gap_atr") or 0.0)
                fill_pct = float(g.get("fill_pct") or 0.0)
                if gap_atr > 0:
                    size_note = f" ({gap_atr:.2f} ATR"
                    if fill_pct > 0:
                        size_note += f", {fill_pct:.0%} filled"
                    size_note += ")"
            except (TypeError, ValueError):
                size_note = ""
            label = (
                "Fair Value Gap · bullish" if side == "bullish"
                else "Fair Value Gap · bearish"
            ) + size_note + tag
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

    # D-058 — EXACT window parity with the engine's _drawing_geometry():
    # both sides build their M5/M15 snapshots through setup_snapshot()
    # (GEOMETRY_BARS windows == services/analysis.py BARS_PER_TF), so the
    # box the user watches is numerically identical to the order the
    # broker receives — no 240-vs-GEOMETRY window drift ever again.
    s5_geo = setup_snapshot(m5, "M5")
    s15_geo = setup_snapshot(frames.get("M15"), "M15")
    geo = setup_geometry(s5_geo, s15_geo, direction, price, atr_fallback=atr5)
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

    # D-061 — the TRAP verdict on the box the user watches (directive:
    # "এই বিষয় টা কিভাবে আমার অ্যাপ বুজবে। এবং আমিও দেখতে পারবো"): the
    # same manipulation.trap_risk the engine gates on, drawn where the
    # trade is drawn — the user sees WHY a setup is suspect before the
    # order ever exists
    trap_note: str | None = None
    try:
        from app.analysis.manipulation import trap_risk

        trap = trap_risk(m1, direction, entry, sl, now=now)
        if float(trap.get("risk", 0.0) or 0.0) >= 0.4:
            factors.append(f"trap risk {float(trap['risk']):.2f}")
            reasons = trap.get("reasons") or []
            if reasons:
                trap_note = reasons[0]
    except Exception:  # noqa: BLE001 — the tag is best-effort
        pass

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
    if trap_note:
        note += f" · TRAP: {trap_note}"

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
    # D-071 — the bare `_sweeps(snap)` X-marks are superseded by the
    # `liq` life-cycle set below (same pools, now with the
    # untouched/swept/run verdict + post-take displacement); the legacy
    # `sweep` kind stays renderable for cached snapshots.
    out.extend(_structure_events(snap, t_start))
    out.extend(_arrows(snap, price))

    # D-058 — the deep-professional layers (user directive: "ICT, SMC,
    # price action… বেস্ট গুলো অ্যাড করবেন"):
    # 10. momentum ribbon (EMA 9/21/50) — under price, whisper-thin
    # 11. HH/HL/LH/LL swing structure reads
    # 12. ICT kill-zone session bands (intraday views only)
    # 13. D-061 — the AMD cycle (accumulation range / manipulation /
    #     distribution) — the institutional trap labeled on the chart
    # 14. D-064 — the REST anatomy: where the market rested (boxes),
    #     where it rests next (magnets), the live leg-count ladder
    out.append(_ema_ribbon(base))
    out.extend(_swing_labels(base, atr))
    out.extend(_sessions(base, tf))
    out.extend(_amd_marks(base, tf))
    out.extend(_structure_marks(base, tf, price))
    # 15. D-067 — the candle battle: who dominates the last few
    #     candles, who won, and the decisive rejections inside them
    out.extend(_battle_marks(base, tf))

    # 16. D-071 — the LIQUIDITY life-cycle: every pool's live state
    #     (untouched draw / swept / run) on THIS timeframe + the
    #     DIRECTION outlook (fresh event > draw > regime) + the
    #     dotted draw-path to the target pool. One liquidity_flow()
    #     read feeds all three marks.
    try:
        from app.analysis.liquidity_flow import liquidity_flow

        flow = liquidity_flow(base, price)
    except Exception:  # noqa: BLE001 — liquidity marks must never break
        flow = {}
    out.extend(_liquidity_marks(base, tf, price, flow))
    outlook = _outlook_mark(frames, snaps, base, price, flow)
    if outlook is not None:
        out.append(outlook)
        path = _path_mark(price, flow, outlook.get("dir"))
        if path is not None:
            out.append(path)

    return [d for d in out if d is not None][:MAX_DRAWINGS]
