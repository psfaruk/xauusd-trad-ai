"""D-042 — ICT/SMC analysis package + engine confluence tests.

Crafted (deterministic) tapes:
- a bullish M1 uptrend with an order block, an unfilled FVG, equal lows
  (SSL pool), a demand zone and a terminal SFP sweep that hunts the SSL
  pool — the exact ICT anatomy the engine should reward;
- sawtooth-rising HTF frames (H4/H1/M15/M5) whose fractal structure is
  HH/HL (a monotonic ramp has NO swings and would read "balanced").
"""

from __future__ import annotations

import pandas as pd
import pytest

from app.analysis import indicators as ind
from app.analysis import orderflow as of
from app.analysis import smc
from app.analysis.context import (
    build_confluence,
    confluence_score,
    smart_targets,
)
from app.engine.config import EngineConfig
from app.engine.engine import evaluate

COLS = ["time_utc", "o", "h", "l", "c", "v"]


def _mk(rows: list[tuple]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=COLS)


def sawtooth_frame(base: pd.Timestamp, tf_min: int, bars: int = 80,
                   start: float = 90.0) -> pd.DataFrame:
    """Rising sawtooth (3 up, 1 down) — HH/HL fractal structure = bullish."""
    rows = []
    prev_c = start
    for i in range(bars):
        t = base - pd.Timedelta(minutes=tf_min * bars) + pd.Timedelta(minutes=tf_min * i)
        up = i % 4 != 3
        c = prev_c + (0.9 if up else -0.4)
        o = prev_c
        rows.append([t, o, max(o, c) + 0.15, min(o, c) - 0.15, c, 50])
        prev_c = c
    return _mk(rows)


def bullish_ict_m1() -> pd.DataFrame:
    """M1 tape with OB + FVG + equal lows + demand zone + SFP sweep trigger.

    Timeline (all UTC, day 1):
      12:00..12:44  rising sawtooth 100 -> ~108 (HH/HL structure)
      two pullback valleys print EQUAL LOWS at 107.0 (SSL pool)
      12:45         down candle 108.4->107.9  (becomes the bullish OB:
                    next bar is a 2.2-body impulse)
      12:46         impulse up 107.9->110.1 leaving a bullish FVG below
                    (12:45 high 108.55 < 12:47 low 108.75)
      12:47..12:52  gentle drift down to ~108.2 (retest of the OB zone)
      12:53         TRIGGER: sweeps the 107.0 SSL pool to 106.8, closes
                    back at 108.2 with a long lower wick (SFP BUY)
    """
    base = pd.Timestamp("2025-01-06 12:00", tz="UTC")
    rows: list[tuple] = []
    prev_c = 100.0
    price = 100.0
    for i in range(45):
        t = base + pd.Timedelta(minutes=i)
        up = i % 4 != 3
        step = 0.55 if up else -0.25
        c = price + step
        o = prev_c
        rows.append([t, o, max(o, c) + 0.12, min(o, c) - 0.12, c, 12])
        price, prev_c = c, c
    # equal-lows pullbacks at 107.0 (two fractal valleys = SSL pool)
    # valley 1: bars 45-47 (down, flat-low, up)
    t = base + pd.Timedelta(minutes=45)
    rows.append([t, 107.6, 107.65, 107.0, 107.1, 12])   # down into valley
    rows.append([t + pd.Timedelta(minutes=1), 107.1, 107.3, 107.0, 107.2, 10])
    rows.append([t + pd.Timedelta(minutes=2), 107.2, 108.0, 107.15, 107.9, 14])
    # push to a HH then valley 2 at the SAME 107.0 low
    rows.append([t + pd.Timedelta(minutes=3), 107.9, 108.6, 107.85, 108.5, 12])
    rows.append([t + pd.Timedelta(minutes=4), 108.5, 108.55, 107.9, 108.0, 11])
    rows.append([t + pd.Timedelta(minutes=5), 108.0, 108.1, 107.0, 107.1, 10])
    rows.append([t + pd.Timedelta(minutes=6), 107.1, 107.4, 107.0, 107.3, 12])
    # pullback lows around 107.0-107.2 (equal lows)
    # OB candle: down-close, body 107.9-108.3, wick down to 107.3
    t53 = t + pd.Timedelta(minutes=7)                     # 12:52
    rows.append([t53, 107.4, 107.55, 107.0, 107.15, 12])  # pullback low
    rows.append([t53 + pd.Timedelta(minutes=1), 108.3, 108.35, 107.3, 107.9, 11])
    # impulse bar body 2.2 (107.9 -> 110.1) -> bullish OB at 12:53
    rows.append([t53 + pd.Timedelta(minutes=2), 107.9, 110.4, 107.85, 110.1, 40])
    # post-impulse bar high above -> bullish FVG between 12:53 high and this low
    rows.append([t53 + pd.Timedelta(minutes=3), 110.1, 110.3, 109.2, 110.05, 30])
    # drift back down into the OB/FVG zone (~108.3)
    px = 110.1
    for j in range(6):
        tt = t53 + pd.Timedelta(minutes=4 + j)
        c = px - 0.32
        rows.append([tt, px, max(px, c) + 0.08, min(px, c) - 0.08, c, 11])
        px = c
    # TRIGGER bar (12:59 close -> 13:00.. wait, keep 13:0x session-safe):
    # sweep the 107.0 SSL, long lower wick, close 108.2 (inside OB zone)
    tt = t53 + pd.Timedelta(minutes=10)                   # 13:02 open
    rows.append([tt, 108.18, 108.4, 106.8, 108.2, 55])
    return _mk(rows)


