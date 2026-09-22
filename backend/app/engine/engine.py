"""SignalEngine (SPEC §8.5, D-041 M1 rework, D-042 ICT) — bar-close driven.

The pure pipeline `evaluate()` is shared by the live engine AND the backtest
runner so both paths apply exactly the same rules (single source of truth).
The live engine additionally runs the async news check, handles cooldown /
active-state, persistence and WS broadcasts.

D-041 pipeline (base TF = M1 by default):
    H1 trend -> MTF align (M5, M15) -> trigger (SFP sweep | pullback
    rejection candle) -> RSI -> ATR -> session -> news -> spread -> levels.

D-042 pipeline adds, right after the trigger:
    ICT/SMC confluence — market structure (M1 + H4/H1/M15/M5), order-block
    retest, FVG fill, liquidity sweep, supply/demand zone, institutional
    volume, kill-zone timing and whale bias must yield >= min_confluence
    votes; SL/TP become zone-aware (smart_targets) and up to max_positions
    signals may run concurrently (multi-entry directive).
"""

from __future__ import annotations

import asyncio
import logging
import time as time_mod
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd

from app.analysis.context import (
    bonus_score,
    build_confluence,
    confluence_score,
    smart_targets,
)
from app.engine.config import EngineConfig
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
    check_trend,
    rsi_position,
)
from app.engine.sfp import (
    PullbackSignal,
    SfpSignal,
    build_levels,
    build_levels_pullback,
    detect_pullback,
    detect_sfp,
    pullback_quality,
    sfp_quality,
)
from app.engine.trace import Trace
from app.engine.zones import (
    ZoneRetestSignal,
    build_levels_zone,
    check_rsi_zone,
    detect_zone_retest,
    zone_retest_note,
    zone_trigger_quality,
)
from app.mt5.base import TIMEFRAME_MINUTES

logger = logging.getLogger("xauusd.engine")

# D-041 — confirm/trend frames change at most once per their own bar close;
# a 30s engine-side TTL cache cuts bridge traffic ~20x at 60 M1 closes/hour
# while staying strictly fresher than the shortest confirm TF (M5 = 300s).
HTF_CACHE_TTL_S = 30.0


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
    near_miss: bool  # trend+mtf+trigger passed but a later filter failed


def closed_asof(df: pd.DataFrame, tf: str, close_time: datetime) -> pd.DataFrame:
    """Bars of `tf` fully closed at/before `close_time` (no lookahead)."""
    tf_min = TIMEFRAME_MINUTES[tf]
    cutoff = pd.Timestamp(close_time)
    close_col = df["time_utc"] + pd.Timedelta(minutes=tf_min)
    return df[close_col <= cutoff].reset_index(drop=True)


def closed_h1_asof(h1: pd.DataFrame, close_time: datetime) -> pd.DataFrame:
    """Pre-D-041 signature kept as a thin wrapper (H1 hardcoded)."""
    return closed_asof(h1, "H1", close_time)


