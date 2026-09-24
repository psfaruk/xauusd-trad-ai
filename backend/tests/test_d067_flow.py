"""D-067 tests — the candle-by-candle buyer/seller BATTLE.

User directive (Bengali): "একটি রানিং ক্যান্ডেল বা কয়েক টি ক্যান্ডেল buyer
Sellar position, কারা কাদের কে ডোমেনেট করছে, কারা জিতেছে, লাস্ট কয়েক টি
ক্যান্ডেল এর ভিতর কি ঘটেছে, মোট কথা রানিং ক্যান্ডেল এর রিয়েকশন"

Covers:
- candle_anatomy: who won the period (close vs mid), conviction words,
  the wick classification and its ATR depth;
- battle_read: the last-N-candle war — volume-weighted domination,
  wins, the winning streak, net displacement in ATR, the events INSIDE
  the candles (rejection / absorption / momentum), the verdict line;
- running_candle_read: the forming bar's live verdict (dominance,
  wick war, control, reaction sentence);
- evaluate(): the pulse carries the battle every close; the flow gate
  penalizes a signal fighting a dominating battle (the user's exact
  trap shape), rewards an aligned one, and a tug of war costs nothing
  — nothing is ever hard-blocked (D-049);
- drawings: the BATTLE badge + REJECT marks land on the chart for
  intraday TFs and stay off the HTF;
- config: the D-067 block defaults.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd

from app.analysis.drawings import build_drawings
from app.analysis.orderflow import (
    battle_read,
    candle_anatomy,
    running_candle_read,
)
from app.engine.config import EngineConfig
from app.engine.engine import evaluate
from tests.test_d048_poi_zones import _demand_zone_frame, _uptrend_htf

CFG = EngineConfig()


# ------------------------------------------------------------- fixtures


def _t0() -> datetime:
    return datetime(2026, 9, 24, 10, 0, tzinfo=UTC)


def _frame(rows: list[tuple], start: datetime | None = None) -> pd.DataFrame:
    """rows: (o, h, l, c, v) tuples on a 1-minute grid."""
    t0 = start or _t0()
    recs = [
        {
            "time_utc": t0 + timedelta(minutes=i),
            "o": o, "h": h, "l": lo, "c": c, "v": v,
        }
        for i, (o, h, lo, c, v) in enumerate(rows)
    ]
    return pd.DataFrame(recs)


def _bull_tail_frame(n: int = 80, tail: int = 6) -> pd.DataFrame:
    """A flat frame whose last `tail` candles are decisive bulls that
    CLOSE AT THEIR HIGHS — buyers won every period."""
    rng = np.random.default_rng(5)
    rows = []
    p = 100.0
    for i in range(n):
        if i >= n - tail:
            o, c = p, p + 0.9  # strong bull body
            h, lo = c + 0.01, o - 0.01  # closes at the high
        else:
            o, c = p, p + rng.normal(0, 0.1)
            h, lo = max(o, c) + 0.12, min(o, c) - 0.12
        rows.append((o, h, lo, c, 100.0))
        p = c
    return _frame(rows)


def _bear_tail_frame(n: int = 80, tail: int = 6) -> pd.DataFrame:
    """Mirror: the last `tail` candles are decisive bears closing at
    their lows — sellers won every period."""
    rng = np.random.default_rng(6)
    rows = []
    p = 100.0
    for i in range(n):
        if i >= n - tail:
            o, c = p, p - 0.9
            h, lo = o + 0.01, c - 0.01  # closes at the low
        else:
            o, c = p, p + rng.normal(0, 0.1)
            h, lo = max(o, c) + 0.12, min(o, c) - 0.12
        rows.append((o, h, lo, c, 100.0))
        p = c
    return _frame(rows)


def _alternating_frame(n: int = 80) -> pd.DataFrame:
    """Alternating winners — a tug of war, nobody dominates."""
    rows = []
    p = 100.0
    for i in range(n):
        o = p
        c = p + (0.5 if i % 2 == 0 else -0.5)
        h, lo = max(o, c) + 0.3, min(o, c) - 0.3
        rows.append((o, h, lo, c, 100.0))
        p = c
    return _frame(rows)


# ------------------------------------------------------- candle_anatomy


def test_anatomy_buyers_won_the_period() -> None:
    bar = pd.Series(
        {"o": 100.0, "h": 101.0, "l": 99.5, "c": 100.9, "v": 120.0,
         "time_utc": _t0()}
    )
    an = candle_anatomy(bar, atr_v=0.8, vol_mean=100.0)
    assert an["won_by"] == "buyers"  # close above the mid
    assert an["dir"] == "bull"
    assert an["buy_pct"] > 90.0  # closed near the high
    assert an["conviction"] == "strong"  # body >= 60% of range
    assert an["wick"] == "lower"  # 0.5 dip wick — buyers bought the dip
    assert an["vol_ratio"] == 1.2


def test_anatomy_upper_wick_is_a_seller_rejection() -> None:
    bar = pd.Series(
        {"o": 100.0, "h": 101.6, "l": 99.9, "c": 100.1, "v": 120.0,
         "time_utc": _t0()}
    )
    an = candle_anatomy(bar, atr_v=0.8, vol_mean=100.0)
    assert an["wick"] == "upper"
    assert an["reject_atr"] == round((101.6 - 100.1) / 0.8, 2)  # 1.88
    assert an["won_by"] == "sellers"  # closed in the lower half of the range


def test_anatomy_zero_range_doji_is_safe() -> None:
    bar = pd.Series(
        {"o": 100.0, "h": 100.0, "l": 100.0, "c": 100.0, "v": 10.0,
         "time_utc": _t0()}
    )
    an = candle_anatomy(bar)
    assert an["won_by"] == "none"
    assert an["conviction"] == "no range"


# ----------------------------------------------------------- battle_read


def test_battle_buyers_dominate_and_won_every_candle() -> None:
    df = _bull_tail_frame()
    b = battle_read(df, n=6)
    assert b["state"] == "buyers"
    assert b["buy_pct"] >= 90.0
    assert b["wins"]["buyers"] == 6 and b["wins"]["sellers"] == 0
    assert b["streak"]["side"] == "buyers" and b["streak"]["len"] == 6
    assert b["net_atr"] > 3.0  # 6 x ~0.9 = ~5.4 ATR (ATR ~1.0x body)
    assert "buyers dominate" in b["verdict"]
    assert "6/6" in b["verdict"]
    assert len(b["candles"]) == 6


def test_battle_sellers_dominate_mirror() -> None:
    df = _bear_tail_frame()
    b = battle_read(df, n=6)
    assert b["state"] == "sellers"
    assert b["sell_pct"] >= 90.0
    assert b["wins"]["sellers"] == 6
    assert b["net_atr"] < -3.0
    assert "sellers dominate" in b["verdict"]


def test_battle_tug_of_war_nobody_dominates() -> None:
    b = battle_read(_alternating_frame(), n=6)
    assert b["state"] == "tug"
    assert "tug of war" in b["verdict"]


def test_battle_reports_the_rejection_inside_the_candles() -> None:
    """A decisive upper wick INSIDE the window is reported as a seller
    rejection event at the wick extreme — 'কি ঘটেছে ভিতরে'."""
    rows = [
        (100.0, 100.2, 99.8, 100.0, 100.0),
        (100.0, 100.2, 99.8, 100.1, 100.0),
        (100.0, 100.2, 99.8, 100.0, 100.0),
        (100.0, 103.0, 99.9, 100.1, 400.0),  # huge upper wick + volume
        (100.0, 100.2, 99.8, 100.05, 100.0),
        (100.0, 100.2, 99.8, 100.1, 100.0),
    ]
    # pad with 60 bars of calm so ATR is small (the wick is deep in ATR)
    calm = [(100.0 + 0.01 * i, 100.2 + 0.01 * i, 99.8 + 0.01 * i,
             100.0 + 0.01 * i, 100.0) for i in range(60)]
    df = _frame(calm + rows)
    b = battle_read(df, n=6)
    rejections = [e for e in b["events"] if e["kind"] == "rejection"]
    assert rejections
    r = rejections[0]
    assert r["side"] == "sellers"
    assert r["price"] == 103.0
    assert r["depth_atr"] >= 0.35
    assert "rejected the high" in r["note"]


def test_battle_short_frame_returns_safe_defaults() -> None:
    b = battle_read(_frame([(100.0, 100.2, 99.8, 100.1, 10.0)]), n=6)
    assert b["n"] == 0
    assert b["state"] == "tug"
    b2 = battle_read(None)
    assert b2["n"] == 0


# --------------------------------------------------- running_candle_read


def test_running_candle_buyers_winning_with_wick_war() -> None:
    bar = pd.Series(
        {"o": 100.0, "h": 101.0, "l": 96.0, "c": 100.8, "v": 50.0}
    )
    r = running_candle_read(bar)
    assert r["buy_pct"] > 90.0
    assert r["control"] == "buyers"
    assert r["wick"] == "lower"  # lower wick 100-96 = 4 > 45% of range
    assert r["wick_war"] == "buyers absorbing the dip"
    assert "buyers winning" in r["reaction"]
    assert "absorbing the dip" in r["reaction"]


def test_running_candle_just_opened() -> None:
    bar = pd.Series({"o": 100.0, "h": 100.0, "l": 100.0, "c": 100.0, "v": 0.0})
    r = running_candle_read(bar)
    assert "battle has not started" in r["reaction"]


# ------------------------------------------------------------- evaluate()


def test_pulse_carries_the_battle_every_close() -> None:
    """The radar knows the war on every M1 close: state, split, wins,
    streak, net ATR and the human verdict."""
    from app.engine.backtest import load_mock_history, resample_ohlc

    m1 = load_mock_history(3000, seed=42)
    close_time = m1["time_utc"].iloc[-1] + timedelta(minutes=1)
    htf = {"M5": resample_ohlc(m1, 5, src_min=1),
           "M15": resample_ohlc(m1, 15, src_min=1),
           "H1": resample_ohlc(m1, 60, src_min=1)}
    ev = evaluate(m1, htf, close_time, EngineConfig(), spread_points=20)
    b = ev.pulse.get("battle")
    assert b is not None
    assert b["state"] in ("buyers", "sellers", "tug")
    assert b["n"] == EngineConfig().flow_window
    assert isinstance(b["wins"], dict)
    assert b["verdict"]
    assert isinstance(b["events"], list)


def _patched_battle(fake: dict):
    """Patch the engine's lazy orderflow import with a fixed battle."""
    from app.analysis import orderflow as of_mod

    orig = of_mod.battle_read
    of_mod.battle_read = lambda *a, **k: dict(fake)
    return orig, of_mod


