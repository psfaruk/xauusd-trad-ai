"""D-071 tests — liquidity RUN vs SWEEP life-cycle + the direction outlook.

User directive (Bengali): "কোনো লেভেল বা zone এর বা কোনো একটি ক্যান্ডেল এর
লিকুডিটি নিলো, কি নিল না। নেওয়ার পরে লিকুডিটি রান করে নাকি সুয়েপ
করবে" + "চার্ট এর ড্রয়িং আরও বিস্তারিত করতে হবে। যেনো আমি বুঝতে পারি
মার্কেট কোন দিকে যাবে।"

Covered:
- pool detection: equal highs -> BSL, equal lows -> SSL;
- the three states: untouched (the draw) / swept (reversal) / run
  (continuation) with the close-position rule (a sweep price later
  closed through IS a run — the honest rule);
- post-take displacement (disp_atr) in the expected direction;
- the draw map: nearest untouched pool above/below + fresh event;
- drawings contract: `liq` marks + the `outlook` verdict + the `path`
  target per timeframe, full-word labels, bounded, thin-data safe.
"""

from __future__ import annotations

import pandas as pd

from app.analysis.context import analyze_frame
from app.analysis.drawings import build_drawings
from app.analysis.liquidity_flow import liquidity_flow

from .test_confluence import _mk
from .test_drawings import wavy_frame

COLS = ["time_utc", "o", "h", "l", "c", "v"]


def liq_frame(
    take: str = "none",
    after: str = "flat",
    bars: int = 55,
) -> pd.DataFrame:
    """M1 tape with an equal-high BSL pool at 104.20 and an equal-low SSL
    pool at 100.50, then an optional TAKE candle at bar 45:

    take="sweep" — wick through the BSL, close back under (stop-hunt)
    take="run"   — close through the BSL (pool consumed)
    take="both"  — a sweep print first, then a closing break (run wins)
    after        — the follow-through after the take ("down" / "up" / "flat")
    """

    base = pd.Timestamp("2025-01-06 12:00", tz="UTC")
    rows: list[list] = []

    def bar(i: int, o: float, h: float, low: float, c: float) -> None:
        rows.append([base + pd.Timedelta(minutes=i), o, h, low, c, 50])

    # phase 1: rise 101 -> 102.2
    price = 101.0
    for i in range(5):
        c = price + 0.3
        bar(i, price, c + 0.05, price - 0.05, c)
        price = c
    # PEAK 1 — BSL pool print
    bar(5, 102.2, 104.20, 102.1, 102.4)
    # phase 2: fall to 101.5, rise to 102.4
    price = 102.4
    for i in range(6, 10):
        c = price - 0.25
        bar(i, price, price + 0.05, c - 0.05, c)
        price = c
    for i in range(10, 14):
        c = price + 0.25
        bar(i, price, c + 0.05, price - 0.05, c)
        price = c
    # PEAK 2 — the equal high completes the BSL pool (i = 14)
    bar(14, 102.4, 104.20, 102.3, 102.5)
    # phase 3: fall to 101.3
    price = 102.5
    for i in range(15, 20):
        c = price - 0.24
        bar(i, price, price + 0.05, c - 0.05, c)
        price = c
    # TROUGH 1 — SSL pool print
    bar(20, 101.3, 101.4, 100.50, 101.1)
    # phase 4: rise to 101.8
    price = 101.1
    for i in range(21, 26):
        c = price + 0.14
        bar(i, price, c + 0.05, price - 0.05, c)
        price = c
    # TROUGH 2 — the equal low completes the SSL pool (i = 26)
    bar(26, 101.8, 101.9, 100.50, 101.2)
    # phase 5: drift 101.8 -> 102.5 (never touches either pool)
    price = 101.2
    for i in range(27, 45):
        step = 0.04 if (i % 2 == 0) else -0.02
        c = price + step
        bar(i, price, max(price, c) + 0.06, min(price, c) - 0.06, c)
        price = c
    # the TAKE candle (bar 45)
    if take == "sweep":
        bar(45, 102.4, 104.90, 102.2, 102.6)  # wick through, close back
    elif take == "run":
        bar(45, 102.4, 104.90, 102.3, 104.70)  # close through the pool
    elif take == "both":
        bar(45, 102.4, 104.90, 102.2, 102.6)  # sweep print first...
    else:
        bar(45, 102.4, 102.55, 102.3, 102.45)
    # the follow-through (bars 46..54)
    if take == "sweep":
        price, step = 102.6, -0.15  # reversal DOWN away from the swept BSL
    elif take == "run":
        price, step = 104.7, 0.18  # continuation UP through the consumed pool
    elif take == "both":
        price = 102.6
        for i in range(46, 50):  # ...then price closes through it
            c = price + 0.45
            bar(i, price, c + 0.05, price - 0.05, c)
            price = c
        bar(50, 104.3, 105.10, 104.2, 104.65)  # the closing break
        price = 104.65
        for i in range(51, bars):
            c = price + 0.18
            bar(i, price, c + 0.05, price - 0.05, c)
            price = c
        return _mk(rows[:bars])
    else:
        price, step = 102.45, 0.006  # flat — pools stay untouched
    for i in range(46, bars):
        c = price + step
        bar(i, price, max(price, c) + 0.05, min(price, c) - 0.05, c)
        price = c
    return _mk(rows[:bars])


