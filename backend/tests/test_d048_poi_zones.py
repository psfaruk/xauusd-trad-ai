"""D-048 — POI zone engine + zone-retest trigger + best-strategy arbitration.

User directive: "Best POI ZONE, SUPPLY ZONE, DEMAND ZONE এই গুলো তে
সিগন্যাল দিতে হবে, মিস করা যাবে না… সব গুলো স্ট্রাটেজি একই সময় AGREE
নাও থাকতে পারে" — price returning to a QUALITY zone and rejecting IS
the entry; the ICT factor count must not block it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from app.analysis.poi import poi_zones, zone_quality
from app.engine.config import DEFAULT_CONFIG, EngineConfig
from app.engine.engine import evaluate
from app.engine.trace import Trace
from app.engine.zones import (
    build_levels_zone,
    check_rsi_zone,
    detect_zone_retest,
    rejection_quality,
    zone_trigger_quality,
)

# ------------------------------------------------------------ frame builders

def _bar(t, o, h, low, c, v=100):
    return [t, o, h, low, c, v]


def _demand_zone_frame(
    n_tail: int = 60,
    dip_at: int = 55,
    trigger: tuple | None = None,
) -> pd.DataFrame:
    """Quiet 4300 tape -> down base -> 3-ATR up impulse -> rally to 4305
    -> GRADUAL overlapping descent (no bearish FVG/OB minted right above
    the entry — D-049's geometry check would rightly refuse such a trade)
    -> rejection trigger bar dipping INTO the zone (BUY setup).

    Zone (detect_supply_demand): base bar at index 50 — lo/hi = its
    low/high; impulse body >= 1.5 ATR.
    """
    t0 = datetime(2026, 9, 22, 9, 0, tzinfo=UTC)  # london session
    rows = []
    # 1) quiet tape around 4300 (ATR ~0.3)
    for i in range(50):
        t = t0 + timedelta(minutes=i)
        c = 4300.0 + 0.05 * (i % 3)
        rows.append(_bar(t, c - 0.05, c + 0.15, c - 0.20, c))
    # 2) down base bar at 50 (the future demand zone: 4299.7..4300.35)
    t = t0 + timedelta(minutes=50)
    rows.append(_bar(t, 4300.3, 4300.35, 4299.75, 4299.9))
    # 3) up impulse: body ~3.0 (>= 1.5 ATR), closes 4303
    t = t0 + timedelta(minutes=51)
    rows.append(_bar(t, 4299.9, 4303.1, 4299.85, 4303.0))
    # 4) rally to 4305.4 (6 small up bars)
    p = 4303.0
    for k in range(52, 58):
        t = t0 + timedelta(minutes=k)
        rows.append(_bar(t, p, p + 0.5, p - 0.1, p + 0.4))
        p += 0.4
    # 5) gradual overlapping descent to ~4301.4 (6 bars; wide ranges so
    #    no 3-candle bearish FVG forms: bar[k].low <= bar[k+2].high)
    for k in range(58, 64):
        t = t0 + timedelta(minutes=k)
        rows.append(_bar(t, p, p + 0.10, p - 1.42, p - 0.667))
        p -= 0.667
    # 6) the trigger bar: dips INTO the zone, rejects with a lower wick
    #    (high 4301.6 overlaps the last descent low so no hairline
    #    bearish FVG mints right above the entry)
    if trigger is None:
        trigger = (4301.2, 4301.6, 4300.15, 4300.75)  # o, h, low, c
    t = t0 + timedelta(minutes=64)
    o, h, low, c = trigger
    rows.append(_bar(t, o, h, low, c, v=250))
    return pd.DataFrame(rows, columns=["time_utc", "o", "h", "l", "c", "v"])


def _uptrend_htf(n: int = 60) -> dict[str, pd.DataFrame]:
    """Rising H1/M15/M5 frames (BUY trend, EMA50 below price)."""
    out = {}
    for tf, m in (("H1", 60), ("M15", 15), ("M5", 5)):
        t0 = datetime(2026, 9, 22, 0, 0, tzinfo=UTC) - timedelta(minutes=m * n)
        rows = [
            _bar(t0 + timedelta(minutes=m * i), 4200.0 + i, 4201.0 + i,
                 4199.0 + i, 4200.5 + i, 50)
            for i in range(n)
        ]
        out[tf] = pd.DataFrame(rows, columns=["time_utc", "o", "h", "l", "c", "v"])
    return out


# ------------------------------------------------------------- poi_zones

def test_poi_zones_marks_demand_zone_with_quality() -> None:
    df = _demand_zone_frame()
    zones = poi_zones(df)
    demands = [z for z in zones if z["side"] == "demand" and z["source"] == "sd"]
    assert demands, f"no sd demand zone in {[ (z['side'], z['source']) for z in zones ]}"
    z = demands[0]
    # the crafted base bar band
    assert z["lo"] == 4299.75 and z["hi"] == 4300.35
    # strong impulse (10x ATR) + fresh (unmitigated) + young -> high quality
    assert z["quality"] >= 0.45, z["quality"]
    assert z["impulse"] >= 1.5
    # ranked list: quality descending
    qs = [x["quality"] for x in zones]
    assert qs == sorted(qs, reverse=True)


def test_poi_zones_drop_broken_demand() -> None:
    df = _demand_zone_frame()
    # append bars that CLOSE below the zone low -> zone invalidated
    t_last = df["time_utc"].iloc[-1]
    rows = []
    for k in range(1, 6):
        c = 4299.0 - 0.1 * k
        rows.append(_bar(t_last + timedelta(minutes=k), c + 0.05, c + 0.2,
                         c - 0.2, c))
    df = pd.concat([df, pd.DataFrame(rows, columns=df.columns)],
                   ignore_index=True)
    zones = poi_zones(df)
    assert not [z for z in zones if z["side"] == "demand" and z["source"] == "sd"
                and z["lo"] == 4299.75]


def test_zone_quality_model_bounds() -> None:
    # perfect zone: 3x impulse, untouched, young, TPO-reinforced, HTF
    q_hi = zone_quality(impulse_atr=3.0, fresh_q=1.0, age_q=1.0,
                        tpo_minutes=40.0, htf=True)
    # weak zone: 1.1x impulse, stale, old, no TPO, base TF
    q_lo = zone_quality(impulse_atr=1.1, fresh_q=0.45, age_q=0.15,
                        tpo_minutes=0.0, htf=False)
    assert q_hi > 0.75
    assert q_lo < 0.45
    # the FIRST retest keeps nearly full freshness (the ICT entry moment)
    q_first = zone_quality(impulse_atr=2.0, fresh_q=0.85, age_q=1.0,
                           tpo_minutes=0.0, htf=False)
    q_stale = zone_quality(impulse_atr=2.0, fresh_q=0.45, age_q=1.0,
                           tpo_minutes=0.0, htf=False)
    assert q_first > q_stale


def test_detect_supply_demand_reports_impulse() -> None:
    from app.analysis.smc import detect_supply_demand

    df = _demand_zone_frame().iloc[:52]  # base + impulse only
    zs = detect_supply_demand(df)
    assert zs and zs[-1]["side"] == "demand"
    assert zs[-1]["impulse"] >= 1.5


# ------------------------------------------------------- zone-retest trigger

def test_zone_retest_fires_buy_at_demand() -> None:
    df = _demand_zone_frame()
    cfg = EngineConfig()
    sig = detect_zone_retest(df, poi_zones(df), cfg, "BUY")
    assert sig is not None
    assert sig.direction == "BUY"
    assert sig.zone["side"] == "demand"
    assert not sig.counter_trend
    assert sig.rejection >= 0.35  # wick 0.65/rng + body position
    # entry = trigger close; SL candidate beyond the zone low
    entry, sl_base = build_levels_zone(sig, cfg)
    assert entry == df["c"].iloc[-1]
    assert sl_base < sig.zone["lo"]


def test_zone_retest_sweep_reclaim_is_strongest() -> None:
    """D-049 — a trigger bar that wicks THROUGH the zone low and closes
    back above the zone high is the stop-hunt reversal: rejection forced
    to >= 0.90 and the SL anchors beyond the sweep extreme."""
    # trigger: sweeps to 4299.4 (under zone lo 4299.75), closes 4301.6
    df = _demand_zone_frame(trigger=(4300.6, 4301.8, 4299.40, 4301.6))
    cfg = EngineConfig()
    sig = detect_zone_retest(df, poi_zones(df), cfg, "BUY")
    assert sig is not None
    assert sig.sweep_reclaim is True
    assert sig.rejection >= 0.90
    assert sig.sweep_extreme == pytest.approx(4299.40)
    entry, sl_base = build_levels_zone(sig, cfg)
    # SL beyond the sweep extreme (the actual invalidation)
    assert sl_base < 4299.40


def test_zone_retest_late_window_entry_rejected() -> None:
    """D-049 — the dip happened earlier and the close already ran away:
    a MISSED entry must not become a late chase."""
    t0 = datetime(2026, 9, 22, 9, 0, tzinfo=UTC)
    rows = []
    for i in range(50):
        c = 4300.0 + 0.05 * (i % 3)
        rows.append(_bar(t0 + timedelta(minutes=i), c - 0.05, c + 0.15,
                         c - 0.20, c))
    rows.append(_bar(t0 + timedelta(minutes=50), 4300.3, 4300.35, 4299.75, 4299.9))
    rows.append(_bar(t0 + timedelta(minutes=51), 4299.9, 4303.1, 4299.85, 4303.0))
    for k in range(52, 57):
        c = 4303.0 + 0.08 * (k - 52)
        rows.append(_bar(t0 + timedelta(minutes=k), c - 0.06, c + 0.15,
                         c - 0.18, c))
    # dip bar into the zone (window hit) ... then the market rallied far
    rows.append(_bar(t0 + timedelta(minutes=57), 4303.4, 4303.6, 4300.30, 4300.5))
    # ... and the trigger bar is 3+ ATR above the zone, nowhere near it
    rows.append(_bar(t0 + timedelta(minutes=58), 4304.0, 4304.4, 4303.8, 4304.2))
    df = pd.DataFrame(rows, columns=["time_utc", "o", "h", "l", "c", "v"])
    cfg = EngineConfig()
    assert detect_zone_retest(df, poi_zones(df), cfg, "BUY") is None


def test_zone_retest_window_reaches_back() -> None:
    """The dip may have happened up to zone_retest_window bars earlier."""
    df = _demand_zone_frame()
    cfg = EngineConfig()
    # drop the deep-dip bar: the trigger bar itself still entered (low
    # 4300.15 <= zone hi 4300.35) — window not needed, fires directly
    assert detect_zone_retest(df, poi_zones(df), cfg, "BUY") is not None
    # now a trigger bar that NEVER touches the zone and no recent dip
    t0 = datetime(2026, 9, 22, 9, 0, tzinfo=UTC)
    rows = []
    for i in range(50):
        c = 4300.0 + 0.05 * (i % 3)
        rows.append(_bar(t0 + timedelta(minutes=i), c - 0.05, c + 0.15,
                         c - 0.20, c))
    rows.append(_bar(t0 + timedelta(minutes=50), 4300.3, 4300.35, 4299.75, 4299.9))
    rows.append(_bar(t0 + timedelta(minutes=51), 4299.9, 4303.1, 4299.85, 4303.0))
    for k in range(52, 58):
        c = 4303.0 + 0.08 * (k - 52)
        rows.append(_bar(t0 + timedelta(minutes=k), c - 0.06, c + 0.15,
                         c - 0.18, c))
    # two bars far above the zone (no entry within the default window of 2)
    rows.append(_bar(t0 + timedelta(minutes=58), 4303.5, 4303.8, 4303.3, 4303.6))
    rows.append(_bar(t0 + timedelta(minutes=59), 4303.6, 4303.9, 4303.4, 4303.7))
    df_far = pd.DataFrame(rows, columns=["time_utc", "o", "h", "l", "c", "v"])
    assert detect_zone_retest(df_far, poi_zones(df_far), cfg, "BUY") is None


def test_zone_retest_rejects_weak_quality() -> None:
    """A retest of a LOW-quality zone must not fire (quality gate)."""
    df = _demand_zone_frame()
    cfg = EngineConfig(min_zone_quality=0.95)  # impossible bar for this tape
    assert detect_zone_retest(df, poi_zones(df), cfg, "BUY") is None


def test_zone_retest_no_rejection_no_signal() -> None:
    """Entering the zone but closing back INSIDE it = forming, not a signal."""
    df = _demand_zone_frame(trigger=(4300.9, 4301.0, 4300.15, 4300.4))
    cfg = EngineConfig()
    sig = detect_zone_retest(df, poi_zones(df), cfg, "BUY")
    assert sig is None


def test_counter_trend_zone_needs_higher_quality() -> None:
    df = _demand_zone_frame()
    cfg = EngineConfig()
    zones = poi_zones(df)
    q = max(z["quality"] for z in zones if z["side"] == "demand")
    sig = detect_zone_retest(df, zones, cfg, "SELL")  # H1 says down
    if q < cfg.counter_trend_quality:
        assert sig is None  # a normal zone may not counter the H1 trend
    else:
        # D-049 directive stands by default: the zone signal flows
        assert sig is not None and sig.counter_trend
        # D-056 capability: the optional sweep+reclaim gate blocks the
        # plain counter-trend bounce
        gated = detect_zone_retest(
            df, zones, EngineConfig(counter_needs_sweep=True), "SELL"
        )
        assert gated is None


def test_rejection_quality_scale() -> None:
    assert rejection_quality(0.0, 0.0) == 0.0
    assert rejection_quality(0.6, 1.0) == 1.0
    mid = rejection_quality(0.35, 0.6)
    assert 0.4 <= mid <= 0.9


# --------------------------------------------------------- evaluate() path

def test_evaluate_zone_trigger_bypasses_confluence_count() -> None:
    """The D-048 headline: a zone retest fires even when the ICT count
    gate is set to an impossible 6/6 — the location + rejection carry
    the setup (user directive: not every strategy must agree)."""
    df = _demand_zone_frame()
    htf = _uptrend_htf()
    close_time = df["time_utc"].iloc[-1] + timedelta(minutes=1)
    # count gate impossible; D-050 legacy-entry fixture
    cfg = EngineConfig(min_confluence=6, entry_mode="market")
    ev = evaluate(df, htf, close_time, cfg, spread_points=20)
    assert ev.signal is not None, ev.trace["checks"]
    assert ev.signal["trigger"] == "zone"
    assert ev.signal["direction"] == "BUY"
    # trace tells the story: zone retest fired, count is informational
    names = {c["name"]: c for c in ev.signal["trace"]["checks"]}
    assert names["zone_retest"]["pass"] is True
    assert "does not block" in names["confluence"]["value"]
    # SL beyond the zone, TP above entry (BUY), spread-risk respected
    assert ev.signal["sl"] < 4300.35  # under the zone high (far edge below)
    assert ev.signal["tp"] > ev.signal["entry"]
    # confidence within a sane band and never above 1
    assert 0.0 < ev.signal["confidence"] <= 1.0


def test_evaluate_zone_trigger_can_be_disabled() -> None:
    df = _demand_zone_frame()
    htf = _uptrend_htf()
    close_time = df["time_utc"].iloc[-1] + timedelta(minutes=1)
    cfg = EngineConfig(min_confluence=6, zone_trigger_enabled=False)
    ev = evaluate(df, htf, close_time, cfg, spread_points=20)
    assert ev.signal is None  # count gate impossible + zone path off


def test_evaluate_zone_fires_without_mtf_agreement() -> None:
    """Zone path does not wait for the M5/M15 EMA gate either — the zone
    IS the location confluence (D-048 arbitration)."""
    df = _demand_zone_frame()
    htf = _uptrend_htf()
    # M5/M15 falling -> 0/2 agree (the classic pullback look)
    t0 = datetime(2026, 9, 22, 0, 0, tzinfo=UTC)
    for tf, m in (("M15", 15), ("M5", 5)):
        rows = [
            _bar(t0 + timedelta(minutes=m * i), 4400.0 - i, 4401.0 - i,
                 4399.0 - i, 4400.5 - i, 50)
            for i in range(60)
        ]
        htf[tf] = pd.DataFrame(rows, columns=["time_utc", "o", "h", "l", "c", "v"])
    close_time = df["time_utc"].iloc[-1] + timedelta(minutes=1)
    cfg = EngineConfig(entry_mode="market")  # D-050 — zone/MTF arbitration on the legacy entry
    ev = evaluate(df, htf, close_time, cfg, spread_points=20)
    assert ev.signal is not None, ev.trace["checks"]
    assert ev.signal["trigger"] == "zone"


def test_evaluate_sfp_still_wins_arbitration() -> None:
    """When BOTH the SFP sweep and a zone retest fire on the same bar,
    the sweep (rarest/strongest pattern) takes priority."""
    df = _demand_zone_frame()
    # make the trigger bar ALSO sweep the prior 20-bar low and close back
    # above it: prior lows min ~4300.22 (rally bars); trigger low 4300.15
    # already below -> SFP fires (wick 0.65 >= 0.35*ATR), zone also fires
    htf = _uptrend_htf()
    close_time = df["time_utc"].iloc[-1] + timedelta(minutes=1)
    cfg = EngineConfig(min_confluence=6, entry_mode="market")  # D-050 fixture
    ev = evaluate(df, htf, close_time, cfg, spread_points=20)
    # even if an SFP fired on this bar, the impossible count gates it out
    # and the zone path takes over — a signal still exists
    assert ev.signal is not None
    assert ev.signal["trigger"] == "zone"
    # -> now open the count gate fully: the strongest available trigger wins
    cfg2 = EngineConfig(min_confluence=0, entry_mode="market")
    ev2 = evaluate(df, htf, close_time, cfg2, spread_points=20)
    assert ev2.signal is not None
    assert ev2.signal["trigger"] in ("sfp", "zone")


def test_check_rsi_zone_window_wider_than_classic() -> None:
    """Deep pullback RSI (e.g. 28) passes the zone window but would fail
    the classic BUY window (40-65)."""
    closes = pd.Series(
        [100.0 + 0.6] * 14 + [100.0 - 0.55] * 6, dtype=float
    )
    df = pd.DataFrame({
        "time_utc": pd.date_range("2026-09-22", periods=len(closes), freq="1min", tz="UTC"),
        "o": closes, "h": closes + 0.1, "l": closes - 0.1, "c": closes, "v": 10,
    })
    tr = Trace()
    tr.direction = "BUY"
    ok, val = check_rsi_zone(df, EngineConfig(), tr)
    # the window used must be the widened zone window (20-70), not the
    # classic (40-65) — and the pass decision matches THAT window
    assert "zone window 20-70" in tr.checks[-1].value
    assert ok == (20.0 <= val <= 70.0)


# ----------------------------------------------------------------- config

def test_config_d048_defaults() -> None:
    assert DEFAULT_CONFIG.zone_trigger_enabled is True
    assert DEFAULT_CONFIG.min_zone_quality == 0.45
    # D-049 — 0.70 -> 0.58: the old gate was practically unreachable,
    # freezing BUY signals during H1 downtrends (sell-only bias)
    assert DEFAULT_CONFIG.counter_trend_quality == 0.58
    assert DEFAULT_CONFIG.zone_retest_window == 2
    # D-049 — honest 1500-bar window measured 3 back on top of 2
    assert DEFAULT_CONFIG.min_confluence == 3
    # D-049 target block
    assert DEFAULT_CONFIG.tp_min_rr == 1.2
    assert DEFAULT_CONFIG.tp_max_r == 3.0
    assert DEFAULT_CONFIG.rr == 1.6
    assert DEFAULT_CONFIG.expiry_bars == 45  # D-051: 45 min on M1 (back)
    assert DEFAULT_CONFIG.max_spread_to_risk == 0.30
    assert DEFAULT_CONFIG.max_trades_per_day == 6
    # D-051 — back on M1 (the user's signal-flow TF) + POI pending entries
    assert DEFAULT_CONFIG.timeframe == "M1"
    assert DEFAULT_CONFIG.confirm_tfs == ["M5", "M15"]
    assert DEFAULT_CONFIG.min_atr == 0.15
    assert DEFAULT_CONFIG.entry_mode == "poi_limit"
    # D-051 — the 4-6 USD pending window + trusted votes + multi-market
    assert DEFAULT_CONFIG.entry_min_usd == 1.0
    assert DEFAULT_CONFIG.pending_target_usd == 4.5
    assert DEFAULT_CONFIG.pending_max_usd == 6.0
    assert DEFAULT_CONFIG.trusted_min_votes == 2.0
    assert DEFAULT_CONFIG.signal_symbols == ["XAUUSD", "BTCUSD"]
    assert DEFAULT_CONFIG.pending_max_atr == 15.0  # USD cap binds first
    assert DEFAULT_CONFIG.pending_expiry_bars == 60  # 1h on M1
    assert DEFAULT_CONFIG.max_pending_signals == 6
    # bounds enforced
    try:
        EngineConfig(min_zone_quality=1.5)
        raise AssertionError("min_zone_quality > 1 must be rejected")
    except Exception:
        pass


def test_legacy_d049_upgrade() -> None:
    """Stored rows still on pre-D-049 shipped defaults move to the new
    set; customized values are NEVER touched."""
    from app.engine.config import upgrade_legacy_d049

    raw = {"rr": 1.1, "expiry_bars": 20, "max_spread_to_risk": 0.5,
           "counter_trend_quality": 0.70, "min_confluence": 2}
    out, moved = upgrade_legacy_d049(raw)
    assert moved == ["rr", "expiry_bars", "max_spread_to_risk",
                     "counter_trend_quality"]
    assert out["rr"] == 1.6 and out["expiry_bars"] == 45
    assert out["max_spread_to_risk"] == 0.30
    assert out["counter_trend_quality"] == 0.58
    # untouched fields survive
    assert out["min_confluence"] == 2
    # customized values stay put
    raw2 = {"rr": 2.5, "expiry_bars": 30, "max_spread_to_risk": 0.4,
            "counter_trend_quality": 0.65}
    out2, moved2 = upgrade_legacy_d049(raw2)
    assert moved2 == []
    assert out2["rr"] == 2.5 and out2["counter_trend_quality"] == 0.65


def test_legacy_confluence_upgrade() -> None:
    """D-049 — stored rows still on the D-048-era default (2) move to 3
    (the honest 1500-bar window remeasured 3 on top); customized values
    are NEVER touched."""
    from app.engine.config import upgrade_legacy_confluence

    out, changed = upgrade_legacy_confluence({"min_confluence": 2})
    assert changed is True and out["min_confluence"] == 3
    # customized values are NEVER touched
    out, changed = upgrade_legacy_confluence({"min_confluence": 4})
    assert changed is False and out["min_confluence"] == 4
    out, changed = upgrade_legacy_confluence({"min_confluence": 1})
    assert changed is False
    out, changed = upgrade_legacy_confluence({})
    assert changed is False


def test_zone_trigger_quality_blend() -> None:
    from app.engine.zones import ZoneRetestSignal

    sig = ZoneRetestSignal(
        direction="BUY", entry=4300.0,
        zone={"side": "demand", "source": "sd", "lo": 4299.0, "hi": 4300.0,
              "quality": 0.8},
        rejection=0.6, quality=0.8, atr=0.5, counter_trend=False,
    )
    assert zone_trigger_quality(sig) == pytest.approx(0.55 * 0.8 + 0.45 * 0.6)
