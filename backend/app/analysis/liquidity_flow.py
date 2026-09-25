"""D-071 — liquidity RUN vs SWEEP state machine + the draw-on-liquidity map.

User directive (Bengali): "গোল্ড মার্কেট আমি দেখতে পাই, ক্যান্ডেল এর লিকুডিটি
নিয়ে চলে — কোনো লেভেল বা zone এর বা কোনো একটি ক্যান্ডেল এর লিকুডিটি নিলো,
কি নিল না। নেওয়ার পরে লিকুডিটি রান করে নাকি সুয়েপ করবে এই বিষয় টি কোন
টাইম ফ্রেম এ চার্ট এ কিভাবে বুঝাবেন, বা কিভাবে ড্রয়িং করবেন।"

The gold market runs on liquidity. Every pool of resting stops (equal
highs = BSL, equal lows = SSL, prior-day H/L, swing extremes) goes
through a LIFE CYCLE the chart can now draw on every timeframe:

  untouched  — the pool is intact; price never traded through it. This
               is the DRAW: unharvested liquidity the market is pulled
               toward ("price moves from pool to pool").
  swept      — a candle wicked THROUGH the pool and price now sits back
               on the origin side: the stops were harvested and
               REJECTED (stop-hunt / manipulation). Expectation:
               REVERSAL away from the pool.
  run        — a candle CLOSED THROUGH the pool and price still sits
               beyond it: the pool was consumed for CONTINUATION.
               Expectation: price keeps running in the break direction
               toward the NEXT pool.

The distinction is exactly what the user asked for: "নেওয়ার পরে
লিকুডিটি রান করে নাকি সুয়েপ করবে" — after the take, RUN or SWEEP?

State rule (no lookahead, closed bars only):
  let X = the LAST bar whose wick traded through the pool level
  - no such bar                -> untouched
  - close(last bar) on origin  -> swept  (harvested & rejected)
  - close(last bar) beyond     -> run    (consumed, continuation)

Per-pool telemetry:
  t_event   — when the liquidity was FIRST taken (the harvest candle)
  disp_atr  — displacement since the take in the EXPECTED direction,
              in ATR units (reversal distance for sweeps, continuation
              distance for runs) — the honest "did it follow through?"
  dist_atr  — untouched pools only: how far price sits from the draw
  hits      — how many times the pool was probed/taken
"""

from __future__ import annotations

import logging

import pandas as pd

from app.analysis import indicators as ind

logger = logging.getLogger("xauusd.liqflow")

#: equal-high/low cluster tolerance in ATR units (same as smc liquidity)
POOL_TOL_ATR = 0.15
#: a wick must exceed the pool by THIS much (ATR units) to count as a
#: take — micro-wicks and float dust never harvest liquidity (the
#: same noise lesson as D-069's FVG floors)
TAKE_DEPTH_ATR = 0.05
#: single-swing ("candle") pools within this ATR distance of price are
#: noise — price is sitting on them, they are not a forward draw
SWING_POOL_MIN_ATR = 0.30
#: pools closer than this to current price are "at the door", not a draw
DRAW_MIN_ATR = 0.35
#: a sweep/run event counts as FRESH (directional driver) this many bars
FRESH_EVENT_BARS = 12
#: max pools per timeframe (a pro chart labels a handful, not a swarm)
MAX_POOLS = 8


def _atr(df: pd.DataFrame) -> float:
    try:
        s = ind.atr_series(df, 14)
        v = float(s.iloc[-1])
        return v if v > 0 else 0.0
    except Exception:  # noqa: BLE001 — telemetry must never break
        return 0.0


def _pool(kind: str, price: float, t, i: int, hits: int, source: str) -> dict:
    return {
        "kind": kind,  # "BSL" | "SSL"
        "price": round(float(price), 2),
        "t": t,
        "i": int(i),
        "hits": int(hits),
        "source": source,  # "eqh" | "eql" | "pdh" | "pdl" | "swing"
        "state": "untouched",
        "t_event": None,
        "i_event": None,
        "disp_atr": None,
        "dist_atr": None,
    }


