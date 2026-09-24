"""D-068 — market-regime engine: trend TYPE x timeframes + volatility state.

User question (Bengali): "মার্কেট এর ভিতরে ট্রেন্ট তৈরি হয় — up ট্রেন্ড,
ডাউন ট্রেন্ড, সাইড ওয়েস, zigzag... এই ট্রেন্ড গুলো কোন টাইম ফ্রেম এর সাথে
কীভাবে এনালাইসিস করে, সিগন্যাল প্রেডিকশন এ কোনটি কে কিভাবে ব্যবহার করা
যায়?" and "মার্কেট এ যখন বেশি ভোলাটেলিটি তখন সিগন্যাল বেশি ভুল হচ্ছে —
সকল অবস্থা বুঝার মত সিস্টেম কি অ্যাপ এ আছে?"

The app already had the PARTS (bias vote, structure ladder, AMD squeeze,
ATR-scaled geometry) but never a single REGIME verdict that states WHAT
KIND of market this is on each timeframe and HOW VOLATILE it is right
now. This module is that verdict — two independent axes:

AXIS 1 — trend TYPE per timeframe (trend_state):
    trend_up / trend_down  — directional efficiency (Kaufman ER) high,
                             EMA 21>50 (or <), ADX above the trend line
    range                  — compression: no net direction, quiet vol,
                             the sideways box (AMD accumulation's home)
    chop                   — violent alternation with NO net direction:
                             low ER + elevated volatility — the zigzag
                             the user named; every momentum signal pays
    (+ "transition" flavor — a fresh CHoCH close: the regime is shifting)

AXIS 2 — volatility STATE (volatility_state), self-scaling:
    vol_ratio = ATR(now) / median(ATR over the window)
    quiet <= 0.75 < normal <= 1.25 < elevated <= 1.75 < extreme
    (+ spike: the just-closed bar's range >= 3.5 x ATR — the news candle)

regime_read() combines the per-TF grid (H4 bias / H1 trend / M15
confirm / M5 setup) into ONE market label + the policy sentence, and
the engine (evaluate) turns that label into signal policy:
    TREND   -> with-trend signals earn a bonus; counter fades pay
    RANGE   -> zone fades at the edges EARN (that IS the range play);
               momentum/breakout chases pay
    CHOP    -> momentum signals pay hard; zone trades pay a little;
               (the structure guard + trap gate still own hard blocks)
    extreme vol / spike -> MARKET entries are refused (fill quality is
               unpredictable inside a news candle; limit entries at the
               drawn levels still live — they only fill on the retrace)

Everything here is pure, closed-bars, JSON-serializable (same contract
as structure.py / manipulation.py) so the SAME output feeds the engine
gates, the strategy-radar pulse, the signal context and the tests.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from app.analysis import indicators as ind
from app.analysis import smc

# ------------------------------------------------------------------ tuning
# (self-scaling thresholds — ATR-relative / ratio-based so the same
# numbers read M1 gold, BTC and the mock without re-fitting)

#: volatility ratio bands (ATR now / median ATR of the window)
VOL_QUIET = 0.75
VOL_NORMAL = 1.25
VOL_ELEVATED = 1.75

#: bars of ATR history the vol state ranks against
VOL_WINDOW = 300

#: a single bar whose true range >= this multiple of ATR is a SPIKE
SPIKE_MULT = 3.5

#: Kaufman efficiency-ratio bands (net displacement / path traveled)
ER_TREND = 0.30
ER_CHOP = 0.18

#: ADX bands — above ER_TREND+ADX 22 the direction is real; below 19 flat
ADX_TREND = 22.0
ADX_FLAT = 19.0

#: strong-ADX override: a persistent direction meter this high names a
#: trend even when the efficiency ratio sits just under ER_TREND
ADX_STRONG = 28.0

#: mean |body| / ATR at/above this = every bar is a full-ATR decision —
#: the violent-alternation (zigzag) signature at CONSTANT volatility
#: (choch-count and vol-ratio alone cannot separate it from a tight box:
#: measured tight-range body ratio ~0.5, strict zigzag ~0.9)
BODY_VIOLENT = 0.75

#: structure flips (CHoCH) window + rates: at/above ZIGZAG_HIGH_RATE the
#: flips are frequent enough to name a zigzag outright; ZIGZAG_FLIPS is
#: the milder count that only NAMES the flavor once chop is established
ZIGZAG_WINDOW = 60
ZIGZAG_FLIPS = 3
ZIGZAG_HIGH_RATE = 5


# ------------------------------------------------------------ volatility


def volatility_state(
    df: pd.DataFrame | None,
    window: int = VOL_WINDOW,
    spike_mult: float = SPIKE_MULT,
) -> dict:
    """The volatility verdict on the frame's OPERATING scale.

    Self-scaling: the current ATR is ranked against the frame's own
    median ATR (not an absolute USD number) so the same bands read
    M1 gold, BTCUSD and the mock. A SPIKE (news candle: one bar whose
    true range is >= spike_mult x ATR) overrides to the dangerous end
    for the next read — that is the exact moment the user's "high vol
    = wrong signals" complaint lives.

    Returns {state, vol_ratio, atr, atr_median, pct_rank, spike}.
    """
    out: dict[str, Any] = {
        "state": "normal",
        "vol_ratio": 1.0,
        "atr": None,
        "atr_median": None,
        "pct_rank": None,
        "spike": False,
    }
    if df is None or len(df) < 30:
        return out
    atr_s = ind.atr_series(df, 14).dropna()
    if len(atr_s) < 20:
        return out
    atr_now = float(atr_s.iloc[-1])
    # rank against the frame's own history (as much as exists)
    hist = atr_s.iloc[-window:]
    atr_med = float(hist.median())
    if atr_med <= 0:
        return out
    ratio = atr_now / atr_med
    rank = float((hist < atr_now).mean())  # 0..1 percentile of now

    # spike: the just-closed bar's true range vs ATR
    last_tr = float(ind.true_range(df).iloc[-1])
    spike = last_tr >= spike_mult * max(atr_now, 1e-9)

    if spike or ratio > VOL_ELEVATED:
        state = "extreme"
    elif ratio > VOL_NORMAL:
        state = "elevated"
    elif ratio < VOL_QUIET:
        state = "quiet"
    else:
        state = "normal"

    out.update({
        "state": state,
        "vol_ratio": round(ratio, 2),
        "atr": round(atr_now, 4),
        "atr_median": round(atr_med, 4),
        "pct_rank": round(rank, 2),
        "spike": bool(spike),
    })
    return out


# ----------------------------------------------------------- trend type


def _efficiency_ratio(closes: pd.Series, n: int) -> float:
    """Kaufman ER over `n` bars: |net move| / path traveled.

    ~1.0 = every bar pushed the same way (clean trend);
    ~0.0 = price went back and forth (range / chop / zigzag).
    """
    if len(closes) <= n or n < 2:
        return 0.0
    tail = closes.iloc[-(n + 1):].astype(float)
    net = abs(float(tail.iloc[-1]) - float(tail.iloc[0]))
    path = float(tail.diff().abs().sum())
    return net / path if path > 0 else 0.0


def _choch_count(df: pd.DataFrame, window: int = ZIGZAG_WINDOW) -> int:
    """Structure flips (CHoCH events) inside the recent window — the
    zigzag signature: each flip is the ladder resetting the other way."""
    try:
        st = smc.detect_structure(df.iloc[-window:])
        events = st.get("events") or []
        return int(len([e for e in events if e.get("kind") == "CHoCH"]))
    except Exception:  # noqa: BLE001 — never break the read
        return 0


def trend_state(df: pd.DataFrame | None, er_window: int = 24) -> dict:
    """The trend-TYPE verdict on ONE timeframe.

    Returns::

        {
          "state":   "trend_up" | "trend_down" | "range" | "chop",
          "dir":     "up" | "down" | None,
          "er":      float,      # Kaufman efficiency ratio
          "adx":     float,
          "ema_gap_atr": float,  # EMA21-EMA50 gap in ATR (direction size)
          "body_ratio": float,  # mean |body| / ATR (violence meter)
          "flavor":  "zigzag" | "transition" | "squeeze" | None,
          "note":    str,
        }
    """
    out: dict[str, Any] = {
        "state": "unknown",
        "dir": None,
        "er": 0.0,
        "adx": 0.0,
        "ema_gap_atr": 0.0,
        "body_ratio": 0.0,
        "flavor": None,
        "note": "no readable history",
    }
    if df is None or len(df) < 35:
        return out
    closes = df["c"].astype(float)
    # graceful degradation: short frames (H4 in backtests) still get a
    # read with a tighter EMA pair — 50 needs ~55 bars to mean anything
    ema_slow = 50 if len(df) >= 55 else 34
    er = _efficiency_ratio(closes, er_window)
    adx_v = float(ind.adx(df, 14).get("adx") or 0.0)
    e21 = float(ind.ema(closes, 21).iloc[-1])
    e50 = float(ind.ema(closes, ema_slow).iloc[-1])
    atr = ind.atr(df, 14) or 1e-9
    gap_atr = (e21 - e50) / atr
    bodies = (closes - df["o"].astype(float)).abs()
    body_ratio = float(bodies.iloc[-er_window:].mean() / atr) if atr > 0 else 0.0
    net_up = float(closes.iloc[-1]) > float(closes.iloc[-1 - er_window])
    d = "up" if gap_atr > 0 else "down"
    if abs(gap_atr) < 0.05:  # EMAs entangled — no directional conviction
        d = "up" if net_up else "down"
    out["er"] = round(er, 2)
    out["adx"] = round(adx_v, 1)
    out["ema_gap_atr"] = round(gap_atr, 2)
    out["body_ratio"] = round(body_ratio, 2)
    out["dir"] = d

    if (
        (er >= ER_TREND or adx_v >= ADX_STRONG)
        and adx_v >= ADX_TREND
        and abs(gap_atr) >= 0.15
    ):
        out["state"] = f"trend_{d}"
        out["note"] = (
            f"trend {d}: efficiency {er:.2f}, ADX {adx_v:.0f}, "
            f"EMA gap {gap_atr:+.1f} ATR — directional and clean"
        )
        return out

    # no net direction — WHICH kind of sideways?
    vol = volatility_state(df)
    choch_n = _choch_count(df)
    violent = (
        vol["state"] in ("elevated", "extreme")
        or choch_n >= ZIGZAG_HIGH_RATE
        or body_ratio >= BODY_VIOLENT
    )
    if er <= ER_CHOP and violent:
        out["state"] = "chop"
        out["flavor"] = "zigzag" if choch_n >= ZIGZAG_FLIPS else "noise"
        out["note"] = (
            f"chop ({out['flavor']}): efficiency {er:.2f} — violent "
            f"alternation, {choch_n} structure flips, body {body_ratio:.2f} "
            f"ATR, vol {vol['state']} ({vol['vol_ratio']}x) — momentum "
            "signals pay here"
        )
        return out

    # fresh CHoCH = the regime is SHIFTING (transition flavor)
    try:
        from app.analysis.structure import structure_ladder

        lad = structure_ladder(df)
        if lad.get("choch_fresh"):
            out["flavor"] = "transition"
    except Exception:  # noqa: BLE001
        pass

    out["state"] = "range"
    out["note"] = (
        f"range: efficiency {er:.2f}, ADX {adx_v:.0f}, vol {vol['state']}"
        + (" — compressed sideways box" if vol["state"] == "quiet" else "")
        + (" — structure shifting (fresh CHoCH)" if out["flavor"] == "transition" else "")
    )
    return out


# -------------------------------------------------------- combined read


#: the TF grid the regime reads (mirrors the D-065 ladder roles)
REGIME_TFS = ("H4", "H1", "M15", "M5")

#: per-TF trend-state cache — M5/H1/H4 frames only change on their OWN
#: closes, but the engine asks on every M1 close: keying by (tf, last
#: bar time, len, close-tail) turns 4 full reads into ~1 per M5 close
#: (measured 9.5ms -> ~1ms amortized per evaluate call). Bounded; the
#: returned dicts must be treated as read-only.
_TF_STATE_CACHE: dict[tuple, dict] = {}
_TF_STATE_CACHE_MAX = 256


def _cached_trend_state(tf: str, frame: pd.DataFrame | None) -> dict:
    if frame is None or len(frame) == 0:
        return trend_state(None)
    try:
        t_last = str(frame["time_utc"].iloc[-1])
        n = len(frame)
        tail = tuple(frame["c"].iloc[-30:].round(4))
        key = (tf, t_last, n, hash(tail))
    except Exception:  # noqa: BLE001 — cache must never break the read
        return trend_state(frame)
    hit = _TF_STATE_CACHE.get(key)
    if hit is not None:
        return hit
    state = trend_state(frame)
    if len(_TF_STATE_CACHE) >= _TF_STATE_CACHE_MAX:
        _TF_STATE_CACHE.clear()
    _TF_STATE_CACHE[key] = state
    return state


def regime_read(base: pd.DataFrame | None, htf: dict, price: float | None = None) -> dict:
    """The everything-in-one market verdict the engine + radar share.

    Per-TF trend types (H4 bias / H1 trend / M15 confirm / M5 setup),
    the operating-frame volatility state, the MTF alignment count, ONE
    market label (TREND UP / TREND DOWN / RANGE / CHOP / TRANSITION)
    and the policy sentence — what the engine does in this state.

    The LABEL follows the H1 (trend) frame — the context timeframe of
    the D-065 ladder — falling back to M15/M5/H4 when H1 is missing:
    the grid itself is always surfaced so nothing is hidden.
    """
    out: dict[str, Any] = {
        "label": None,
        "dir": None,
        "vol": None,
        "tfs": {},
        "alignment": 0,
        "action": "",
        "note": "",
    }
    per_tf: dict[str, dict] = {}
    for tf in REGIME_TFS:
        frame = htf.get(tf) if hasattr(htf, "get") else None
        per_tf[tf] = _cached_trend_state(tf, frame)
    out["tfs"] = {
        tf: {
            "state": s["state"],
            "dir": s["dir"],
            "er": s["er"],
            "adx": s["adx"],
            "body_ratio": s.get("body_ratio"),
            "flavor": s["flavor"],
            "note": s["note"],
        }
        for tf, s in per_tf.items()
    }

    # volatility on the OPERATING (base) frame + the H1 frame for context
    vol_base = volatility_state(base)
    vol_h1 = volatility_state(htf.get("H1")) if hasattr(htf, "get") else {}
    out["vol"] = {
        "state": vol_base["state"],
        "vol_ratio": vol_base["vol_ratio"],
        "atr": vol_base["atr"],
        "spike": vol_base["spike"],
        "pct_rank": vol_base["pct_rank"],
        "h1_state": (vol_h1 or {}).get("state"),
        "h1_vol_ratio": (vol_h1 or {}).get("vol_ratio"),
    }

    # MTF alignment: how many frames read trend_* in the SAME direction
    dirs = [
        s["dir"] for s in per_tf.values()
        if str(s["state"]).startswith("trend_")
    ]
    up = dirs.count("up")
    down = dirs.count("down")
    out["alignment"] = max(up, down)
    out["dir"] = "up" if up >= down else "down"

    # the LABEL: follow the H1 frame (the context TF of the D-065
    # ladder), falling back down the ladder when a frame is unreadable
    label_src = next(
        (
            tf for tf in ("H1", "M15", "M5", "H4")
            if per_tf[tf]["note"] != "no readable history"
        ),
        "H1",
    )
    src_state = per_tf[label_src]["state"]
    transition = any(
        s.get("flavor") == "transition"
        for s in (per_tf["H1"], per_tf["M15"])
    )
    if src_state == "range" and transition:
        # a fresh CHoCH just closed the old range — the regime is shifting
        label = "TRANSITION"
    elif src_state.startswith("trend_"):
        label = f"TREND {'UP' if src_state == 'trend_up' else 'DOWN'}"
        if out["alignment"] >= 3:
            label += " (STACKED)"
    elif src_state == "chop":
        label = "CHOP"
        if per_tf[label_src].get("flavor") == "zigzag":
            label += " (ZIGZAG)"
    else:
        label = "RANGE"
    out["label"] = label
    out["label_tf"] = label_src

    vol_state = vol_base["state"]
    out["note"] = (
        f"{label} on {label_src} · vol {vol_state}"
        f" ({vol_base['vol_ratio']}x median"
        + (", SPIKE bar" if vol_base["spike"] else "")
        + ") · alignment " + f"{out['alignment']}/4"
    )

    # the policy sentence — what this state MEANS for signal prediction
    a = "unknown regime"
    if label.startswith("TREND"):
        a = (
            "trending market — with-trend pullback/zone signals earn, "
            "counter-trend fades pay; trade WITH the ladder direction"
        )
    elif label == "RANGE":
        a = (
            "range market — fade the edges: zone retests at the box "
            "boundary are the play; momentum breakouts pay inside the box"
        )
    elif label.startswith("CHOP"):
        a = (
            "chop/zigzag — momentum signals pay; limit entries at drawn "
            "levels only; wait for the squeeze to resolve"
        )
    elif label == "TRANSITION":
        a = (
            "structure shifting (fresh CHoCH) — reversal-confirmed plays "
            "live here; entries at the new leg's pullback magnets"
        )
    if vol_state == "extreme":
        a += "; EXTREME volatility — market entries paused, limit entries still live"
    elif vol_state == "elevated":
        a += "; elevated volatility — confidence discounted"
    elif vol_state == "quiet":
        a += "; quiet tape — expect slow fills and small ranges"
    out["action"] = a
    return out


# ------------------------------------------------------- engine policy


def regime_policy(
    regime: dict | None,
    direction: str,
    trigger: str,
    entry_type: str,
) -> dict:
    """What the engine DOES with the regime verdict on THIS trade.

    Returns {adjust: float, notes: [str], block_market: bool} — the
    caller (evaluate) applies the confidence adjustment, adds the
    trace lines and refuses MARKET entries when the block flag is up
    (extreme volatility / spike: a market order fills wherever the
    news candle happens to be; the limit at the drawn level only fills
    on the retrace — that asymmetry is the whole point).

    D-049 precedence preserved: nothing is silently lost. Zone trades
    keep flowing in every regime; the only hard refusal is the market
    entry inside extreme vol, and it is VISIBLY attributed.
    """
    out = {"adjust": 0.0, "notes": [], "block_market": False}
    if not regime:
        return out
    label = str(regime.get("label") or "")
    vol_state = ((regime.get("vol") or {}).get("state")) or "normal"
    notes: list[str] = out["notes"]

    if label.startswith("TREND"):
        with_trend = (label.endswith("UP") and direction == "BUY") or (
            label.endswith("DOWN") and direction == "SELL"
        )
        if with_trend:
            out["adjust"] += 0.04
            notes.append("regime TREND with the trade (+0.04)")
        else:
            out["adjust"] -= 0.05
            notes.append("regime TREND against the trade (-0.05)")
    elif label == "RANGE":
        if trigger == "zone":
            out["adjust"] += 0.05
            notes.append("regime RANGE — zone fade at the edge (+0.05)")
        else:
            out["adjust"] -= 0.06
            notes.append("regime RANGE — momentum chase inside the box (-0.06)")
    elif label.startswith("CHOP"):
        if trigger == "zone":
            out["adjust"] -= 0.06
            notes.append("regime CHOP — zone trade discounted (-0.06)")
        else:
            out["adjust"] -= 0.10
            notes.append("regime CHOP — momentum signal pays (-0.10)")

    if vol_state == "elevated":
        out["adjust"] -= 0.04
        vr = (regime.get("vol") or {}).get("vol_ratio")
        notes.append(
            f"elevated volatility{f' {vr}x' if vr else ''} (-0.04)"
        )
    elif vol_state == "extreme":
        out["adjust"] -= 0.08
        if entry_type == "market":
            # a market order fills wherever the news candle happens to
            # be — the limit at the drawn level only fills on the retrace
            out["block_market"] = True
        notes.append(
            "EXTREME volatility — market entry refused (limit entries at "
            "the drawn level still live)"
        )
    return out
