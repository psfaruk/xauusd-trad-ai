"""D-057 tests — the chart drawing IS the trade contract.

User directive (Bengali): "চার্ট এর মধ্যে যে ড্রয়িং হচ্ছে, আমি চাই সিগন্যাল
গুলো এই ড্রয়িং ফলো করে আসবে ... SL TP ENTRY সব কিছু এই চার্ট ফলো করে হবে।"

Covers:
- setup_geometry: BUY/SELL contracts (near-edge entry, SL beyond the zone,
  TP at the nearest DRAWN target), the tradeability gates (wide zone /
  far target / no zone), the mid anchor option;
- _drawing_geometry: the engine books the drawn entry as a limit at the
  drawn zone edge with the drawn SL/TP;
- evaluate(): the geometry field ("drawing" | "legacy") + the fallback;
- detect_zone_retest: counter-trend bounces need sweep+reclaim proof;
- simulate_risk: the kill switch re-arms on the next UTC day;
- drawings._setup: the box mirrors the FIRED signal's actual levels.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd

from app.analysis.poi import poi_zones
from app.analysis.setup_geometry import (
    setup_geometry,
    setup_snapshot,
)
from app.engine.backtest import (
    BacktestResult,
    BacktestSignal,
    simulate_risk,
)
from app.engine.config import EngineConfig
from app.engine.engine import _drawing_geometry, evaluate
from app.engine.zones import detect_zone_retest
from tests.test_d048_poi_zones import _bar, _demand_zone_frame

CFG = EngineConfig()


def _snaps(
    zones: list[dict],
    levels: list[dict],
    atr: float = 1.0,
) -> tuple[dict, dict]:
    s5 = {
        "ok": True, "atr": atr, "zones": zones, "order_blocks": [],
        "fvgs": [],
        "liquidity": {"levels": levels, "sweeps": []},
        "premium_discount": {},
    }
    s15 = {
        "ok": True, "atr": atr, "zones": [], "order_blocks": [],
        "fvgs": [], "liquidity": {"levels": [], "sweeps": []},
        "premium_discount": {"state": "discount"},
        "structure": {"trend": "bullish"},
    }
    return s5, s15


# ------------------------------------------------------- setup_geometry

def test_setup_geometry_buy_contract() -> None:
    """BUY at a demand zone: entry = the zone's NEAR edge (the drawn
    boundary a retest touches first), SL beyond the far edge, TP at the
    nearest drawn liquidity line, RR inside the tradeable band."""
    s5, s15 = _snaps(
        zones=[{"side": "demand", "lo": 100.0, "hi": 101.0, "t": None}],
        levels=[{"kind": "BSL", "price": 103.0}],
    )
    geo = setup_geometry(s5, s15, "BUY", price=101.4)
    assert geo is not None
    assert geo["entry"] == 101.0            # near edge (hi for a demand zone)
    assert geo["sl"] == 99.65               # lo - 0.35 ATR pad
    assert geo["tp"] == 102.85              # BSL 103.0 parked 0.15 in front
    assert 1.2 <= geo["rr"] <= 1.8          # short-time tradeable band
    assert geo["tp_source"] == "BSL liquidity line"
    assert geo["atr5"] == 1.0


def test_setup_geometry_sell_contract() -> None:
    s5, s15 = _snaps(
        zones=[{"side": "supply", "lo": 200.0, "hi": 201.0, "t": None}],
        levels=[{"kind": "SSL", "price": 197.5}],
    )
    geo = setup_geometry(s5, s15, "SELL", price=199.6)
    assert geo is not None
    assert geo["entry"] == 200.0            # near edge (lo for a supply zone)
    assert geo["sl"] == 201.35              # hi + 0.35 ATR pad
    assert geo["tp"] == 197.65              # SSL 197.5 parked 0.15 in front
    assert 1.2 <= geo["rr"] <= 1.8
    assert geo["tp_source"] == "SSL liquidity line"


def test_setup_geometry_uses_opposing_zone_edge_as_target() -> None:
    """The TP may be the opposing zone's near edge — also a drawn mark."""
    s5, s15 = _snaps(
        zones=[
            {"side": "demand", "lo": 100.0, "hi": 101.0, "t": None},
            {"side": "supply", "lo": 102.4, "hi": 103.1, "t": None},
        ],
        levels=[],
    )
    geo = setup_geometry(s5, s15, "BUY", price=101.4)
    assert geo is not None
    assert geo["tp"] == 102.25              # 102.4 - 0.15 pad
    assert geo["tp_source"] == "supply zone edge"