def evaluate(
    base: pd.DataFrame,  # closed bars of cfg.timeframe (trigger TF)
    htf: dict[str, pd.DataFrame],  # {"H1": ..., "M5": ..., "M15": ...} closed-only
    bar_close_time: datetime,  # UTC close time of the newest closed base bar
    cfg: EngineConfig,
    spread_points: float,  # rule 7b — current spread in points
    news: NewsState | None = None,  # None -> treated as skipped
    point_size: float = 0.01,  # D-041 — for the spread-vs-risk gate
) -> Evaluation:
    """Run the full §8.2/D-041 check pipeline on closed bars only.

    `htf` frames must contain only bars closed at/before `bar_close_time`
    (see closed_asof — prevents lookahead in backtests).
    """
    trace = Trace(
        params={
            "timeframe": cfg.timeframe,
            "trend_tf": cfg.trend_tf,
            "confirm_tfs": list(cfg.confirm_tfs),
            "ema_trend": cfg.trend_ema,
            "sfp_lookback": cfg.sfp_lookback,
            "wick_atr": cfg.sfp_wick_atr_ratio,
            "rr": cfg.rr,
        }
    )

    trend_frame = htf.get(cfg.trend_tf)
    if trend_frame is None or not check_trend(trend_frame, cfg, trace):
        return Evaluation(None, trace.to_dict(), near_miss=False)

    # D-048 — trigger ARBITRATION (user directive: "সব গুলো স্ট্রাটেজি
    # একই সময় AGREE নাও থাকতে পারে"): every candidate setup is gathered
    # first (all need only the H1 direction), then each trigger passes
    # ITS OWN gates — the BEST available strategy fires on its own merit
    # instead of waiting for the whole panel to agree.
    sig_sfp = detect_sfp(base, cfg, trace)
    sig_zone: ZoneRetestSignal | None = None
    if cfg.zone_trigger_enabled:
        from app.analysis.poi import poi_zones

        zones = poi_zones(base, htf)
        sig_zone = detect_zone_retest(base, zones, cfg, trace.direction)
    sig_pb: PullbackSignal | None = None
    trigger: str
    if sig_sfp is not None:
        trigger = "sfp"  # sweep reversal — rarest, strongest pattern
    elif sig_zone is not None:
        trigger = "zone"  # D-048 — POI zone retest (location + rejection)
        trace.direction = sig_zone.direction  # signal direction wins downstream
        trace.add("zone_retest", True, zone_retest_note(sig_zone))
    elif cfg.pullback_enabled:
        sig_pb = detect_pullback(base, cfg, trace)
        if sig_pb is None:
            return Evaluation(None, trace.to_dict(), near_miss=False)
        trigger = "pullback"
    else:
        return Evaluation(None, trace.to_dict(), near_miss=False)

    # After a trigger fired, every later failure is a NEAR MISS (worth a log).
    #
    # Per-trigger gates (D-048):
    #   sfp / pullback — MTF confirmation + the ICT factor-count gate
    #                    (the premium "everything aligns" setups);
    #   zone           — the POI location + rejection IS the setup: MTF and
    #                    the factor count NEVER block (confidence only) —
    #                    signals at supply/demand zones must not be missed.
    mtf_score: float
    if trigger in ("sfp", "pullback"):
        agreed = check_mtf(htf, trace.direction, cfg, trace)
        if agreed < cfg.min_tf_agree:
            return Evaluation(None, trace.to_dict(), near_miss=False)
        mtf_score = agreed / max(len(cfg.confirm_tfs), 1)
    else:
        # informational posture vs the SIGNAL direction — never gates
        agreed = check_mtf(htf, trace.direction, cfg, trace)
        mtf_score = agreed / max(len(cfg.confirm_tfs), 1)

    # D-042 — ICT/SMC confluence stage (zone/OB/structure/volume votes).
    factors: list[dict] = []
    if cfg.smc_enabled:
        candidate_entry = float(base["c"].iloc[-1])
        factors = build_confluence(
            base, htf, trace.direction, candidate_entry, bar_close_time,
            max_zone_atr=cfg.max_zone_atr,
        )
        votes = confluence_score(factors)
        for f in factors:
            trace.add(f["name"], f["ok"], f["detail"])
        n_gate = len([f for f in factors if f["name"] in (
            "structure_m1", "htf_structure", "ob_retest",
            "fvg_fill", "liquidity_sweep", "zone",
        )])
        if trigger == "zone":
            trace.add(
                "confluence", True,
                f"{votes}/{n_gate} ICT factors align (zone setup — "
                "quality-gated, count does not block)",
            )
        else:
            confluence_ok = votes >= cfg.min_confluence
            if not confluence_ok:
                trace.add(
                    "confluence", False,
                    f"{votes}/{n_gate} ICT factors confirm"
                    f" (need {cfg.min_confluence})",
                )
                return Evaluation(None, trace.to_dict(), near_miss=True)
            trace.add(
                "confluence", True,
                f"{votes} ICT factors confirm (need {cfg.min_confluence})"
                f" + {bonus_score(factors)} bonus",
            )

    if trigger == "zone":
        rsi_ok, rsi_value = check_rsi_zone(base, cfg, trace)
    else:
        rsi_ok, rsi_value = check_rsi(base, cfg, trace)
    if not rsi_ok:
        return Evaluation(None, trace.to_dict(), near_miss=True)
    atr_ok, atr_value = check_atr(base, cfg, trace)
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

    # Rule 7b — spread (absolute cap)
    spread_ok = spread_points <= cfg.max_spread_points
    trace.add(
        "spread", spread_ok,
        f"spread {spread_points:.0f} points (max {cfg.max_spread_points})",
    )
    if not spread_ok:
        return Evaluation(None, trace.to_dict(), near_miss=True)

    if sig_sfp is not None:
        entry, sl_base, _ = build_levels(sig_sfp, cfg)
        trigger_quality = sfp_quality(sig_sfp, cfg)
        trigger_tag: SfpSignal | PullbackSignal | ZoneRetestSignal = sig_sfp
    elif sig_zone is not None:
        # D-048 — zone retest: SL beyond the zone's far edge, then the
        # same smart_targets pass (ATR floor/cap + TPO anchor + liquidity
        # TP snap) every other trigger uses.
        entry, sl_base = build_levels_zone(sig_zone, cfg)
        trigger_quality = zone_trigger_quality(sig_zone)
        trigger_tag = sig_zone
    else:
        assert sig_pb is not None  # for the type checker
        entry, sl_base, _ = build_levels_pullback(sig_pb, cfg)
        trigger_quality = pullback_quality(sig_pb)
        trigger_tag = sig_pb

    # D-042 — zone-aware exits: SL beyond the structural invalidation,
    # floored/capped in ATRs; TP snapped toward opposing liquidity.
    # D-047 — TPO levels from the last day of M1 bars: strong time-at-price
    # nodes extend the stop just past the level (never past max_sl_atr).
    tpo_levels: list[dict] = []
    try:
        from app.analysis.tpo import tpo_profile

        tpo_levels = tpo_profile(
            base, lookback_minutes=cfg.tpo_lookback_min
        )["levels"]
    except Exception:  # noqa: BLE001 — anchoring is best-effort
        tpo_levels = []
    entry, sl, tp = smart_targets(
        base, trigger_tag.direction, entry, sl_base, cfg.rr,
        cfg.min_sl_atr, cfg.max_sl_atr, tpo_levels=tpo_levels,
    )

    # D-041 — spread vs risk: entering at ask/exiting at bid costs one spread;
    # when that cost exceeds `max_spread_to_risk` of the stop distance the
    # trade is structurally unprofitable (noise-level stop).
    risk = abs(entry - sl)
    point = point_size if point_size > 0 else 0.01
    spread_price = spread_points * point
    spread_risk_ok = risk > 0 and spread_price <= cfg.max_spread_to_risk * risk
    trace.add(
        "spread_risk",
        spread_risk_ok,
        f"spread ${spread_price:.2f} vs risk ${risk:.2f}"
        f" (max {cfg.max_spread_to_risk:.0%} of risk)",
    )
    if not spread_risk_ok:
        return Evaluation(None, trace.to_dict(), near_miss=True)

    base_confidence = (
        1.0 * W_TREND
        + mtf_score * W_MTF
        + trigger_quality * W_TRIGGER
        + rsi_position(rsi_value, cfg, trigger_tag.direction) * W_RSI
        + (1.0 if session_ok else 0.0) * W_SESSION
        + atr_strength(atr_value, cfg) * W_ATR
    ) / max(W_TREND + W_MTF + W_TRIGGER + W_RSI + W_SESSION + W_ATR, 1e-9)
    if trigger == "zone":
        # D-048 — zone setups: the LOCATION + rejection carry the score;
        # the ICT factor panel and the classic filters shape it but the
        # zone blend is the dominant term (best-strategy arbitration).
        assert sig_zone is not None
        zone_blend = zone_trigger_quality(sig_zone)
        if cfg.smc_enabled and factors:
            gate = confluence_score(factors) / 6.0
            bonus = bonus_score(factors) / 6.0
            confidence = (
                0.50 * zone_blend
                + 0.30 * base_confidence
                + 0.20 * (0.7 * gate + 0.3 * bonus)
            )
        else:
            confidence = 0.60 * zone_blend + 0.40 * base_confidence
        if sig_zone.counter_trend:
            confidence = min(confidence, 0.72)  # reversal trades stay modest
    elif cfg.smc_enabled and factors:
        gate = confluence_score(factors) / 6.0
        bonus = bonus_score(factors) / 6.0  # D-047: 6 bonus factors
        confidence = (1.0 - W_CONFLUENCE) * base_confidence + W_CONFLUENCE * (
            0.7 * gate + 0.3 * bonus
        )
    else:
        confidence = base_confidence
    trace_dict = trace.to_dict()
    trace_dict["trigger"] = trigger  # survives inside signals.trace JSON (D-041)
    trace_dict["confluence_factors"] = factors  # D-042 — Signal Analysis panel
    payload = {
        "direction": trigger_tag.direction,
        "entry": round(entry, 2),
        "sl": round(sl, 2),
        "tp": round(tp, 2),
        "confidence": round(min(max(confidence, 0.0), 1.0), 3),
        "trigger": trigger,
        "trace": trace_dict,
        "bar_time": base["time_utc"].iloc[-1],
        "session": session_name,
        "spread_points": spread_points,
    }
    return Evaluation(payload, trace_dict, near_miss=False)


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
        # D-041 — confirm/trend frame cache: {(symbol, tf): (mono_ts, df)}
        self._htf_cache: dict[tuple[str, str], tuple[float, pd.DataFrame]] = {}
        # Phase 4: async callback fired with (payload, signal_id) right after a
        # signal is persisted+ broadcast — the runtime routes it into the
        # per-user trading planes (copy-trading agent relay).
        self.on_signal: Any | None = None

    @property
    def cfg(self) -> EngineConfig:
        return self._cfg

    async def apply_config(self, cfg: EngineConfig) -> None:
        async with self._lock:
            self._cfg = cfg
            self._htf_cache.clear()  # TF set may have changed

    def invalidate_frames(self) -> None:
        """Drop the confirm/trend frame cache (tests with accelerated
        clocks; also correct after any external data reset)."""
        self._htf_cache.clear()

    def note_spread(self, bid: float, ask: float) -> None:
        """Track the latest spread so bar-close evaluation uses a fresh value."""
        point = self._point_size if self._point_size > 0 else 0.01
        self._last_spread_points = (ask - bid) / point

    async def _htf_frame(
        self, source: Any, symbol: str, tf: str, bars: int
    ) -> pd.DataFrame | None:
        """Confirm/trend frame with a short TTL cache (D-041)."""
        key = (symbol, tf)
        now = time_mod.monotonic()
        hit = self._htf_cache.get(key)
        if hit is not None and now - hit[0] < HTF_CACHE_TTL_S:
            return hit[1]
        try:
            df = await source.get_rates(symbol, tf, bars)
        except Exception:  # noqa: BLE001 — data hiccup: caller skips this close
            logger.exception("get_rates(%s, %s) failed", symbol, tf)
            return None
        if len(df) == 0:
            return None
        self._htf_cache[key] = (now, df)
        return df

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

        # Rule 7a — state: cooldown + concurrent-signal budget (D-042
        # multi-entry: up to cfg.max_positions tracked signals at once).
        if self._in_cooldown(cfg, bar_open):
            return
        active_now = tracker.active
        if callable(active_now):  # duck-typed fakes may expose a method
            active_now = active_now()
        if len(active_now) >= max(int(cfg.max_positions), 1):
            return

        try:
            base = await source.get_rates(
                symbol, cfg.timeframe, max(200, cfg.sfp_lookback + 5)
            )
        except Exception:  # noqa: BLE001 — data hiccup: skip this close
            logger.exception("get_rates failed at bar close %s", bar_close_time)
            return
        if len(base) < cfg.ema_fast + 4:
            return

        htf: dict[str, pd.DataFrame] = {}
        fetch_tfs = [cfg.trend_tf, *cfg.confirm_tfs]
        if cfg.smc_enabled:
            fetch_tfs += cfg.bias_tfs  # D-042 ICT structure-bias frames
        for higher in fetch_tfs:
            frame = await self._htf_frame(source, symbol, higher, 100)
            if frame is None:
                return  # bridge hiccup — better to skip than evaluate blind
            htf[higher] = closed_asof(frame, higher, bar_close_time)

        # Rule 6 — async news check BEFORE the sync pipeline (graceful degrade)
        news = NewsState()
        if self._news is not None:
            from app.engine.filters import check_news as _check_news
            from app.engine.trace import Trace as _Trace

            t = _Trace()
            news_ok = await _check_news(self._news, bar_close_time, cfg, t)
            news = NewsState(blocked=not news_ok, skipped=False, value=t.checks[0].value)

        ev = evaluate(
            base, htf, bar_close_time, cfg, self._last_spread_points, news=news,
            point_size=self._point_size,
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
            "signal",
            {**payload, "id": signal_id, "ts": bar_open.isoformat(),
             "symbol": symbol, "tf": tf},
        )
        if self.on_signal is not None:
            try:
                await self.on_signal(
                    {**payload, "id": signal_id, "ts": bar_open.isoformat(),
                     "symbol": symbol, "tf": tf},
                    symbol,
                    self._point_size,
                )
            except Exception:  # noqa: BLE001 — relay must never kill the engine
                logger.exception("on_signal relay callback failed")
        await self._log(
            "info",
            f"SIGNAL {payload['direction']} {symbol} @ {payload['entry']} "
            f"SL {payload['sl']} TP {payload['tp']} conf {payload['confidence']} "
            f"({payload['trigger']})",
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
