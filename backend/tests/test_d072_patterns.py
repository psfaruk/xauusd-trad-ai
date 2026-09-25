"""D-072 tests — classic chart-pattern engine (the studied YouTube channel
@easytradingeasy's recipe: numbered swing points, thin geometry, ENTRY /
SL / TARGET measured-move plan).

Crafted deterministic tapes:
- DOUBLE TOP: two equal highs over a shared trough, close breaks neckline;
- DOUBLE BOTTOM: mirror image, confirmed up;
- HEAD & SHOULDERS: three highs, head tallest, neckline break;
- BULL FLAG: strong pole, tight 3-pivot consolidation;
- ASCENDING TRIANGLE: flat highs, rising lows;
- robustness: short frames / random walk — never crashes, bounded output;
- integration: build_drawings emits kind=pattern with the full payload.
"""

from __future__ import annotations

import math
import random
from datetime import UTC, datetime, timedelta

import pandas as pd

from app.analysis.drawings import build_drawings
from app.analysis.patterns import detect_patterns

COLS = ["time_utc", "o", "h", "l", "c", "v"]


def _mk(rows: list[tuple]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=COLS)


def _atr(df: pd.DataFrame, n: int = 14) -> float:
    tr = []
    for i in range(1, len(df)):
        tr.append(max(
            df["h"].iloc[i] - df["l"].iloc[i],
            abs(df["h"].iloc[i] - df["c"].iloc[i - 1]),
            abs(df["l"].iloc[i] - df["c"].iloc[i - 1]),
        ))
    if not tr:
        return 1.0
    return sum(sorted(tr)[-n:]) / min(n, len(tr))


def _leg(rows, t0, minutes, start, end, bars, wick=0.15, vol=50):
    """Linear leg from start to end over `bars` bars."""
    step = (end - start) / bars
    price = start
    for i in range(bars):
        t = t0 + timedelta(minutes=minutes * (len(rows) + i))
        c = price + step
        o = price
        rows.append([t, o, max(o, c) + wick, min(o, c) - wick, c, vol])
        price = c


def _hold(rows, t0, minutes, price, bars, wick=0.15, vol=50):
    _leg(rows, t0, minutes, price, price, bars, wick=wick, vol=vol)


def base_time() -> pd.Timestamp:
    return pd.Timestamp(datetime.now(UTC)).floor("min") - timedelta(minutes=400)


# ---------------------------------------------------------- double top


def double_top_frame(confirmed: bool = True) -> pd.DataFrame:
    """Rally -> 1st top 110 -> trough 104 -> equal top 110 -> drop."""
    rows: list[tuple] = []
    t0 = base_time()
    _leg(rows, t0, 5, 100.0, 108.0, 14)          # rally to shoulder zone
    _leg(rows, t0, 5, 108.0, 110.0, 4)           # 1st top
    _leg(rows, t0, 5, 110.0, 104.0, 6)           # dip to trough
    _leg(rows, t0, 5, 104.0, 110.0, 6)           # 2nd equal top
    if confirmed:
        _leg(rows, t0, 5, 110.0, 103.0, 5)       # neckline BREAK (close < 104)
    else:
        _leg(rows, t0, 5, 110.0, 107.0, 5)       # still hanging below the top
    return _mk(rows)


def test_double_top_confirmed():
    df = double_top_frame(confirmed=True)
    atr = _atr(df)
    pats = detect_patterns(df, atr, float(df["c"].iloc[-1]))
    names = [p["name"] for p in pats]
    assert "DOUBLE TOP" in names, names
    p = next(p for p in pats if p["name"] == "DOUBLE TOP")
    assert p["kind"] == "pattern"
    assert p["dir"] == "down"
    assert p["state"] == "confirmed"
    assert p["tone"] == "bear"
    assert p["sl"] > max(pt["price"] for pt in p["points"])  # SL above tops
    assert p["target"] < p["entry"]["price"]                  # target below neck
    assert p["rr"] is not None and p["rr"] > 0
    # numbered structure walk 1..N on every pivot (double top = 3)
    assert len(p["points"]) >= 3
    assert [pt["n"] for pt in p["points"]] == list(range(1, len(p["points"]) + 1))
    # every line has both endpoints
    for ln in p["lines"]:
        assert ln["t1"] and ln["t2"]
    # payload the frontend needs
    assert p["target_zone"]["lo"] <= p["target"] <= p["target_zone"]["hi"]
    assert p["height_atr"] and p["height_atr"] >= 0.9


