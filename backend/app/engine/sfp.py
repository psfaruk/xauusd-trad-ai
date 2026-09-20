"""SFP sweep detection (SPEC §8.2 rule 2) — pure functions, bar-close only.

BUY sweep: last closed M15 bar takes out the low of the previous `lookback`
bars, then closes back ABOVE that level, with a lower wick >= ratio * ATR(14).
SELL sweeps the highs (mirror).
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from app.engine.config import EngineConfig
from app.engine.indicators import atr
from app.engine.trace import Trace


@dataclass(frozen=True)
class SfpSignal:
    direction: str  # "BUY" | "SELL"
    entry: float  # close of the sweep bar
    sweep_extreme: float  # wick low (BUY) / wick high (SELL)
    swept_level: float  # prior min-low (BUY) / max-high (SELL)
    wick: float
    wick_ratio: float  # wick / atr
    atr: float


def detect_sfp(m15: pd.DataFrame, cfg: EngineConfig, trace: Trace) -> SfpSignal | None:
    """Check rule 2 for the direction implied by the H1 trend.

    `trace.direction` must already be set ("BUY"/"SELL") by check_trend.
    Returns None (with the trace entry appended) when no sweep.
    """
    direction = trace.direction
    if direction not in ("BUY", "SELL") or len(m15) < cfg.sfp_lookback + 1:
        trace.add("sfp_sweep", False, "insufficient history")
        return None

    last = m15.iloc[-1]
    prior = m15.iloc[-cfg.sfp_lookback - 1 : -1]  # `lookback` bars before `last`
    atr_val = atr(m15, cfg.atr_period)
    if atr_val <= 0:
        trace.add("sfp_sweep", False, "ATR undefined")
        return None

    o, h, low, c = float(last["o"]), float(last["h"]), float(last["l"]), float(last["c"])

    if direction == "BUY":
        swept = float(prior["l"].min())
        lower_wick = min(o, c) - low
        is_sweep = low < swept and c > swept and lower_wick >= cfg.sfp_wick_atr_ratio * atr_val
        if not is_sweep:
            trace.add(
                "sfp_sweep",
                False,
                f"low {low:.2f} vs min {swept:.2f}; wick {lower_wick:.2f} "
                f"< {cfg.sfp_wick_atr_ratio}*ATR({atr_val:.2f}) or close {c:.2f} not above swept",
            )
            return None
        trace.add(
            "sfp_sweep",
            True,
            f"low {low:.2f} swept min {swept:.2f}; wick {lower_wick:.2f} "
            f">= {cfg.sfp_wick_atr_ratio}*ATR({atr_val:.2f})",
        )
        return SfpSignal(
            direction="BUY",
            entry=c,
            sweep_extreme=low,
            swept_level=swept,
            wick=lower_wick,
            wick_ratio=lower_wick / atr_val,
            atr=atr_val,
        )

    swept = float(prior["h"].max())
    upper_wick = h - max(o, c)
    is_sweep = h > swept and c < swept and upper_wick >= cfg.sfp_wick_atr_ratio * atr_val
    if not is_sweep:
        trace.add(
            "sfp_sweep",
            False,
            f"high {h:.2f} vs max {swept:.2f}; wick {upper_wick:.2f} "
            f"< {cfg.sfp_wick_atr_ratio}*ATR({atr_val:.2f}) or close {c:.2f} not below swept",
        )
        return None
    trace.add(
        "sfp_sweep",
        True,
        f"high {h:.2f} swept max {swept:.2f}; wick {upper_wick:.2f} "
        f">= {cfg.sfp_wick_atr_ratio}*ATR({atr_val:.2f})",
    )
    return SfpSignal(
        direction="SELL",
        entry=c,
        sweep_extreme=h,
        swept_level=swept,
        wick=upper_wick,
        wick_ratio=upper_wick / atr_val,
        atr=atr_val,
    )


def sfp_quality(sig: SfpSignal, cfg: EngineConfig) -> float:
    """0..1 confidence contribution of the sweep (wick_ratio vs threshold).

    wick at threshold -> 0.0, at 2x threshold -> 1.0 (clamped).
    """
    thr = cfg.sfp_wick_atr_ratio
    return max(0.0, min(1.0, (sig.wick_ratio - thr) / thr))


def build_levels(sig: SfpSignal, cfg: EngineConfig) -> tuple[float, float, float]:
    """(entry, sl, tp) per SPEC §8.2 levels."""
    entry = sig.entry
    if sig.direction == "BUY":
        sl = sig.sweep_extreme - cfg.sl_buffer_atr * sig.atr
        risk = entry - sl
        tp = entry + cfg.rr * risk
    else:
        sl = sig.sweep_extreme + cfg.sl_buffer_atr * sig.atr
        risk = sl - entry
        tp = entry - cfg.rr * risk
    return entry, sl, tp