_DOM_SELLERS = {
    "n": 6, "state": "sellers", "buy_pct": 18.0, "sell_pct": 82.0,
    "wins": {"buyers": 0, "sellers": 6},
    "streak": {"side": "sellers", "len": 4},
    "net_atr": -2.1, "events": [], "candles": [],
    "verdict": "sellers dominate the last 6 candles — 82% of the flow",
    "participation": 1.1,
}

_DOM_BUYERS = {
    "n": 6, "state": "buyers", "buy_pct": 82.0, "sell_pct": 18.0,
    "wins": {"buyers": 6, "sellers": 0},
    "streak": {"side": "buyers", "len": 5},
    "net_atr": 2.2, "events": [], "candles": [],
    "verdict": "buyers dominate the last 6 candles — 82% of the flow",
    "participation": 1.1,
}

_TUG = {
    "n": 6, "state": "tug", "buy_pct": 52.0, "sell_pct": 48.0,
    "wins": {"buyers": 3, "sellers": 3}, "streak": None,
    "net_atr": 0.1, "events": [], "candles": [],
    "verdict": "tug of war", "participation": 1.0,
}


def test_flow_gate_penalizes_fighting_the_domination() -> None:
    """The user's exact trap shape: a BUY printed while SELLERS were
    stacking winning candles. The trade still FIRES (D-049 — never a
    silent block) but pays the confidence cost, visibly in the trace."""
    df = _demand_zone_frame()
    htf = _uptrend_htf()
    close_time = df["time_utc"].iloc[-1] + timedelta(minutes=1)

    orig, mod = _patched_battle(_TUG)  # baseline: tug costs nothing
    try:
        ev_base = evaluate(
            df, htf, close_time,
            EngineConfig(entry_mode="market", regime_guard=False),
            spread_points=20,
        )
    finally:
        mod.battle_read = orig

    orig, mod = _patched_battle(_DOM_SELLERS)
    try:
        ev_pen = evaluate(
            df, htf, close_time,
            EngineConfig(entry_mode="market", regime_guard=False),
            spread_points=20,
        )
    finally:
        mod.battle_read = orig

    assert ev_base.signal is not None and ev_pen.signal is not None
    guard = [c for c in ev_pen.trace["checks"] if c["name"] == "flow_guard"]
    assert guard and guard[0]["pass"]
    assert "dominating sellers battle" in guard[0]["value"]
    assert ev_pen.signal["confidence"] \
        < ev_base.signal["confidence"] - 0.03
    # the context + fired blocks carry the verdict
    assert ev_pen.signal["context"]["flow"]["state"] == "sellers"
    assert ev_pen.signal["context"]["flow"]["note"]
    assert ev_pen.pulse["fired"]["flow"]["sell_pct"] == 82.0
    assert ev_pen.signal["flow_note"]


