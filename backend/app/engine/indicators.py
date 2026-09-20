"""Pure indicator functions (Wilder-smoothed, matching MT5 terminal values).

All functions take ascending-by-time, closed-bars-only DataFrames with the
canonical RATES_COLUMNS and return the last value as a float (engine evaluates
on bar close only — SPEC §8.2).
"""

from __future__ import annotations

import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential moving average (pandas ewm: alpha=2/(period+1))."""
    return series.ewm(span=period, adjust=False).mean()


def rsi(closes: pd.Series, period: int = 14) -> float:
    """Wilder's RSI of the last bar. Returns 50.0 when undefined (flat market)."""
    if len(closes) < period + 1:
        return 50.0
    delta = closes.astype(float).diff()
    gains = delta.clip(lower=0.0)
    losses = (-delta).clip(lower=0.0)
    # Wilder smoothing == ewm with alpha = 1/period
    avg_gain = float(gains.ewm(alpha=1.0 / period, adjust=False).mean().iloc[-1])
    avg_loss = float(losses.ewm(alpha=1.0 / period, adjust=False).mean().iloc[-1])
    if avg_loss == 0.0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def true_range(df: pd.DataFrame) -> pd.Series:
    h, low, c = df["h"], df["l"], df["c"]
    prev_c = c.shift(1)
    return pd.concat([h - low, (h - prev_c).abs(), (low - prev_c).abs()], axis=1).max(axis=1)


def atr(df: pd.DataFrame, period: int = 14) -> float:
    """Wilder-smoothed ATR of the last closed bar. 0.0 when insufficient data."""
    if len(df) < period + 1:
        return 0.0
    tr = true_range(df)
    return float(tr.ewm(alpha=1.0 / period, adjust=False).mean().iloc[-1])


def atr_series(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Full ATR series (used by backtest/fixture crafting)."""
    tr = true_range(df)
    return tr.ewm(alpha=1.0 / period, adjust=False).mean()
