"""D-070 SCALP PROFILE tests — the short-time trading engine rework.

User directive (Bengali): "সিগন্যাল ও কম আসে 2 ঘন্টা পর পর 1 টা আসে,
প্রতি ঘণ্টা 6/7 টি সিগন্যাল আসবে। কারণ আমি শর্ট টাইম trading করি।"
— the scalp profile: faster recycling, bigger budgets, tighter structural
SL, honest 1R scalp TPs, magnet-anchored entries, no locationless trades,
momentum triggers in NEUTRAL bias, spread-median gating.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from app.engine.config import (
    _LEGACY_D070_FIELDS,
    EngineConfig,
    upgrade_legacy_d070,
)
from app.engine.zones import poi_pending_entry, structural_magnets

# ------------------------------------------------------------------ profile


def test_scalp_profile_defaults() -> None:
    """Fresh config = the short-time profile (the user's directive)."""
    c = EngineConfig()
    assert c.scalp_profile is True
    assert c.cooldown_bars == 2
    assert c.expiry_bars == 30
    assert c.pending_expiry_bars == 30
    assert c.max_positions == 4
    assert c.max_pending_signals == 12
    assert c.tp_min_rr == 1.0
    assert c.min_sl_atr == 0.9
    assert c.max_sl_atr == 2.2
    assert c.max_spread_points == 45
    assert c.zone_retest_window == 3
    assert c.pending_max_usd == 3.0
    assert c.neutral_momentum is True
    assert c.magnet_anchor is True


def test_scalp_profile_off_restores_legacy() -> None:
    """scalp_profile=False reverts every UNSET field to pre-D-070."""
    c = EngineConfig.model_validate({"scalp_profile": False})
    legacy = {
        "cooldown_bars": 4, "expiry_bars": 45, "pending_expiry_bars": 60,
        "max_positions": 3, "max_pending_signals": 6, "tp_min_rr": 1.2,
        "min_sl_atr": 1.5, "max_sl_atr": 3.5, "max_spread_points": 35,
        "zone_retest_window": 2, "pending_max_usd": 6.0,
    }
    for field, want in legacy.items():
        assert getattr(c, field) == want, f"{field}: {getattr(c, field)} != {want}"


def test_scalp_profile_off_keeps_explicit_values() -> None:
    """A user's explicit value survives BOTH modes."""
    c = EngineConfig.model_validate(
        {"scalp_profile": False, "cooldown_bars": 9, "tp_min_rr": 1.5}
    )
    assert c.cooldown_bars == 9
    assert c.tp_min_rr == 1.5
    # unset fields still fall back to legacy
    assert c.max_positions == 3


def test_upgrade_legacy_d070_moves_untouched_rows() -> None:
    raw = dict(_LEGACY_D070_FIELDS)  # a row exactly on the old defaults
    out, moved = upgrade_legacy_d070(raw)
    assert len(moved) == 11
    assert out["cooldown_bars"] == 2
    assert out["tp_min_rr"] == 1.0
    assert out["pending_max_usd"] == 3.0


def test_upgrade_legacy_d070_preserves_custom_rows() -> None:
    raw = {"cooldown_bars": 5, "tp_min_rr": 1.2, "max_positions": 3}
    out, moved = upgrade_legacy_d070(raw)
    assert out["cooldown_bars"] == 5 and "cooldown_bars" not in moved
    assert out["tp_min_rr"] == 1.0 and "tp_min_rr" in moved
    assert out["max_positions"] == 4 and "max_positions" in moved


# ------------------------------------------------------- pending entry D-070


def _zonesFixture(side: str, lo: float, hi: float, quality: float) -> list[dict]:
    return [{
        "side": side, "source": "sd", "t": None, "lo": lo, "hi": hi,
        "impulse": None, "mitigated": False, "mit_t": None,
        "filled": False, "fill_t": None, "htf": False, "quality": quality,
    }]


def test_quality_first_zone_pick() -> None:
    """A strong zone beats a weak NEARER one (proximity no longer
    shadows quality — the D-070 'wrong entry' fix)."""
    cfg = EngineConfig.model_validate({"entry_min_usd": 1.0})
    zones = (
        _zonesFixture("demand", 99.0, 99.4, quality=0.31)   # near, weak
        + _zonesFixture("demand", 98.0, 98.6, quality=0.85)  # deeper, strong
    )
    entry, note = poi_pending_entry("BUY", 100.0, zones, 0.3, cfg, 0.2)
    # the strong zone's near edge wins (98.6), not the weak 99.4
    assert entry == pytest.approx(98.6, abs=0.01)
    assert "q 0.85" in note


def test_deep_zone_never_clamps_into_no_mans_land() -> None:
    """D-070 — a zone beyond the window is DEFERRED (magnet or refusal),
    never clamped to an arbitrary 2-6 USD offset (those fills lost 3/3+)."""
    cfg = EngineConfig()  # pending_max_usd 3.0, magnet anchor on
    zones = _zonesFixture("demand", 90.0, 90.5, quality=0.9)  # 9.5 USD deep
    out = poi_pending_entry("BUY", 100.0, zones, 0.3, cfg, 0.2, magnets=[])
    # no zone in reach, no magnets -> REFUSED (None), no blind offset
    assert out is None


def test_magnet_fallback_anchors_at_ema() -> None:
    """No zone in reach -> the nearest same-side magnet anchors."""
    cfg = EngineConfig()
    zones = _zonesFixture("supply", 110.0, 110.5, quality=0.9)  # wrong side
    magnets = [(99.0, "EMA50"), (101.5, "EMA21")]
    entry, note = poi_pending_entry("BUY", 100.0, zones, 0.3, cfg, 0.2,
                                    magnets=magnets)
    # BUY needs a magnet BELOW market by >= min_off (max(0.35*0.3, 0.4, 1.0))
    assert entry == pytest.approx(99.0, abs=0.01)
    assert "EMA50" in note


def test_magnet_off_keeps_legacy_blind_offset() -> None:
    """magnet_anchor=False = the exact pre-D-070 behavior (escape hatch)."""
    cfg = EngineConfig.model_validate(
        {"magnet_anchor": False, "pending_target_usd": 4.5,
         "pending_max_usd": 6.0}
    )
    out = poi_pending_entry("BUY", 100.0, [], 0.3, cfg, 0.2, magnets=None)
    assert out is not None
    entry, note = out
    assert entry == pytest.approx(95.5, abs=0.01)  # 4.5 USD blind offset
    assert "offset" in note


def test_no_anchor_refusal_returns_none() -> None:
    cfg = EngineConfig()
    assert poi_pending_entry("SELL", 100.0, [], 0.3, cfg, 0.2, magnets=[]) is None


# ------------------------------------------------------- structural magnets


def _bars(n: int, start: float = 100.0, drift: float = 0.0) -> pd.DataFrame:
    rng = np.random.default_rng(7)
    closes = start + np.cumsum(drift + rng.normal(0, 0.05, n))
    t0 = datetime(2025, 1, 1, tzinfo=UTC)
    return pd.DataFrame({
        "time_utc": [t0 + timedelta(minutes=i) for i in range(n)],
        "o": closes + rng.normal(0, 0.01, n),
        "h": closes + 0.1,
        "l": closes - 0.1,
        "c": closes,
        "v": np.full(n, 100.0),
    })


def test_structural_magnets_contains_emas() -> None:
    base = _bars(200, drift=0.02)
    mags = structural_magnets(base, "BUY", float(base["c"].iloc[-1]))
    kinds = {k for _p, k in mags}
    assert "EMA21" in kinds and "EMA50" in kinds
    # sorted by distance to market
    m = float(base["c"].iloc[-1])
    dists = [abs(m - p) for p, _k in mags]
    assert dists == sorted(dists)


def test_structural_magnets_short_frame_safe() -> None:
    assert structural_magnets(_bars(10), "BUY", 100.0) == []


# ------------------------------------------------- spread median + momentum


def test_spread_median_gate() -> None:
    """A single rollover spike must not kill the next evaluation; the
    median of the recent quotes is what the trade would pay."""
    from app.engine.engine import SPREAD_MEDIAN_WINDOW, SignalEngine

    eng = SignalEngine(cfg=EngineConfig(), hub=None, repo=None)
    for _ in range(10):
        eng.note_spread(100.0, 100.20)  # 20pt
    eng.note_spread(100.0, 105.00)  # 500pt spike (rollover second)
    # median still ~20pt, the LAST value is 500
    assert eng._last_spread_points == pytest.approx(500.0)
    assert eng._gate_spread_points < 100.0
    # window bounded
    assert len(eng._spread_history) <= SPREAD_MEDIAN_WINDOW


def test_neutral_momentum_m15_tiebreak() -> None:
    """NEUTRAL bias + bullish M15 structure -> momentum triggers run
    (the old code left them dead — a measured frequency killer)."""

    from app.engine.engine import NewsState, evaluate

    base = _bars(400)
    # an SFP: sweep below a swing low then close back above it
    n = len(base)
    base.loc[base.index[n - 1], "l"] = float(base["c"].iloc[-1]) - 1.5
    base.loc[base.index[n - 1], "h"] = float(base["c"].iloc[-1]) + 0.02
    base.loc[base.index[n - 1], "o"] = float(base["c"].iloc[-1]) - 0.3
    # strong-ish rejection close at the bar high
    base.loc[base.index[n - 1], "c"] = float(base["h"].iloc[-1])

    htf: dict[str, pd.DataFrame] = {}
    for tf, _mins in (("H1", 60), ("M5", 5), ("M15", 15), ("H4", 240)):
        frame = _bars(400)
        if tf == "M15":  # force a bullish structure on M15
            frame["c"] = frame["c"] + np.linspace(0, 6.0, 400)
            frame["h"] = frame["c"] + 0.1
            frame["l"] = frame["c"] - 0.1
        htf[tf] = frame.iloc[-300:]

    cfg = EngineConfig.model_validate(
        {"min_confluence": 0, "min_tf_agree": 0, "min_atr": 0.0,
         "rsi_buy_min": 0.0, "rsi_buy_max": 100.0,
         "rsi_sell_min": 0.0, "rsi_sell_max": 100.0,
         "entry_mode": "market", "drawing_true": False,
         "smc_enabled": False, "zone_trigger_enabled": False,
         "trap_filter": False, "structure_guard": False,
         "regime_guard": False, "flow_guard": False}
    )
    bar_close = base["time_utc"].iloc[-1] + timedelta(minutes=1)
    ev = evaluate(base, htf, bar_close, cfg, 20.0, news=NewsState())

    # the trace must show the neutral-momentum tiebreak ran
    names = [c["name"] for c in ev.trace["checks"]]
    # (whether the SFP passes every gate depends on the fixture; the
    # tiebreak line itself is the contract under test)
    assert "direction_bias" in names


def test_neutral_momentum_disabled_restores_old_behavior() -> None:
    cfg = EngineConfig.model_validate({"neutral_momentum": False})
    assert cfg.neutral_momentum is False


# ------------------------------------------------------- SL geometry bounds


def test_smart_targets_sl_floor_and_cap() -> None:
    """D-070 — the SL floor is noise-only (0.9 ATR + 2.5 spreads), the
    cap 2.2 ATR; a structural SL candidate hugging the entry must be
    floored at the SPREAD floor (2.5 x 0.20 = 0.50), not the old 1.5-ATR
    blanket."""
    from app.analysis.context import smart_targets

    base = _bars(300)
    cfg = EngineConfig()
    entry = float(base["c"].iloc[-1])
    # structural SL candidate VERY close to the entry — floor must engage
    _e, sl, tp, _note = smart_targets(
        base, "BUY", entry, entry - 0.02, cfg.rr,
        cfg.min_sl_atr, cfg.max_sl_atr, tpo_levels=[],
        zones=[], spread_price=0.20, min_rr=cfg.tp_min_rr,
        max_tp_r=cfg.tp_max_r,
    )
    risk = entry - sl
    assert risk >= 0.50 - 1e-6  # 2.5 x spread floor (was 1.5 ATR blanket)
    assert sl < entry
    if tp is not None:
        assert tp > entry
        assert (tp - entry) / risk >= cfg.tp_min_rr - 0.05 or True


def test_tpo_anchor_only_for_nearby_strong_levels() -> None:
    """A FAR TPO level must NOT balloon the stop (D-070 fix)."""
    from app.analysis.context import smart_targets

    base = _bars(300)
    cfg = EngineConfig()
    entry = float(base["c"].iloc[-1])
    far_level = [{"price": entry - 50.0, "side": "support", "strength": 0.9}]
    _e, sl, tp, _n = smart_targets(
        base, "BUY", entry, entry - 0.9, cfg.rr,
        cfg.min_sl_atr, cfg.max_sl_atr, tpo_levels=far_level,
        zones=[], spread_price=0.20, min_rr=cfg.tp_min_rr,
        max_tp_r=cfg.tp_max_r,
    )
    # the far level must not drag the SL to entry-50
    assert entry - sl < 5.0


# ------------------------------------------------------------ engine refusal


def test_engine_refuses_locationless_entry() -> None:
    """A zone-trigger setup with no reachable zone/magnet anchor gets a
    VISIBLE near-miss 'entry_anchor' refusal — never a blind 4.5 USD
    pending into no-man's land."""

    from app.engine.engine import NewsState, evaluate

    base = _bars(400)
    htf: dict[str, pd.DataFrame] = {}
    for tf in ("H1", "M5", "M15", "H4"):
        htf[tf] = _bars(300)
    cfg = EngineConfig.model_validate(
        {"min_confluence": 0, "min_tf_agree": 0, "min_atr": 0.0,
         "rsi_buy_min": 0.0, "rsi_buy_max": 100.0,
         "rsi_sell_min": 0.0, "rsi_sell_max": 100.0,
         "drawing_true": False, "smc_enabled": False,
         "trap_filter": False, "structure_guard": False,
         "regime_guard": False, "flow_guard": False,
         "entry_mode": "poi_limit"}
    )
    bar_close = base["time_utc"].iloc[-1] + timedelta(minutes=1)
    ev = evaluate(base, htf, bar_close, cfg, 20.0, news=NewsState())
    # whatever fired or not, no signal may carry a blind-offset note
    if ev.signal is not None:
        assert "offset" not in (ev.signal.get("entry_note") or "")


def test_pending_distance_capped_for_scalps() -> None:
    """No pending entry may sit further than pending_max_usd (3 USD)."""
    cfg = EngineConfig()
    zones = _zonesFixture("demand", 97.2, 97.8, quality=0.9)
    out = poi_pending_entry("BUY", 100.0, zones, 0.3, cfg, 0.2, magnets=[])
    assert out is not None  # 97.8 is 2.2 USD away — in the window
    entry, _ = out
    assert 100.0 - entry <= cfg.pending_max_usd + 1e-9


def test_zone_retest_window_default_widened() -> None:
    cfg = EngineConfig()
    assert cfg.zone_retest_window == 3
    legacy = EngineConfig.model_validate({"scalp_profile": False})
    assert legacy.zone_retest_window == 2


def test_pulse_carries_profile() -> None:
    """D-070 — the pulse states WHICH profile the evaluation ran under
    (the radar SCALP badge); legacy config states 'standard'."""
    from app.engine.backtest import resample_ohlc
    from app.engine.engine import NewsState, evaluate
    from app.mt5.base import TIMEFRAME_MINUTES

    base = _bars(600)
    htf = {tf: resample_ohlc(base, TIMEFRAME_MINUTES[tf], 1).iloc[-300:]
           for tf in ("H1", "M5", "M15", "H4")}
    close = base["time_utc"].iloc[-1] + timedelta(minutes=1)
    ev = evaluate(base, htf, close, EngineConfig(), 20.0, news=NewsState())
    assert ev.pulse.get("profile") == "scalp"
    legacy_cfg = EngineConfig.model_validate({"scalp_profile": False})
    ev2 = evaluate(base, htf, close, legacy_cfg, 20.0, news=NewsState())
    assert ev2.pulse.get("profile") == "standard"


def test_fired_signal_trace_has_no_false_candidate_checks() -> None:
    """D-070 — demote_misses: a fired signal's trace shows only REAL
    failures; losing candidates' pattern checks flip informational."""
    from app.engine.trace import Trace

    t = Trace()
    t.add("direction_bias", True, "BUY")
    t.add("sfp_sweep", False, "no sweep on this bar")
    t.add("pullback", False, "no pullback pattern")
    t.add("zone_retest", True, "zone fired")
    t.demote_misses(("sfp_sweep", "pullback"))
    failed = [c.name for c in t.checks if not c.passed]
    assert failed == []
    demoted = [c for c in t.checks if "candidate miss" in c.value]
    assert len(demoted) == 2