def test_setup_geometry_mid_anchor_option() -> None:
    """entry_anchor='mid' keeps the classic D-052 box midpoint."""
    s5, s15 = _snaps(
        zones=[{"side": "demand", "lo": 100.0, "hi": 101.0, "t": None}],
        levels=[{"kind": "BSL", "price": 102.0}],
    )
    geo = setup_geometry(s5, s15, "BUY", price=100.8, entry_anchor="mid")
    assert geo is not None
    assert geo["entry"] == 100.5


def test_setup_geometry_none_without_supporting_zone() -> None:
    s5, s15 = _snaps(zones=[{"side": "supply", "lo": 100.0, "hi": 101.0,
                             "t": None}], levels=[])
    assert setup_geometry(s5, s15, "BUY", price=101.4) is None


def test_setup_geometry_rejects_swing_scale_risk() -> None:
    """A zone whose structural stop exceeds max_risk_atr M5-ATRs is a
    swing trade — the short-time engine declines it."""
    s5, s15 = _snaps(
        zones=[{"side": "demand", "lo": 100.0, "hi": 102.5, "t": None}],
        levels=[{"kind": "BSL", "price": 105.0}],
    )
    assert setup_geometry(s5, s15, "BUY", price=102.9) is None


def test_setup_geometry_rejects_far_target() -> None:
    """Nearest drawn target beyond max_rr is swing-scale — no contract."""
    s5, s15 = _snaps(
        zones=[{"side": "demand", "lo": 100.0, "hi": 101.0, "t": None}],
        levels=[{"kind": "BSL", "price": 106.0}],
    )
    assert setup_geometry(s5, s15, "BUY", price=101.4) is None


def test_setup_geometry_needs_a_target() -> None:
    """No drawn target in the trade's direction — no contract (the old
    box invented a 1.5R TP from thin air)."""
    s5, s15 = _snaps(
        zones=[{"side": "demand", "lo": 100.0, "hi": 101.0, "t": None}],
        levels=[],
    )
    assert setup_geometry(s5, s15, "BUY", price=101.4) is None


# ------------------------------------------------- _drawing_geometry (engine)

def _m5_with_drawn_zones() -> pd.DataFrame:
    """M5 frame that mints a demand zone [4301.0, 4301.6] (base bar +
    up impulse) and a supply zone [4303.1, 4303.9] (base bar + down
    impulse) with the market drifting between them (~4302.4); the quiet
    tape's low cluster sits FAR below the demand zone so no SSL pool
    extends the drawn SL past the cap."""
    t0 = datetime(2026, 9, 22, 9, 0, tzinfo=UTC)
    rows = []
    for i in range(20):  # quiet tape ~4299, unique lows (no SSL pools)
        t = t0 + timedelta(minutes=5 * i)
        c = 4299.0 + 0.1 * (i % 3)
        rows.append(_bar(t, c - 0.1, c + 0.5, c - 0.6 - 0.01 * i, c + 0.1))
    # demand base (down candle) at 20 — zone [4301.0, 4301.6]
    t = t0 + timedelta(minutes=5 * 20)
    rows.append(_bar(t, 4301.3, 4301.6, 4301.0, 4301.1))
    # up impulse (body ~2.4 >= 1.5 ATR)
    t = t0 + timedelta(minutes=5 * 21)
    rows.append(_bar(t, 4301.1, 4303.7, 4301.15, 4303.5))
    # supply base (up candle) at 22 — zone [4303.1, 4303.9]
    t = t0 + timedelta(minutes=5 * 22)
    rows.append(_bar(t, 4303.2, 4303.9, 4303.1, 4303.6))
    # down impulse
    t = t0 + timedelta(minutes=5 * 23)
    rows.append(_bar(t, 4303.6, 4303.8, 4301.3, 4301.4))
    # drift inside the range: fills the bullish FVG, keeps both zones
    # intact (closes stay inside 4301.0..4303.9), unique lows/highs
    legs = [
        (4301.8, 4302.4, 4301.5, 4302.0),
        (4302.0, 4302.5, 4301.9, 4302.3),
        (4302.3, 4302.7, 4302.2, 4302.5),
        (4302.5, 4302.8, 4302.4, 4302.4),
        (4302.4, 4302.7, 4302.3, 4302.4),
        (4302.4, 4302.6, 4302.35, 4302.4),
    ]
    for i, (o, h, low, c) in enumerate(legs):
        t = t0 + timedelta(minutes=5 * (24 + i))
        rows.append(_bar(t, o, h, low, c))
    return pd.DataFrame(rows, columns=["time_utc", "o", "h", "l", "c", "v"])


