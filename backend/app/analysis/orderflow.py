"""Order-flow & institutional-activity estimation (D-042).

MetaTrader's retail API does not expose the true depth-of-book, so this
module ESTIMATES institutional footprints from what the broker DOES
give us per bar: tick volume, OHLC shape and sequence. The user asked
for "where did the big orders land, what volume, where did banks enter,
their manipulation and accumulation" — delivered honestly:

- volume_profile — volume-at-price histogram (POC / value area) over
  the window: WHERE the size traded.
- whale events — bars whose tick volume spikes >= z standard deviations
  above the trailing mean, classified by shape:
    * momentum  — huge body in the spike direction (institutional
                  market entry / "bank entry")
    * sweep     — long rejection wick, close back inside (stop hunt /
                  liquidity grab = ICT manipulation)
    * absorption — huge volume, tiny body (limit orders absorbing
                  market orders = accumulation / distribution)
- delta proxy — buy-vs-sell share of each bar's volume inferred from
  the close's position inside the bar range (a standard proxy).

All outputs are JSON-serializable and drive both the engine's volume
confluence factor and the chart's whale markers + the Signals-tab
context lines.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from app.analysis.indicators import atr_series


def volume_profile(df: pd.DataFrame, bins: int = 24) -> dict:
    """Volume-at-price over the frame: POC + value area (70%)."""
    if len(df) < 10:
        return {"poc": None, "vah": None, "val": None, "hvns": [], "lvns": []}
    lo = float(df["l"].min())
    hi = float(df["h"].max())
    if hi <= lo:
        return {"poc": lo, "vah": hi, "val": lo, "hvns": [], "lvns": []}
    vol = [0.0] * bins
    for _, bar in df.iterrows():
        b_lo, b_hi = float(bar["l"]), float(bar["h"])
        v = float(bar["v"])
        if b_hi <= lo or b_lo >= hi or v <= 0:
            continue
        # distribute the bar's volume evenly across the bins it spans
        first = max(0, min(bins - 1, int((b_lo - lo) / (hi - lo) * bins)))
        last = max(0, min(bins - 1, int((b_hi - lo) / (hi - lo) * bins)))
        span = last - first + 1
        for b in range(first, last + 1):
            vol[b] += v / span
    total = sum(vol)
    if total <= 0:
        return {"poc": None, "vah": None, "val": None, "hvns": [], "lvns": []}
    poc_i = max(range(bins), key=lambda i: vol[i])
    # value area: expand from POC until ~70% of volume is covered
    covered = vol[poc_i]
    lo_i = hi_i = poc_i
    while covered < 0.70 * total and (lo_i > 0 or hi_i < bins - 1):
        down = vol[lo_i - 1] if lo_i > 0 else -1.0
        up = vol[hi_i + 1] if hi_i < bins - 1 else -1.0
        if up >= down:
            hi_i += 1
            covered += max(0.0, up)
        else:
            lo_i -= 1
            covered += max(0.0, down)
    width = (hi - lo) / bins
    mean_v = total / bins
    hvns = [lo + (i + 0.5) * width for i in range(bins) if vol[i] >= 1.8 * mean_v]
    lvns = [lo + (i + 0.5) * width for i in range(bins)
            if vol[i] <= 0.25 * mean_v and vol[i] > 0]
    return {
        "poc": lo + (poc_i + 0.5) * width,
        "vah": lo + (hi_i + 1) * width,
        "val": lo + lo_i * width,
        "hvns": hvns[-4:],
        "lvns": lvns[-4:],
    }


def delta_proxy(bar: pd.Series) -> float:
    """-1..1 — estimated buy-minus-sell share of one bar's volume."""
    hi, lo = float(bar["h"]), float(bar["l"])
    rng = hi - lo
    if rng <= 0:
        return 0.0
    pos = (float(bar["c"]) - lo) / rng  # 0 = closed at low, 1 = at high
    return pos * 2.0 - 1.0


