"""Strategy filter checks (SPEC §8.2 rules 1, 3-7) — pure/async helpers.

Every check appends a CheckResult to the trace with a human-readable value so
the frontend TraceList can render ✓/✗ with real numbers (SPEC §8.3).
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from app.engine.config import EngineConfig
from app.engine.indicators import atr, ema, rsi
from app.engine.trace import Trace

# Confidence weights (SPEC §8.2, D-041 rebalance) — pass-score contributions.
W_TREND = 0.25
W_MTF = 0.20
W_TRIGGER = 0.25
W_RSI = 0.15
W_SESSION = 0.05
W_ATR = 0.10


def check_mtf(
    frames: dict[str, pd.DataFrame],
    direction: str,
    cfg: EngineConfig,
    trace: Trace,
) -> int:
    """D-041 rule 1b — multi-timeframe confirmation.

    Every confirm TF frame (closed bars only) must sit on the same side of
    its EMA(trend_ema) as the trade direction. Returns how many agreed;
    the caller hard-gates on `cfg.min_tf_agree`.
    """
    agreed = 0
    for tf in cfg.confirm_tfs:
        df = frames.get(tf)
        if df is None or len(df) < cfg.trend_ema + 1:
            trace.add(f"mtf_{tf.lower()}", False, "insufficient history")
            continue
        close = float(df["c"].iloc[-1])
        ema_val = float(ema(df["c"], cfg.trend_ema).iloc[-1])
        ok = close > ema_val if direction == "BUY" else close < ema_val
        if ok:
            agreed += 1
        trace.add(
            f"mtf_{tf.lower()}",
            ok,
            f"{tf} close {close:.2f} {'>' if direction == 'BUY' else '<'}"
            f" EMA{cfg.trend_ema} {ema_val:.2f} — {'agrees' if ok else 'conflicts'}",
        )
    return agreed


def check_trend(h1: pd.DataFrame, cfg: EngineConfig, trace: Trace) -> bool:
    """Rule 1 — H1 close vs EMA(trend_ema). Sets trace.direction."""
    if len(h1) < cfg.trend_ema + 1:
        trace.add("trend_h1", False, "insufficient H1 history")
        return False
    close = float(h1["c"].iloc[-1])
    ema_val = float(ema(h1["c"], cfg.trend_ema).iloc[-1])
    if close > ema_val:
        trace.direction = "BUY"
        trace.add("trend_h1", True, f"close {close:.2f} > EMA{cfg.trend_ema} {ema_val:.2f}")
        return True
    if close < ema_val:
        trace.direction = "SELL"
        trace.add("trend_h1", True, f"close {close:.2f} < EMA{cfg.trend_ema} {ema_val:.2f}")
        return True
    trace.add("trend_h1", False, f"close {close:.2f} == EMA{cfg.trend_ema} {ema_val:.2f}")
    return False


def check_rsi(base: pd.DataFrame, cfg: EngineConfig, trace: Trace) -> tuple[bool, float]:
    """Rule 3 — RSI(14) window per direction. Returns (pass, rsi_value)."""
    value = rsi(base["c"], cfg.rsi_period)
    lo, hi = (
        (cfg.rsi_buy_min, cfg.rsi_buy_max)
        if trace.direction == "BUY"
        else (cfg.rsi_sell_min, cfg.rsi_sell_max)
    )
    passed = lo <= value <= hi
    trace.add("rsi", passed, f"{value:.1f} (window {lo:.0f}-{hi:.0f})")
    return passed, value


def check_atr(base: pd.DataFrame, cfg: EngineConfig, trace: Trace) -> tuple[bool, float]:
    """Rule 4 — ATR(14) >= min_atr. Returns (pass, atr_value)."""
    value = atr(base, cfg.atr_period)
    passed = value >= cfg.min_atr
    trace.add("atr", passed, f"{value:.2f} (min {cfg.min_atr})")
    return passed, value


def check_session(bar_time_utc: datetime, cfg: EngineConfig, trace: Trace) -> tuple[bool, str]:
    """Rule 5 — bar time inside a configured UTC session window."""
    name = cfg.session_for(bar_time_utc.hour)
    passed = name is not None
    trace.add("session", passed, name or f"off-session ({bar_time_utc:%H:%M} UTC)")
    return passed, name or "none"


async def check_news(
    news_service, now_utc: datetime, cfg: EngineConfig, trace: Trace
) -> bool:
    """Rule 6 — no high-impact USD event within ±news_blackout_min.

    Graceful degradation (SPEC C6 / §8.2): calendar unavailable -> check is
    skipped (passes) with a warning value in the trace.
    """
    window = timedelta(minutes=cfg.news_blackout_min)
    try:
        events = await news_service.usd_high_impact(now_utc - window, now_utc + window)
    except Exception:  # noqa: BLE001 — external API must never break the engine
        trace.add("news", True, "calendar unavailable — check skipped (warning)")
        return True
    if events is None:
        trace.add("news", True, "calendar unavailable — check skipped (warning)")
        return True
    if not events:
        # also report distance to the next event for the trace value
        upcoming = await news_service.next_usd_high_impact(now_utc)
        trace.add(
            "news",
            True,
            f"next USD event in {upcoming}" if upcoming else "no upcoming USD events",
        )
        return True
    nearest = min(abs((e["time"] - now_utc).total_seconds()) / 60.0 for e in events)
    trace.add(
        "news", False,
        f"high-impact USD event in {nearest:.0f} min (< {cfg.news_blackout_min})",
    )
    return False


def check_spread(
    bid: float, ask: float, point_size: float, cfg: EngineConfig, trace: Trace
) -> bool:
    """Rule 7b — spread <= max_spread_points. `point` for XAUUSD = 0.01 (2 digits)."""
    if point_size <= 0:
        point_size = 0.01
    points = (ask - bid) / point_size
    passed = points <= cfg.max_spread_points
    trace.add("spread", passed, f"{points:.0f} points (max {cfg.max_spread_points})")
    return passed


def rsi_position(value: float, cfg: EngineConfig, direction: str) -> float:
    """0..1 — how well-centered RSI sits inside its window (confidence weight)."""
    lo, hi = (
        (cfg.rsi_buy_min, cfg.rsi_buy_max)
        if direction == "BUY"
        else (cfg.rsi_sell_min, cfg.rsi_sell_max)
    )
    mid, half = (lo + hi) / 2.0, max((hi - lo) / 2.0, 1e-9)
    return max(0.0, min(1.0, 1.0 - abs(value - mid) / half))


def atr_strength(atr_value: float, cfg: EngineConfig) -> float:
    """0..1 — ATR vs min_atr (2x min_atr -> full score)."""
    if cfg.min_atr <= 0:
        return 1.0
    return max(0.0, min(1.0, atr_value / (2.0 * cfg.min_atr)))