def _quiet_m1() -> pd.DataFrame:
    t0 = datetime(2026, 9, 22, 13, 0, tzinfo=UTC)
    rows = [
        _bar(t0 + timedelta(minutes=i), 4301.5, 4301.7, 4301.4, 4301.6)
        for i in range(30)
    ]
    return pd.DataFrame(rows, columns=["time_utc", "o", "h", "l", "c", "v"])


def test_engine_books_the_drawn_trade() -> None:
    """_drawing_geometry: the order IS the chart's contract — a BUY LIMIT
    at the drawn demand zone's near edge, SL beyond the zone, TP at the
    drawn supply zone edge. D-073: the market sits INSIDE the chart-box
    reach (0.75 M5-ATRs) and entry_min_usd is loosened so the drawn
    level clears the noise margin — the default 1.0 USD floor would push
    this near-zone case onto the market-entry branch instead."""
    htf = {"M5": _m5_with_drawn_zones(), "M15": _quiet_m1()}
    cfg = EngineConfig(entry_min_usd=0.3)
    geo = _drawing_geometry(
        _quiet_m1(), htf, "BUY", market=4302.2, cfg=cfg, spread_price=0.20,
    )
    assert geo is not None
    snap = setup_snapshot(htf["M5"], "M5")
    demands = [z for z in snap["zones"] if z["side"] == "demand"]
    assert demands, "fixture must mint a demand zone"
    z = demands[0]
    assert geo["entry"] == round(z["hi"], 2)      # the drawn near edge
    assert geo["sl"] < z["lo"]                    # beyond the drawn zone
    assert geo["tp"] > geo["entry"]               # at the drawn target
    assert geo["entry_type"] == "limit"           # beyond the noise margin
    assert 1.2 <= geo["rr"] <= CFG.setup_max_rr
    assert "chart-true" in geo["entry_note"]
    assert z["lo"] - geo["sl"] <= 0.35 * snap["atr"] + 0.06  # the drawn pad


def test_engine_declines_when_no_zone_is_drawn() -> None:
    """No drawn zone within reach -> None -> the caller's legacy chain."""
    htf = {"M5": _quiet_m1(), "M15": _quiet_m1()}
    geo = _drawing_geometry(
        _quiet_m1(), htf, "BUY", market=4301.9, cfg=CFG, spread_price=0.20,
    )
    assert geo is None


def test_evaluate_reports_geometry_mode() -> None:
    """The signal payload carries which geometry placed the trade —
    'legacy' here because the uptrend HTF frames draw no zone near the
    market (the D-057 fallback path, fully exercised)."""
    df = _demand_zone_frame()
    from tests.test_d048_poi_zones import _uptrend_htf

    htf = _uptrend_htf()
    close_time = df["time_utc"].iloc[-1] + timedelta(minutes=1)
    cfg = EngineConfig(entry_mode="market", regime_guard=False)  # D-068 isolated
    ev = evaluate(df, htf, close_time, cfg, spread_points=20)
    assert ev.signal is not None
    assert ev.signal["geometry"] == "legacy"
    names = {c["name"] for c in ev.signal["trace"]["checks"]}
    assert "geometry" in names  # the trace tells the fallback story