# ------------------------------------------------------------- pools/states


def test_pools_detected_equal_highs_and_lows():
    flow = liquidity_flow(liq_frame(), price=102.5)
    pools = flow["pools"]
    bsl = [p for p in pools if p["kind"] == "BSL"]
    ssl = [p for p in pools if p["kind"] == "SSL"]
    assert any(abs(p["price"] - 104.20) < 0.01 for p in bsl)
    assert any(abs(p["price"] - 100.50) < 0.01 for p in ssl)
    for p in pools:
        assert p["state"] == "untouched"
        assert p["dist_atr"] is not None and p["dist_atr"] > 0.35


def test_untouched_pools_are_the_draw():
    flow = liquidity_flow(liq_frame(), price=102.5)
    draw = flow["draw"]
    assert draw["above"] is not None and draw["above"]["kind"] == "BSL"
    assert draw["below"] is not None and draw["below"]["kind"] == "SSL"
    assert draw["dir"] in ("up", "down")
    assert draw["fresh"] is None  # nothing was taken


def test_sweep_state_wick_through_close_back():
    flow = liquidity_flow(liq_frame(take="sweep", after="down"), price=101.4)
    bsl = next(p for p in flow["pools"] if p["kind"] == "BSL")
    assert bsl["state"] == "swept"
    assert bsl["t_event"] is not None
    # post-take displacement measured in the reversal direction (down)
    assert bsl["disp_atr"] is not None and bsl["disp_atr"] > 0
    # the sweep is the FRESH event: BSL swept -> reversal down
    fresh = flow["draw"]["fresh"]
    assert fresh is not None
    assert fresh["state"] == "swept" and fresh["side"] == "BSL"
    assert fresh["dir"] == "down"
    assert fresh["bars_ago"] <= 12


def test_run_state_close_through():
    flow = liquidity_flow(liq_frame(take="run", after="up"), price=105.2)
    bsl = next(p for p in flow["pools"] if p["kind"] == "BSL")
    assert bsl["state"] == "run"
    assert bsl["t_event"] is not None
    # continuation displacement measured upward
    assert bsl["disp_atr"] is not None and bsl["disp_atr"] > 0
    fresh = flow["draw"]["fresh"]
    assert fresh is not None
    assert fresh["state"] == "run" and fresh["dir"] == "up"


def test_sweep_then_close_through_is_a_run():
    """The honest rule: a sweep print that price LATER closed through is
    a RUN (the pool was consumed), with t_event at the FIRST take."""
    flow = liquidity_flow(liq_frame(take="both"), price=105.2)
    bsl = next(p for p in flow["pools"] if p["kind"] == "BSL")
    assert bsl["state"] == "run"
    # the event time is the FIRST excursion (the sweep print at bar 45)
    t0 = pd.Timestamp("2025-01-06 12:45", tz="UTC")
    assert pd.Timestamp(bsl["t_event"]) == t0