def htf_frames() -> dict[str, pd.DataFrame]:
    base = pd.Timestamp("2025-01-06 12:00", tz="UTC")
    return {
        "H4": sawtooth_frame(base, 240, start=80.0),
        "H1": sawtooth_frame(base, 60, start=85.0),
        "M15": sawtooth_frame(base, 15, start=95.0),
        "M5": sawtooth_frame(base, 5, start=100.0),
    }


# --------------------------------------------------------------- indicators


class TestIndicators:
    def _df(self, closes):
        base = pd.Timestamp("2025-01-06 12:00", tz="UTC")
        rows = []
        prev = closes[0]
        for i, c in enumerate(closes):
            o = prev
            rows.append([base + pd.Timedelta(minutes=i), o,
                         max(o, c) + 0.1, min(o, c) - 0.1, c, 10])
            prev = c
        return _mk(rows)

    def test_rsi_bounds(self):
        up = self._df([100 + i * 0.5 for i in range(40)])
        down = self._df([120 - i * 0.5 for i in range(40)])
        assert ind.rsi_series(up["c"]).iloc[-1] > 70
        assert ind.rsi_series(down["c"]).iloc[-1] < 30

    def test_macd_direction(self):
        up = self._df([100 + i * 0.4 for i in range(60)])
        m = ind.macd(up["c"])
        assert m["hist"] > 0

    def test_swings_detect_fractals(self):
        df = bullish_ict_m1()
        pts = ind.swings(df.iloc[:50], 2, 2)
        kinds = {p["kind"] for p in pts}
        assert kinds == {"high", "low"}

    def test_volume_zscore_spike(self):
        df = bullish_ict_m1()
        z = ind.volume_zscore(df)
        assert z > 1.5  # the trigger bar carries a volume spike


# --------------------------------------------------------------------- smc


