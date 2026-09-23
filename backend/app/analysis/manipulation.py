"""D-061 — Institutional manipulation & AMD detection (user directive).

The user's failing scenario (Bengali, paraphrased): "the last red candle
printed a SELL signal, the sell order filled a few pips lower, then the
market suddenly printed several LARGE consecutive bullish candles — the
retail sell was exactly the liquidity the institutions needed."

That is the ICT **A**ccumulation → **M**anipulation → **D**istribution
(AMD) cycle, also called a stop hunt / turtle soup / Judas swing:

- **ACCUMULATION** — a compressed range builds while institutions fill
  positions in both directions; retail sees "sideways, nothing to trade".
- **MANIPULATION** — a false break BEYOND the range extreme harvests the
  resting retail stops on that side (SSL below lows / BSL above highs).
  The move LOOKS like a breakout — that is exactly when the retail
  signal fires — but price CLOSES BACK INSIDE the range (the reclaim).
- **DISTRIBUTION** — displacement the other way: several large
  consecutive candles as institutions release the position into the
  trapped crowd. The retail trade that filled during the manipulation is
  now underwater.

This module answers, on closed bars only (no lookahead, same contract as
smc.py):

1. ``amd_state(df)`` — which AMD phase the recent window sits in, plus
   the manipulation sweep (side / level / reclaimed / bars ago) and any
   displacement run.
2. ``trap_risk(df, direction, entry, sl, ...)`` — for a CANDIDATE trade:
   is it the retail side of a pending or completed manipulation?
   Returns 0..1 + human-readable reasons (the engine gates and tags on
   this; the app displays it).
3. ``session_context(ts)`` — kill-zone timing + the Judas-window flag
   (session opens, when the manipulation statistically happens) — the
   "কোনো ইভেন্ট ছিল নাকি সেশন শেষ নাকি শুরু" part of the user question.

Everything is JSON-serializable so the same output feeds the signal
engine (gate + payload context + trace) and the chart drawing layer.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from app.analysis import smc

# ------------------------------------------------------------------ tuning
# (ATR-relative so the same thresholds scale across M1 gold / BTC / mock)

#: bars scanned for the AMD window (the "range" the phases live in)
AMD_WINDOW_BARS = 90
#: how fresh a false-break must be to count as the manipulation leg
SWEEP_FRESH_BARS = 12
#: a false break must close back inside the range within this many bars
RECLAIM_BARS = 3
#: displacement body = >= this multiple of ATR (institutional footprint)
DISP_ATR = 1.1
#: how many consecutive displacement bodies form a distribution run
DISP_MIN_RUN = 2
#: range width below this fraction of the random-walk expectation
#: (atr * sqrt(n)) counts as accumulation compression
SQUEEZE_K = 0.62
#: minutes around a session open that count as the Judas window
JUDAS_TOL_MIN = 45.0
#: component A distance gate: the manipulation is "pending" (trap-worthy)
#: only while price still sits within this many ATR of the swept level —
#: once the reversal has already paid out beyond it, the trap has
#: PLAYED OUT (e.g. demand-zone pullbacks after a completed sweep leg
#: are fresh entries, not the retail side of a pending stop hunt)
SWEEP_NEAR_ATR = 2.0
#: session opens (UTC) where the Judas swing statistically happens
SESSION_OPENS: tuple[tuple[str, float], ...] = (
    ("london open", 7.0),
    ("new york AM open", 12.0),
    ("new york PM open", 15.5),
)


def _body_run(df: pd.DataFrame, atr: float) -> dict | None:
    """Consecutive same-direction big bodies at the END of the window.

    Institutional displacement = several candles in a row whose BODIES
    (not wicks) each exceed ``DISP_ATR`` * ATR — the footprint the user
    saw as "অনেক বড় কয়েকটি এক সাথে ভায়ার ক্যান্ডেল".
    """
    if len(df) < DISP_MIN_RUN or atr <= 0:
        return None
    o = df["o"].to_numpy(dtype=float)
    c = df["c"].to_numpy(dtype=float)
    t_arr = df["time_utc"].to_numpy()
    n = len(o)
    k = n - 1
    run_dir = 1 if c[k] > o[k] else (-1 if c[k] < o[k] else 0)
    if run_dir == 0:
        return None
    run = 0
    strength = 0.0
    first_i = k
    while k >= 0:
        body = (c[k] - o[k]) * run_dir
        if body >= DISP_ATR * atr:
            run += 1
            strength += body / atr
            first_i = k
            k -= 1
        else:
            break
    if run < DISP_MIN_RUN:
        return None
    return {
        "dir": "up" if run_dir > 0 else "down",
        "run": int(run),
        "strength_atr": round(float(strength), 2),
        "t": t_arr[first_i],
        "bars_ago": int(n - 1 - first_i),
        "price": round(float(o[first_i]), 2),  # chart anchor for the leg
    }


def amd_state(df: pd.DataFrame, window: int = AMD_WINDOW_BARS) -> dict:
    """Classify the last ``window`` bars into the AMD cycle.

    Returns::

        {
          "phase":  "accumulation" | "manipulation" | "distribution" | "none",
          "range":  {"hi", "lo", "mid", "width_atr", "t0"},
          "sweep":  {"side": "SSL"|"BSL", "level", "t", "bars_ago",
                     "reclaimed": bool, "depth_atr"} | None,
          "displacement": {"dir", "run", "strength_atr", "t", "bars_ago"} | None,
          "atr": float,          # ATR(14) of the window
          "note": str,           # one human sentence
        }

    The sweep scan uses the range of the window EXCLUDING the fresh
    sweep bars themselves — the manipulation is a break of the PRIOR
    range, not of itself.
    """
    out: dict[str, Any] = {
        "phase": "none",
        "range": None,
        "sweep": None,
        "displacement": None,
        "atr": 0.0,
        "note": "no readable AMD structure",
    }
    if df is None or len(df) < 30:
        out["note"] = "insufficient bars for AMD read"
        return out
    frame = df.iloc[-window:]
    n = len(frame)
    h = frame["h"].to_numpy(dtype=float)
    low = frame["l"].to_numpy(dtype=float)
    c = frame["c"].to_numpy(dtype=float)
    t_arr = frame["time_utc"].to_numpy()
    atr = float(np.mean(h[-14:] - low[-14:])) or 1e-9
    out["atr"] = round(atr, 3)

    last_close = float(c[-1])

    # ---- the PRIOR range (excludes the freshest sweep bars) and the
    # range of the whole window (for the box the chart draws)
    prior = frame.iloc[:-SWEEP_FRESH_BARS]
    p_hi = float(prior["h"].max())
    p_lo = float(prior["l"].min())
    w_hi = float(h.max())
    w_lo = float(low.min())
    width = w_hi - w_lo
    out["range"] = {
        "hi": round(w_hi, 2),
        "lo": round(w_lo, 2),
        "mid": round((w_hi + w_lo) / 2.0, 2),
        "width_atr": round(width / atr, 2),
        "t0": t_arr[0],
    }

    # ---- displacement run at the end of the window (dist. footprint)
    disp = _body_run(frame, atr)
    out["displacement"] = disp

    # ---- freshest manipulation: a break of the PRIOR range extreme that
    # closed back inside within RECLAIM_BARS bars
    sweep: dict | None = None
    seg = min(SWEEP_FRESH_BARS, n)
    for k in range(n - seg, n):
        reclaimed = False
        depth = 0.0
        # SSL manipulation: wick below the prior low, close back above it
        if low[k] < p_lo and c[k] > p_lo:
            reclaimed = True
            depth = (p_lo - low[k]) / atr
            side = "SSL"
            level = p_lo
        # BSL manipulation: wick above the prior high, close back below it
        elif h[k] > p_hi and c[k] < p_hi:
            reclaimed = True
            depth = (h[k] - p_hi) / atr
            side = "BSL"
            level = p_hi
        if reclaimed:
            sweep = {
                "side": side,
                "level": round(float(level), 2),
                "t": t_arr[k],
                "bars_ago": int(n - 1 - k),
                "reclaimed": True,
                "depth_atr": round(float(depth), 2),
            }
    # keep the FRESHEST qualifying sweep (last in the loop wins)

    out["sweep"] = sweep

    # ---- phase verdict (priority: distribution > manipulation >
    # accumulation; a phase needs its evidence)
    if (
        disp is not None
        and sweep is not None
        and (
            (sweep["side"] == "SSL" and disp["dir"] == "up")
            or (sweep["side"] == "BSL" and disp["dir"] == "down")
        )
        and disp["bars_ago"] <= sweep["bars_ago"] + RECLAIM_BARS + 4
    ):
        # the sweep happened, price reclaimed, and displacement already
        # ran the OTHER way — the cycle is completing
        out["phase"] = "distribution"
        out["note"] = (
            f"manipulation ({sweep['side']} @ {sweep['level']:.2f}) complete — "
            f"{disp['run']}x displacement {disp['dir']} "
            f"({disp['strength_atr']:.1f} ATR) is distributing the move"
        )
    elif sweep is not None:
        out["phase"] = "manipulation"
        out["note"] = (
            f"{sweep['side']} @ {sweep['level']:.2f} swept "
            f"{sweep['depth_atr']:.2f} ATR deep and reclaimed "
            f"{sweep['bars_ago']} bars ago — false break, reversal pending"
        )
    else:
        # no fresh false break: is the window at least compressed?
        expected = atr * np.sqrt(n)
        if width <= SQUEEZE_K * expected:
            out["phase"] = "accumulation"
            where = "premium" if last_close > out["range"]["mid"] else "discount"
            out["note"] = (
                f"range {w_lo:.2f}-{w_hi:.2f} compressed "
                f"({out['range']['width_atr']:.1f} ATR wide, price "
                f"{last_close:.2f} at {where}"
                " — coiling for the next expansion"
            )
        else:
            out["phase"] = "none"
            out["note"] = "trending window — no AMD trap structure"
    return out


# ------------------------------------------------------------------ timing


def session_context(ts_utc: datetime | pd.Timestamp) -> dict:
    """Kill-zone name + the Judas-window flag for one timestamp.

    The Judas window = within ``JUDAS_TOL_MIN`` of a session open
    (London 07:00 / NY AM 12:00 / NY PM 15:30 UTC): the time ICT marks
    as the statistical home of the manipulation leg — a sweep that
    prints inside this window deserves EXTRA suspicion.
    """
    ts = pd.Timestamp(ts_utc)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    hour = ts.hour + ts.minute / 60.0
    kz = smc.kill_zone(ts)
    judas_of: str | None = None
    for name, open_h in SESSION_OPENS:
        # distance across the midnight wrap is irrelevant for 7 / 12 / 15.5
        if abs(hour - open_h) * 60.0 <= JUDAS_TOL_MIN:
            judas_of = name
            break
    return {
        "name": kz["name"] if kz["in"] else "off-session",
        "in_killzone": bool(kz["in"]),
        "judas_window": judas_of is not None,
        "judas_of": judas_of,
        "note": (
            f"{kz['name']} kill zone" + (
                f" — Judas window ({judas_of} ±{JUDAS_TOL_MIN:.0f}m), "
                "session-open sweeps deserve extra suspicion"
                if judas_of else ""
            )
        ) if kz["in"] else (
            f"Judas window ({judas_of} ±{JUDAS_TOL_MIN:.0f}m) — session open"
            if judas_of else "off-session — no kill-zone timing edge"
        ),
    }


# ------------------------------------------------------------------ trap


def trap_risk(
    df: pd.DataFrame,
    direction: str,
    entry: float,
    sl: float,
    state: dict | None = None,
    now: datetime | pd.Timestamp | None = None,
) -> dict:
    """Is this candidate trade the RETAIL side of an AMD manipulation?

    Components (the exact anatomy of the user's trapped sell):

    A. **Fresh opposing sweep reclaimed** (0.85): a SELL while an SSL
       sweep was just reclaimed = the institutions already BOUGHT those
       sell stops — the distribution runs UP into the sell. BUY mirrors
       with a reclaimed BSL sweep.
    B. **Liquidity pool inside the risk window** (0.55, 0.70 when it
       sits on the FILL PATH): a SELL whose entry..SL brackets an
       unswept SSL pool is fishing INTO the draw — the market visits
       that pool before it reverses, stopping the sell out.
    C. **Opposing displacement run** (0.75): >= 2 consecutive
       institutional bodies (>= DISP_ATR * ATR) already printing against
       the trade — momentum has flipped; the entry is chasing.
    D. **Judas timing** (+0.15, only stacked on A or C): the signal
       prints inside a session-open window — where false breaks live.

    Returns ``{"risk": 0..1, "phase", "reasons": [...], "components":
    {...}, "session": {...}}`` — reasons are full sentences for the
    trace / the app's Signal Analysis panel.
    """
    st = state if state is not None else amd_state(df)
    ses = session_context(now if now is not None else pd.Timestamp.now(tz="UTC"))
    out: dict[str, Any] = {
        "risk": 0.0,
        "phase": st.get("phase", "none"),
        "amd_note": st.get("note", ""),
        "reasons": [],
        "components": {},
        "session": ses,
    }
    if df is None or len(df) < 30 or st.get("atr", 0) <= 0:
        return out
    want_sell = direction == "SELL"
    reasons: list[str] = []
    comp: dict[str, float] = {}

    # ---- A: fresh opposing sweep + reclaim (the reversal PENDING)
    sweep = st.get("sweep")
    if sweep and sweep.get("bars_ago", 99) <= SWEEP_FRESH_BARS:
        opposing = (sweep["side"] == "SSL") if want_sell else (sweep["side"] == "BSL")
        if opposing:
            # has the reversal already paid out? A sweep whose level sits
            # far behind the market already distributed — trading the
            # pullback AFTER it is a fresh entry, not the trapped side
            atr_st = float(st.get("atr") or 0.0) or 1e-9
            last_close = float(df["c"].iloc[-1])
            dist_atr = abs(last_close - float(sweep["level"])) / atr_st
            if dist_atr <= SWEEP_NEAR_ATR:
                comp["sweep_reclaimed"] = 0.85
                reasons.append(
                    f"{sweep['side']} @ {sweep['level']:.2f} swept "
                    f"{sweep['depth_atr']:.2f} ATR deep and reclaimed "
                    f"{sweep['bars_ago']} bars ago — institutions harvested the "
                    f"{'sell' if want_sell else 'buy'} stops there; the "
                    f"reversal runs against this {direction}"
                )

    # ---- B: liquidity pool inside the risk window / on the fill path
    try:
        liq = smc.detect_liquidity(df.iloc[-160:])
    except Exception:  # noqa: BLE001 — analysis must never break the gate
        liq = {"levels": [], "sweeps": []}
    pools = [lv for lv in (liq.get("levels") or [])
             if lv.get("kind") == ("SSL" if want_sell else "BSL")]
    if pools:
        lo_r, hi_r = (sl, entry) if want_sell else (entry, sl)
        in_window = [p for p in pools if lo_r <= float(p["price"]) <= hi_r]
        if in_window:
            p = in_window[0]
            comp["pool_in_risk"] = 0.55
            reasons.append(
                f"{p['kind']} pool @ {float(p['price']):.2f} sits inside the "
                f"risk window ({lo_r:.2f}..{hi_r:.2f}) — the market is drawn "
                f"there before it reverses; the {direction} stop feeds the hunt"
            )
            # on the fill path (between market and a pending entry) is worse
            market = float(df["c"].iloc[-1])
            on_path = (
                entry <= float(p["price"]) <= market
                if want_sell else market <= float(p["price"]) <= entry
            )
            if on_path:
                comp["pool_in_risk"] = 0.70
                reasons[-1] += " (and on the fill path — the pending fills AT the pool)"

    # ---- C: opposing displacement run
    disp = st.get("displacement")
    if disp and disp.get("bars_ago", 99) <= SWEEP_FRESH_BARS:
        against = (disp["dir"] == "up") if want_sell else (disp["dir"] == "down")
        if against:
            comp["displacement_against"] = 0.75
            reasons.append(
                f"{disp['run']} consecutive institutional bodies "
                f"({disp['strength_atr']:.1f} ATR total) printing "
                f"{disp['dir']} — momentum already flipped against this {direction}"
            )

    risk = max(comp.values()) if comp else 0.0
    if comp and ses.get("judas_window"):
        comp["judas_timing"] = 0.15
        risk = min(1.0, risk + 0.15)
        reasons.append(
            f"timing: {ses['judas_of']} Judas window — session-open false "
            "breaks live here"
        )
    out["risk"] = round(float(min(max(risk, 0.0), 1.0)), 2)
    out["components"] = comp
    out["reasons"] = reasons[:4]
    return out