def candle_pulse(
    bar: pd.Series, prev_close: float | None = None,
    vol_window: pd.Series | None = None,
) -> dict:
    """D-051 — one closed candle's buyer/seller story (user directive:
    "প্রতি সেকেন্ডে ক্যান্ডেলস্টিকের reaction আর ক্রেতা-বিক্রেতা কে
    dominate করছে সেটা বোঝা") — streamed on the strategy_pulse frame
    every M1 close, and recomputed in the UI from the forming bar's
    live ticks.

    Returns {o, h, l, c, v, dir, change, range, delta, buy_pct,
    sell_pct, body_ratio, wick, vol_ratio, reaction}:

    - delta / buy_pct / sell_pct — the close's position inside the
      range, weighted by volume, maps to an estimated buy-vs-sell
      dominance split;
    - body_ratio — conviction (body / range);
    - wick — the dominant REJECTION side (upper = sellers rejected
      higher prices, lower = buyers bought the dip);
    - vol_ratio — this bar's tick volume vs the trailing mean (a
      spike >= 2.0 marks participation, not noise);
    - reaction — a one-line human verdict for the radar panel.
    """
    o, h, lo, c = (float(bar["o"]), float(bar["h"]),
                   float(bar["l"]), float(bar["c"]))
    v = float(bar["v"])
    rng = h - lo
    delta = delta_proxy(bar)
    buy_pct = round((delta + 1.0) / 2.0 * 100.0, 1)
    body = abs(c - o)
    body_ratio = (body / rng) if rng > 0 else 0.0
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - lo
    if upper_wick > lower_wick * 1.2 and upper_wick > 0.25 * rng:
        wick = "upper"
    elif lower_wick > upper_wick * 1.2 and lower_wick > 0.25 * rng:
        wick = "lower"
    else:
        wick = "none"
    vol_ratio = 1.0
    if vol_window is not None and len(vol_window) and float(vol_window.mean()) > 0:
        vol_ratio = v / float(vol_window.mean())
    change = c - (prev_close if prev_close is not None else o)

    if rng <= 0:
        direction, reaction = "flat", "no trade — a doji with zero range"
    elif delta >= 0.6:
        direction = "bull"
        reaction = f"buyers dominate — closed near the high ({buy_pct:.0f}% buy)"
    elif delta <= -0.6:
        direction = "bear"
        reaction = f"sellers dominate — closed near the low ({100 - buy_pct:.0f}% sell)"
    elif c >= o:
        direction = "bull"
        reaction = f"mild buying pressure ({buy_pct:.0f}% buy)"
    else:
        direction = "bear"
        reaction = f"mild selling pressure ({100 - buy_pct:.0f}% sell)"
    if wick == "upper":
        reaction += "; upper wick — sellers rejected the high"
    elif wick == "lower":
        reaction += "; lower wick — buyers absorbed the dip"
    if vol_ratio >= 2.0:
        reaction += f"; volume spike {vol_ratio:.1f}x"
    return {
        "o": round(o, 2), "h": round(h, 2),
        "l": round(lo, 2), "c": round(c, 2),
        "v": v,
        "dir": direction,
        "change": round(change, 2),
        "range": round(rng, 2),
        "delta": round(delta, 3),
        "buy_pct": buy_pct,
        "sell_pct": round(100.0 - buy_pct, 1),
        "body_ratio": round(body_ratio, 2),
        "wick": wick,
        "vol_ratio": round(vol_ratio, 2),
        "reaction": reaction,
    }


def cumulative_delta(df: pd.DataFrame, window: int = 0) -> float:
    """Sum of per-bar delta proxy (whole frame or trailing window)."""
    frame = df.iloc[-window:] if window > 0 else df
    if len(frame) == 0:
        return 0.0
    return float(sum(delta_proxy(frame.iloc[i]) * float(frame["v"].iloc[i])
                     for i in range(len(frame))))


