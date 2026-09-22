"""Filter check tests (SPEC §8.2 rules 1, 3-7; confidence weights §8.2)."""

from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd
import pytest

from app.engine.config import EngineConfig, SessionRule
from app.engine.filters import (
    W_ATR,
    W_CONFLUENCE,
    W_MTF,
    W_RSI,
    W_SESSION,
    W_TREND,
    W_TRIGGER,
    atr_strength,
    check_atr,
    check_mtf,
    check_rsi,
    check_session,
    check_spread,
    check_trend,
    rsi_position,
)
from app.engine.trace import Trace

CFG = EngineConfig()


def frame(closes, start="2025-01-06 07:00"):
    rows = [
        [
            pd.Timestamp(start, tz="UTC") + pd.Timedelta(hours=i),
            c, c + 1, c - 1, c, 10,
        ]
        for i, c in enumerate(closes)
    ]
    return pd.DataFrame(rows, columns=["time_utc", "o", "h", "l", "c", "v"])


class TestTrend:
    def test_uptrend_sets_buy(self):
        df = frame([100 + i for i in range(60)])
        trace = Trace()
        assert check_trend(df, CFG, trace)
        assert trace.direction == "BUY"

    def test_downtrend_sets_sell(self):
        df = frame([160 - i for i in range(60)])
        trace = Trace()
        assert check_trend(df, CFG, trace)
        assert trace.direction == "SELL"

    def test_flat_insufficient_history(self):
        df = frame([100.0] * 10)
        trace = Trace()
        assert not check_trend(df, CFG, trace)


class TestRsi:
    def test_buy_window(self):
        df = frame(_zigzag(60, final_rsi=50))
        trace = Trace()
        trace.direction = "BUY"
        ok, val = check_rsi(df, CFG, trace)
        assert 40 <= val <= 65
        assert ok

    def test_sell_window_mismatch(self):
        df = frame(_zigzag(60, final_rsi=70))
        trace = Trace()
        trace.direction = "SELL"  # 70 outside SELL window 35-60
        ok, _ = check_rsi(df, CFG, trace)
        assert not ok


def _zigzag(n, final_rsi):
    """Closes whose RSI converges near `final_rsi` (rough, 0<->100 scaled)."""
    out = []
    price = 100.0
    for i in range(n):
        step = (final_rsi - 50) / 50.0 * 0.6
        price += step + (0.4 if i % 2 else -0.4) * 0.2
        out.append(price)
    return out


class TestAtrFilter:
    def test_range_two_passes_min(self):
        df = frame([100.0] * 30)  # h-l = 2.0 >= 0.8
        trace = Trace()
        ok, val = check_atr(df, CFG, trace)
        assert ok and val >= CFG.min_atr

    def test_tiny_range_fails(self):
        rows = [
            [
                pd.Timestamp("2025-01-06", tz="UTC") + pd.Timedelta(hours=i),
                100.0, 100.05, 99.95, 100.0, 10,
            ]
            for i in range(30)
        ]
        df = pd.DataFrame(rows, columns=["time_utc", "o", "h", "l", "c", "v"])
        trace = Trace()
        ok, _ = check_atr(df, CFG, trace)
        assert not ok


class TestSession:
    def test_london_window(self):
        trace = Trace()
        ok, name = check_session(datetime(2025, 1, 6, 10, 0, tzinfo=UTC), CFG, trace)
        assert ok and name == "london"

    def test_newyork_window(self):
        trace = Trace()
        # 18:00 UTC — london (7-16) closed, newyork (13-20) open
        ok, name = check_session(datetime(2025, 1, 6, 18, 0, tzinfo=UTC), CFG, trace)
        assert ok and name == "newyork"

    def test_asia_now_in_session(self):
        # D-047 — the Asian morning (03:00 UTC = 09:00 Dhaka) is IN-SESSION
        # via the Tokyo window; the old default silently blocked it.
        trace = Trace()
        ok, name = check_session(datetime(2025, 1, 6, 3, 0, tzinfo=UTC), CFG, trace)
        assert ok and name == "tokyo"

    def test_dead_zone_off_session(self):
        # the true dead zone is now only after the NY close (20-24 UTC)
        trace = Trace()
        ok, _ = check_session(datetime(2025, 1, 6, 21, 30, tzinfo=UTC), CFG, trace)
        assert not ok

    def test_custom_24h_session(self):
        cfg = EngineConfig(sessions=[SessionRule(name="allday", utc=(0, 24))])
        trace = Trace()
        ok, _ = check_session(datetime(2025, 1, 6, 3, 0, tzinfo=UTC), cfg, trace)
        assert ok

    def test_wrap_midnight_session(self):
        cfg = EngineConfig(sessions=[SessionRule(name="asia", utc=(22, 6))])
        trace = Trace()
        ok, _ = check_session(datetime(2025, 1, 6, 23, 0, tzinfo=UTC), cfg, trace)
        assert ok


class TestSpread:
    def test_within_max(self):
        trace = Trace()
        assert check_spread(2695.10, 2695.30, 0.01, CFG, trace)  # 20 pts

    def test_above_max_fails(self):
        trace = Trace()
        assert not check_spread(2695.00, 2695.60, 0.01, CFG, trace)  # 60 pts


class TestConfidenceParts:
    def test_weights_sum_to_one(self):
        # D-042: base weights + the ICT confluence block sum to one
        assert (
            W_TREND + W_MTF + W_TRIGGER + W_RSI + W_SESSION + W_ATR
            + W_CONFLUENCE
            == pytest.approx(1.0)
        )

    def test_rsi_position_centered_full(self):
        assert rsi_position(52.5, CFG, "BUY") == pytest.approx(1.0)  # mid of 40-65

    def test_rsi_position_edge_zero(self):
        assert rsi_position(40.0, CFG, "BUY") == pytest.approx(0.0)

    def test_atr_strength_double_full(self):
        assert atr_strength(0.3, CFG) == pytest.approx(1.0)  # 2x min_atr (0.15)

    def test_atr_strength_below_min_zero(self):
        assert atr_strength(0.075, CFG) == pytest.approx(0.25)


class TestMtfConfirm:
    def test_both_agree(self):
        up = frame([100 + i for i in range(60)])
        trace = Trace()
        trace.direction = "BUY"
        assert check_mtf({"M5": up, "M15": up}, "BUY", CFG, trace) == 2

    def test_conflict_reduces_agreement(self):
        up = frame([100 + i for i in range(60)])
        down = frame([160 - i for i in range(60)])
        trace = Trace()
        agreed = check_mtf({"M5": up, "M15": down}, "BUY", CFG, trace)
        assert agreed == 1

    def test_insufficient_history_counts_against(self):
        short = frame([100.0] * 10)
        trace = Trace()
        assert check_mtf({"M5": short, "M15": short}, "SELL", CFG, trace) == 0
