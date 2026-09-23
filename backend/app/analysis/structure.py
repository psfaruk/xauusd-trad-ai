"""D-064 — market-structure engine: legs, REST zones, reversal reads.

User question (Bengali): "মার্কেট নিচে যাচ্ছে নিচে যাচ্ছে... মার্কেট কোথায়
গিয়ে রেস্ট করে বা একটু বিশ্রাম নেয়, বিশ্রাম নিয়ে একটু উপরের দিকে যায়,
তারপর আবার ডাউন এ যায়। কি এমন লজিক আছে যে মার্কেট এখন রিভার্স করবে?
আর কত বার HL LL LH HH LOWER HIGHER হলে রিভার্স বা কনটিনিউ করে?"

The calibrated answer (scripts/measure_legs.py, 5 mock seeds, M5+M15,
2 142 structural events):

1. A same-direction structure RUN rarely exceeds 3 legs — 87% of runs
   end at leg 3, 94% at leg 4. So after the 3rd consecutive LL (or HH)
   the move is statistically MATURE: expect a REST, not an endless trend.
2. The REST itself: the counter-move between two same-direction legs is
   ~2.3 ATR deep (p25-p75: 1.8-2.8 ATR) and lasts ~10-16 bars, inside a
   range compressed to <= 0.62 x (ATR * sqrt(n)) — the exact squeeze the
   AMD accumulation detector uses.
3. Counting legs ALONE does not flip the hazard (~50% on an efficient
   walk): the only CONFIRMATION of reversal is a CHoCH (close beyond the
   last opposite swing) plus liquidity-sweep evidence — which is what
   this module measures and what the engine gates on.

Everything here is pure, closed-bars, JSON-serializable (same contract
as smc.py / manipulation.py) so the SAME output feeds the engine gates,
the strategy-radar pulse, the signal context and the chart drawings.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from app.analysis import indicators as ind
from app.analysis import smc

# ------------------------------------------------------------------ tuning
# (calibrated by scripts/measure_legs.py; ATR-relative so the same
# thresholds scale across M1 gold / BTC / the mock)

#: consecutive same-direction break events after which a run is MATURE
#: (87% of runs end at 3, 94% at 4) — "expect a rest, do not chase"
LEGS_EXHAUST = 3

#: a pause whose range/(ATR*sqrt(n)) is at/below this is a REST zone
#: (measured p25 = 0.60, p50 = 0.69; aligned with AMD SQUEEZE_K)
REST_COMPRESS_K = 0.62

#: a pause shorter than this many bars is noise, not a rest
REST_MIN_BARS = 6

#: how many bars back the rest-zone map scans
REST_WINDOW_BARS = 120

#: a rest magnet further than this many ATR from price stops being a
#: "the market rests HERE next" candidate (measured p75 depth = 2.8 ATR)
MAGNET_MAX_ATR = 3.0

#: a CHoCH older than this many bars no longer counts as fresh proof
CHOCH_FRESH_BARS = 12

#: hazard calibration — base flip odds + per-leg increase (measured)
HAZARD_BASE = 0.46
HAZARD_PER_LEG = 0.05
HAZARD_LEG_CAP = 0.15


# ------------------------------------------------------------- the ladder


def structure_ladder(df: pd.DataFrame | None) -> dict:
    """Swing-event ladder — the "কত বার LL/LH" counter.

    Returns::

        {
          "trend":    "bullish" | "bearish" | "balanced",
          "run":      N,                  # consecutive same-dir breaks
          "run_dir":  "up" | "down" | None,
          "labels":   [ {t, price, kind, label} ],  # last labeled swings
          "last_event": {...} | None,
          "choch_fresh": {"dir", "level", "t", "bars_ago"} | None,
          "note": str,
        }

    A BOS in the run direction extends the run; a CHoCH (close beyond
    the last OPPOSITE swing) resets it to leg 1 the other way — that
    close is the only structural CONFIRMATION a reversal has happened.
    """
    out: dict[str, Any] = {
        "trend": "balanced",
        "run": 0,
        "run_dir": None,
        "labels": [],
        "last_event": None,
        "choch_fresh": None,
        "note": "no readable structure",
    }
    if df is None or len(df) < 30:
        return out
    st = smc.detect_structure(df)
    out["trend"] = st.get("trend", "balanced")
    out["labels"] = st.get("swings") or []
    events = st.get("events") or []
    out["last_event"] = st.get("last_event")

    run, run_dir = 0, None
    for ev in events:
        d = ev["dir"]
        if d == run_dir:
            run += 1
        else:
            run_dir, run = d, 1
    out["run"] = run
    out["run_dir"] = run_dir

    # fresh CHoCH — the reversal proof (within CHOCH_FRESH_BARS bars)
    if events:
        last = events[-1]
        if last["kind"] == "CHoCH":
            bars_ago = _bars_ago(df, last["t"])
            if bars_ago is not None and bars_ago <= CHOCH_FRESH_BARS:
                out["choch_fresh"] = {
                    "dir": last["dir"],
                    "level": round(float(last["level"]), 2),
                    "t": last["t"],
                    "bars_ago": int(bars_ago),
                }

    if run_dir is not None:
        word = "lower" if run_dir == "down" else "higher"
        out["note"] = (
            f"{run} consecutive {word} breaks — "
            + ("run mature, expect a REST" if run >= LEGS_EXHAUST
               else "run intact")
        )
        if out["choch_fresh"]:
            out["note"] = (
                f"STRUCTURE SHIFT {out['choch_fresh']['dir']} "
                f"{out['choch_fresh']['bars_ago']} bars ago — new leg 1"
            )
    return out


def _bars_ago(df: pd.DataFrame, t: Any) -> int | None:
    """Bar distance from event time `t` to the last bar (None if unknown)."""
    try:
        t_arr = df["time_utc"].to_numpy()
        i = int(np.searchsorted(t_arr, t))
        if i < len(t_arr) and t_arr[i] == t:
            return len(t_arr) - 1 - i
        # event times always come from this frame; be defensive anyway
        return max(0, len(t_arr) - i)
    except Exception:  # noqa: BLE001 — never break the read
        return None


# ------------------------------------------------------------- rest zones


def rest_zones(
    df: pd.DataFrame | None,
    window: int = REST_WINDOW_BARS,
    probe: int = 9,
    min_bars: int = REST_MIN_BARS,
    k: float = REST_COMPRESS_K,
) -> list[dict]:
    """WHERE the market actually rested — compressed pause boxes.

    Sliding probe of `probe` bars: a segment is compressed when its
    high-low range <= k * ATR * sqrt(probe) (the random-walk floor).
    Overlapping compressed probes merge into zones; only pauses at least
    `min_bars` long survive (measured median rest = 10-16 bars).

    Returns [{t0, t1, lo, hi, bars, mid, compress}] most-recent-first.
    """
    if df is None or len(df) < 30:
        return []
    frame = df.iloc[-window:]
    h = frame["h"].to_numpy(dtype=float)
    low = frame["l"].to_numpy(dtype=float)
    t_arr = frame["time_utc"].to_numpy()
    n = len(frame)
    atr = ind.atr(frame, 14) or 1e-9

    # compressed-probe mask (vectorized rolling max/min)
    from numpy.lib.stride_tricks import sliding_window_view

    hw = sliding_window_view(h, probe)
    lw = sliding_window_view(low, probe)
    rng = hw.max(axis=1) - lw.min(axis=1)
    floor = k * atr * np.sqrt(probe)
    mask = rng <= floor  # len = n - probe + 1, index j covers bars j..j+probe-1

    # merge runs of compressed probes into zones
    zones: list[dict] = []
    j = 0
    while j < len(mask):
        if not mask[j]:
            j += 1
            continue
        j0 = j
        while j < len(mask) and mask[j]:
            j += 1
        # zone bars: j0 .. j + probe - 2 (inclusive)
        b0 = j0
        b1 = min(j + probe - 2, n - 1)
        bars = b1 - b0 + 1
        if bars >= min_bars:
            seg_h = h[b0:b1 + 1]
            seg_l = low[b0:b1 + 1]
            zrng = float(seg_h.max() - seg_l.min())
            compress = zrng / (atr * np.sqrt(bars)) if atr > 0 else 0.0
            zones.append({
                "t0": t_arr[b0],
                "t1": t_arr[b1],
                "lo": round(float(seg_l.min()), 2),
                "hi": round(float(seg_h.max()), 2),
                "mid": round(float((seg_h.max() + seg_l.min()) / 2.0), 2),
                "bars": int(bars),
                "compress": round(float(compress), 2),
            })
    return zones[::-1]  # most recent first


# ---------------------------------------------------------- rest magnets


def rest_magnets(
    df: pd.DataFrame | None,
    price: float,
    side: str = "up",
    max_atr: float = MAGNET_MAX_ATR,
) -> list[dict]:
    """WHERE the market is expected to rest NEXT (the pullback magnets).

    `side` = the counter-direction the rest lives in: for a MATURE DOWN
    run the market rests back UP (unfilled bullish FVG above, EMA 21/50,
    equilibrium, the last REST zone, the OTE band). Mirror for up runs.

    Returns [{price, kind, note}] sorted by distance from price — the
    levels the chart draws as "REST MAGNET" and the engine treats as
    the good continuation-entry locations.
    """
    if df is None or len(df) < 30 or not price or price <= 0:
        return []
    atr = ind.atr(df, 14) or 1e-9
    want_above = side == "up"
    cands: list[tuple[float, str, str]] = []

    # EMA 21 / EMA 50 — where a trend run habitually pauses
    for period in (21, 50):
        v = float(ind.ema(df["c"], period).iloc[-1])
        if (want_above and v > price) or (not want_above and v < price):
            cands.append((v, f"EMA {period}",
                          f"EMA {period} — the run's average-price magnet"))

    # equilibrium of the dealing range (fair value — the balance point)
    pd_state = smc.premium_discount(df)
    eq = pd_state.get("eq")
    if eq is not None and (
        (want_above and float(eq) > price)
        or (not want_above and float(eq) < price)
    ):
        cands.append((float(eq), "EQ", "equilibrium — fair value of the range"))

    # the OTE band (0.62-0.79 of the active leg) on the rest side
    ote = (pd_state or {}).get("ote")
    if isinstance(ote, dict) and ote.get("lo") is not None:
        olo, ohi = float(ote["lo"]), float(ote["hi"])
        mid = (olo + ohi) / 2.0
        if (want_above and mid > price) or (not want_above and mid < price):
            cands.append((round(mid, 2), "OTE",
                          "OTE 0.62-0.79 — the deep-pullback entry band"))

    # nearest unfilled FVG on the rest side (the imbalance price returns to)
    for g in smc.detect_fvg(df, max_gaps=8):
        if g.get("filled"):
            continue
        gv = (float(g["lo"]) + float(g["hi"])) / 2.0
        if (want_above and g["side"] == "bullish" and gv > price) or (
            not want_above and g["side"] == "bearish" and gv < price
        ):
            cands.append((round(gv, 2), "FVG",
                          f"unfilled {g['side']} gap — inefficiency to fill"))

    # the last REAL rest zone boundary (where price already rested once)
    for z in rest_zones(df, window=REST_WINDOW_BARS)[:2]:
        edge = z["hi"] if want_above else z["lo"]
        if (want_above and edge > price) or (not want_above and edge < price):
            cands.append((float(edge), "PRIOR REST",
                          f"prior rest {z['lo']:.2f}-{z['hi']:.2f} "
                          f"({z['bars']} bars) — proven pause area"))

    out = []
    seen: set[float] = set()
    for p, kind, note in sorted(cands, key=lambda x: abs(x[0] - price)):
        if abs(p - price) > max_atr * atr:
            continue
        key = round(p, 1)
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "price": round(float(p), 2),
            "kind": kind,
            "dist_atr": round(abs(p - price) / atr, 2),
            "note": note,
        })
    return out[:3]


# ------------------------------------------------------- the combined read


def structure_read(df: pd.DataFrame | None, price: float | None = None) -> dict:
    """The everything-in-one verdict the engine + radar + chart share.

    Returns the ladder plus::

        {
          "phase":   "leg" | "extended" | "resting" | "reversal-confirmed",
          "p_reversal": 0..1,      # honest odds the CURRENT run ends here
          "drivers": [...],        # why the number is what it is
          "action":  str,          # one human sentence (Bengali-aware UI)
          "magnets": [...],        # where the market is expected to REST
          "rests":   [...],        # recent REST zones (chart boxes)
        }
    """
    ladder = structure_ladder(df)
    out: dict[str, Any] = dict(ladder)
    out.update({
        "phase": "leg",
        "p_reversal": 0.0,
        "drivers": [],
        "action": "",
        "magnets": [],
        "rests": [],
    })
    if df is None or len(df) < 30:
        out["action"] = "insufficient history for a structure read"
        return out
    closes = df["c"].astype(float)
    price = float(price or closes.iloc[-1])
    atr = ind.atr(df, 14) or 1e-9
    run = int(out["run"])
    run_dir = out["run_dir"]
    drivers: list[str] = []

    # recent REST zones (the "where did it rest" map)
    rests = rest_zones(df)
    out["rests"] = rests[:2]

    # momentum decay: bodies shrinking along the run
    bodies = (closes - df["o"].astype(float)).abs().to_numpy()
    recent = float(np.mean(bodies[-3:])) if len(bodies) >= 3 else 0.0
    prior = float(np.mean(bodies[-13:-3])) if len(bodies) >= 13 else 0.0
    decay = prior > 0 and recent < 0.6 * prior
    if decay and run_dir:
        drivers.append(f"momentum decay — bodies {recent / max(prior, 1e-9):.0%}"
                       " of the run's average")

    # stretch: price extended from the EMA21 mean
    e21 = float(ind.ema(closes, 21).iloc[-1])
    stretch = abs(price - e21) / atr
    if run_dir:
        same_side = (price < e21) if run_dir == "down" else (price > e21)
        if same_side and stretch > 2.0:
            drivers.append(f"stretched {stretch:.1f} ATR from EMA 21")

    # rest-in-progress: price inside/near the newest rest zone
    resting = False
    if rests:
        z = rests[0]
        near = atr * 0.5
        resting = (z["lo"] - near) <= price <= (z["hi"] + near)
        if resting:
            drivers.append(
                f"price inside the {z['bars']}-bar REST "
                f"{z['lo']:.2f}-{z['hi']:.2f}"
            )

    # honest hazard: base + per-leg + decay + stretch + fresh CHoCH
    p = HAZARD_BASE + min(HAZARD_PER_LEG * max(0, run - 1), HAZARD_LEG_CAP)
    if decay:
        p += 0.10
    if stretch > 2.0:
        p += 0.10
    if resting:
        p += 0.06
    phase = "leg"
    if out["choch_fresh"]:
        phase = "reversal-confirmed"
        p = max(p, 0.72)
        drivers.append(
            f"CHoCH {out['choch_fresh']['dir']} "
            f"{out['choch_fresh']['bars_ago']} bars ago — reversal CONFIRMED"
        )
    elif resting:
        phase = "resting"
    elif run >= LEGS_EXHAUST or (stretch > 2.0 and run >= 2):
        phase = "extended"
    p = float(np.clip(p, 0.35, 0.85))
    out["phase"] = phase
    out["p_reversal"] = round(p, 2)
    out["drivers"] = drivers

    # magnets on the counter side of the run (a mature DOWN run rests UP)
    if run_dir:
        side = "up" if run_dir == "down" else "down"
        out["magnets"] = rest_magnets(df, price, side=side)

    # the action sentence — what the user should be doing RIGHT NOW
    if phase == "reversal-confirmed":
        d = out["choch_fresh"]["dir"]
        out["action"] = (
            f"structure shifted {d} — reversal confirmed; trade the NEW "
            f"leg, entries at its pullback magnets"
        )
    elif phase == "resting":
        out["action"] = (
            "market is RESTING in the compressed range — wait for the "
            "break direction; no chasing inside the box"
        )
    elif phase == "extended":
        n = run
        out["action"] = (
            f"{n} consecutive {('lower' if run_dir == 'down' else 'higher')} "
            f"breaks — run mature: expect a REST (~2 ATR counter-move) at "
            + (", ".join(m["kind"] + " " + str(m["price"])
                         for m in out["magnets"][:2]) or "the mean")
            + " before the next leg; do not chase"
        )
    else:
        out["action"] = (
            f"run intact ({run} "
            f"{'lower' if run_dir == 'down' else 'higher' if run_dir else ''}"
            " breaks) — continuation entries at the pullback magnets"
        )
    return out


def reversal_evidence(
    signal_dir: str,
    ladder: dict,
    amd_sweep: dict | None,
) -> bool:
    """Does a COUNTER-run signal carry structural PROOF?

    The engine's D-064 guard: fading a live run is only allowed WITH
    evidence — a fresh CHoCH in the signal direction (close beyond the
    last opposite swing) or a swept-and-reclaimed liquidity pool on the
    signal's side (turtle soup). Without either, the counter signal is
    the falling-knife trade the D-061 trap autopsy kept finding.
    """
    want_up = str(signal_dir).upper() == "BUY"
    choch = ladder.get("choch_fresh") if ladder else None
    if choch and str(choch.get("dir", "")).lower() == ("up" if want_up else "down"):
        return True
    if amd_sweep and amd_sweep.get("reclaimed"):
        side = amd_sweep.get("side")
        if want_up and side == "SSL":
            return True  # lows swept and reclaimed — reversal proof up
        if not want_up and side == "BSL":
            return True  # highs swept and reclaimed — reversal proof down
    return False