def detect_whale_events(df: pd.DataFrame, lookback: int = 60,
                        z_thr: float = 2.2, max_events: int = 12) -> list[dict]:
    """High-volume institutional-activity bars, classified by shape.

    Each event: {t, side: "buy"|"sell", kind, vol_z, price, note} where
    kind ∈ {momentum, sweep, absorption} and `price` is the event's
    reference price (close for momentum, wick extreme for sweep).
    """
    if len(df) < lookback // 2 + 5:
        return []
    a = atr_series(df, 14)
    events: list[dict] = []
    n = len(df)
    for k in range(lookback, n):
        vols = df["v"].astype(float).iloc[k - lookback:k]
        mu = float(vols.mean())
        sigma = float(vols.std(ddof=0))
        if sigma <= 0 or mu <= 0:
            continue
        v = float(df["v"].iloc[k])
        z = (v - mu) / sigma
        if z < z_thr:
            continue
        bar = df.iloc[k]
        o, h, low, c = (float(bar["o"]), float(bar["h"]),
                        float(bar["l"]), float(bar["c"]))
        rng = h - low
        if rng <= 0:
            continue
        atr_v = float(a.iloc[k]) if pd.notna(a.iloc[k]) else rng
        body = abs(c - o)
        up_wick = h - max(o, c)
        low_wick = min(o, c) - low
        t = df["time_utc"].iloc[k]
        if body >= 0.60 * rng and body >= 1.0 * max(atr_v, 1e-9):
            side = "buy" if c > o else "sell"
            mag = round(body / max(atr_v, 1e-9), 1)
            events.append({
                "t": t, "side": side, "kind": "momentum", "vol_z": round(z, 1),
                "price": c,
                "note": f"institutional {side} momentum — {mag}x ATR body",
            })
        elif low_wick >= 0.55 * rng and c > (h + low) / 2.0:
            events.append({
                "t": t, "side": "buy", "kind": "sweep", "vol_z": round(z, 1),
                "price": low,
                "note": "sell-side liquidity swept (stop hunt) then rejected — manipulation below",
            })
        elif up_wick >= 0.55 * rng and c < (h + low) / 2.0:
            events.append({
                "t": t, "side": "sell", "kind": "sweep", "vol_z": round(z, 1),
                "price": h,
                "note": "buy-side liquidity swept (stop hunt) then rejected — manipulation above",
            })
        elif body <= 0.25 * rng and rng >= 1.2 * max(atr_v, 1e-9):
            side = "buy" if c >= o else "sell"
            events.append({
                "t": t, "side": side, "kind": "absorption", "vol_z": round(z, 1),
                "price": c,
                "note": "heavy volume absorbed with a small body — accumulation" if side == "buy"
                else "heavy volume absorbed with a small body — distribution",
            })
    return events[-max_events:]


def whale_summary(df: pd.DataFrame, lookback: int = 60) -> dict[str, Any]:
    """Compact stats for the analysis API: recent whale pressure."""
    events = detect_whale_events(df, lookback=lookback)
    recent = events[-6:]
    buys = sum(1 for e in recent if e["side"] == "buy")
    sells = len(recent) - buys
    return {
        "events": recent,
        "buy_events": buys,
        "sell_events": sells,
        "bias": "buy" if buys > sells else ("sell" if sells > buys else "neutral"),
        "last": recent[-1] if recent else None,
    }


