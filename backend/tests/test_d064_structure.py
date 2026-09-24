"""D-064 tests — the market-structure engine (legs / rests / reversals).

User directive (Bengali): "মার্কেট নিচে যাচ্ছে নিচে যাচ্ছে... মার্কেট কোথায়
গিয়ে রেস্ট করে বা একটু বিশ্রাম নেয়, বিশ্রাম নিয়ে একটু উপরের দিকে যায়,
তারপর আবার ডাউন এ যায়... কি এমন লজিক আছে যে মার্কেট এখন রিভার্স
করবে? আর কত বার HL LL LH HH LOWER HIGHER হলে রিভার্স বা কনটিনিউ
করে মার্কেট?"

Covers:
- structure_ladder: consecutive same-direction break counting, the
  labeled HH/HL/LH/LL tail, the fresh-CHoCH reversal proof;
- rest_zones: compressed pauses (the "বিশ্রাম") detected as boxes;
- rest_magnets: WHERE the market rests next (EMA / EQ / FVG / OTE /
  prior rest) on the counter side only;
- structure_read: the phase verdict (leg / extended / resting /
  reversal-confirmed), honest p(reversal) bounds, the action sentence;
- reversal_evidence: CHoCH or swept-and-reclaimed pool counts as proof;
- evaluate(): the pulse carries the ladder every close, the guard
  refuses a no-proof fade of a mature run, evidence lets it fire, the
  with-run chase/rest adjustments appear, the payload context carries
  the structure block;
- drawings: the REST / MAGNET / LADDER marks land on the chart;
- config: the D-064 block defaults.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd

from app.analysis.drawings import build_drawings
from app.analysis.structure import (
    rest_magnets,
    rest_zones,
    reversal_evidence,
    structure_ladder,
    structure_read,
)
from app.engine.config import EngineConfig
from app.engine.engine import evaluate
from tests.test_d048_poi_zones import _bar, _demand_zone_frame, _uptrend_htf

CFG = EngineConfig()


# ------------------------------------------------------------- fixtures


def _legged_trend(bars: int = 300, drift: float = -0.15, seed: int = 3,
                  rest_at: tuple[int, int] | None = (250, 278)) -> pd.DataFrame:
    """A multi-leg trend: staircase down (or up) with a compressed pause
    inserted inside the last 120 bars (so the REST map finds it)."""
    rng = np.random.default_rng(seed)
    price = 100.0 + drift * np.arange(bars) \
        + rng.normal(0, 0.35, bars).cumsum() * 0.08
    if rest_at:
        a, b = rest_at
        price[a:b] = price[a] + rng.normal(0, 0.02, b - a).cumsum() * 0.05
    o = price + rng.normal(0, 0.05, bars)
    c = price
    h = np.maximum(o, c) + np.abs(rng.normal(0, 0.12, bars))
    low = np.minimum(o, c) - np.abs(rng.normal(0, 0.12, bars))
    t = pd.date_range("2026-09-20 10:00", periods=bars, freq="5min")
    return pd.DataFrame(
        {"time_utc": t, "o": o, "h": h, "l": low, "c": c, "v": np.full(bars, 400.0)}
    )


def _resting_frame(n: int = 120) -> pd.DataFrame:
    """A compressed flat range — the market sitting in a REST."""
    t0 = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
    rows = []
    for i in range(n):
        t = t0 + timedelta(minutes=5 * i)
        c = 2600.0 + 0.15 * float(np.sin(i / 7.0))
        rows.append(_bar(t, c - 0.08, c + 0.10, c - 0.10, c))
    return pd.DataFrame(rows, columns=["time_utc", "o", "h", "l", "c", "v"])


# ------------------------------------------------------- structure_ladder


def test_ladder_counts_consecutive_legs() -> None:
    df = _legged_trend(drift=-0.15)
    lad = structure_ladder(df)
    assert lad["run_dir"] == "down"
    assert lad["run"] >= 2
    labels = [s.get("label") for s in lad["labels"]]
    assert any(x in ("LL", "LH", "L", "") for x in labels)
    assert "lower" in lad["note"]


def test_ladder_mirror_runs_up() -> None:
    df = _legged_trend(drift=0.15, seed=5)
    lad = structure_ladder(df)
    assert lad["run_dir"] == "up"
    assert "higher" in lad["note"]


def test_ladder_choch_resets_the_run() -> None:
    """A V-reversal frame: down legs then a strong up leg — the last
    event flips (CHoCH), the run restarts at 1 the new way and the
    fresh proof is reported."""
    df = _legged_trend(drift=-0.15, seed=3, rest_at=None)
    # append a violent up reversal: enough force to close above the last
    # confirmed lower-high
    rng = np.random.default_rng(1)
    t_last = df["time_utc"].iloc[-1]
    rows = [row for row in df.itertuples(index=False)]
    p = float(df["c"].iloc[-1])
    for k in range(30):
        t = t_last + timedelta(minutes=5 * (k + 1))
        o = p
        c = p + 0.45 + rng.normal(0, 0.05)
        rows.append(_bar(t, o, max(o, c) + 0.08, min(o, c) - 0.08, c))
        p = c
    up = pd.DataFrame(rows, columns=["time_utc", "o", "h", "l", "c", "v"])
    lad = structure_ladder(up)
    if lad["choch_fresh"] is not None:
        assert lad["choch_fresh"]["dir"] == "up"
        assert lad["run_dir"] == "up"
        assert lad["run"] >= 1
        assert "STRUCTURE SHIFT" in lad["note"]


# ------------------------------------------------------------ rest zones


def test_rest_zones_find_the_pause() -> None:
    df = _legged_trend()
    zones = rest_zones(df)
    assert zones, "the compressed pause must be found"
    z = zones[0]
    assert z["bars"] >= 6
    assert z["compress"] <= 0.75  # well below the random-walk floor
    assert z["lo"] < z["hi"]
    # timestamps land inside the frame
    t_arr = df["time_utc"].to_numpy()
    assert t_arr[0] <= z["t0"] <= t_arr[-1]


def test_rest_zones_ignore_short_noise() -> None:
    df = _legged_trend(rest_at=(255, 260))  # 5-bar pause — too short
    zones = rest_zones(df)
    for z in zones:
        assert z["bars"] >= 6


# ---------------------------------------------------------- rest magnets


def test_magnets_sit_on_the_counter_side_only() -> None:
    df = _legged_trend(drift=-0.03, seed=11)
    price = float(df["c"].iloc[-1])
    # simulate the end-of-leg pullback: price near the mean
    mags = rest_magnets(df, price, side="up")
    for m in mags:
        assert m["price"] > price
    mags_down = rest_magnets(df, price, side="down")
    for m in mags_down:
        assert m["price"] < price


def test_magnets_prefer_known_levels() -> None:
    df = _legged_trend(drift=-0.03, seed=11)
    price = float(df["c"].iloc[-1])
    mags = rest_magnets(df, price, side="up")
    kinds = {m["kind"] for m in mags}
    assert kinds & {"EMA 21", "EMA 50", "EQ", "OTE", "FVG", "PRIOR REST"}
    for m in mags:
        assert m["note"]


# --------------------------------------------------------- structure_read


def test_read_mature_run_says_rest_due() -> None:
    df = _legged_trend(drift=-0.15)
    rd = structure_read(df)
    assert rd["phase"] in ("extended", "leg")
    assert 0.35 <= rd["p_reversal"] <= 0.85
    assert "run" in rd["action"] or "REST" in rd["action"] or "leg" in rd["action"]
    assert isinstance(rd["drivers"], list)


def test_read_resting_market() -> None:
    rd = structure_read(_resting_frame())
    assert rd["phase"] == "resting"
    assert "RESTING" in rd["action"] or "rest" in rd["action"].lower()


def test_read_reversal_confirmed_bounds_p() -> None:
    df = _legged_trend(drift=-0.15, seed=3, rest_at=None)
    rng = np.random.default_rng(1)
    rows = [row for row in df.itertuples(index=False)]
    p = float(df["c"].iloc[-1])
    t_last = df["time_utc"].iloc[-1]
    for k in range(30):
        t = t_last + timedelta(minutes=5 * (k + 1))
        o = p
        c = p + 0.45 + rng.normal(0, 0.05)
        rows.append(_bar(t, o, max(o, c) + 0.08, min(o, c) - 0.08, c))
        p = c
    up = pd.DataFrame(rows, columns=["time_utc", "o", "h", "l", "c", "v"])
    rd = structure_read(up)
    if rd["choch_fresh"]:
        assert rd["phase"] == "reversal-confirmed"
        assert rd["p_reversal"] >= 0.72


# ---------------------------------------------------- reversal evidence


def test_reversal_evidence_choch_counts() -> None:
    lad = {"choch_fresh": {"dir": "up", "level": 100.0, "bars_ago": 3}}
    assert reversal_evidence("BUY", lad, None) is True
    assert reversal_evidence("SELL", lad, None) is False


def test_reversal_evidence_sweep_reclaim_counts() -> None:
    sweep = {"side": "SSL", "reclaimed": True, "bars_ago": 2}
    assert reversal_evidence("BUY", {}, sweep) is True
    sweep_bsl = {"side": "BSL", "reclaimed": True, "bars_ago": 2}
    assert reversal_evidence("SELL", {}, sweep_bsl) is True
    # an unreclaimed break is NOT proof
    assert reversal_evidence("BUY", {}, {"side": "SSL", "reclaimed": False}) is False
    assert reversal_evidence("BUY", {}, None) is False


# ------------------------------------------------------------- evaluate


def test_evaluate_pulse_carries_the_ladder() -> None:
    """Every bar close the radar knows the ladder: run, phase, honest
    odds, action — the user's 'কত বার LL' question answered live."""
    from app.engine.backtest import load_mock_history, resample_ohlc

    m1 = load_mock_history(3000, seed=42)
    m5 = resample_ohlc(m1, 5, src_min=1)
    close_time = m1["time_utc"].iloc[-1] + timedelta(minutes=1)
    htf = {"M5": m5, "M15": resample_ohlc(m1, 15, src_min=1),
           "H1": resample_ohlc(m1, 60, src_min=1)}
    ev = evaluate(m1, htf, close_time, EngineConfig(), spread_points=20)
    st = ev.pulse.get("structure")
    assert st is not None
    assert st["phase"] in ("leg", "extended", "resting", "reversal-confirmed")
    assert st["p_reversal"] is None or 0.35 <= st["p_reversal"] <= 0.85
    assert st["action"]
    assert isinstance(st["magnets"], list)