class TestSMC:
    def test_structure_bullish_trend(self):
        df = bullish_ict_m1().iloc[:-1]  # before the trigger bar
        st = smc.detect_structure(df)
        assert st["trend"] in ("bullish", "balanced")

    def test_order_block_found_and_mitigated_tracking(self):
        df = bullish_ict_m1()
        obs = smc.detect_order_blocks(df)
        bull = [ob for ob in obs if ob["side"] == "bullish"]
        assert bull, "expected at least one bullish OB (12:53 candle)"

    def test_fvg_detected(self):
        df = bullish_ict_m1()
        gaps = smc.detect_fvg(df)
        assert any(g["side"] == "bullish" and g["gap"] > 0.5 for g in gaps)

    def test_liquidity_pool_and_sweep(self):
        df = bullish_ict_m1()
        liq = smc.detect_liquidity(df)
        ssl = [lv for lv in liq["levels"] if lv["kind"] == "SSL"]
        assert ssl, "expected the equal-lows SSL pool"
        assert any(abs(lv["price"] - 107.0) < 0.35 for lv in ssl)
        assert any(s["kind"] == "SSL" for s in liq["sweeps"]), \
            "the trigger bar must register the SSL sweep"

    def test_premium_discount_states(self):
        df = bullish_ict_m1()
        pdz = smc.premium_discount(df)
        assert pdz["state"] in ("premium", "discount", "equilibrium")
        assert pdz["range_hi"] >= pdz["range_lo"]

    def test_kill_zone_windows(self):
        assert smc.kill_zone(pd.Timestamp("2025-01-06 08:30", tz="UTC"))["name"] == "london"
        assert smc.kill_zone(pd.Timestamp("2025-01-06 13:30", tz="UTC"))["name"] == "ny-am"
        assert smc.kill_zone(pd.Timestamp("2025-01-06 05:30", tz="UTC"))["name"] == "asia"
        assert smc.kill_zone(pd.Timestamp("2025-01-06 11:00", tz="UTC"))["in"] is False


# --------------------------------------------------------------- orderflow


class TestOrderFlow:
    def test_volume_profile_poc(self):
        df = bullish_ict_m1()
        vp = of.volume_profile(df.iloc[:50])
        assert vp["poc"] is not None
        assert vp["val"] <= vp["poc"] <= vp["vah"]

    def test_whale_events_classified(self):
        df = bullish_ict_m1()
        evs = of.detect_whale_events(df, lookback=30, z_thr=1.5)
        kinds = {e["kind"] for e in evs}
        assert "momentum" in kinds  # the 2.2-body impulse bar
        assert any(e["side"] == "buy" for e in evs)

    def test_candle_pulse_buyers_dominate(self):
        """D-051 — a bar closing at its high reads as buyer dominance."""
        bar = pd.Series(
            {"o": 4500.0, "h": 4512.0, "l": 4499.0, "c": 4511.5, "v": 300.0}
        )
        cp = of.candle_pulse(bar, prev_close=4500.0)
        assert cp["dir"] == "bull"
        assert cp["buy_pct"] > 80.0
        assert cp["sell_pct"] < 20.0
        assert cp["delta"] > 0.6
        assert "buyers dominate" in cp["reaction"]
        assert cp["wick"] == "none"  # tiny wicks, full conviction

    def test_candle_pulse_sellers_reject_the_high(self):
        """D-051 — long upper wick = sellers rejected the high."""
        bar = pd.Series(
            {"o": 4500.0, "h": 4514.0, "l": 4499.0, "c": 4501.0, "v": 100.0}
        )
        cp = of.candle_pulse(bar)
        assert cp["wick"] == "upper"
        assert "upper wick" in cp["reaction"]
        assert cp["body_ratio"] < 0.2

    def test_candle_pulse_volume_spike_flagged(self):
        """D-051 — volume >= 2x the trailing mean is flagged in the story."""
        bar = pd.Series(
            {"o": 4500.0, "h": 4504.0, "l": 4499.5, "c": 4503.5, "v": 500.0}
        )
        vols = pd.Series([80.0, 90.0, 110.0, 100.0])
        cp = of.candle_pulse(bar, vol_window=vols)
        assert cp["vol_ratio"] >= 4.0
        assert "volume spike" in cp["reaction"]


# ----------------------------------------------------------------- context