def flow_stats(
    df: pd.DataFrame, contract_size: float = 100.0
) -> dict[str, Any]:
    """D-044 — session order-flow statistics off the M1 tape.

    Everything a professional flow panel shows, estimated honestly from
    broker data (tick volume + OHLC):

    - usd_24h / usd_1h   — estimated traded value in USD
                           (tick_volume x contract x typical price)
    - volume_24h         — raw tick volume (activity)
    - buy_pct / delta_1h — aggressive buy-vs-sell split (delta proxy)
    - velocity           — ticks per minute over the last hour vs the
                           day average (tape speed — institutions trade
                           in bursts)
    - whale_zones        — price bands where the largest volume spikes
                           traded (institutional entry areas)
    """
    n = len(df)
    if n < 20:
        return {}
    out: dict[str, Any] = {}
    try:
        vol = df["v"].astype(float)
        typical = (df["h"].astype(float) + df["l"].astype(float) + df["c"].astype(float)) / 3.0
        usd = (vol * contract_size * typical).astype(float)
        day = df.iloc[-1440:] if n > 1440 else df
        hour = df.iloc[-60:] if n > 60 else df
        out["usd_24h"] = float(usd.iloc[-len(day):].sum())
        out["usd_1h"] = float(usd.iloc[-len(hour):].sum())
        out["volume_24h"] = int(vol.iloc[-len(day):].sum())
        # aggressive-side split over the last hour (delta proxy weighted)
        d1h = sum(
            delta_proxy(hour.iloc[i]) * float(hour["v"].iloc[i])
            for i in range(len(hour))
        )
        v1h = float(hour["v"].sum())
        out["delta_1h"] = round(d1h, 1)
        out["buy_pct_1h"] = (
            round(100.0 * (0.5 + 0.5 * (d1h / v1h if v1h > 0 else 0.0)), 1)
        )
        # tape velocity: ticks/min last 15m vs 24h average
        v15 = float(df["v"].iloc[-15:].sum()) / 15.0 if n >= 15 else None
        vavg = float(day["v"].sum()) / max(len(day), 1)
        out["velocity"] = round(v15 / vavg, 2) if v15 is not None and vavg > 0 else None
        # whale zones — cluster the biggest volume bars into price bands
        events = detect_whale_events(df, lookback=60, max_events=24)
        zones: list[dict[str, Any]] = []
        atr = atr_series(df, 14)
        band = 0.25 * float(atr.iloc[-1]) if pd.notna(atr.iloc[-1]) else 0.5
        for e in sorted(events, key=lambda x: -x["vol_z"])[-18:]:
            price = float(e["price"])
            for z in zones:
                if abs((z["lo"] + z["hi"]) / 2.0 - price) <= band:
                    z["lo"] = min(z["lo"], price - band)
                    z["hi"] = max(z["hi"], price + band)
                    z["events"] += 1
                    z["vol_z"] = max(z["vol_z"], e["vol_z"])
                    z["side"] = e["side"] if z["side"] == e["side"] else "mixed"
                    break
            else:
                zones.append(
                    {
                        "lo": price - band,
                        "hi": price + band,
                        "price": price,
                        "side": e["side"],
                        "kind": e["kind"],
                        "vol_z": e["vol_z"],
                        "events": 1,
                        "t": e["t"],
                        "note": e["note"],
                    }
                )
        out["whale_zones"] = sorted(
            zones, key=lambda z: -z["vol_z"]
        )[:5]
        out["bias"] = whale_summary(df).get("bias", "neutral")
    except Exception:  # noqa: BLE001 — flow panel must never break the snapshot
        return {}
    return out


# ------------------------------------------------------------- D-067 battle
#
# User directive (D-067, Bengali): "একটি রানিং ক্যান্ডেল বা কয়েক টি
# ক্যান্ডেল buyer Sellar position, কারা কাদের কে ডোমেনেট করছে, কারা
# জিতেছে, লাস্ট কয়েক টি ক্যান্ডেল এর ভিতর কি ঘটেছে, মোট কথা রানিং
# ক্যান্ডেল এর রিয়েকশন" — the candle-by-candle WAR, not just one bar:
#
# Every candle is the auction's visible record: the BODY is net
# conviction (who pushed price), the WICKS are rejections (who
# DEFENDED a level), the CLOSE position is who WON the period, and
# tick volume is participation (forex has no centralized DOM — this
# anatomy is the honest professional proxy, the same language ICT/PA
# traders read). battle_read() aggregates the war over the last N
# closed candles; running_candle_read() reads the LIVE forming bar.