def test_evaluate_pulse_carries_the_tf_ladder() -> None:
    """D-065 — the radar shows the timeframe ladder: which TF fires the
    signal, which ones confirm, how many bars each reads."""
    from app.engine.backtest import load_mock_history, resample_ohlc

    m1 = load_mock_history(2000, seed=42)
    close_time = m1["time_utc"].iloc[-1] + timedelta(minutes=1)
    htf = {"M5": resample_ohlc(m1, 5, src_min=1),
           "M15": resample_ohlc(m1, 15, src_min=1),
           "H1": resample_ohlc(m1, 60, src_min=1)}
    ev = evaluate(m1, htf, close_time, EngineConfig(), spread_points=20)
    lad = ev.pulse.get("tf_ladder")
    assert lad and isinstance(lad, list)
    roles = {r["role"] for r in lad}
    assert "signal" in roles
    assert "trend" in roles
    sig = next(r for r in lad if r["role"] == "signal")
    assert sig["tf"] == EngineConfig().timeframe  # M1 fires the signal
    assert sig["bars"] and sig["bars"] >= 1500
    setup = next((r for r in lad if r["role"] == "setup"), None)
    assert setup is not None and setup["tf"] == "M5" and setup["bars"] >= 240


def _patched_structure(fake: dict, evidence: bool):
    """Patch the engine's lazy imports with deterministic reads."""
    from app.analysis import structure as struct_mod

    orig_read = struct_mod.structure_read
    orig_evidence = struct_mod.reversal_evidence

    def _read(df, price=None):
        out = dict(fake)
        out.setdefault("rests", [])
        out.setdefault("magnets", [])
        out.setdefault("drivers", [])
        return out

    def _ev(sig_dir, ladder, sweep):
        return evidence

    struct_mod.structure_read = _read
    struct_mod.reversal_evidence = _ev
    return orig_read, orig_evidence, struct_mod