def detect_pools(df: pd.DataFrame, atr: float, max_pools: int = MAX_POOLS) -> list[dict]:
    """Resting-liquidity pools of this frame: equal highs/lows clusters,
    prior-day H/L and the freshest swing extremes (single-candle
    liquidity). Ordered oldest-first so the state scan walks forward."""

    if df is None or len(df) < 20 or atr <= 0:
        return []

    tol = max(atr * POOL_TOL_ATR, 1e-9)
    pts = ind.swings(df, 2, 2)
    highs = [p for p in pts if p["kind"] == "high"]
    lows = [p for p in pts if p["kind"] == "low"]
    pools: list[dict] = []

    # 1. equal highs (BSL) / equal lows (SSL) — the classic pools.
    for arr, kind, source in ((highs, "BSL", "eqh"), (lows, "SSL", "eql")):
        i = len(arr) - 1
        made = 0
        while i >= 1 and made < 3:
            if abs(arr[i]["price"] - arr[i - 1]["price"]) <= tol:
                pools.append(_pool(
                    kind,
                    (arr[i]["price"] + arr[i - 1]["price"]) / 2.0,
                    arr[i]["t"], arr[i]["i"], 2, source,
                ))
                made += 1
            i -= 1

    # 2. prior-day high / low — the session's classic draw.
    if "time_utc" in df and len(df) > 2:
        t_ser = pd.Series(pd.to_datetime(df["time_utc"]))
        last_day = t_ser.iloc[-1].date()
        prev = df[t_ser.dt.date.to_numpy() != last_day]
        if len(prev):
            pools.append(_pool(
                "BSL", float(prev["h"].max()),
                prev["time_utc"].iloc[-1], len(prev) - 1, 1, "pdh",
            ))
            pools.append(_pool(
                "SSL", float(prev["l"].min()),
                prev["time_utc"].iloc[-1], len(prev) - 1, 1, "pdl",
            ))

    # 3. the freshest swing high/low — single-candle liquidity (the
    #    "কোনো একটি ক্যান্ডেল এর লিকুডিটি" of the directive).
    for arr, kind in ((highs, "BSL"), (lows, "SSL")):
        if arr:
            p = arr[-1]
            pools.append(_pool(kind, p["price"], p["t"], p["i"], 1, "swing"))

    # dedupe near-identical levels (keep the first = oldest origin)
    dedup: list[dict] = []
    for p in pools:
        if any(
            q["kind"] == p["kind"] and abs(q["price"] - p["price"]) < tol
            for q in dedup
        ):
            continue
        dedup.append(p)
    return dedup[:max_pools]


def _classify(pool: dict, df: pd.DataFrame, atr: float) -> dict:
    """Walk the bars after the pool origin and stamp its live state."""

    n = len(df)
    h = df["h"].to_numpy(dtype=float)
    low = df["l"].to_numpy(dtype=float)
    c = df["c"].to_numpy(dtype=float)
    times = df["time_utc"].tolist()
    p = float(pool["price"])
    start = int(pool["i"]) + 1
    is_bsl = pool["kind"] == "BSL"

    # a TAKE needs real depth: the wick must pierce the pool by
    # TAKE_DEPTH_ATR (or the bar must CLOSE beyond it — a close is a
    # real trade, always a consumption). Wicks that graze the level
    # by float dust never harvest liquidity.
    depth = max(atr * TAKE_DEPTH_ATR, 1e-9)
    first_i = None
    hits = 0
    for k in range(start, n):
        wick = (h[k] > p + depth) if is_bsl else (low[k] < p - depth)
        closed = (c[k] > p) if is_bsl else (c[k] < p)
        if wick or closed:
            if first_i is None:
                first_i = k
            hits += 1

    pool["hits"] = max(pool["hits"], hits)
    if first_i is None:
        pool["state"] = "untouched"
        return pool

    pool["t_event"] = times[first_i]
    pool["i_event"] = first_i
    # where price stands NOW relative to the pool (after the LAST
    # excursion through it) decides run vs sweep — the honest rule:
    # a sweep that price later closed through IS a run.
    last_close = float(c[-1])
    beyond_now = last_close > p if is_bsl else last_close < p
    pool["state"] = "run" if beyond_now else "swept"

    # displacement since the take in the EXPECTED direction:
    #  - sweep(BSL): reversal DOWN  -> lowest low since the event
    #  - run(BSL):   continuation UP -> highest high since the event
    if pool["state"] == "swept":
        if is_bsl:
            ext = float(low[first_i:].min())
        else:
            ext = float(h[first_i:].max())
    else:
        if is_bsl:
            ext = float(h[first_i:].max())
        else:
            ext = float(low[first_i:].min())
    if atr > 0:
        pool["disp_atr"] = round(abs(ext - p) / atr, 2)
    return pool