class TestConfluence:
    def test_factors_on_bullish_setup(self):
        m1 = bullish_ict_m1()
        htf = htf_frames()
        close_time = m1["time_utc"].iloc[-1] + pd.Timedelta(minutes=1)
        factors = build_confluence(
            m1, htf, "BUY", float(m1["c"].iloc[-1]), close_time,
        )
        by_name = {f["name"]: f for f in factors}
        assert set(by_name) >= {
            "structure_m1", "htf_structure", "ob_retest", "fvg_fill",
            "liquidity_sweep", "zone", "volume", "killzone", "whale_bias",
        }
        assert by_name["liquidity_sweep"]["ok"] is True
        assert by_name["ob_retest"]["ok"] is True
        assert by_name["fvg_fill"]["ok"] is True
        assert confluence_score(factors) >= 3

    def test_smart_targets_floor_cap_and_snap(self):
        m1 = bullish_ict_m1()
        entry, sl, tp, note = smart_targets(
            m1, "BUY", 108.2, 106.8, rr=1.1, min_sl_atr=1.3, max_sl_atr=3.5,
        )
        a = ind.atr(m1, 14)
        assert entry == 108.2
        assert 108.2 - 3.5 * a - 1e-9 <= sl <= 108.2 - 1.3 * a + 1e-9
        assert tp is None or tp > entry  # BUY target above (or skipped)
        assert isinstance(note, str) and note

    def test_smart_targets_caps_risk(self):
        m1 = bullish_ict_m1()
        # absurd structural SL far below -> capped at max_sl_atr
        _, sl, _, _ = smart_targets(
            m1, "BUY", 108.2, 80.0, rr=1.0, min_sl_atr=1.3, max_sl_atr=1.5,
        )
        a = ind.atr(m1, 14)
        assert sl == pytest.approx(108.2 - 1.5 * a)


# ------------------------------------------------------------ engine gate


class TestEngineConfluenceGate:
    def _run(
        self, min_confluence: int, spread: float = 20.0,
        trusted_min_votes: float = 2.0,
    ):
        m1 = bullish_ict_m1()
        htf = htf_frames()
        close_time = m1["time_utc"].iloc[-1] + pd.Timedelta(minutes=1)
        cfg = EngineConfig(
            smc_enabled=True, min_confluence=min_confluence,
            trusted_min_votes=trusted_min_votes,
        )
        return evaluate(m1, htf, close_time, cfg, spread_points=spread)

    def test_high_gate_blocks(self):
        # D-051 — the trusted core must be unreachable for the pure
        # full-panel-gate semantics this test pins (max trusted = 5)
        ev = self._run(min_confluence=6, trusted_min_votes=5.5)
        assert ev.signal is None
        names = [c["name"] for c in ev.trace["checks"]]
        assert "confluence" in names

    def test_trusted_votes_fire_without_full_panel(self):
        """D-051 (user directive): a few TRUSTED strategies voting together
        is enough — the signal fires even when the full panel is short."""
        ev = self._run(min_confluence=6, trusted_min_votes=2.0)
        assert ev.signal is not None, ev.trace
        fired = ev.pulse and ev.pulse.get("fired")
        assert fired and fired["direction"] == "BUY"

    def test_default_gate_emits_ict_signal(self):
        ev = self._run(min_confluence=2)
        assert ev.signal is not None, ev.trace
        assert ev.signal["direction"] == "BUY"
        assert ev.signal["trigger"] == "sfp"
        # the ICT factors ride inside the trace for the Signal panel
        factors = ev.signal["trace"].get("confluence_factors") or []
        assert len(factors) >= 6
        assert ev.signal["sl"] < ev.signal["entry"] < ev.signal["tp"]
        risk = ev.signal["entry"] - ev.signal["sl"]
        assert risk > 0
        # confidence stays in bounds with the ICT block folded in
        assert 0.0 <= ev.signal["confidence"] <= 1.0

    def test_smc_disabled_skips_gate(self):
        m1 = bullish_ict_m1()
        htf = htf_frames()
        close_time = m1["time_utc"].iloc[-1] + pd.Timedelta(minutes=1)
        cfg = EngineConfig(smc_enabled=False, min_confluence=6)
        ev = evaluate(m1, htf, close_time, cfg, spread_points=20)
        assert ev.signal is not None  # old pipeline unaffected by the gate
        assert "confluence" not in [c["name"] for c in ev.trace["checks"]]
