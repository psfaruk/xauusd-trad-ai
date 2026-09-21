"""ICT / Smart Money Concepts (D-042) — pure, closed-bars, no lookahead.

Implements the concepts the user asked for, on any timeframe frame:

- Market structure: fractal swings labeled HH/HL/LH/LL, trend state
  (bullish / bearish / balanced) and BOS (break of structure — trend
  continuation) vs CHoCH (change of character — trend flip) events.
- Order blocks (OB): the last opposite candle before a displacement
  impulse that breaks a swing — the institutional footprint zone.
- Fair value gaps (FVG): 3-candle imbalances (inefficiency) price
  tends to revisit.
- Liquidity: equal highs / equal lows clusters (BSL/SSL pools) and the
  sweeps that harvest them (stop hunts / manipulation).
- Supply & demand zones: swing-base zones from which displacement
  originated.
- Premium/discount + OTE: position of price inside the dealing range
  (ICT optimal-trade-entry 0.62–0.79 retracement).
- Kill zones: London / New-York AM / NY PM / Asia ICT windows.

Every function returns plain JSON-serializable dicts so the same output
feeds the signal engine (confluence) and the /api/analysis chart
overlays.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd

from app.analysis.indicators import atr as atr_last
from app.analysis.indicators import atr_series, swings

# ICT kill zones (UTC) — the windows where institutions move size.
KILL_ZONES: tuple[tuple[str, float, float], ...] = (
    ("asia", 0.0, 6.0),
    ("london", 7.0, 10.0),
    ("ny-am", 12.0, 15.0),
    ("ny-pm", 15.5, 17.0),
)


def kill_zone(ts_utc: datetime | pd.Timestamp) -> dict:
    """Which ICT kill zone a timestamp sits in (None outside)."""
    hour = ts_utc.hour + ts_utc.minute / 60.0
    for name, start, end in KILL_ZONES:
        if start <= hour < end:
            return {"name": name, "in": True}
    return {"name": "off-session", "in": False}


# ------------------------------------------------------- market structure


def detect_structure(df: pd.DataFrame, left: int = 2, right: int = 2,
                     max_events: int = 6) -> dict:
    """Swing sequence -> trend state + BOS/CHoCH events.

    Returns {
      trend: "bullish" | "bearish" | "balanced",
      swings: [{t, price, kind, label}],          # last few, labeled
      events: [{t, level, kind: "BOS"|"CHoCH", dir: "up"|"down"}],
      last_event: {...} | None,
    }
    """
    pts = swings(df, left, right)
    out: dict[str, Any] = {
        "trend": "balanced",
        "swings": [],
        "events": [],
        "last_event": None,
    }
    if len(pts) < 3:
        return out

    # label the swing sequence (compare like kinds: highs vs highs)
    highs = [p for p in pts if p["kind"] == "high"]
    lows = [p for p in pts if p["kind"] == "low"]
    for arr, up_label, down_label in (
        (highs, "HH", "LH"),
        (lows, "HL", "LL"),
    ):
        for i in range(1, len(arr)):
            arr[i]["label"] = up_label if arr[i]["price"] > arr[i - 1]["price"] else down_label
    if lows:
        lows[0]["label"] = lows[0].get("label", "L")

    # trend from the latest labels on each side
    bull = 0
    if highs and highs[-1].get("label") == "HH":
        bull += 1
    if lows and lows[-1].get("label") == "HL":
        bull += 1
    bear = 0
    if highs and highs[-1].get("label") == "LH":
        bear += 1
    if lows and lows[-1].get("label") == "LL":
        bear += 1
    out["trend"] = "bullish" if bull > bear else ("bearish" if bear > bull else "balanced")

    # BOS / CHoCH events: a CLOSE beyond the last confirmed swing level.
    events: list[dict] = []
    closes = df["c"].values
    times = df["time_utc"].tolist()
    last_high: dict | None = None
    last_low: dict | None = None
    trend_dir: str | None = None
    pi = 0
    for k in range(len(df)):
        # swings confirmed at bar i become actionable at i+right
        while pi < len(pts) and pts[pi]["i"] + right <= k:
            p = pts[pi]
            if p["kind"] == "high":
                last_high = p
            else:
                last_low = p
            pi += 1
        if last_high is not None and closes[k] > last_high["price"]:
            kind = "BOS" if trend_dir == "up" else ("CHoCH" if trend_dir == "down" else "BOS")
            events.append({"t": times[k], "level": float(last_high["price"]),
                           "kind": kind, "dir": "up"})
            trend_dir = "up"
            last_high = None  # consumed until a new swing high confirms
        elif last_low is not None and closes[k] < last_low["price"]:
            kind = "BOS" if trend_dir == "down" else ("CHoCH" if trend_dir == "up" else "BOS")
            events.append({"t": times[k], "level": float(last_low["price"]),
                           "kind": kind, "dir": "down"})
            trend_dir = "down"
            last_low = None
    out["events"] = events[-max_events:]
    out["last_event"] = events[-1] if events else None
    # structure trend from events refines the swing-label vote
    if out["last_event"] is not None:
        if out["last_event"]["kind"] == "CHoCH":
            out["trend"] = "bullish" if out["last_event"]["dir"] == "up" else "bearish"
        elif out["trend"] == "balanced":
            out["trend"] = "bullish" if out["last_event"]["dir"] == "up" else "bearish"
    out["swings"] = [
        {"t": p["t"], "price": p["price"], "kind": p["kind"],
         "label": p.get("label", "")}
        for p in pts[-8:]
    ]
    return out


# ------------------------------------------------------------ order blocks


def detect_order_blocks(df: pd.DataFrame, max_zones: int = 6,
                        impulse_atr: float = 1.2) -> list[dict]:
    """Last opposite candle before a displacement move (institutional entry).

    Bullish OB: a down-close candle whose NEXT candle's body >= impulse_atr*ATR
    and closes above the swing area — zone = the down candle's full candle
    range. Mitigated once price trades back through it.
    """
    a = atr_series(df, 14)
    if len(df) < 20 or a.isna().all():
        return []
    out: list[dict] = []
    n = len(df)
    for k in range(2, n - 1):
        atr_v = float(a.iloc[k]) if pd.notna(a.iloc[k]) else 0.0
        if atr_v <= 0:
            continue
        bar, nxt = df.iloc[k], df.iloc[k + 1]
        body = abs(float(nxt["c"]) - float(nxt["o"]))
        if body < impulse_atr * atr_v:
            continue
        zone_hi = max(float(bar["o"]), float(bar["c"]))
        zone_lo = min(float(bar["o"]), float(bar["c"]))
        wick_hi, wick_lo = float(bar["h"]), float(bar["l"])
        if float(bar["c"]) < float(bar["o"]) and float(nxt["c"]) > float(nxt["o"]):
            out.append({"side": "bullish", "t": df["time_utc"].iloc[k],
                        "hi": zone_hi, "lo": zone_lo, "wick_hi": wick_hi,
                        "wick_lo": wick_lo, "impulse": round(body / atr_v, 2)})
        elif float(bar["c"]) > float(bar["o"]) and float(nxt["c"]) < float(nxt["o"]):
            out.append({"side": "bearish", "t": df["time_utc"].iloc[k],
                        "hi": zone_hi, "lo": zone_lo, "wick_hi": wick_hi,
                        "wick_lo": wick_lo, "impulse": round(body / atr_v, 2)})
    # mitigation: first time price returned into the zone after formation
    # (`mit_t` — ICT retests the zone; the FIRST touch is the entry, so
    # the confluence factor accepts fresh touches, not never-touched only)
    n = len(df)
    for ob in out:
        idx = df.index[df["time_utc"] == ob["t"]]
        start = int(idx[0]) + 2 if len(idx) else 0
        ob["mitigated"] = False
        ob["mit_t"] = None
        for k in range(start, n):
            hi, lo = float(df["h"].iloc[k]), float(df["l"].iloc[k])
            if lo <= ob["hi"] and hi >= ob["lo"]:
                ob["mitigated"] = True
                ob["mit_t"] = df["time_utc"].iloc[k]
                break
    return out[-max_zones:]


# -------------------------------------------------------- fair value gaps


def detect_fvg(df: pd.DataFrame, max_gaps: int = 8) -> list[dict]:
    """3-candle imbalances. Bullish FVG: bar[k-1].high < bar[k+1].low."""
    out: list[dict] = []
    n = len(df)
    if n < 3:
        return out
    for k in range(1, n - 1):
        prev_hi = float(df["h"].iloc[k - 1])
        prev_lo = float(df["l"].iloc[k - 1])
        nxt_hi = float(df["h"].iloc[k + 1])
        nxt_lo = float(df["l"].iloc[k + 1])
        if nxt_lo > prev_hi:
            gap = nxt_lo - prev_hi
            out.append({"side": "bullish", "t": df["time_utc"].iloc[k],
                        "hi": nxt_lo, "lo": prev_hi, "gap": gap,
                        "filled": False, "filled_pct": 0})
        elif nxt_hi < prev_lo:
            gap = prev_lo - nxt_hi
            out.append({"side": "bearish", "t": df["time_utc"].iloc[k],
                        "hi": prev_lo, "lo": nxt_hi, "gap": gap,
                        "filled": False, "filled_pct": 0})
    # fill check: when did price first trade through the gap's near edge
    n = len(df)
    for g in out:
        idx = df.index[df["time_utc"] == g["t"]]
        start = int(idx[0]) + 2 if len(idx) else 0
        g["fill_t"] = None
        for k in range(start, n):
            lo, hi = float(df["l"].iloc[k]), float(df["h"].iloc[k])
            if g["side"] == "bullish" and lo <= g["lo"]:
                g["filled"] = True
                g["fill_t"] = df["time_utc"].iloc[k]
                break
            if g["side"] == "bearish" and hi >= g["hi"]:
                g["filled"] = True
                g["fill_t"] = df["time_utc"].iloc[k]
                break
    return [g for g in out if g["gap"] > 0][-max_gaps:]


# ------------------------------------------------------------- liquidity


def detect_liquidity(df: pd.DataFrame, tol_atr: float = 0.15,
                     max_levels: int = 6) -> dict:
    """Equal highs/lows pools + prior-day H/L + recent sweeps.

    Returns {levels: [{kind: "BSL"|"SSL", price, t, hits}], sweeps: [...]}.
    BSL = buy-side liquidity (resting buys above equal highs); SSL =
    sell-side liquidity (resting sells below equal lows).
    """
    a = atr_last(df, 14)
    out: dict[str, Any] = {"levels": [], "sweeps": []}
    if len(df) < 20:
        return out
    tol = max(a * tol_atr, 1e-9)
    pts = swings(df, 2, 2)
    highs = [p for p in pts if p["kind"] == "high"]
    lows = [p for p in pts if p["kind"] == "low"]
    levels: list[dict] = []
    for arr, kind in ((highs, "BSL"), (lows, "SSL")):
        i = len(arr) - 1
        while i >= 1 and len([lv for lv in levels if lv["kind"] == kind]) < 2:
            if abs(arr[i]["price"] - arr[i - 1]["price"]) <= tol:
                levels.append({
                    "kind": kind,
                    "price": float((arr[i]["price"] + arr[i - 1]["price"]) / 2.0),
                    "t": arr[i]["t"], "hits": 2,
                })
            i -= 1
    # prior-day high/low as classic liquidity draws
    if "time_utc" in df and len(df) > 2:
        last_day = pd.Timestamp(df["time_utc"].iloc[-1]).date()
        prev = df[[pd.Timestamp(t).date() != last_day for t in df["time_utc"]]]
        if len(prev):
            levels.append({"kind": "BSL", "price": float(prev["h"].max()),
                           "t": prev["time_utc"].iloc[-1], "hits": 1,
                           "tag": "PDH"})
            levels.append({"kind": "SSL", "price": float(prev["l"].min()),
                           "t": prev["time_utc"].iloc[-1], "hits": 1,
                           "tag": "PDL"})
    out["levels"] = levels[-max_levels:]
    # sweeps: last bar wicked beyond a level but closed back inside
    if len(df) >= 1 and out["levels"]:
        last = df.iloc[-1]
        h, low, c = float(last["h"]), float(last["l"]), float(last["c"])
        for lv in out["levels"]:
            if lv["kind"] == "BSL" and h > lv["price"] and c < lv["price"]:
                out["sweeps"].append({"kind": "BSL", "price": lv["price"],
                                      "t": df["time_utc"].iloc[-1]})
            if lv["kind"] == "SSL" and low < lv["price"] and c > lv["price"]:
                out["sweeps"].append({"kind": "SSL", "price": lv["price"],
                                      "t": df["time_utc"].iloc[-1]})
    return out


# ------------------------------------------------------ supply and demand


def detect_supply_demand(df: pd.DataFrame, max_zones: int = 4) -> list[dict]:
    """Swing-base zones: consolidation candles that launched displacement.

    Demand: the last down candle before an up impulse that set a swing
    high. Supply: mirror. Zones use the base candle range (wick-to-wick
    for the origin side).
    """
    a = atr_series(df, 14)
    out: list[dict] = []
    n = len(df)
    if n < 30:
        return out
    for k in range(3, n - 1):
        atr_v = float(a.iloc[k]) if pd.notna(a.iloc[k]) else 0.0
        if atr_v <= 0:
            continue
        base, nxt = df.iloc[k], df.iloc[k + 1]
        body = abs(float(nxt["c"]) - float(nxt["o"]))
        if body < 1.5 * atr_v:
            continue
        if float(nxt["c"]) > float(nxt["o"]) and float(base["c"]) < float(base["o"]):
            out.append({"side": "demand", "t": df["time_utc"].iloc[k],
                        "hi": float(base["h"]), "lo": float(base["l"])})
        elif float(nxt["c"]) < float(nxt["o"]) and float(base["c"]) > float(base["o"]):
            out.append({"side": "supply", "t": df["time_utc"].iloc[k],
                        "hi": float(base["h"]), "lo": float(base["l"])})
    # drop zones fully traded through (broken)
    kept: list[dict] = []
    for z in out:
        idx = df.index[df["time_utc"] == z["t"]]
        start = int(idx[0]) + 2 if len(idx) else 0
        broken = False
        for k in range(start, n):
            c = float(df["c"].iloc[k])
            if z["side"] == "demand" and c < z["lo"]:
                broken = True
            if z["side"] == "supply" and c > z["hi"]:
                broken = True
        if not broken:
            kept.append(z)
    return kept[-max_zones:]


# ---------------------------------------------------- premium / discount


def premium_discount(df: pd.DataFrame, lookback: int = 60) -> dict:
    """Dealing-range position: premium/discount/equilibrium + OTE zone.

    The range = last `lookback` bars' swing extremes; OTE = the ICT
    0.62–0.79 retracement of the active leg.
    """
    if len(df) < 10:
        return {"state": "unknown", "range_hi": None, "range_lo": None,
                "eq": None, "ote": None}
    window = df.iloc[-lookback:]
    hi = float(window["h"].max())
    lo = float(window["l"].min())
    last = float(df["c"].iloc[-1])
    if hi <= lo:
        return {"state": "unknown", "range_hi": hi, "range_lo": lo,
                "eq": (hi + lo) / 2, "ote": None}
    eq = (hi + lo) / 2.0
    rng = hi - lo
    # direction of the active leg: which extreme is more recent
    hi_i = int(window["h"].idxmax())
    lo_i = int(window["l"].idxmin())
    leg_up = hi_i > lo_i
    if leg_up:  # up leg: OTE = deep retracement zone below price
        ote = {"hi": hi - 0.62 * rng, "lo": hi - 0.79 * rng}
    else:
        ote = {"hi": lo + 0.79 * rng, "lo": lo + 0.62 * rng}
    state = "premium" if last > eq + 0.05 * rng else (
        "discount" if last < eq - 0.05 * rng else "equilibrium"
    )
    return {"state": state, "range_hi": hi, "range_lo": lo, "eq": eq,
            "ote": ote, "leg": "up" if leg_up else "down"}