BATTLE_TUG_PCT = 8.0  # |buy - sell| below this = tug of war, no side owns


def candle_anatomy(
    bar: pd.Series, atr_v: float = 0.0, vol_mean: float = 0.0,
) -> dict:
    """One candle's full buyer/seller anatomy (D-067).

    Returns {t, o, h, l, c, dir, delta, buy_pct, body_ratio, wick,
    reject_atr, vol_ratio, won_by, conviction}:
    - won_by — who owned the period (close vs the mid): the auction's
      winner, independent of tick direction;
    - conviction — "strong" body >= 60% of range, "weak" <= 25%,
      else "normal" (a strong body is a decided battle, a weak one a
      fight that never resolved — an indecision bar).
    """
    o, h, lo, c = (float(bar["o"]), float(bar["h"]),
                   float(bar["l"]), float(bar["c"]))
    v = float(bar["v"])
    rng = h - lo
    delta = delta_proxy(bar)
    buy_pct = (delta + 1.0) / 2.0 * 100.0
    body = abs(c - o)
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - lo
    if upper_wick > lower_wick * 1.2 and upper_wick > 0.25 * rng:
        wick = "upper"
    elif lower_wick > upper_wick * 1.2 and lower_wick > 0.25 * rng:
        wick = "lower"
    else:
        wick = "none"
    vol_ratio = (v / vol_mean) if vol_mean > 0 else 1.0
    mid = (h + lo) / 2.0
    if rng <= 0:
        won_by, direction, conviction = "none", "flat", "no range"
    else:
        won_by = "buyers" if c > mid else ("sellers" if c < mid else "none")
        direction = "bull" if c >= o else "bear"
        br = body / rng
        conviction = "strong" if br >= 0.60 else ("weak" if br <= 0.25 else "normal")
    # decisive wick events reference the wick extreme + size in ATR
    reject_atr = 0.0
    if atr_v > 0 and wick != "none":
        reject_atr = round(
            (upper_wick if wick == "upper" else lower_wick) / atr_v, 2,
        )
    return {
        "t": str(bar["time_utc"]) if "time_utc" in bar.index else None,
        "o": round(o, 2), "h": round(h, 2),
        "l": round(lo, 2), "c": round(c, 2),
        "dir": direction,
        "delta": round(delta, 3),
        "buy_pct": round(buy_pct, 1),
        "body_ratio": round(body / rng, 2) if rng > 0 else 0.0,
        "wick": wick,
        "reject_atr": reject_atr,
        "vol_ratio": round(vol_ratio, 2),
        "won_by": won_by,
        "conviction": conviction,
    }


