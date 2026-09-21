"""Classic indicator suite (D-042) — MT5/TradingView-equivalent math.

Every function takes an ascending closed-bars DataFrame with
RATES_COLUMNS ([time_utc, o, h, l, c, v]) and returns either a Series
(aligned to the input index) or a plain dict of the LAST bar's values.
Wilder smoothing (alpha = 1/period) is used for RSI/ATR/ADX so the
numbers match the MetaTrader 5 terminal within rounding.
"""

from __future__ import annotations

import math

import pandas as pd

# ------------------------------------------------------------------ helpers


def ema(series: pd.Series, period: int) -> pd.Series:
    """EMA with alpha = 2/(period+1) (TradingView/MT5 default)."""
    return series.ewm(span=period, adjust=False).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period, min_periods=period).mean()


def true_range(df: pd.DataFrame) -> pd.Series:
    h, low, c = df["h"], df["l"], df["c"]
    prev_c = c.shift(1)
    return pd.concat(
        [h - low, (h - prev_c).abs(), (low - prev_c).abs()], axis=1
    ).max(axis=1)


def atr_series(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder-smoothed ATR series (NaN for the first bar)."""
    return true_range(df).ewm(alpha=1.0 / period, adjust=False).mean()


def atr(df: pd.DataFrame, period: int = 14) -> float:
    """Last-bar ATR value (0.0 with insufficient data)."""
    if len(df) < period + 1:
        return 0.0
    return float(atr_series(df, period).iloc[-1])


# ---------------------------------------------------------------- momentum


def rsi_series(closes: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI series (50 where undefined, 100 on pure gains)."""
    delta = closes.astype(float).diff()
    gains = delta.clip(lower=0.0)
    losses = (-delta).clip(lower=0.0)
    avg_gain = gains.ewm(alpha=1.0 / period, adjust=False).mean()
    avg_loss = losses.ewm(alpha=1.0 / period, adjust=False).mean()
    rs = avg_gain / avg_loss          # inf on pure gains, nan on flat
    out = 100.0 - 100.0 / (1.0 + rs)  # inf -> 100 naturally
    return out.fillna(50.0).clip(0.0, 100.0)


def macd(closes: pd.Series, fast: int = 12, slow: int = 26,
         signal: int = 9) -> dict:
    """Last-bar MACD: {macd, signal, hist, hist_prev}."""
    if len(closes) < slow + signal:
        return {"macd": 0.0, "signal": 0.0, "hist": 0.0, "hist_prev": 0.0}
    m = ema(closes, fast) - ema(closes, slow)
    s = m.ewm(span=signal, adjust=False).mean()
    hist = m - s
    return {
        "macd": float(m.iloc[-1]),
        "signal": float(s.iloc[-1]),
        "hist": float(hist.iloc[-1]),
        "hist_prev": float(hist.iloc[-2]),
    }


def stochastic(df: pd.DataFrame, k_period: int = 14, d_period: int = 3,
               smooth: int = 3) -> dict:
    """Last-bar Stochastic: {k, d} (0..100)."""
    if len(df) < k_period + smooth + d_period:
        return {"k": 50.0, "d": 50.0}
    low_k = df["l"].rolling(k_period).min()
    high_k = df["h"].rolling(k_period).max()
    rng = (high_k - low_k).replace(0.0, math.nan)
    raw = ((df["c"] - low_k) / rng * 100.0).fillna(50.0)
    k = raw.rolling(smooth).mean()
    d = k.rolling(d_period).mean()
    return {"k": float(k.iloc[-1]), "d": float(d.iloc[-1])}


def cci(df: pd.DataFrame, period: int = 20) -> float:
    """Commodity Channel Index of the last bar."""
    if len(df) < period:
        return 0.0
    tp = (df["h"] + df["l"] + df["c"]) / 3.0
    ma = tp.rolling(period).mean()
    md = (tp - ma).abs().rolling(period).mean()
    out = (tp - ma) / (0.015 * md.replace(0.0, math.nan))
    return float(out.fillna(0.0).iloc[-1])


def momentum(closes: pd.Series, period: int = 10) -> float:
    """Rate of change of the last `period` bars (fraction of price)."""
    if len(closes) <= period:
        return 0.0
    prev = float(closes.iloc[-period - 1])
    return 0.0 if prev == 0 else float(closes.iloc[-1]) / prev - 1.0


# -------------------------------------------------------------- volatility


def bollinger(closes: pd.Series, period: int = 20, mult: float = 2.0) -> dict:
    """Last-bar Bollinger Bands: {upper, mid, lower, width, pct_b}."""
    if len(closes) < period:
        mid = float(closes.iloc[-1]) if len(closes) else 0.0
        return {"upper": mid, "mid": mid, "lower": mid, "width": 0.0,
                "pct_b": 0.5}
    mid_s = sma(closes, period)
    std_s = closes.rolling(period).std(ddof=0)
    upper, lower = mid_s + mult * std_s, mid_s - mult * std_s
    mid, up, lo = float(mid_s.iloc[-1]), float(upper.iloc[-1]), float(lower.iloc[-1])
    rng = up - lo
    last = float(closes.iloc[-1])
    return {
        "upper": up,
        "mid": mid,
        "lower": lo,
        "width": rng,
        "pct_b": 0.5 if rng <= 0 else (last - lo) / rng,
    }


def adx(df: pd.DataFrame, period: int = 14) -> dict:
    """Last-bar ADX + DI lines: {adx, plus_di, minus_di}."""
    need = period * 2 + 2
    if len(df) < need:
        return {"adx": 0.0, "plus_di": 0.0, "minus_di": 0.0}
    h, low = df["h"], df["l"]
    up_move = h.diff()
    down_move = -low.diff()
    plus_dm = pd.Series(
        (up_move > down_move) & (up_move > 0), index=df.index
    ) * up_move
    minus_dm = pd.Series(
        (down_move > up_move) & (down_move > 0), index=df.index
    ) * down_move
    tr = true_range(df)
    atr_s = tr.ewm(alpha=1.0 / period, adjust=False).mean()
    pdi = 100.0 * plus_dm.ewm(alpha=1.0 / period, adjust=False).mean() / atr_s
    mdi = 100.0 * minus_dm.ewm(alpha=1.0 / period, adjust=False).mean() / atr_s
    dx = 100.0 * (pdi - mdi).abs() / (pdi + mdi).replace(0.0, math.nan)
    adx_s = dx.fillna(0.0).ewm(alpha=1.0 / period, adjust=False).mean()
    return {
        "adx": float(adx_s.iloc[-1]),
        "plus_di": float(pdi.iloc[-1]),
        "minus_di": float(mdi.iloc[-1]),
    }


# -------------------------------------------------------------------- volume


def obv(df: pd.DataFrame) -> pd.Series:
    """On-Balance Volume series."""
    direction = df["c"].diff().fillna(0.0).apply(lambda x: 1 if x > 0 else (-1 if x < 0 else 0))
    return (direction * df["v"].astype(float)).cumsum()


def vwap(df: pd.DataFrame, period: int = 0) -> float:
    """Rolling VWAP of the last `period` bars (0 = session-anchored).

    Anchored per UTC day when period == 0 (session VWAP, the TradingView
    default): the accumulation covers the bars of the LAST UTC day only.

    D-043 FIX: the old session branch recursed as `vwap(df[mask], period=0)`
    — the SAME call, forever (RecursionError on every /api/analysis request
    — the "analysis not running" report). Now the day-window is computed
    inline and shared with the rolling branch.
    """
    if len(df) == 0:
        return 0.0
    if period > 0:
        window = df.iloc[-period:]
    elif "time_utc" in df:
        day = df["time_utc"].iloc[-1].date()
        window = df[[t.date() == day for t in df["time_utc"]]]
        if window.empty:  # defensive — the last bar's day is always present
            window = df.iloc[-1:]
    else:
        window = df
    tp = (window["h"] + window["l"] + window["c"]) / 3.0
    vol = window["v"].astype(float)
    total = float(vol.sum())
    return float((tp * vol).sum() / total) if total > 0 else float(df["c"].iloc[-1])


def volume_zscore(df: pd.DataFrame, lookback: int = 60) -> float:
    """Z-score of the LAST bar's volume vs the trailing window."""
    if len(df) < 5:
        return 0.0
    window = df["v"].astype(float).iloc[-(lookback + 1):-1]
    if len(window) < 5:
        return 0.0
    mu = float(window.mean())
    sigma = float(window.std(ddof=0))
    if sigma <= 0:
        return 0.0
    return (float(df["v"].iloc[-1]) - mu) / sigma


# ------------------------------------------------------------ price structure


def swings(df: pd.DataFrame, left: int = 2, right: int = 2) -> list[dict]:
    """Fractal swing points (confirmed `right` bars later — no lookahead).

    Returns [{t, price, kind: "high"|"low", i}] ascending by bar index.
    """
    out: list[dict] = []
    h, low = df["h"].values, df["l"].values
    n = len(df)
    need = left + right + 1
    if n < need:
        return out
    times = df["time_utc"].iloc[left: n - right]
    for k in range(left, n - right):
        hi_ok = all(h[k] >= h[k - j] for j in range(1, left + 1)) and all(
            h[k] >= h[k + j] for j in range(1, right + 1)
        )
        lo_ok = all(low[k] <= low[k - j] for j in range(1, left + 1)) and all(
            low[k] <= low[k + j] for j in range(1, right + 1)
        )
        # D-043 FIX — dedupe ADJACENT equal prints: two neighbouring bars
        # with the exact same high (or low) both qualify as fractals, which
        # used to emit duplicate swing points. detect_structure then
        # labelled the duplicate pair "LH"/"LL" (equal price never counts
        # as a higher print) — flipping real bullish structure to bearish.
        # Equal prints SEPARATED in time stay (they are the BSL/SSL pools).
        if hi_ok:
            dup = out and out[-1]["kind"] == "high" and out[-1]["i"] >= k - 1 \
                and out[-1]["price"] == float(h[k])
            if not dup:
                out.append({"t": times.iloc[k - left], "price": float(h[k]),
                            "kind": "high", "i": k})
        if lo_ok:
            dup = out and out[-1]["kind"] == "low" and out[-1]["i"] >= k - 1 \
                and out[-1]["price"] == float(low[k])
            if not dup:
                out.append({"t": times.iloc[k - left], "price": float(low[k]),
                            "kind": "low", "i": k})
    return out
