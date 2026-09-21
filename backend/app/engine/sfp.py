"""Pattern detection (SPEC §8.2 rule 2 + D-041 pullback) — pure, closed bars.

Triggers (D-041 dual-trigger, direction fixed by the H1 trend):

1. SFP sweep  — last closed bar takes out the prior `lookback` extreme, then
   closes back inside, with a rejection wick >= ratio * ATR(14).
2. Pullback   — in a trending tape the price pulls back to the fast EMA and
   the last closed bar prints a rejection candle back in trend direction
   (wick + body position quality-scored). The classic "trend + pullback"
   M1 entry.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from app.engine.config import EngineConfig
from app.engine.indicators import atr, ema
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


@dataclass(frozen=True)
class PullbackSignal:
    """D-041 pullback trigger — trend-pullback rejection candle on the base TF."""

    direction: str
    entry: float  # close of the trigger bar
    anchor: float  # EMA(ema_fast) the bar rejected from
    swing_extreme: float  # min low of the last 3 bars (BUY) / max high (SELL)
    wick_ratio: float  # rejection wick / bar range (0..1)
    body_pos: float  # close position inside the bar range (0..1, 1 = at high)
    atr: float


def detect_sfp(base: pd.DataFrame, cfg: EngineConfig, trace: Trace) -> SfpSignal | None:
    """Sweep check for the direction implied by the H1 trend.

    `base` = closed bars of the engine timeframe (M1 by default, D-041).
    `trace.direction` must already be set ("BUY"/"SELL") by check_trend.
    Returns None (with the trace entry appended) when no sweep.
    """
    direction = trace.direction
    if direction not in ("BUY", "SELL") or len(base) < cfg.sfp_lookback + 1:
        trace.add("sfp_sweep", False, "insufficient history")
        return None

    last = base.iloc[-1]
    prior = base.iloc[-cfg.sfp_lookback - 1 : -1]  # `lookback` bars before `last`
    atr_val = atr(base, cfg.atr_period)
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
    """(entry, sl, tp) per SPEC §8.2 levels + the D-041 min-SL ATR floor.

    The floor keeps the stop at least `min_sl_atr`*ATR from the entry so the
    spread + M1 noise can't eat the whole risk distance.
    """
    entry = sig.entry
    if sig.direction == "BUY":
        sl = min(
            sig.sweep_extreme - cfg.sl_buffer_atr * sig.atr,
            entry - cfg.min_sl_atr * sig.atr,
        )
        risk = entry - sl
        tp = entry + cfg.rr * risk
    else:
        sl = max(
            sig.sweep_extreme + cfg.sl_buffer_atr * sig.atr,
            entry + cfg.min_sl_atr * sig.atr,
        )
        risk = sl - entry
        tp = entry - cfg.rr * risk
    return entry, sl, tp


# ---------------------------------------------------------------- D-041 pullback


def detect_pullback(
    base: pd.DataFrame, cfg: EngineConfig, trace: Trace
) -> PullbackSignal | None:
    """Trend-pullback rejection candle on the base TF (D-041 trigger B).

    BUY (H1 uptrend):
    - EMA(ema_fast) still RISING over the last 3 bars (healthy trend),
    - the trigger bar's LOW trades at/below EMA(ema_fast) (the pullback),
    - the bar closes back ABOVE the EMA with a rejection lower-wick
      >= `pullback_wick_ratio` of the bar range and the close in the upper
      half of the range (the rejection),
    - bar range >= `pullback_min_range_atr` * ATR (doji filter).
    SELL mirrors everything.
    """
    direction = trace.direction
    if direction not in ("BUY", "SELL"):
        trace.add("pullback", False, "no trend direction")
        return None
    need = max(cfg.ema_fast, cfg.atr_period) + 4
    if len(base) < need:
        trace.add("pullback", False, "insufficient history")
        return None

    last = base.iloc[-1]
    recent = base.iloc[-4:-1]  # the 3 bars before the trigger
    atr_val = atr(base, cfg.atr_period)
    if atr_val <= 0:
        trace.add("pullback", False, "ATR undefined")
        return None

    o, h, low, c = float(last["o"]), float(last["h"]), float(last["l"]), float(last["c"])
    rng = h - low
    if rng <= 0 or rng < cfg.pullback_min_range_atr * atr_val:
        trace.add(
            "pullback", False,
            f"bar range {rng:.2f} < {cfg.pullback_min_range_atr}*ATR({atr_val:.2f}) — doji",
        )
        return None

    ema_series = ema(base["c"], cfg.ema_fast)
    ema_now = float(ema_series.iloc[-1])
    ema_prev = float(ema_series.iloc[-3])
    body_pos = (c - low) / rng  # 0 = at low, 1 = at high
    lower_wick = min(o, c) - low
    upper_wick = h - max(o, c)

    if direction == "BUY":
        rising = ema_now > ema_prev
        touched = low <= ema_now
        rejected = (
            c > ema_now
            and lower_wick >= cfg.pullback_wick_ratio * rng
            and body_pos >= 0.5
        )
        pull_depth = min(recent["l"].astype(float)) <= ema_now or touched
        if not (rising and pull_depth and rejected):
            trace.add(
                "pullback", False,
                f"no rejection: ema rising={rising}, pullback to"
                f" EMA{cfg.ema_fast}={pull_depth}, wick {lower_wick / rng:.0%}"
                f" (need {cfg.pullback_wick_ratio:.0%}), close pos {body_pos:.0%}",
            )
            return None
        trace.add(
            "pullback", True,
            f"BUY pullback: low {low:.2f} into rising EMA{cfg.ema_fast} {ema_now:.2f}; "
            f"wick {lower_wick / rng:.0%} of range, closed {body_pos:.0%} up",
        )
        return PullbackSignal(
            direction="BUY",
            entry=c,
            anchor=ema_now,
            swing_extreme=float(base.iloc[-3:]["l"].astype(float).min()),
            wick_ratio=lower_wick / rng,
            body_pos=body_pos,
            atr=atr_val,
        )

    falling = ema_now < ema_prev
    touched = h >= ema_now
    rejected = (
        c < ema_now
        and upper_wick >= cfg.pullback_wick_ratio * rng
        and body_pos <= 0.5
    )
    pull_depth = max(recent["h"].astype(float)) >= ema_now or touched
    if not (falling and pull_depth and rejected):
        trace.add(
            "pullback", False,
            f"no rejection: ema falling={falling}, pullback to"
            f" EMA{cfg.ema_fast}={pull_depth}, wick {upper_wick / rng:.0%}"
            f" (need {cfg.pullback_wick_ratio:.0%}), close pos {body_pos:.0%}",
        )
        return None
    trace.add(
        "pullback", True,
        f"SELL pullback: high {h:.2f} into falling EMA{cfg.ema_fast} {ema_now:.2f}; "
        f"wick {upper_wick / rng:.0%} of range, closed {1 - body_pos:.0%} down",
    )
    return PullbackSignal(
        direction="SELL",
        entry=c,
        anchor=ema_now,
        swing_extreme=float(base.iloc[-3:]["h"].astype(float).max()),
        wick_ratio=upper_wick / rng,
        body_pos=body_pos,
        atr=atr_val,
    )


def pullback_quality(sig: PullbackSignal) -> float:
    """0..1 confidence contribution of the rejection candle."""
    return max(0.0, min(1.0, 0.6 * sig.wick_ratio + 0.4 * sig.body_pos))


def build_levels_pullback(
    sig: PullbackSignal, cfg: EngineConfig
) -> tuple[float, float, float]:
    """(entry, sl, tp) — SL beyond the micro-swing, floored at min_sl_atr."""
    entry = sig.entry
    if sig.direction == "BUY":
        sl = min(
            sig.swing_extreme - cfg.sl_buffer_atr * sig.atr,
            entry - cfg.min_sl_atr * sig.atr,
        )
        risk = entry - sl
        tp = entry + cfg.rr * risk
    else:
        sl = max(
            sig.swing_extreme + cfg.sl_buffer_atr * sig.atr,
            entry + cfg.min_sl_atr * sig.atr,
        )
        risk = sl - entry
        tp = entry - cfg.rr * risk
    return entry, sl, tp
