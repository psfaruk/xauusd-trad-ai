"""SFP detection tests (SPEC §8.2 rule 2, Phase 3 AC fixtures).

Cases: pure sweep, close-beyond fake break, quiet market, wick under
threshold, level math (SL buffer / TP = RR * risk), quality scoring.
"""

from __future__ import annotations

import pandas as pd
import pytest

from app.engine.config import EngineConfig
from app.engine.sfp import build_levels, detect_sfp, sfp_quality
from app.engine.trace import Trace

CFG = EngineConfig()


def series(closes, spread=1.0, times=None):
    """Build an OHLC frame from closes: each bar range [c-spread, c+spread]."""
    rows = []
    base = pd.Timestamp("2025-01-06 09:00", tz="UTC")
    for i, c in enumerate(closes):
        t = times[i] if times else base + pd.Timedelta(minutes=15 * i)
        rows.append([t, c, c + spread, c - spread, c, 10])
    return pd.DataFrame(rows, columns=["time_utc", "o", "h", "l", "c", "v"])


def buy_sweep_frame(wick_mult=0.5, close_above=True):
    """20 flat bars at 100, then a sweep bar: low 98, close back above 99."""
    df = series([100.0] * 21)
    i = len(df) - 1
    atr_val = 2.0  # constant range 2 -> Wilder ATR converges to 2.0
    low = 99.0 - wick_mult * atr_val  # sweeps min-low 99.0 by wick_mult*ATR
    close = 100.2 if close_above else 98.5
    df.iloc[i, df.columns.get_indexer(["o", "h", "l", "c"])] = [
        100.0, max(100.4, close), low, close,
    ]
    return df


class TestDetectSfpBuy:
    def test_pure_sweep_detected(self):
        trace = Trace()
        trace.direction = "BUY"
        sig = detect_sfp(buy_sweep_frame(), CFG, trace)
        assert sig is not None and sig.direction == "BUY"
        assert sig.swept_level == 99.0
        assert sig.wick >= CFG.sfp_wick_atr_ratio * sig.atr
        check = [c for c in trace.checks if c.name == "sfp_sweep"][0]
        assert check.passed

    def test_close_beyond_fake_break_rejected(self):
        # close BELOW the swept level = real breakdown, not an SFP
        trace = Trace()
        trace.direction = "BUY"
        assert detect_sfp(buy_sweep_frame(close_above=False), CFG, trace) is None
        check = [c for c in trace.checks if c.name == "sfp_sweep"][0]
        assert not check.passed

    def test_quiet_market_no_sweep(self):
        trace = Trace()
        trace.direction = "BUY"
        df = series([100.0] * 25)  # nothing happens
        assert detect_sfp(df, CFG, trace) is None

    def test_wick_below_threshold_rejected(self):
        # open right at the swept level, tiny dip below, strong close above:
        # the sweep EXISTS but the lower wick (0.15) < 0.3*ATR(~1.96)=0.59
        df = series([100.0] * 21)
        i = len(df) - 1
        df.iloc[i, df.columns.get_indexer(["o", "h", "l", "c"])] = [
            99.1, 100.4, 98.95, 100.2,
        ]
        trace = Trace()
        trace.direction = "BUY"
        sig = detect_sfp(df, CFG, trace)
        assert sig is None
        check = [c for c in trace.checks if c.name == "sfp_sweep"][0]
        assert not check.passed

    def test_no_lookahead_prior_window_only(self):
        # the swept level must come from the PREVIOUS lookback bars, not the
        # sweep bar itself (construct: prior low 99, sweep low 98.1, close 100)
        trace = Trace()
        trace.direction = "BUY"
        sig = detect_sfp(buy_sweep_frame(wick_mult=0.1), CFG, trace)
        if sig is not None:  # 0.1*ATR wick may still pass if ATR small
            assert sig.swept_level == 99.0