def battle_read(
    df: pd.DataFrame, n: int = 6, vol_lookback: int = 60,
) -> dict:
    """D-067 — the last N CLOSED candles' buyer-vs-seller war.

    Who dominates whom (volume-weighted buy/sell split), who WON (the
    candle-count + net displacement in ATR — price is the judge), what
    happened INSIDE (decisive wick rejections, absorption bars,
    momentum bodies), and the streak of one side winning consecutive
    candles — the exact "লাস্ট কয়েক টি ক্যান্ডেল এর ভিতর কি ঘটেছে" read.

    Returns {} when the frame is too short. All values JSON-serializable.
    """
    out: dict[str, Any] = {
        "n": 0, "buy_pct": 50.0, "sell_pct": 50.0,
        "state": "tug", "streak": None, "net_atr": 0.0,
        "wins": {"buyers": 0, "sellers": 0},
        "events": [], "candles": [], "verdict": "", "participation": None,
    }
    if df is None or len(df) < 3:
        return out
    n = max(3, min(int(n), 12))
    a = atr_series(df, 14)
    atr_v = float(a.iloc[-1]) if pd.notna(a.iloc[-1]) else 0.0
    if atr_v <= 0:
        atr_v = float(df["c"].tail(20).std() or 0.0) or 1e-9
    vol_win = df["v"].astype(float).iloc[-(vol_lookback + n):-n]
    vol_mean = float(vol_win.mean()) if len(vol_win) else 0.0

    frame = df.iloc[-n:]
    candles = []
    buy_score = 0.0
    sell_score = 0.0
    wins = {"buyers": 0, "sellers": 0}
    events: list[dict] = []
    for i in range(len(frame)):
        bar = frame.iloc[i]
        an = candle_anatomy(bar, atr_v=atr_v, vol_mean=vol_mean)
        candles.append(an)
        w = float(bar["v"]) if vol_mean > 0 else 1.0
        buy_score += max(0.0, an["delta"]) * w
        sell_score += max(0.0, -an["delta"]) * w
        won = an["won_by"]
        if won == "buyers":
            wins["buyers"] += 1
        elif won == "sellers":
            wins["sellers"] += 1
        # what happened INSIDE this candle — the decisive moments only
        if an["wick"] != "none" and an["reject_atr"] >= 0.35:
            side = "sellers" if an["wick"] == "upper" else "buyers"
            events.append({
                "t": an["t"], "kind": "rejection", "side": side,
                "price": an["h"] if an["wick"] == "upper" else an["l"],
                "depth_atr": an["reject_atr"],
                "note": (
                    f"{side} rejected the "
                    f"{'high' if an['wick'] == 'upper' else 'low'} — "
                    f"{an['reject_atr']} ATR wick"
                ),
            })
        elif an["conviction"] == "weak" and an["vol_ratio"] >= 1.8:
            side = "buyers" if an["delta"] >= 0 else "sellers"
            events.append({
                "t": an["t"], "kind": "absorption", "side": side,
                "price": an["c"], "depth_atr": an["vol_ratio"],
                "note": (
                    f"heavy volume, tiny body — {side} absorbed the "
                    "other side's push"
                ),
            })
        elif an["conviction"] == "strong" and an["vol_ratio"] >= 2.0:
            side = "buyers" if an["dir"] == "bull" else "sellers"
            events.append({
                "t": an["t"], "kind": "momentum", "side": side,
                "price": an["c"], "depth_atr": an["body_ratio"],
                "note": f"{side} printed a decided institutional body",
            })
    total = buy_score + sell_score
    if total > 0:
        buy_pct = 100.0 * buy_score / total
    else:
        # no volume signal at all: fall back to equal-weight delta
        buy_pct = 50.0 + 50.0 * float(np.mean([c["delta"] for c in candles])) \
            if candles else 50.0
    sell_pct = 100.0 - buy_pct
    dom = buy_pct - 50.0
    if abs(dom) < BATTLE_TUG_PCT:
        state = "tug"
    else:
        state = "buyers" if dom > 0 else "sellers"

    # the streak — one side winning consecutive candles (the close vs
    # the mid, i.e. the auction's winner, not just the tick color)
    streak_dir, streak_len = None, 0
    for c in reversed(candles):
        w = c["won_by"]
        if w in ("buyers", "sellers"):
            if streak_dir is None:
                streak_dir, streak_len = w, 1
            elif w == streak_dir:
                streak_len += 1
            else:
                break
        elif streak_dir is None:
            continue  # a doji does not break the streak, just pauses it
        else:
            break

    # who won in PRICE terms — the only judge that pays
    first_open = float(frame["o"].iloc[0])
    last_close = float(frame["c"].iloc[-1])
    net_atr = round((last_close - first_open) / atr_v, 2)

    # participation trend — volume of the recent half vs the earlier half
    if vol_mean > 0:
        half = n // 2
        recent_v = float(frame["v"].astype(float).iloc[half:].mean())
        earlier_v = float(frame["v"].astype(float).iloc[:half or 1].mean())
        if earlier_v > 0:
            out["participation"] = round(recent_v / earlier_v, 2)

    out.update({
        "n": len(candles),
        "buy_pct": round(buy_pct, 1),
        "sell_pct": round(sell_pct, 1),
        "state": state,
        "streak": {"side": streak_dir, "len": streak_len}
        if streak_dir else None,
        "net_atr": net_atr,
        "wins": wins,
        "events": events[-4:],
        "candles": candles,
    })

    # the human verdict — the sentence the user reads on the radar
    if state == "tug":
        verdict = (
            f"tug of war — the last {len(candles)} candles split "
            f"{round(buy_pct)}/{round(sell_pct)}, nobody dominates"
        )
    else:
        side = state
        pct = round(max(buy_pct, sell_pct))
        verdict = (
            f"{side} dominate the last {len(candles)} candles — "
            f"{pct}% of the flow"
        )
    if wins["buyers"] != wins["sellers"]:
        verdict += (
            f"; won {max(wins['buyers'], wins['sellers'])}/{len(candles)}"
            f" candles"
        )
    verdict += f"; net {net_atr:+.1f} ATR"
    if streak_dir and streak_len >= 2:
        verdict += f", {streak_len} in a row for the {streak_dir}"
    if events:
        verdict += f". {events[-1]['note']}"
    out["verdict"] = verdict
    return out