def test_thin_frame_returns_empty():
    thin = _mk([
        [pd.Timestamp("2025-01-06 12:00", tz="UTC") + pd.Timedelta(minutes=i),
         100.0, 100.5, 99.5, 100.2, 50]
        for i in range(10)
    ])
    flow = liquidity_flow(thin)
    assert flow["pools"] == []
    assert flow["draw"]["dir"] is None


# ---------------------------------------------------------------- drawings


def _frames() -> dict[str, pd.DataFrame]:
    frame = liq_frame()
    base = pd.Timestamp("2025-01-06 12:00", tz="UTC")
    return {
        "M1": frame,
        "M5": frame,
        "M15": wavy_frame(base, 15, start=81.6, bars=120),
        "M30": wavy_frame(base, 30, start=81.6, bars=120),
        "H1": wavy_frame(base, 60, start=81.6, bars=120),
        "H4": wavy_frame(base, 240, start=81.6, bars=120),
    }


def _snaps(frames: dict[str, pd.DataFrame]) -> dict[str, dict]:
    return {tf: analyze_frame(df) for tf, df in frames.items()}


def test_drawings_carry_liq_outlook_and_path():
    frames = _frames()
    out = build_drawings(frames, _snaps(frames), price=102.5, tf="M1")
    kinds = {d["kind"] for d in out}
    assert "liq" in kinds, f"liq marks missing: {sorted(kinds)}"
    assert "outlook" in kinds, f"outlook missing: {sorted(kinds)}"
    assert "path" in kinds, f"path missing: {sorted(kinds)}"
    liqs = [d for d in out if d["kind"] == "liq"]
    assert len(liqs) <= 5
    for d in liqs:
        assert d["state"] in ("untouched", "swept", "run")
        assert d["side"] in ("BSL", "SSL")
        assert "Liquidity" in d["label"]  # full-word labels
        assert d["tone"] in ("bull", "bear", "gold", "violet", "neutral")


def test_outlook_synthesizes_direction():
    frames = _frames()
    out = build_drawings(frames, _snaps(frames), price=102.5, tf="M1")
    outlook = next(d for d in out if d["kind"] == "outlook")
    assert outlook["dir"] in ("up", "down")
    assert len(outlook["lines"]) == 3
    assert outlook["lines"][0].startswith("DIRECTION")
    assert outlook["regime"] is not None
    # the path points at the untouched pool the outlook drew toward
    path = next(d for d in out if d["kind"] == "path")
    assert path["dir"] == outlook["dir"]
    assert path["to_price"] in (104.20, 100.50)


def test_outlook_fresh_event_beats_draw():
    frames = {tf: df for tf, df in _frames().items()}
    frames["M1"] = liq_frame(take="sweep", after="down")
    frames["M5"] = frames["M1"]
    out = build_drawings(frames, _snaps(frames), price=101.4, tf="M1")
    outlook = next(d for d in out if d["kind"] == "outlook")
    # the fresh BSL sweep says DOWN even though the nearest untouched
    # pool below is the SSL draw — the fresh event is the driver
    assert outlook["dir"] == "down"
    assert outlook["fresh"] is not None
    assert "sweep" in outlook["lines"][0]


def test_every_tf_gets_its_own_liquidity_set():
    frames = _frames()
    snaps = _snaps(frames)
    for tf in ("M1", "M5", "M15", "H1"):
        out = build_drawings(frames, snaps, price=102.5, tf=tf)
        kinds = {d["kind"] for d in out}
        assert "liq" in kinds, f"{tf} missing liq marks"
        assert "outlook" in kinds, f"{tf} missing outlook"


def test_drawings_bounded_and_thin_safe():
    frames = _frames()
    out = build_drawings(frames, _snaps(frames), price=102.5, tf="M1")
    assert len(out) <= 60
    thin = {"M1": _mk([
        [pd.Timestamp("2025-01-06 12:00", tz="UTC") + pd.Timedelta(minutes=i),
         100.0, 100.5, 99.5, 100.2, 50]
        for i in range(12)
    ])}
    assert build_drawings(thin, _snaps(thin), price=100.2, tf="M1") == []