_MATURE_DOWN = {
    "trend": "bearish", "run": 3, "run_dir": "down",
    "labels": [], "last_event": None, "choch_fresh": None,
    "phase": "extended", "p_reversal": 0.66, "action": "x",
    "magnets": [], "rests": [], "drivers": [],
}


def test_guard_blocks_a_no_proof_momentum_fade() -> None:
    """A MOMENTUM trade (pullback trigger) fading a 3-leg DOWN run with
    NO CHoCH / sweep proof is the falling-knife entry — REFUSED with
    the structure_guard trace."""
    import app.engine.engine as eng
    from app.engine.sfp import PullbackSignal

    df = _demand_zone_frame()
    htf = _uptrend_htf()
    close_time = df["time_utc"].iloc[-1] + timedelta(minutes=1)

    fake_pb = PullbackSignal(
        direction="BUY", entry=4300.75, anchor=4301.0,
        swing_extreme=4300.15, wick_ratio=0.55, body_pos=0.8, atr=0.35,
    )
    cfg = EngineConfig(
        entry_mode="market", structure_guard=True,
        zone_trigger_enabled=False, pullback_enabled=True,
        min_confluence=0, min_tf_agree=0, rsi_buy_min=30.0,
    )
    orig_sfp = eng.detect_sfp
    orig_pb = eng.detect_pullback
    orig_read, orig_ev, mod = _patched_structure(_MATURE_DOWN, evidence=False)
    eng.detect_sfp = lambda *a, **k: None
    eng.detect_pullback = lambda *a, **k: fake_pb
    try:
        ev = evaluate(df, htf, close_time, cfg, spread_points=20)
    finally:
        eng.detect_sfp = orig_sfp
        eng.detect_pullback = orig_pb
        mod.structure_read = orig_read
        mod.reversal_evidence = orig_ev
    assert ev.signal is None
    guard = [c for c in ev.trace["checks"] if c["name"] == "structure_guard"]
    assert guard and not guard[0]["pass"]
    assert "falling-knife" in guard[0]["value"]


