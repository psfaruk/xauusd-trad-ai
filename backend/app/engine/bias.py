"""Direction bias (D-049) — multi-source verdict, one number.

User directive: "বিভিন্ন সোর্স থেকে দেখে দেখুন, মার্কেট এর কোন সেটাপ এ কি
এন্ট্রি নেওয়ার উচিত! Buy order নাকি sell order" — the direction must be
read from SEVERAL independent sources, not one lagging EMA cross.

The D-041..D-048 engine let a single H1 EMA(50) cross dictate EVERY signal
direction: while gold sat below H1 EMA50 the engine could only SELL —
demand-zone BUYs needed counter_trend_quality 0.70 (practically
unreachable), so the platform printed sell after sell straight into a
mature downtrend. D-049 replaces that with a weighted vote:

    0.30  H1 close vs EMA(trend_ema)          — the classic trend read
    0.20  H4 market structure (BOS/CHoCH)     — the big-frame reality
    0.20  H1 market structure                 — the operating frame
    0.20  M15 market structure                — the tactical frame
    0.10  H1 momentum (EMA fast vs slow)      — acceleration

score in [-1, +1]:
    >= +BIAS_THRESHOLD   -> "BUY"   (trend-following triggers may buy)
    <= -BIAS_THRESHOLD   -> "SELL"
    in between           -> "NEUTRAL" (zone-retest entries only — the
                            location itself carries the trade; both a
                            demand BUY and a supply SELL remain valid)

Pure function on closed frames only — no lookahead, no I/O.
"""

from __future__ import annotations

import pandas as pd

from app.analysis import smc
from app.engine.config import EngineConfig
from app.engine.trace import Trace

#: |score| at or beyond which the verdict is directional
BIAS_THRESHOLD = 0.30

#: hard-coded vote weights (sum = 1.0)
W_TREND_EMA = 0.30
W_STRUCT: dict[str, float] = {"H4": 0.20, "H1": 0.20, "M15": 0.20}
W_MOMENTUM = 0.10


def _structure_vote(frame: pd.DataFrame | None) -> float:
    """-1 | 0 | +1 from the frame's market-structure trend."""
    if frame is None or len(frame) < 25:
        return 0.0
    try:
        st = smc.detect_structure(frame.iloc[-160:])
    except Exception:  # noqa: BLE001 — structure must never break the engine
        return 0.0
    trend = st.get("trend", "balanced")
    if trend == "bullish":
        return 1.0
    if trend == "bearish":
        return -1.0
    return 0.0


def _ema_vote(frame: pd.DataFrame | None, period: int) -> float:
    """-1 | 0 | +1 — close vs EMA(period) on the trend frame."""
    if frame is None or len(frame) < period + 1:
        return 0.0
    from app.engine.indicators import ema

    close = float(frame["c"].iloc[-1])
    ema_val = float(ema(frame["c"], period).iloc[-1])
    if close > ema_val:
        return 1.0
    if close < ema_val:
        return -1.0
    return 0.0


def _momentum_vote(frame: pd.DataFrame | None, fast: int, slow: int) -> float:
    """-1 | 0 | +1 — EMA(fast) vs EMA(slow) slope alignment on H1."""
    if frame is None or len(frame) < slow + 2:
        return 0.0
    from app.engine.indicators import ema

    closes = frame["c"]
    ema_f = float(ema(closes, fast).iloc[-1])
    ema_s = float(ema(closes, slow).iloc[-1])
    if ema_f > ema_s:
        return 1.0
    if ema_f < ema_s:
        return -1.0
    return 0.0


def direction_bias(
    htf: dict[str, pd.DataFrame],
    cfg: EngineConfig,
    trace: Trace | None = None,
) -> tuple[str, float, list[str]]:
    """("BUY"|"SELL"|"NEUTRAL", score, notes) — the multi-source verdict.

    `trace.direction` is NOT touched here — the caller decides how the
    verdict maps onto triggers (D-049: trend-following triggers need a
    directional verdict; POI zone entries work in all three states).
    """
    notes: list[str] = []
    score = 0.0

    trend_frame = htf.get(cfg.trend_tf)
    ema_v = _ema_vote(trend_frame, cfg.trend_ema)
    score += W_TREND_EMA * ema_v
    if trend_frame is not None and len(trend_frame) >= cfg.trend_ema + 1:
        close = float(trend_frame["c"].iloc[-1])
        from app.engine.indicators import ema

        ema_val = float(ema(trend_frame["c"], cfg.trend_ema).iloc[-1])
        notes.append(
            f"{cfg.trend_tf} close {close:.2f} "
            f"{'>' if ema_v > 0 else ('<' if ema_v < 0 else '=')}"
            f" EMA{cfg.trend_ema} {ema_val:.2f}"
        )
    else:
        notes.append(f"{cfg.trend_tf} insufficient history")

    for tf, w in W_STRUCT.items():
        frame = htf.get(tf)
        v = _structure_vote(frame)
        score += w * v
        trend_txt = "bullish" if v > 0 else ("bearish" if v < 0 else "balanced/none")
        notes.append(f"{tf} structure {trend_txt}")

    mom_v = _momentum_vote(trend_frame, cfg.ema_fast, cfg.ema_slow)
    score += W_MOMENTUM * mom_v
    notes.append(
        f"{cfg.trend_tf} momentum {'up' if mom_v > 0 else ('down' if mom_v < 0 else 'flat')}"
    )

    if score >= BIAS_THRESHOLD:
        verdict = "BUY"
    elif score <= -BIAS_THRESHOLD:
        verdict = "SELL"
    else:
        verdict = "NEUTRAL"

    if trace is not None:
        trace.add(
            "direction_bias",
            True,
            f"{verdict} (score {score:+.2f}) — " + "; ".join(notes),
        )
    return verdict, round(score, 3), notes


def bias_for_zone(zone_direction: str | None, verdict: str) -> bool:
    """Is a zone-retest direction counter-trend under this verdict?

    NEUTRAL never counts as counter-trend — both sides trade at the
    with-trend quality gate (the zone location carries the trade).
    """
    if zone_direction is None or verdict == "NEUTRAL":
        return False
    return zone_direction != verdict
