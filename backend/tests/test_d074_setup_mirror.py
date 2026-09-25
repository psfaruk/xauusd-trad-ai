"""D-074 tests — THE SIGNAL IS THE DRAWING.

User directives (Bengali, verbatim):
- "নতুন এন্ট্রি যে সিগন্যাল টি আসে, সেটি চার্ট এর উপরে ড্রয়িং করে না।
  পুরাতন একটি এন্ট্রি সেটাপ দেখায়, মার্কেট প্রাইস ওই পর্যন্ত যায় নি,
  যাবেও না।" — the backend `_setup` box must mirror the NEWEST LIVE
  signal (pending/active) regardless of its direction; dead signals and
  direction-mismatches no longer leave the stale forming box in charge.
- "Fibonacci set-up লজিক এ আমার মনে হচ্ছে এটি সমস্যা আছে" — the fib
  retracement must anchor the IMPULSE leg (forward in time), never the
  pullback leg read backwards.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from app.analysis.drawings import _fib, _setup
from app.analysis.setup_geometry import setup_snapshot
from app.mt5.base import market_key

from .test_confluence import _mk
from .test_d057_drawing_true import _m5_with_drawn_zones, _quiet_m1

NOW = datetime(2026, 9, 22, 14, 30, tzinfo=UTC)


# ------------------------------------------- the signal IDENTITY symbol


@pytest.mark.asyncio
async def test_signal_identity_uses_the_platform_symbol():
    """D-074 — the engine runs on the broker's concrete symbol
    ("XAUUSDm") but the persisted signal + the WS broadcast speak the
    platform market name ("XAUUSD"). The chart, the analysis snapshot
    and the signals panel are platform-keyed — a suffixed row matched
    NOTHING there and the newest entry signal never drew on the chart
    (user: "নতুন এন্ট্রি যে সিগন্যাল টি আসে, সেটি চার্ট এর উপরে ড্রয়িং
    করে না")."""
    from app.engine.config import EngineConfig
    from app.engine.engine import SignalEngine
    from app.engine.repo import SignalRepo
    from app.engine.tracker import SignalTracker
    from app.mt5.mock_source import SweepScenario
    from tests.conftest import make_mock

    from .test_engine_e2e import EventHub, _trend_direction, closed_bar_of

    src = make_mock(seed=7)
    await src.connect({"server": "s", "login": "1", "password": "x"})
    broker_symbol = src.SYMBOL  # "XAUUSDm"
    assert market_key(broker_symbol) == "XAUUSD"
    src.advance_minutes(10 * 60)

    hub = EventHub()
    repo = SignalRepo(None)
    engine = SignalEngine(
        EngineConfig(smc_enabled=False, timeframe="M1", entry_mode="market",
                     confirm_tfs=["M5", "M15"]),
        hub, repo,
    )
    tracker = SignalTracker()

    fired = None
    for _attempt in range(30):
        direction = await _trend_direction(src, broker_symbol)
        if direction is None:
            src.advance_minutes(45)
            engine.invalidate_frames()
            continue
        scenario = src.inject_sweep(SweepScenario(direction=direction, tf="M1"))
        bucket_end = scenario.bucket_time + timedelta(minutes=1)
        now = src._vnow()
        src.advance_minutes(max(0.0, (bucket_end - now).total_seconds() / 60))
        m1 = await src.get_rates(broker_symbol, "M1", 200)
        await engine.on_bar_close(
            "M1", closed_bar_of(m1), src, tracker, broker_symbol
        )
        if await repo.list():
            fired = (await repo.list())[0]
            break
        engine.invalidate_frames()

    assert fired is not None, "fixture must fire at least one signal"
    assert fired["symbol"] == "XAUUSD"  # the platform name, NOT XAUUSDm
    # the WS signal event carries the same platform identity
    ev = next(p for t, p in hub.events if t == "signal")
    assert ev["symbol"] == "XAUUSD"

@pytest.mark.asyncio
async def test_repo_read_heals_legacy_broker_spellings():
    """Rows persisted BEFORE the normalization (broker spelling) heal on
    read — the panel and the chart see the platform name."""
    from app.engine.repo import SignalRepo

    repo = SignalRepo(None)
    bar_time = datetime(2026, 9, 22, 14, 0, tzinfo=UTC)
    await repo.insert(
        {
            "bar_time": bar_time,
            "direction": "BUY",
            "entry": 2700.0,
            "sl": 2699.0,
            "tp": 2702.0,
            "confidence": 0.7,
            "trace": {},
        },
        "XAUUSDm",
        "M1",
    )
    rows = await repo.list()
    assert len(rows) == 1
    assert rows[0]["symbol"] == "XAUUSD"



def _snaps_fixture() -> dict:
    m5 = _m5_with_drawn_zones()
    return {
        "M1": {"ok": True, "atr": 0.3, "whales": {},
               "liquidity": {"sweeps": []}},
        "M5": setup_snapshot(m5, "M5"),
        "M15": {"ok": True, "structure": {"trend": "bullish"},
                "premium_discount": {"state": "discount"}},
    }, {"M1": _quiet_m1(), "M5": m5}


def test_box_mirrors_newest_live_signal_any_direction():
    """A fresh SELL signal while the box bias says BUY: the box becomes
    the SELL trade (its own levels) — the old code kept drawing the BUY
    forming box at a level the market never reached."""
    snaps, frames = _snaps_fixture()
    price = 4302.4
    recent = [{
        "direction": "SELL",
        "status": "pending",
        "entry": 4304.2,
        "sl": 4305.6,
        "tp": 4301.1,
        "rr": 2.24,
        "ts": (NOW - timedelta(minutes=4)).isoformat(),
    }]
    box = _setup(frames, snaps, {"bias": "bullish"}, price, NOW, recent)
    assert box is not None
    assert box["dir"] == "SELL"            # THE signal's direction
    assert box["status"] == "pending"      # limit order waiting
    assert box["entry"] == 4304.2          # the order's exact levels
    assert box["sl"] == 4305.6
    assert box["tp"] == 4301.1
    assert "limit waiting" in box["note"]


def test_dead_signal_never_mirrors():
    """A signal that already hit SL/TP (won/lost) must NOT pin the box —
    that was the stale 'ENTRY TAKEN' ink."""
    snaps, frames = _snaps_fixture()
    price = 4302.4
    recent = [{
        "direction": "BUY",
        "status": "won",
        "entry": 4301.9,
        "sl": 4300.5,
        "tp": 4303.5,
        "ts": (NOW - timedelta(minutes=3)).isoformat(),
    }]
    box = _setup(frames, snaps, {"bias": "bullish"}, price, NOW, recent)
    assert box is not None
    assert box["status"] == "forming"


def test_newest_live_wins_over_older_live_and_dead():
    """recent_signals arrives newest-first: the FIRST live signal is the
    one drawn — a newer dead order must be skipped, not chosen."""
    snaps, frames = _snaps_fixture()
    price = 4302.4
    recent = [
        {  # newest — already closed: skipped
            "direction": "SELL",
            "status": "lost",
            "entry": 4306.0,
            "sl": 4307.0,
            "tp": 4304.0,
            "ts": (NOW - timedelta(minutes=1)).isoformat(),
        },
        {  # the newest LIVE order: this one mirrors
            "direction": "BUY",
            "status": "active",
            "entry": 4301.9,
            "sl": 4300.5,
            "tp": 4303.5,
            "rr": 1.75,
            "ts": (NOW - timedelta(minutes=2)).isoformat(),
        },
        {  # older live: superseded by the newer one
            "direction": "BUY",
            "status": "pending",
            "entry": 4300.0,
            "sl": 4299.0,
            "tp": 4302.0,
            "ts": (NOW - timedelta(minutes=10)).isoformat(),
        },
    ]
    box = _setup(frames, snaps, {"bias": "bullish"}, price, NOW, recent)
    assert box is not None
    assert box["status"] == "triggered"
    assert box["entry"] == 4301.9  # the newest LIVE, not the dead 4306.0


def test_no_live_signal_leaves_forming_box():
    """No live signals -> the honest forming analysis box, unchanged."""
    snaps, frames = _snaps_fixture()
    price = 4302.4
    box = _setup(frames, snaps, {"bias": "bullish"}, price, NOW, [])
    assert box is not None
    assert box["status"] == "forming"
    assert box["dir"] in ("BUY", "SELL")


# ------------------------------------------------------------- fib anchor


def _impulse_then_pullback(quiet: int = 30, bars_up: int = 12,
                           pullback: int = 4, bounce: int = 2) -> pd.DataFrame:
    """A quiet wiggle, then a clean up-impulse, then a CONFIRMED pullback
    low AFTER the high — the exact tape where the old fib anchored the
    leg backwards (a = the pullback low, b = the older high)."""
    t0 = pd.Timestamp("2025-01-06 10:00", tz="UTC")
    rows = []
    price = 100.0
    for i in range(quiet):  # fractal-rich flat base around 100
        c = 100.4 if i % 2 else 100.0
        o = price if i % 2 else 100.4
        o, c = (price, c) if i % 2 else (c, price)
        rows.append([t0 + pd.Timedelta(minutes=len(rows)), o, max(o, c) + 0.2,
                     min(o, c) - 0.2, c, 50])
        price = c
    for _ in range(bars_up):
        o, c = price, price + 1.0
        rows.append([t0 + pd.Timedelta(minutes=len(rows)), o, c + 0.2,
                     o - 0.2, c, 50])
        price = c
    for _ in range(pullback):
        o, c = price, price - 0.8
        rows.append([t0 + pd.Timedelta(minutes=len(rows)), o, o + 0.2,
                     c - 0.2, c, 50])
        price = c
    for _ in range(bounce):  # confirms the pullback low as a fractal
        o, c = price, price + 0.3
        rows.append([t0 + pd.Timedelta(minutes=len(rows)), o, o + 0.25,
                     c - 0.25, c, 50])
        price = c
    return _mk(rows)


def test_fib_anchors_the_impulse_leg_forward_in_time():
    """The leg must run t0 -> t1 (impulse start -> impulse high); with a
    pullback low printing AFTER the high, the old code produced a
    backwards leg (t0 > t1) and measured the pullback as the impulse."""
    df = _impulse_then_pullback()
    f = _fib(df, None)
    assert f is not None
    assert f["dir"] == "up"
    t0 = pd.Timestamp(f["t0"])
    t1 = pd.Timestamp(f["t1"])
    assert t0 < t1, "the fib leg must run forward in time"
    # the anchor high is the impulse's highest high, not the last swing
    assert f["p1"] >= df["h"].max() - 0.25
    # levels: classic retracement of the impulse (0.5 mid checked)
    lo, hi = f["p0"], f["p1"]
    mid = next(lv for lv in f["levels"] if lv["ratio"] == 0.5)
    assert abs(mid["price"] - (hi - 0.5 * (hi - lo))) < 0.05
    ratios = [lv["ratio"] for lv in f["levels"]]
    assert ratios == [0.236, 0.382, 0.5, 0.618, 0.786]


def test_fib_clean_impulse_without_internal_swings_still_draws():
    """A monotonic impulse has no internal fractal lows — the raw-bar
    fallback must still anchor a forward leg (never return None)."""
    df = _impulse_then_pullback(pullback=0, bounce=0)
    f = _fib(df, None)
    assert f is not None
    assert pd.Timestamp(f["t0"]) < pd.Timestamp(f["t1"])
    assert f["p1"] > f["p0"]


def test_fib_down_leg_anchors_forward():
    """The SELL mirror: impulse down, pullback up after the low."""
    df = _impulse_then_pullback()
    # invert the tape: a down impulse with an upward pullback after
    df_inv = df.copy()
    mid = (df["h"].max() + df["l"].min()) / 2
    for col in ("o", "h", "l", "c"):
        df_inv[col] = 2 * mid - df[col]
    df_inv["h"] = df_inv[["o", "c"]].max(axis=1) + 0.2
    df_inv["l"] = df_inv[["o", "c"]].min(axis=1) - 0.2
    f = _fib(df_inv, None)
    assert f is not None
    assert f["dir"] == "down"
    assert pd.Timestamp(f["t0"]) < pd.Timestamp(f["t1"])
    assert f["p1"] < f["p0"]