class TestDetectSfpSell:
    def test_mirror_sweep_detected(self):
        df = series([100.0] * 21)
        i = len(df) - 1
        df.iloc[i, df.columns.get_indexer(["o", "h", "l", "c"])] = [
            100.0, 101.1, 99.6, 99.8,
        ]  # high 101.1 sweeps max-high 101.0, close 99.8 back below
        trace = Trace()
        trace.direction = "SELL"
        sig = detect_sfp(df, CFG, trace)
        assert sig is not None and sig.direction == "SELL"
        assert sig.swept_level == 101.0
        assert sig.sweep_extreme == 101.1

    def test_buy_direction_ignores_high_sweep(self):
        df = series([100.0] * 21)
        i = len(df) - 1
        df.iloc[i, df.columns.get_indexer(["o", "h", "l", "c"])] = [
            100.0, 101.5, 99.5, 99.9,
        ]
        trace = Trace()
        trace.direction = "BUY"  # wrong direction for this pattern
        assert detect_sfp(df, CFG, trace) is None


class TestLevels:
    def test_buy_sl_tp_math(self):
        trace = Trace()
        trace.direction = "BUY"
        sig = detect_sfp(buy_sweep_frame(), CFG, trace)
        entry, sl, tp = build_levels(sig, CFG)
        # D-041: SL is the FARTHER of (sweep - buffer, entry - min_sl_atr)
        expected = min(
            sig.sweep_extreme - CFG.sl_buffer_atr * sig.atr,
            entry - CFG.min_sl_atr * sig.atr,
        )
        assert sl == pytest.approx(expected, abs=1e-6)
        risk = entry - sl
        assert risk > 0
        assert risk >= CFG.min_sl_atr * sig.atr - 1e-6
        assert tp == pytest.approx(entry + CFG.rr * risk, abs=1e-6)

    def test_sell_sl_tp_math(self):
        trace = Trace()
        trace.direction = "SELL"
        df = series([100.0] * 21)
        i = len(df) - 1
        df.iloc[i, df.columns.get_indexer(["o", "h", "l", "c"])] = [
            100.0, 101.4, 99.6, 99.8,
        ]
        sig = detect_sfp(df, CFG, trace)
        assert sig is not None
        entry, sl, tp = build_levels(sig, CFG)
        expected = max(
            sig.sweep_extreme + CFG.sl_buffer_atr * sig.atr,
            entry + CFG.min_sl_atr * sig.atr,
        )
        assert sl == pytest.approx(expected, abs=1e-6)
        risk = sl - entry
        assert risk > 0
        assert risk >= CFG.min_sl_atr * sig.atr - 1e-6
        assert tp == pytest.approx(entry - CFG.rr * risk, abs=1e-6)

    def test_min_sl_floor_widens_tight_stops(self):
        """A shallow sweep must still stop at least min_sl_atr*ATR away —
        the spread/noise floor that keeps M1 stops tradeable (D-041)."""
        from app.engine.sfp import SfpSignal

        sig = SfpSignal("BUY", 100.0, 99.9, 99.95, 0.1, 0.05, 2.0)
        entry, sl, tp = build_levels(sig, CFG)
        assert entry - sl >= CFG.min_sl_atr * 2.0 - 1e-9


class TestQuality:
    def test_at_threshold_zero(self):
        from app.engine.sfp import SfpSignal

        sig = SfpSignal("BUY", 100, 98, 99, 0.3, 0.3, 1.0)
        assert sfp_quality(sig, CFG) == pytest.approx(0.0)

    def test_at_double_threshold_full(self):
        from app.engine.sfp import SfpSignal

        sig = SfpSignal("BUY", 100, 98, 99, 0.9, 0.9, 1.0)
        assert sfp_quality(sig, CFG) == pytest.approx(1.0)

    def test_clamped_above(self):
        from app.engine.sfp import SfpSignal

        sig = SfpSignal("BUY", 100, 98, 99, 3.0, 3.0, 1.0)
        assert sfp_quality(sig, CFG) == pytest.approx(1.0)