def running_candle_read(
    bar: pd.Series, atr_v: float = 0.0,
) -> dict:
    """D-067 — the LIVE forming candle's read (the "রানিং ক্যান্ডেল এর
    রিয়েকশন"): who is winning RIGHT NOW inside the unfinished bar —
    the dominance split, the wick war (who is rejecting at the
    extremes), price vs the developing mid, and the one-line reaction.

    The backend computes this from a forming bar when it has one; the
    UI runs the same math on every tick (bar_update frames), so the
    verdict visibly moves second-by-second between closes.
    """
    o, h, lo, c = (float(bar["o"]), float(bar["h"]),
                   float(bar["l"]), float(bar["c"]))
    rng = h - lo
    out: dict[str, Any] = {
        "o": round(o, 2), "h": round(h, 2), "l": round(lo, 2),
        "c": round(c, 2), "range": round(rng, 2),
        "buy_pct": 50.0, "sell_pct": 50.0, "delta": 0.0,
        "wick": "none", "wick_war": None, "control": "none",
        "body_ratio": 0.0, "reaction": "forming…",
    }
    if rng <= 0:
        out["reaction"] = "just opened — the battle has not started"
        return out
    delta = (c - lo) / rng * 2.0 - 1.0
    buy_pct = (delta + 1.0) / 2.0 * 100.0
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - lo
    if upper_wick > 0.45 * rng:
        wick, wick_war = "upper", "sellers rejecting the high"
    elif lower_wick > 0.45 * rng:
        wick, wick_war = "lower", "buyers absorbing the dip"
    else:
        wick, wick_war = "none", None
    mid = (h + lo) / 2.0
    control = "buyers" if c > mid else ("sellers" if c < mid else "none")
    body_ratio = abs(c - o) / rng
    out.update({
        "buy_pct": round(buy_pct, 1),
        "sell_pct": round(100.0 - buy_pct, 1),
        "delta": round(delta, 3),
        "wick": wick,
        "wick_war": wick_war,
        "control": control,
        "body_ratio": round(body_ratio, 2),
    })
    if buy_pct >= 60:
        reaction = f"buyers winning — {round(buy_pct)}% of the bar"
    elif buy_pct <= 40:
        reaction = f"sellers winning — {round(100 - buy_pct)}% of the bar"
    else:
        reaction = "even fight inside the running candle"
    if wick_war:
        reaction += f"; {wick_war}"
    out["reaction"] = reaction
    return out
