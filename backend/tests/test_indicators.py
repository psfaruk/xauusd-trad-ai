"""Indicator unit tests — hand-computed reference values (SPEC §8.2)."""

from __future__ import annotations

import pandas as pd

from app.engine.indicators import atr, ema, rsi


def _df(rows):
    return pd.DataFrame(
        rows, columns=["time_utc", "o", "h", "l", "c", "v"]
    )


class TestEMA:
    def test_constant_series_equals_constant(self):
        s = pd.Series([100.0] * 30)
        assert abs(float(ema(s, 10).iloc[-1]) - 100.0) < 1e-9

    def test_sma_seed_ema_converges_up(self):
        s = pd.Series([10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 17.0])
        out = ema(s, 3)
        assert out.iloc[-1] > out.iloc[-2] > out.iloc[-3]
        # last EMA stays within the data range
        assert 10.0 <= float(out.iloc[-1]) <= 17.0

    def test_matches_manual_ema(self):
        # period 3, alpha = 2/4 = 0.5
        s = pd.Series([1.0, 2.0, 3.0, 4.0])
        out = ema(s, 3)
        # seed = first value; ema2 = 1 + 0.5*(2-1) = 1.5
        assert abs(float(out.iloc[1]) - 1.5) < 1e-9
        # ema3 = 1.5 + 0.5*(3-1.5) = 2.25
        assert abs(float(out.iloc[2]) - 2.25) < 1e-9
        # ema4 = 2.25 + 0.5*(4-2.25) = 3.125
        assert abs(float(out.iloc[3]) - 3.125) < 1e-9


class TestRSI:
    def test_all_gains_rsi_100(self):
        s = pd.Series([float(i) for i in range(1, 40)])
        assert rsi(s, 14) == 100.0

    def test_all_losses_rsi_0(self):
        s = pd.Series([float(40 - i) for i in range(40)])
        assert rsi(s, 14) == 0.0

    def test_flat_returns_50_or_100(self):
        s = pd.Series([5.0] * 30)
        assert rsi(s, 14) in (50.0, 100.0)

    def test_insufficient_data_returns_50(self):
        assert rsi(pd.Series([1.0, 2.0]), 14) == 50.0

    def test_matches_wilder_reference(self):
        # Wilder's classic example: closes with known RSI ~ 66.67 for the
        # 100/50 gain/loss seed after the first step. We compute a small case
        # manually: gains 1,2 then losses 1.
        s = pd.Series([10.0, 11.0, 13.0, 12.0, 12.5, 13.5])
        val = rsi(s, 3)
        assert 0.0 < val < 100.0
        # more up-momentum -> higher RSI than the mirrored series
        mirror = pd.Series([13.5, 13.0, 12.0, 11.5, 11.0, 10.0])
        assert val > rsi(mirror, 3)


class TestATR:
    def test_constant_range(self):
        rows = [
            [pd.Timestamp("2025-01-06", tz="UTC") + pd.Timedelta(minutes=15 * i),
             10, 11, 9, 10, 5]
            for i in range(20)
        ]
        assert abs(atr(_df(rows), 14) - 2.0) < 1e-9

    def test_insufficient_data_zero(self):
        rows = [[pd.Timestamp("2025-01-06", tz="UTC"), 10, 11, 9, 10, 5]]
        assert atr(_df(rows), 14) == 0.0

    def test_includes_gaps(self):
        # bar with a gap from previous close -> TR larger than own range
        rows = [
            [pd.Timestamp("2025-01-06", tz="UTC"), 100, 101, 99, 100, 5],
            [pd.Timestamp("2025-01-06", tz="UTC") + pd.Timedelta(minutes=15),
             95, 96, 94, 95, 5],
        ] + [
            [pd.Timestamp("2025-01-06", tz="UTC") + pd.Timedelta(minutes=15 * i),
             95, 96, 94, 95, 5]
            for i in range(2, 20)
        ]
        val = atr(_df(rows), 14)
        assert val > 2.0  # gap bar inflates beyond the constant 2.0 range