# ------------------------------------------------- counter-trend sweep gate

def test_counter_trend_bounce_blocked_without_sweep() -> None:
    """D-056 capability: with the gate ON, a plain counter-trend bounce
    is NOT a reversal entry — the bar must wick through the zone and
    close back (sweep + reclaim). Off by default (D-049 directive)."""
    df = _demand_zone_frame()
    zones = poi_zones(df)
    assert zones, "fixture must mint zones"
    gated = detect_zone_retest(
        df, zones, EngineConfig(counter_needs_sweep=True), "SELL",
    )  # counter the bias
    assert gated is None  # no sweep+reclaim -> no counter-trend signal
    default = detect_zone_retest(df, zones, CFG, "SELL")
    if default is not None:
        assert default.counter_trend  # default: the zone signal flows


# --------------------------------------------------- simulate_risk re-arm

def test_kill_switch_rearms_next_day() -> None:
    """The money window re-arms every UTC day (the live behaviour) — the
    old replay stayed disarmed forever after one trip."""
    d1 = datetime(2026, 9, 22, 10, 0, tzinfo=UTC)
    d2 = datetime(2026, 9, 23, 10, 0, tzinfo=UTC)
    cfg = EngineConfig(daily_max_loss_pct=2.0, risk_percent=1.0)

    def sig(ts: datetime, result_r: float) -> BacktestSignal:
        return BacktestSignal(
            ts=ts, direction="BUY", entry=100.0, sl=99.0, tp=102.0,
            confidence=0.6, session="London", result_r=result_r,
            status="lost" if result_r < 0 else "won",
        )

    res = BacktestResult(
        cfg=cfg, bars_tested=100, date_from=d1, date_to=d2,
        signals=[
            sig(d1, -3.0),   # -300 USD on 10k = 3% -> kill switch trips
            sig(d1 + timedelta(hours=2), -1.0),  # same day: skipped
            sig(d2, +1.0),   # NEXT DAY: re-armed, taken
        ],
    )
    risk = simulate_risk(res, start_equity=10_000.0)
    events = [p.get("event") for p in risk.per_trade]
    assert "KILL_SWITCH" in events
    assert any(e and "RE_ARM" in e for e in events)
    assert risk.kill_switch_events == 1
    # day-2 signal was TAKEN, not skipped
    assert risk.signals_skipped_after_kill == 1
    assert risk.trades_taken == 2  # the first loss + the re-armed winner


# ------------------------------------------- drawings box mirrors the order

def test_setup_box_mirrors_fired_signal_levels() -> None:
    """Once the signal fires, the chart's setup box shows the ACTUAL
    order levels (entry/SL/TP) — the box IS the trade contract."""
    from app.analysis.drawings import _setup

    m5 = _m5_with_drawn_zones()
    s5 = setup_snapshot(m5, "M5")
    m1 = _quiet_m1()
    snaps = {
        "M1": {"ok": True, "atr": 0.3, "whales": {},
               "liquidity": {"sweeps": []}},
        "M5": s5,
        "M15": {"ok": True, "structure": {"trend": "bullish"},
                "premium_discount": {"state": "discount"}},
    }
    frames = {"M1": m1, "M5": m5}
    price = 4302.4  # inside the demand zone's near window
    now = datetime(2026, 9, 22, 14, 30, tzinfo=UTC)
    recent = [{
        "direction": "BUY",
        "entry": 4301.9,
        "sl": 4300.5,
        "tp": 4303.5,
        "rr": 1.75,
        "ts": (now - timedelta(minutes=2)).isoformat(),
    }]
    box = _setup(frames, snaps, {"bias": "bullish"}, price, now, recent)
    assert box is not None, "fixture must draw a setup box"
    assert box["status"] == "triggered"
    assert box["entry"] == 4301.9          # the ORDER's entry, mirrored
    assert box["sl"] == 4300.5             # the ORDER's SL, mirrored
    assert box["tp"] == 4303.5             # the ORDER's TP, mirrored
    assert "order live" in box["note"]