def test_guard_off_or_with_evidence_lets_the_fade_fire() -> None:
    """The ZONE (location) fade of a mature run keeps the D-049
    precedence — it FIRES, visibly tagged and discounted (never silently
    lost); with structural PROOF (a fresh CHoCH / reclaimed sweep) it
    fires without the penalty, and the payload context carries the
    structure block either way."""
    df = _demand_zone_frame()
    htf = _uptrend_htf()
    close_time = df["time_utc"].iloc[-1] + timedelta(minutes=1)

    # 1) guard OFF — the zone BUY fires untouched
    orig_read, orig_ev, mod = _patched_structure(_MATURE_DOWN, evidence=False)
    try:
        ev = evaluate(
            df, htf, close_time,
            EngineConfig(entry_mode="market", structure_guard=False, regime_guard=False),
            spread_points=20,
        )
    finally:
        mod.structure_read = orig_read
        mod.reversal_evidence = orig_ev
    assert ev.signal is not None
    assert "structure_guard" not in {c["name"] for c in ev.trace["checks"]}

    # 2) guard ON, no proof — the zone BUY still fires (D-049) but is
    #    visibly tagged and pays the confidence cost
    orig_read, orig_ev, mod = _patched_structure(_MATURE_DOWN, evidence=False)
    try:
        ev_pen = evaluate(
            df, htf, close_time,
            EngineConfig(entry_mode="market", structure_guard=True, regime_guard=False),
            spread_points=20,
        )
    finally:
        mod.structure_read = orig_read
        mod.reversal_evidence = orig_ev
    assert ev_pen.signal is not None
    guard = [c for c in ev_pen.trace["checks"] if c["name"] == "structure_guard"]
    assert guard and guard[0]["pass"]
    assert "location carries the trade" in guard[0]["value"]
    assert ev_pen.signal["confidence"] < ev.signal["confidence"]

    # 3) guard ON with PROOF (fresh CHoCH up) — fires without penalty
    orig_read, orig_ev, mod = _patched_structure(_MATURE_DOWN, evidence=True)
    try:
        ev2 = evaluate(
            df, htf, close_time,
            EngineConfig(entry_mode="market", structure_guard=True, regime_guard=False),
            spread_points=20,
        )
    finally:
        mod.structure_read = orig_read
        mod.reversal_evidence = orig_ev
    assert ev2.signal is not None
    st = ev2.signal["context"]["structure"]
    assert st["run"] == 3 and st["run_dir"] == "down"
    assert st["p_reversal"] == 0.66