def test_double_top_forming():
    df = double_top_frame(confirmed=False)
    atr = _atr(df)
    pats = detect_patterns(df, atr, float(df["c"].iloc[-1]))
    p = next((p for p in pats if p["name"] == "DOUBLE TOP"), None)
    assert p is not None
    assert p["state"] == "forming"


# ------------------------------------------------------- double bottom


def double_bottom_frame() -> pd.DataFrame:
    rows: list[tuple] = []
    t0 = base_time()
    _leg(rows, t0, 5, 112.0, 104.0, 14)          # slide to the lows
    _leg(rows, t0, 5, 104.0, 110.0, 5)           # bounce (neck ~110)
    _leg(rows, t0, 5, 110.0, 104.0, 5)           # 2nd equal low
    _leg(rows, t0, 5, 104.0, 111.5, 5)           # neckline BREAK (close > 110)
    return _mk(rows)


def test_double_bottom_confirmed():
    df = double_bottom_frame()
    atr = _atr(df)
    pats = detect_patterns(df, atr, float(df["c"].iloc[-1]))
    p = next((p for p in pats if p["name"] == "DOUBLE BOTTOM"), None)
    assert p is not None, [p["name"] for p in pats]
    assert p["dir"] == "up"
    assert p["state"] == "confirmed"
    assert p["tone"] == "bull"
    assert p["sl"] < min(pt["price"] for pt in p["points"])
    assert p["target"] > p["entry"]["price"]


# --------------------------------------------------- head & shoulders


def hs_frame() -> pd.DataFrame:
    rows: list[tuple] = []
    t0 = base_time()
    _leg(rows, t0, 5, 100.0, 106.0, 10)          # approach
    _leg(rows, t0, 5, 106.0, 109.0, 3)           # left shoulder 109
    _leg(rows, t0, 5, 109.0, 104.5, 4)           # neck trough L
    _leg(rows, t0, 5, 104.5, 113.0, 4)           # head 113
    _leg(rows, t0, 5, 113.0, 104.5, 4)           # neck trough R
    _leg(rows, t0, 5, 104.5, 109.0, 3)           # right shoulder 109
    _leg(rows, t0, 5, 109.0, 103.0, 4)           # neckline BREAK
    return _mk(rows)


def test_head_and_shoulders():
    df = hs_frame()
    atr = _atr(df)
    pats = detect_patterns(df, atr, float(df["c"].iloc[-1]))
    p = next((p for p in pats if p["name"] == "HEAD & SHOULDERS"), None)
    assert p is not None, [p["name"] for p in pats]
    assert p["dir"] == "down"
    assert p["state"] == "confirmed"
    # target = head height below the neckline
    neck = p["entry"]["price"]
    head = max(pt["price"] for pt in p["points"])
    assert abs(p["target"] - (neck - (head - neck))) < 1e-6


# ---------------------------------------------------------- bull flag


def bull_flag_frame() -> pd.DataFrame:
    rows: list[tuple] = []
    t0 = base_time()
    _leg(rows, t0, 5, 100.0, 104.0, 5)           # lead-in rise
    _leg(rows, t0, 5, 104.0, 102.5, 3)           # small dip -> fractal LOW
    _leg(rows, t0, 5, 102.5, 109.0, 6)           # POLE up (strong ~6.5)
    _leg(rows, t0, 5, 109.0, 108.4, 3)           # consolidation drift down
    _leg(rows, t0, 5, 108.4, 108.7, 3)
    _leg(rows, t0, 5, 108.7, 108.2, 3)
    _leg(rows, t0, 5, 108.2, 108.8, 3)
    return _mk(rows)


def test_bull_flag():
    df = bull_flag_frame()
    atr = _atr(df)
    pats = detect_patterns(df, atr, float(df["c"].iloc[-1]))
    p = next((x for x in pats if x["family"] == "continuation"), None)
    assert p is not None, [x["name"] for x in pats]
    assert p["dir"] == "up"
    assert p["name"] in ("BULL FLAG", "BULL PENNANT")
    assert p["target"] > p["entry"]["price"]
    assert p["sl"] < p["entry"]["price"]


# --------------------------------------------------- ascending triangle