def test_flow_gate_rewards_the_aligned_battle() -> None:
    """A BUY riding a dominating BUYERS battle earns the small bonus —
    the flow pushing the trade's direction."""
    df = _demand_zone_frame()
    htf = _uptrend_htf()
    close_time = df["time_utc"].iloc[-1] + timedelta(minutes=1)

    orig, mod = _patched_battle(_TUG)
    try:
        ev_base = evaluate(
            df, htf, close_time,
            EngineConfig(entry_mode="market", regime_guard=False),
            spread_points=20,
        )
    finally:
        mod.battle_read = orig

    orig, mod = _patched_battle(_DOM_BUYERS)
    try:
        ev_bon = evaluate(
            df, htf, close_time,
            EngineConfig(entry_mode="market", regime_guard=False),
            spread_points=20,
        )
    finally:
        mod.battle_read = orig

    assert ev_bon.signal is not None
    guard = [c for c in ev_bon.trace["checks"] if c["name"] == "flow_guard"]
    assert guard and "WITH the dominating buyers battle" in guard[0]["value"]
    assert ev_bon.signal["confidence"] > ev_base.signal["confidence"]


def test_flow_gate_off_and_tug_cost_nothing() -> None:
    """Guard OFF (or a tug of war) leaves the confidence untouched —
    the gate never invents risk out of a balanced market."""
    df = _demand_zone_frame()
    htf = _uptrend_htf()
    close_time = df["time_utc"].iloc[-1] + timedelta(minutes=1)

    orig, mod = _patched_battle(_DOM_SELLERS)
    try:
        ev = evaluate(
            df, htf, close_time,
            EngineConfig(entry_mode="market", flow_guard=False, regime_guard=False),
            spread_points=20,
        )
    finally:
        mod.battle_read = orig
    assert ev.signal is not None
    assert "flow_guard" not in {c["name"] for c in ev.trace["checks"]}
    assert ev.signal["context"]["flow"]["state"] == "sellers"  # still reported