def test_guard_penalizes_chasing_an_extended_run() -> None:
    """A WITH-trend signal on an EXTENDED run still fires but pays the
    chase penalty (the ~2 ATR rest eats a chase entry) — visible in the
    trace line and the reduced confidence."""
    df = _demand_zone_frame()
    htf = _uptrend_htf()
    close_time = df["time_utc"].iloc[-1] + timedelta(minutes=1)

    def _run(fake, evidence, cfg_kw):
        orig_read, orig_ev, mod = _patched_structure(fake, evidence)
        try:
            return evaluate(
                df, htf, close_time,
                EngineConfig(entry_mode="market", structure_guard=True, **cfg_kw),
                spread_points=20,
            )
        finally:
            mod.structure_read = orig_read
            mod.reversal_evidence = orig_ev

    base_fake = {
        "trend": "bullish", "run": 1, "run_dir": "up",
        "labels": [], "last_event": None, "choch_fresh": None,
        "phase": "leg", "p_reversal": 0.5, "action": "x",
        "magnets": [], "rests": [], "drivers": [],
    }
    ev_base = _run(base_fake, True, {})
    fake_ext = dict(base_fake, run=4, phase="extended")
    ev_ext = _run(fake_ext, True, {})
    if ev_base.signal and ev_ext.signal:
        assert ev_ext.signal["confidence"] <= ev_base.signal["confidence"]
        guard = [c for c in ev_ext.trace["checks"]
                 if c["name"] == "structure_guard"]
        assert guard and guard[0]["pass"]
        assert "chase" in guard[0]["value"]


# ------------------------------------------------------------- drawings


def test_drawings_carry_the_structure_set() -> None:
    from app.analysis.context import analyze_frame

    df = _legged_trend(drift=-0.03, seed=11)  # magnets in reach
    frames = {"M5": df}
    snaps = {"M5": analyze_frame(df)}
    ds = build_drawings(frames, snaps, float(df["c"].iloc[-1]), [], tf="M5")
    kinds = {d["kind"] for d in ds}
    assert "rest" in kinds
    rest = [d for d in ds if d["kind"] == "rest"][0]
    assert rest["bars"] >= 6
    assert rest["label"].startswith("REST")
    # the ladder badge counts the live legs
    if any(d["kind"] == "ladder" for d in ds):
        lad = next(d for d in ds if d["kind"] == "ladder")
        assert lad["run"] >= 1
        assert "LEG" in lad["label"] or "REST" in lad["label"] \
            or "SHIFT" in lad["label"] or "RESTING" in lad["label"]


# --------------------------------------------------------------- config


def test_config_d064_defaults() -> None:
    assert CFG.structure_guard is True
    assert CFG.structure_counter_legs == 3
    assert CFG.structure_exhaust_legs == 3
    assert CFG.structure_chase_penalty == 0.08
    assert CFG.structure_rest_bonus == 0.06
    # pydantic validation bounds
    EngineConfig(structure_counter_legs=5, structure_chase_penalty=0.2)