def liquidity_flow(df: pd.DataFrame, price: float | None = None,
                   max_pools: int = MAX_POOLS) -> dict:
    """The full liquidity read of one timeframe: pools with live states
    + the draw map (which untouched pool the market is pulled toward).

    Returns {pools: [...], draw: {above, below, dir, fresh}} — pure,
    closed-bars only, safe on thin frames (returns empty).
    """

    out: dict = {
        "pools": [],
        "draw": {"above": None, "below": None, "dir": None, "fresh": None},
    }
    if df is None or len(df) < 20:
        return out
    atr = _atr(df)
    if atr <= 0:
        return out

    pools = detect_pools(df, atr, max_pools)
    pools = [_classify(p, df, atr) for p in pools]

    last_close = float(df["c"].iloc[-1])
    if price is not None and price > 0:
        last_close = float(price)

    n = len(df)
    untouched = [p for p in pools if p["state"] == "untouched"]
    for p in untouched:
        p["dist_atr"] = round(abs(p["price"] - last_close) / atr, 2)
    # single-swing pools the market is sitting on are noise, not a
    # forward draw (equal-cluster pools and PDH/PDL always stay)
    pools = [
        p for p in pools
        if not (
            p["state"] == "untouched" and p.get("source") == "swing"
            and (p.get("dist_atr") or 0.0) < SWING_POOL_MIN_ATR
        )
    ]
    untouched = [p for p in pools if p["state"] == "untouched"]

    # the DRAW: nearest untouched pool on each side of price
    above = [p for p in untouched if p["price"] > last_close and p["kind"] == "BSL"]
    below = [p for p in untouched if p["price"] < last_close and p["kind"] == "SSL"]
    # an untouched SSL ABOVE price (or BSL below) is behind price —
    # already-visited territory, not a forward draw; skip it.
    draw_above = min(above, key=lambda p: p["dist_atr"]) if above else None
    draw_below = min(below, key=lambda p: p["dist_atr"]) if below else None
    if draw_above is not None and draw_above["dist_atr"] < DRAW_MIN_ATR:
        draw_above = None
    if draw_below is not None and draw_below["dist_atr"] < DRAW_MIN_ATR:
        draw_below = None

    # the FRESH event: the most recent sweep/run (bars_ago <= FRESH_EVENT_BARS)
    # — the strongest short-term directional driver on the chart
    fresh = None
    for p in pools:
        if p["state"] == "untouched" or p["i_event"] is None:
            continue
        bars_ago = n - 1 - int(p["i_event"])
        if bars_ago < 0 or bars_ago > FRESH_EVENT_BARS:
            continue
        if fresh is None or bars_ago < fresh["bars_ago"]:
            fresh = {
                "state": p["state"],
                "side": p["kind"],
                "price": p["price"],
                "t": p["t_event"],
                "bars_ago": bars_ago,
                "disp_atr": p["disp_atr"],
                # sweep(BSL)=reversal down; sweep(SSL)=reversal up;
                # run(BSL)=continuation up; run(SSL)=continuation down
                "dir": (
                    ("down" if p["kind"] == "BSL" else "up")
                    if p["state"] == "swept"
                    else ("up" if p["kind"] == "BSL" else "down")
                ),
            }

    draw_dir = None
    if draw_above is not None or draw_below is not None:
        da = draw_above["dist_atr"] if draw_above else 99.0
        db = draw_below["dist_atr"] if draw_below else 99.0
        # nearest untouched pool wins the draw direction
        draw_dir = "up" if da <= db else "down"

    out["pools"] = pools
    out["draw"] = {
        "above": draw_above,
        "below": draw_below,
        "dir": draw_dir,
        "fresh": fresh,
    }
    return out
