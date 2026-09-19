"""SignalEngine (SPEC §8.5) — bar-close driven SFP evaluation.

The pure pipeline `evaluate()` is shared by the live engine AND the backtest
runner so both paths apply exactly the same rules (single source of truth).
The live engine additionally runs the async news check, handles cooldown /
active-state, persistence and WS broadcasts.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd

from app.engine.config import EngineConfig
from app.engine.filters import (
    W_ATR,
    W_RSI,
    W_SESSION,
    W_SFP,
    W_TREND,
    atr_strength,
    check_atr,
    check_rsi,
    check_session,
    check_trend,
    rsi_position,
)
from app.engine.sfp import build_levels, detect_sfp, sfp_quality
from app.engine.trace import Trace
from app.mt5.base import TIMEFRAME_MINUTES

logger = logging.getLogger("xauusd.engine")


@dataclass(frozen=True)
class NewsState:
    """Pre-computed news filter outcome (rule 6).

    `blocked=False, skipped=True` -> calendar unavailable, check skipped (C6).
    """

    blocked: bool = False
    skipped: bool = True
    value: str = "not configured — check skipped"


@dataclass(frozen=True)
class Evaluation:
    signal: dict | None  # full signal payload (None when no signal)
    trace: dict
    near_miss: bool  # trend+sfp passed but a later filter failed


def evaluate(
    m15: pd.DataFrame,
    h1: pd.DataFrame,
    bar_close_time: datetime,  # UTC close time of the newest closed M15 bar
    cfg: EngineConfig,
    spread_points: float,  # rule 7b — current spread in points
    news: NewsState | None = None,  # None -> treated as skipped
) -> Evaluation:
    """Run the full §8.2 check pipeline on closed bars only.

    `h1` must contain only H1 bars closed at/before `bar_close_time`
    (see closed_h1_asof — prevents lookahead in backtests).
    """
    trace = Trace(
        params={
            "ema_trend": cfg.trend_ema,
            "sfp_lookback": cfg.sfp_lookback,
            "wick_atr": cfg.sfp_wick_atr_ratio,
            "rr": cfg.rr,
        }
    )

    if not check_trend(h1, cfg, trace):
        return Evaluation(None, trace.to_dict(), near_miss=False)
    sig = detect_sfp(m15, cfg, trace)
    if sig is None:
        return Evaluation(None, trace.to_dict(), near_miss=False)
    rsi_ok, rsi_value = check_rsi(m15, cfg, trace)
    if not rsi_ok:
        return Evaluation(None, trace.to_dict(), near_miss=True)
    atr_ok, atr_value = check_atr(m15, cfg, trace)
    if not atr_ok:
        return Evaluation(None, trace.to_dict(), near_miss=True)
    session_ok, session_name = check_session(bar_close_time, cfg, trace)
    if not session_ok:
        return Evaluation(None, trace.to_dict(), near_miss=True)

    # Rule 6 — news (pre-computed by the caller; backtests skip it)
    news = news or NewsState()
    trace.add("news", not news.blocked, news.value)
    if news.blocked:
        return Evaluation(None, trace.to_dict(), near_miss=True)

    # Rule 7b — spread
    spread_ok = spread_points <= cfg.max_spread_points
    trace.add("spread", spread_ok, f"{spread_points:.0f} points (max {cfg.max_spread_points})")
    if not spread_ok:
        return Evaluation(None, trace.to_dict(), near_miss=True)

    entry, sl, tp = build_levels(sig, cfg)
    confidence = (
        1.0 * W_TREND
        + sfp_quality(sig, cfg) * W_SFP
        + rsi_position(rsi_value, cfg, sig.direction) * W_RSI
        + (1.0 if session_ok else 0.0) * W_SESSION
        + atr_strength(atr_value, cfg) * W_ATR
    )
    payload = {
        "direction": sig.direction,
        "entry": round(entry, 2),
        "sl": round(sl, 2),
        "tp": round(tp, 2),
        "confidence": round(min(max(confidence, 0.0), 1.0), 3),
        "trace": trace.to_dict(),
        "bar_time": m15["time_utc"].iloc[-1],
        "session": session_name,
        "spread_points": spread_points,
    }
    return Evaluation(payload, trace.to_dict(), near_miss=False)


def closed_h1_asof(h1: pd.DataFrame, close_time: datetime) -> pd.DataFrame:
    """H1 bars fully closed at/before `close_time` (no lookahead in backtests)."""
    tf_min = TIMEFRAME_MINUTES["H1"]
    cutoff = pd.Timestamp(close_time)
    close_col = h1["time_utc"] + pd.Timedelta(minutes=tf_min)
    return h1[close_col <= cutoff].reset_index(drop=True)


class SignalEngine:
    """Live engine: on_bar_close -> checks -> persist -> broadcast -> track."""

    def __init__(
        self,
        cfg: EngineConfig,
        hub: Any,  # services.ws_hub.WSHub (broadcast)
        repo: Any,  # engine.repo.SignalRepo (persistence)
        news_service: Any | None = None,
        point_size: float = 0.01,
    ) -> None:
        self._cfg = cfg
        self._hub = hub
        self._repo = repo
        self._news = news_service
        self._point_size = point_size
        self._lock = asyncio.Lock()
        self._last_signal_bar: datetime | None = None  # cooldown anchor
        self._last_spread_points: float = 0.0

    @property
    def cfg(self) -> EngineConfig:
        return self._cfg

    async def apply_config(self, cfg: EngineConfig) -> None:
        async with self._lock:
            self._cfg = cfg

    def note_spread(self, bid: float, ask: float) -> None:
        """Track the latest spread so bar-close evaluation uses a fresh value."""
        point = self._point_size if self._point_size > 0 else 0.01
        self._last_spread_points = (ask - bid) / point

    async def on_bar_close(
        self,
        tf: str,
        closed_bar: dict,  # {"t","o","h","l","c","v"} — authoritative closed bar
        source: Any,  # DataSource for get_rates
        tracker: Any,  # SignalTracker
        symbol: str,
    ) -> None:
        if tf != self._cfg.timeframe:
            return
        async with self._lock:
            cfg = self._cfg
        tf_min = TIMEFRAME_MINUTES[tf]
        bar_open = datetime.fromtimestamp(closed_bar["t"], tz=UTC)
        bar_close_time = bar_open + timedelta(minutes=tf_min)

        # Rule 7a — state: cooldown + no active signal
        if self._in_cooldown(cfg, bar_open) or tracker.has_active():
            return

        try:
            m15 = await source.get_rates(symbol, cfg.timeframe, max(200, cfg.sfp_lookback + 5))
            h1_full = await source.get_rates(symbol, cfg.trend_tf, 100)
        except Exception:  # noqa: BLE001 — data hiccup: skip this close
            logger.exception("get_rates failed at bar close %s", bar_close_time)
            return
        h1 = closed_h1_asof(h1_full, bar_close_time)

        # Rule 6 — async news check BEFORE the sync pipeline (graceful degrade)
        news = NewsState()
        if self._news is not None:
            from app.engine.filters import check_news as _check_news
            from app.engine.trace import Trace as _Trace

            t = _Trace()
            news_ok = await _check_news(self._news, bar_close_time, cfg, t)
            news = NewsState(blocked=not news_ok, skipped=False, value=t.checks[0].value)

        ev = evaluate(
            m15, h1, bar_close_time, cfg, self._last_spread_points, news=news
        )
        if ev.signal is None:
            if ev.near_miss and ev.trace["checks"]:
                last = ev.trace["checks"][-1]
                await self._log(
                    "info",
                    f"near-miss {tf} {bar_close_time:%m-%d %H:%M}: "
                    f"{last['name']} — {last['value']}",
                )
            return

        payload = ev.signal
        signal_id = await self._repo.insert(payload, symbol, tf)
        self._last_signal_bar = bar_open
        await self._hub.broadcast_all(
            "signal", {**payload, "id": signal_id, "ts": bar_open.isoformat()}
        )
        await self._log(
            "info",
            f"SIGNAL {payload['direction']} {symbol} @ {payload['entry']} "
            f"SL {payload['sl']} TP {payload['tp']} conf {payload['confidence']}",
        )

        from app.engine.tracker import make_tracked

        await tracker.register(
            make_tracked(
                direction=payload["direction"],
                entry=payload["entry"],
                sl=payload["sl"],
                tp=payload["tp"],
                confidence=payload["confidence"],
                trace=payload["trace"],
                bar_time=bar_open,
                signal_id=signal_id,
            )
        )

    def _in_cooldown(self, cfg: EngineConfig, bar_open: datetime) -> bool:
        if self._last_signal_bar is None:
            return False
        elapsed = (bar_open - self._last_signal_bar).total_seconds()
        return elapsed < cfg.cooldown_bars * TIMEFRAME_MINUTES[cfg.timeframe] * 60

    async def _log(self, level: str, message: str) -> None:
        logger.log(getattr(logging, level.upper(), logging.INFO), "%s", message)
        try:
            await self._hub.broadcast_all(
                "engine_log", {"level": level, "message": message}
            )
        except Exception:  # noqa: BLE001
            pass