def asc_triangle_frame() -> pd.DataFrame:
    rows: list[tuple] = []
    t0 = base_time()
    _leg(rows, t0, 5, 100.0, 108.0, 10)          # first rise
    _leg(rows, t0, 5, 108.0, 103.0, 5)           # first low 103
    _leg(rows, t0, 5, 103.0, 108.0, 5)           # back to flat top 108
    _leg(rows, t0, 5, 108.0, 105.5, 4)           # HIGHER low 105.5
    _leg(rows, t0, 5, 105.5, 108.0, 4)           # press the top again
    _leg(rows, t0, 5, 108.0, 106.8, 3)           # still inside
    return _mk(rows)


def test_ascending_triangle():
    df = asc_triangle_frame()
    atr = _atr(df)
    pats = detect_patterns(df, atr, float(df["c"].iloc[-1]))
    p = next((x for x in pats if "TRIANGLE" in x["name"] or "RECTANGLE" in x["name"]), None)
    assert p is not None, [x["name"] for x in pats]
    assert p["entry"]["price"] > 0


# ------------------------------------------------------- robustness


def test_no_crash_short_frames():
    t0 = base_time()
    rows = []
    for i in range(12):
        rows.append([t0 + timedelta(minutes=5 * i), 100, 101, 99, 100, 50])
    assert detect_patterns(_mk(rows), 1.0, 100.0) == []
    assert detect_patterns(_mk(rows), 0.0, 100.0) == []  # atr<=0 guard


def test_no_crash_random_walk():
    rng = random.Random(7)
    rows = []
    price = 100.0
    t0 = base_time()
    for i in range(150):
        price += rng.gauss(0, 0.5)
        o = price - rng.uniform(0, 0.2)
        c = price + rng.gauss(0, 0.1)
        rows.append([
            t0 + timedelta(minutes=5 * i), o, max(o, c) + 0.1,
            min(o, c) - 0.1, c, 50,
        ])
    df = _mk(rows)
    pats = detect_patterns(df, _atr(df), float(df["c"].iloc[-1]))
    assert isinstance(pats, list)
    assert len(pats) <= 2
    for p in pats:
        assert set(p) >= {
            "kind", "name", "dir", "state", "points", "lines", "entry",
            "sl", "target", "target_zone", "tone", "label",
        }


def test_none_df():
    assert detect_patterns(None, 1.0, 100.0) == []


# ------------------------------------------------------- integration


def test_build_drawings_includes_patterns():
    """Full /api/analysis pipeline: the pattern layer joins drawings."""
    from app.analysis.context import analyze_frame

    df = double_top_frame()
    snaps = {"M5": analyze_frame(df)}
    frames = {"M5": df}
    price = float(df["c"].iloc[-1])
    out = build_drawings(frames, snaps, price, [], tf="M5")
    kinds = [d.get("kind") for d in out]
    assert "pattern" in kinds
    pat = next(d for d in out if d.get("kind") == "pattern")
    assert pat["name"] == "DOUBLE TOP"
    # bounded payload
    assert len(out) <= 60
    # ISO times everywhere the frontend parses
    for ln in pat["lines"]:
        assert isinstance(ln["t1"], str) and ln["t1"].endswith("+00:00")
        assert isinstance(ln["t2"], str)


def test_bounded_even_with_two_patterns():
    """Two patterns (reversal + continuation) both fit in the drawing cap."""
    from app.analysis.context import analyze_frame

    df = hs_frame()
    snaps = {"M5": analyze_frame(df)}
    out = build_drawings({"M5": df}, snaps, float(df["c"].iloc[-1]), [], tf="M5")
    pats = [d for d in out if d.get("kind") == "pattern"]
    assert len(pats) <= 2
    # every pattern's numbered walk is strictly increasing
    for p in pats:
        ns = [pt["n"] for pt in p["points"]]
        assert ns == sorted(ns)
        assert len(set(ns)) == len(ns)


def test_math_exactness_of_rr():
    df = double_top_frame()
    atr = _atr(df)
    pats = detect_patterns(df, atr, float(df["c"].iloc[-1]))
    p = next(x for x in pats if x["name"] == "DOUBLE TOP")
    risk = abs(p["entry"]["price"] - p["sl"])
    reward = abs(p["target"] - p["entry"]["price"])
    assert math.isclose(p["rr"], round(reward / risk, 2), rel_tol=1e-9)