def test_flow_streak_threshold_respected() -> None:
    """A single winning candle (streak 1) does NOT veto the trade —
    only the streak (>= flow_streak) makes domination dangerous."""
    df = _demand_zone_frame()
    htf = _uptrend_htf()
    close_time = df["time_utc"].iloc[-1] + timedelta(minutes=1)
    weak = dict(_DOM_SELLERS, streak={"side": "sellers", "len": 1})

    orig, mod = _patched_battle(weak)
    try:
        ev = evaluate(
            df, htf, close_time,
            EngineConfig(entry_mode="market", regime_guard=False),
            spread_points=20,
        )
    finally:
        mod.battle_read = orig
    assert ev.signal is not None
    assert "flow_guard" not in {c["name"] for c in ev.trace["checks"]}


# -------------------------------------------------------------- drawings


def test_battle_marks_land_on_the_chart() -> None:
    """The war badge + the rejection marks are real drawings the chart
    renders (the user SEES who dominates and what happened inside)."""
    from app.analysis.setup_geometry import setup_snapshot

    df = _bull_tail_frame(n=120)
    frames = {"M5": df}
    price = float(df["c"].iloc[-1])
    snap = setup_snapshot(df, "M5")
    snaps = {"M5": snap}
    marks = build_drawings(
        frames, snaps, price, [], "M5",
    )
    battles = [d for d in marks if d["kind"] == "battle"]
    assert battles
    b = battles[0]
    assert "BATTLE" in b["label"] and "BUYERS" in b["label"]
    assert b["tone"] == "bull"
    assert b["note"]  # the full verdict rides the note


def test_battle_marks_off_the_htf_views() -> None:
    from app.analysis.setup_geometry import setup_snapshot

    df = _bull_tail_frame(n=120)
    snaps = {"H1": setup_snapshot(df, "M5")}
    marks = build_drawings({"H1": df}, snaps, float(df["c"].iloc[-1]),
                           [], "H1")
    assert not [d for d in marks if d["kind"] in ("battle", "reject")]


# ---------------------------------------------------------------- config


def test_config_d067_flow_block_defaults() -> None:
    assert CFG.flow_guard is True
    assert CFG.flow_window == 6
    assert CFG.flow_domination == 0.72
    assert CFG.flow_streak == 3
    assert CFG.flow_penalty == 0.08
    assert CFG.flow_bonus == 0.04
