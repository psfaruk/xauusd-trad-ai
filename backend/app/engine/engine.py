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
    trusted_score,
)
from app.engine.bias import direction_bias
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
    rsi_position,
)
from app.engine.indicators import atr as atr_calc
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
    poi_pending_entry,
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
    pulse: dict | None = None  # D-051 — strategy-radar frame (always present)


def closed_asof(df: pd.DataFrame, tf: str, close_time: datetime) -> pd.DataFrame:
    """Bars of `tf` fully closed at/before `close_time` (no lookahead)."""
    tf_min = TIMEFRAME_MINUTES[tf]
    cutoff = pd.Timestamp(close_time)
    close_col = df["time_utc"] + pd.Timedelta(minutes=tf_min)
    return df[close_col <= cutoff].reset_index(drop=True)


def closed_h1_asof(h1: pd.DataFrame, close_time: datetime) -> pd.DataFrame:
    """Pre-D-041 signature kept as a thin wrapper (H1 hardcoded)."""
    return closed_asof(h1, "H1", close_time)


def _pulse_miss(pulse: dict, trace: Trace) -> None:
    """D-051 — stamp the near-miss reason + check states into the radar
    frame (why no signal THIS bar: which check failed, what it said)."""
    checks = [
        {"name": c.name, "ok": c.passed, "value": c.value}
        for c in trace.checks
    ]
    pulse["checks"] = checks
    failed = [c for c in checks if not c["ok"]]
    if failed:
        pulse["near_miss"] = f"{failed[-1]['name']} — {failed[-1]['value']}"
    elif not pulse.get("near_miss"):
        pulse["near_miss"] = "no trigger on this bar"


def _drawing_geometry(
    base: pd.DataFrame,
    htf: dict[str, pd.DataFrame],
    direction: str,
    market: float,
    cfg: EngineConfig,
    spread_price: float,
) -> dict | None:
    """D-057 — the chart-true trade contract: ENTRY/SL/TP exactly where
    the chart's entry-setup box draws them.

    Builds the M5/M15 snapshots from the SAME bar windows the drawing
    layer uses (setup_snapshot mirrors services/analysis.py
    BARS_PER_TF), runs the SHARED setup_geometry (the single source of
    truth both the chart box and this order read), then books the drawn
    entry as a PENDING LIMIT when it sits beyond the noise margin or a
    market entry when the market is already at the drawn level.

    Returns None when the chart draws no supporting zone for this
    direction (the caller falls back to the legacy geometry chain) or
    when the drawn trade fails the sanity gates (risk floor/cap, min
    RR) — the drawing is the instruction, but never a suicide note.
    """
    from app.analysis.setup_geometry import setup_geometry, setup_snapshot

    s5 = setup_snapshot(htf.get("M5"), "M5")
    s15 = setup_snapshot(htf.get("M15"), "M15")
    if s5 is None and s15 is None:
        return None
    a_m1 = atr_calc(base, cfg.atr_period) or 1e-9
    geo = setup_geometry(
        s5, s15, direction, market,
        near_atr=cfg.setup_near_atr, atr_fallback=a_m1,
        entry_anchor=cfg.setup_entry_anchor, max_rr=cfg.setup_max_rr,
        max_risk_atr=cfg.setup_max_risk_atr,
    )
    if geo is None:
        return None
    entry, sl, tp = geo["entry"], geo["sl"], geo["tp"]
    risk = abs(entry - sl)
    risk_cap = cfg.setup_max_risk_atr * float(geo.get("atr5") or a_m1)
    if risk <= 0 or risk > risk_cap:
        return None  # degenerate or swing-scale — not a short-time trade
    # a stop the spread can eat half of is not a stop (D-049 floor)
    floor = 2.5 * max(spread_price, 0.0)
    if risk < floor:
        sl = entry - floor if direction == "BUY" else entry + floor
        risk = floor
        if risk > risk_cap:
            return None  # the floor pushed past the cap — skip, not stretch
    if abs(tp - entry) / risk < cfg.tp_min_rr:
        return None  # the drawn TP is too close — poor geometry

    # market vs pending: the drawn entry beyond the noise margin books
    # as a LIMIT at the drawn level; an entry at/behind the market fills
    # now with the drawn SL/TP contract intact
    min_off = max(
        cfg.entry_offset_atr * a_m1, 2.0 * spread_price, cfg.entry_min_usd
    )
    dist = (market - entry) if direction == "BUY" else (entry - market)
    if dist >= min_off:
        entry_type = "limit"
        rr = abs(tp - entry) / risk
        note = (
            f"{geo['tag']} {geo['lo']:.2f}-{geo['hi']:.2f} entry {entry:.2f} "
            f"({dist:.2f} USD from market) — chart-true"
        )
    else:
        # price already at/through the drawn entry: fill at the market,
        # keep the drawn SL/TP (the zone rejected here and now)
        entry_type = "market"
        entry = round(market, 2)
        risk = abs(entry - sl)
        rr = abs(tp - entry) / risk if risk > 0 else 0.0
        if risk <= 0 or rr < cfg.tp_min_rr or rr > cfg.setup_max_rr:
            return None  # the shifted entry breaks the contract's scale
        note = (
            f"{geo['tag']} {geo['lo']:.2f}-{geo['hi']:.2f} retested at "
            f"market {market:.2f} — chart-true"
        )
    return {
        "entry": round(float(entry), 2),
        "sl": round(float(sl), 2),
        "tp": round(float(tp), 2),
        "rr": round(float(rr), 2),
        "entry_type": entry_type,
        "entry_note": note,
        "tag": geo["tag"],
        "zone": (geo["lo"], geo["hi"]),
        "target_note": f"{geo.get('tp_source', 'drawn target')} "
                       f"(chart-true D-057)",
    }


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

    D-049: the direction comes from the multi-source bias vote (EMA +
    M15/H1/H4 structure + momentum) instead of a single H1 EMA cross;
    triggers arbitrate by QUALITY (best setup fires, not a fixed
    priority); SL/TP are predicted from structure (smart_targets v2).
    D-051: `pulse` carries the live per-strategy state (bias, RSI, ATR,
    whale activity, zone map, trigger candidates, near-miss reason) —
    the caller broadcasts it every bar close so the UI shows WHICH
    strategy is doing WHAT within seconds of the M1 close.
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

    # D-051 — the strategy-radar frame: enriched as the pipeline runs;
    # EVERY return path carries it so the UI never goes blind.
    price_now = float(base["c"].iloc[-1]) if len(base) else None
    pulse: dict = {
        "tf": cfg.timeframe,
        "ts": bar_close_time.isoformat(),
        "price": round(price_now, 2) if price_now is not None else None,
        "bias": None,
        "rsi": None,
        "atr": None,
        "spread_points": spread_points,
        "session": None,
        "triggers": {"sfp": False, "zone": False, "pullback": False},
        "whale": None,
        "candle": None,  # D-051 — closed-bar buyer/seller dominance
        "zones": [],
        "fired": None,
        "near_miss": None,
        "checks": [],
    }

    # D-065 — the TIMEFRAME LADDER (user directive: "কত মিনিটের টাইম
    # ফ্রেম কত টি টাইম এনালাইসিস করে, কোন টাইম ফ্রেম এ সিগন্যাল প্রধান
    # করেন, কোনটি করলে ভালো হবে"): the radar shows which TF plays which
    # role and how much history each one reads — the answer to the
    # short-time-trading ladder question, live in the app.
    try:
        from app.analysis.setup_geometry import GEOMETRY_BARS

        ladder_tfs: list[dict] = [{
            "tf": cfg.timeframe, "role": "signal",
            "bars": int(len(base)),
            "note": "signals fire + pending entries anchor on this close",
        }]
        for tf in cfg.confirm_tfs:
            ladder_tfs.append({
                "tf": tf,
                "role": "setup" if tf == "M5" else "confirm",
                "bars": int(max(100, GEOMETRY_BARS.get(tf, 0))),
                "note": (
                    "drawn zones + trade geometry + structure ladder"
                    if tf == "M5" else "MTF confirmation vote"
                ),
            })
        ladder_tfs.append({
            "tf": cfg.trend_tf, "role": "trend", "bars": 100,
            "note": "bias EMA + momentum vote",
        })
        for tf in cfg.bias_tfs:
            ladder_tfs.append({
                "tf": tf, "role": "bias", "bars": 100,
                "note": "big-frame structure vote",
            })
        pulse["tf_ladder"] = ladder_tfs
    except Exception:  # noqa: BLE001 — the ladder is informational
        pass

    # D-049 — multi-source direction bias (replaces the single-EMA gate:
    # while gold sat under H1 EMA50 the old gate locked the engine into
    # SELL-only mode — the exact complaint that started this rework).
    trend_frame = htf.get(cfg.trend_tf)
    if trend_frame is None:
        return Evaluation(None, trace.to_dict(), near_miss=False, pulse=pulse)
    bias_verdict, _bias_score, _notes = direction_bias(htf, cfg, trace)
    pulse["bias"] = bias_verdict
    if bias_verdict == "NEUTRAL":
        trace.direction = None  # zone entries only — both sides live
    else:
        trace.direction = bias_verdict

    # D-051 — radar context: RSI/ATR values + whale activity + zone map
    # (computed once here; the checks below reuse the same math)
    try:
        from app.engine.indicators import atr as _atr
        from app.engine.indicators import rsi as _rsi

        pulse["rsi"] = round(float(_rsi(base["c"], cfg.rsi_period)), 1)
        pulse["atr"] = round(float(_atr(base, cfg.atr_period) or 0.0), 3)
    except Exception:  # noqa: BLE001 — radar values are best-effort
        pass
    try:
        from app.analysis.orderflow import whale_summary

        wh = whale_summary(base.iloc[-90:], lookback=45)
        pulse["whale"] = {
            "bias": wh.get("bias"),
            "buy_events": wh.get("buy_events"),
            "sell_events": wh.get("sell_events"),
            "last": (wh.get("last") or {}).get("note"),
        }
    except Exception:  # noqa: BLE001
        pass
    # D-051 — the just-closed M1 candle's buyer/seller story (user
    # directive: "প্রতি সেকেন্ডে ক্যান্ডেলস্টিকের reaction আর ক্রেতা-
    # বিক্রেতা কে dominate করছে") — the UI re-derives the same math from
    # the FORMING bar's ticks in between closes.
    try:
        from app.analysis.orderflow import candle_pulse

        pulse["candle"] = candle_pulse(
            base.iloc[-1],
            prev_close=(
                float(base["c"].iloc[-2]) if len(base) > 1 else None
            ),
            vol_window=base["v"].astype(float).iloc[-31:-1],
        )
    except Exception:  # noqa: BLE001
        pass

    # D-061 — the AMD read (user directive: "ইকমেলিউশন ও মেনোপোলেশন
    # কোনো লজিক এড করতে পারবেন?"): EVERY bar close tells the radar which
    # phase the market sits in (accumulation / manipulation /
    # distribution) — the app "understands" the institutional cycle even
    # on bars that never reach a trigger. The per-trade trap gate runs
    # later, once ENTRY/SL exist.
    amd: dict = {}
    try:
        from app.analysis.manipulation import amd_state

        amd = amd_state(base)
        pulse["amd"] = {
            "phase": amd.get("phase"),
            "note": amd.get("note"),
            "sweep": amd.get("sweep"),
            "displacement": amd.get("displacement"),
        }
    except Exception:  # noqa: BLE001 — the AMD read is best-effort
        pass

    # D-064 — the market-STRUCTURE read (user directive: "মার্কেট কোথায়
    # গিয়ে রেস্ট করে... কত বার HL LL LH HH হলে রিভার্স বা কনটিনিউ
    # করে?"): every bar close tells the radar HOW MANY consecutive
    # same-direction breaks the live run has (the LL/LH/HH/HL ladder),
    # whether the market is resting / extended / just shifted, the
    # honest reversal odds, and WHERE the pullback magnets sit — the
    # levels the market statistically returns to before the next leg.
    struct: dict = {}
    try:
        from app.analysis.structure import structure_read

        m5f = htf.get("M5")
        struct_frame = m5f if m5f is not None and len(m5f) >= 60 else base
        struct = structure_read(struct_frame, price_now)
        pulse["structure"] = {
            "run": struct.get("run"),
            "run_dir": struct.get("run_dir"),
            "trend": struct.get("trend"),
            "phase": struct.get("phase"),
            "p_reversal": struct.get("p_reversal"),
            "action": struct.get("action"),
            "drivers": struct.get("drivers") or [],
            "choch": struct.get("choch_fresh"),
            "magnets": [
                {"price": m.get("price"), "kind": m.get("kind"),
                 "dist_atr": m.get("dist_atr")}
                for m in (struct.get("magnets") or [])[:3]
            ],
        }
    except Exception:  # noqa: BLE001 — the structure read is best-effort
        pass

    # D-067 — the CANDLE BATTLE read (user directive: "একটি রানিং
    # ক্যান্ডেল বা কয়েক টি ক্যান্ডেল buyer Sellar position, কারা কাদের
    # কে ডোমেনেট করছে, কারা জিতেছে, লাস্ট কয়েক টি ক্যান্ডেল এর ভিতর
    # কি ঘটেছে"): every close reports the last flow_window closed
    # candles' buyer-vs-seller war — who dominates (volume-weighted
    # split), who WON (candle count + net ATR), what happened INSIDE
    # (decisive rejections / absorptions / momentum bodies) and the
    # winning streak. The RUNNING candle's live reaction is recomputed
    # by the UI from the forming bar on every tick between closes.
    battle: dict = {}
    try:
        from app.analysis.orderflow import battle_read

        battle = battle_read(base, n=cfg.flow_window)
        pulse["battle"] = {
            "n": battle.get("n"),
            "state": battle.get("state"),
            "buy_pct": battle.get("buy_pct"),
            "sell_pct": battle.get("sell_pct"),
            "wins": battle.get("wins"),
            "streak": battle.get("streak"),
            "net_atr": battle.get("net_atr"),
            "participation": battle.get("participation"),
            "events": [
                {"kind": e.get("kind"), "side": e.get("side"),
                 "price": e.get("price"), "note": e.get("note")}
                for e in (battle.get("events") or [])[-3:]
            ],
            "verdict": battle.get("verdict"),
        }
    except Exception:  # noqa: BLE001 — the battle read is best-effort
        pass

    # D-048/D-049 — trigger ARBITRATION by QUALITY (user directive:
    # "সব গুলো স্ট্রাটেজি একই সময় AGREE নাও থাকতে পারে" + "best strategy"):
    # every candidate setup is gathered first, each scored on its own
    # merit, and the BEST one fires — no fixed priority (the old
    # sfp-beats-zone-always rule let a weak sweep shadow a strong zone
    # retest and then die at the confluence gate: signal lost).
    zones: list[dict] = []
    if cfg.zone_trigger_enabled or cfg.smc_enabled or cfg.entry_mode == "poi_limit":
        from app.analysis.poi import poi_zones

        zones = poi_zones(base, htf)

    # D-051 — the zone map for the strategy radar: nearest levels with
    # their USD distance from the current price (support/resistance the
    # user watches; "যে মার্কেট এই পজিশন থেকে এখন ডাউনের যাবে নয়তো আপে
    # যাবে, তখনই সিগন্যাল জেনারেট হবে")
    if price_now is not None:
        for z in sorted(
            zones, key=lambda z: float(z.get("quality", 0.0)), reverse=True
        )[:3]:
            try:
                pulse["zones"].append({
                    "side": z["side"],
                    "lo": round(float(z["lo"]), 2),
                    "hi": round(float(z["hi"]), 2),
                    "quality": round(float(z.get("quality", 0.0)), 2),
                    "source": z.get("source", "?"),
                    "dist_usd": round(
                        min(
                            abs(price_now - float(z["lo"])),
                            abs(price_now - float(z["hi"])),
                        ),
                        2,
                    ),
                })
            except (KeyError, TypeError, ValueError):
                continue

    sig_sfp = detect_sfp(base, cfg, trace) if trace.direction else None
    sig_zone: ZoneRetestSignal | None = None
    if cfg.zone_trigger_enabled:
        sig_zone = detect_zone_retest(base, zones, cfg, bias_verdict)
    sig_pb: PullbackSignal | None = None
    pulse["triggers"] = {
        "sfp": sig_sfp is not None,
        "zone": sig_zone is not None,
        "pullback": False,
    }

    trigger: str | None = None
    trigger_quality = -1.0
    if sig_sfp is not None and sfp_quality(sig_sfp, cfg) > trigger_quality:
        trigger, trigger_quality = "sfp", sfp_quality(sig_sfp, cfg)
    if sig_zone is not None and zone_trigger_quality(sig_zone) > trigger_quality:
        trigger, trigger_quality = "zone", zone_trigger_quality(sig_zone)
    if (
        trigger != "zone"
        and cfg.pullback_enabled
        and trace.direction
        and sig_pb is None
    ):
        sig_pb = detect_pullback(base, cfg, trace)
        if sig_pb is not None and pullback_quality(sig_pb) > trigger_quality:
            trigger, trigger_quality = "pullback", pullback_quality(sig_pb)

    if trigger is None:
        _pulse_miss(pulse, trace)  # near_miss -> "no trigger on this bar"
        return Evaluation(None, trace.to_dict(), near_miss=False, pulse=pulse)
    pulse["trigger"] = trigger
    if trigger in pulse["triggers"]:
        pulse["triggers"][trigger] = True
    if trigger == "zone":
        assert sig_zone is not None
        trace.direction = sig_zone.direction  # signal direction wins downstream
        trace.add("zone_retest", True, zone_retest_note(sig_zone))
    elif trigger == "sfp":
        assert sig_sfp is not None and trace.direction is not None
    else:
        assert sig_pb is not None and trace.direction is not None

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
        mtf_score = agreed / max(len(cfg.confirm_tfs), 1)
        if agreed < cfg.min_tf_agree:
            if sig_zone is not None:
                # D-049 — the premium setup's MTF gate failed, but a VALID
                # zone retest sits right here (it passed its own gates):
                # the zone fires on its own merit instead of the bar dying
                # with nothing (user directive: POI signals must not miss).
                trigger = "zone"
                trace.direction = sig_zone.direction
                trace.add(
                    "zone_retest", True,
                    zone_retest_note(sig_zone)
                    + " (fallback — MTF failed for the primary setup)",
                )
            else:
                return Evaluation(None, trace.to_dict(), near_miss=False, pulse=pulse)
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
            whale_pulse_bars=cfg.whale_pulse_bars,  # D-051 real-time window
        )
        votes = confluence_score(factors)
        trusted = trusted_score(factors)  # D-051 — the trusted core's vote
        pulse["factors"] = [
            {"name": f["name"], "ok": f["ok"], "detail": f["detail"]}
            for f in factors
        ]
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
            # D-051 — TRUSTED-VOTE GATE (user directive: "যদি কয়েকটি
            # স্ট্যাটাজি মিলে ভোট দেয়, বিশ্বাসযোগ্য কয়েকটি স্ট্রাটেজি হতে
            # হবে"): the full panel OR the trusted core (whale pulse counts
            # double) — a few trusted strategies agreeing IS a signal.
            confluence_ok = (
                votes >= cfg.min_confluence
                or trusted >= cfg.trusted_min_votes
            )
            if not confluence_ok:
                if sig_zone is not None:
                    # D-049 — same fallback as the MTF gate: a valid zone
                    # retest fires instead of losing the bar entirely.
                    trigger = "zone"
                    trace.direction = sig_zone.direction
                    trace.add(
                        "zone_retest", True,
                        zone_retest_note(sig_zone)
                        + " (fallback — ICT count failed for the primary"
                          " setup)",
                    )
                else:
                    trace.add(
                        "confluence", False,
                        f"{votes}/{n_gate} ICT factors confirm, trusted "
                        f"{trusted:.0f} (need {cfg.min_confluence} or "
                        f"trusted {cfg.trusted_min_votes:.0f})",
                    )
                    return Evaluation(
                        None, trace.to_dict(), near_miss=True, pulse=pulse
                    )
            else:
                how = (
                    f"trusted core {trusted:.0f} votes"
                    if trusted >= cfg.trusted_min_votes
                    and votes < cfg.min_confluence
                    else f"{votes} ICT factors"
                )
                trace.add(
                    "confluence", True,
                    f"{how} confirm + {bonus_score(factors)} bonus",
                )

    if trigger == "zone":
        rsi_ok, rsi_value = check_rsi_zone(base, cfg, trace)
    else:
        rsi_ok, rsi_value = check_rsi(base, cfg, trace)
    if not rsi_ok:
        _pulse_miss(pulse, trace)
        return Evaluation(None, trace.to_dict(), near_miss=True, pulse=pulse)
    atr_ok, atr_value = check_atr(base, cfg, trace)
    if not atr_ok:
        _pulse_miss(pulse, trace)
        return Evaluation(None, trace.to_dict(), near_miss=True, pulse=pulse)
    session_ok, session_name = check_session(bar_close_time, cfg, trace)
    pulse["session"] = session_name
    if not session_ok:
        _pulse_miss(pulse, trace)
        return Evaluation(None, trace.to_dict(), near_miss=True, pulse=pulse)

    # Rule 6 — news (pre-computed by the caller; backtests skip it)
    news = news or NewsState()
    trace.add("news", not news.blocked, news.value)
    if news.blocked:
        _pulse_miss(pulse, trace)
        return Evaluation(None, trace.to_dict(), near_miss=True, pulse=pulse)

    # Rule 7b — spread (absolute cap)
    spread_ok = spread_points <= cfg.max_spread_points
    trace.add(
        "spread", spread_ok,
        f"spread {spread_points:.0f} points (max {cfg.max_spread_points})",
    )
    if not spread_ok:
        _pulse_miss(pulse, trace)
        return Evaluation(None, trace.to_dict(), near_miss=True, pulse=pulse)

    if trigger == "sfp":
        assert sig_sfp is not None
        entry, sl_base, _ = build_levels(sig_sfp, cfg)
        trigger_quality = sfp_quality(sig_sfp, cfg)
        trigger_tag: SfpSignal | PullbackSignal | ZoneRetestSignal = sig_sfp
    elif trigger == "zone":
        # D-048/D-049 — zone retest: SL beyond the zone's far edge (or the
        # sweep extreme on a sweep+reclaim), then the same smart_targets
        # pass (spread floor + ATR floor/cap + TPO anchor + PREDICTED
        # structural TP) every other trigger uses.
        assert sig_zone is not None
        entry, sl_base = build_levels_zone(sig_zone, cfg)
        trigger_quality = zone_trigger_quality(sig_zone)
        trigger_tag = sig_zone
    else:
        assert sig_pb is not None  # for the type checker
        entry, sl_base, _ = build_levels_pullback(sig_pb, cfg)
        trigger_quality = pullback_quality(sig_pb)
        trigger_tag = sig_pb

    # D-057 — DRAWING-TRUE GEOMETRY (user directive: "SL TP ENTRY সব
    # কিছু এই চার্ট ফলো করে হবে"): when the chart is drawing a setup box
    # (a supporting zone within reach of the market), the signal's
    # ENTRY/SL/TP ARE the drawn levels — app.analysis.setup_geometry is
    # the single source of truth for BOTH the chart box and this order.
    # No drawn zone -> the legacy chain below (poi_pending_entry +
    # smart_targets) still places the trade.
    market_ref = entry  # trigger close = the market price at signal time
    entry_type = "market"
    entry_note = "market entry at trigger close"
    point = point_size if point_size > 0 else 0.01
    spread_price = spread_points * point
    geo = (
        _drawing_geometry(
            base, htf, trigger_tag.direction, market_ref, cfg,
            spread_price=spread_price,
        )
        if cfg.drawing_true else None
    )
    if geo is not None:
        entry, sl, tp = geo["entry"], geo["sl"], geo["tp"]
        entry_type, entry_note = geo["entry_type"], geo["entry_note"]
        target_note = geo["target_note"]
        realized_rr = float(geo["rr"])
        trace.add(
            "geometry", True,
            f"drawing-true setup — {entry_note}; SL {sl:.2f} "
            f"TP {tp:.2f} RR {realized_rr:.2f}",
        )
    else:
        if cfg.drawing_true:
            # informational mode report, never a gate: the chart draws no
            # supporting zone here, so the legacy chain placed the trade
            trace.add(
                "geometry", True,
                "legacy geometry — the chart draws no supporting zone "
                "for this trade",
            )
        # D-050 — POI pending entry (legacy path, user directive): the
        # entry stops being the trigger close (a market order that fills
        # wherever the noise happens to be) and becomes a PENDING LIMIT
        # anchored at a structural POI level BEYOND the market — BUY
        # limits below the demand/support zone, SELL limits above the
        # supply/resistance zone. The distance between market and entry
        # is the margin the old mode lacked; SL/TP are then re-derived
        # from the (deeper) pending entry by the same smart_targets pass
        # below.
        if cfg.entry_mode == "poi_limit":
            atr_val = atr_calc(base, cfg.atr_period) or 1e-9
            pending, entry_note = poi_pending_entry(
                trigger_tag.direction, market_ref, zones, atr_val, cfg,
                spread_price=spread_price,
            )
            entry, entry_type = pending, "limit"
            trace.add(
                "entry_mode", True,
                f"pending {entry_type} @ {entry:.2f} "
                f"({market_ref - entry:+.2f} from market {market_ref:.2f})"
                f" — {entry_note}",
            )

        # D-042/D-047/D-049 — structure-aware exits (legacy path): SL
        # beyond the structural invalidation, floored at 2.5x spread AND
        # min_sl_atr ATRs, capped at max_sl_atr, anchored past the
        # NEAREST strong TPO level; TP predicted from the nearest
        # opposing zone/liquidity/TPO target (tp=None means a barrier
        # stands before min_rr — the geometry itself rejects it).
        tpo_levels: list[dict] = []
        try:
            from app.analysis.tpo import tpo_profile_cached

            tpo_levels = tpo_profile_cached(
                base, lookback_minutes=cfg.tpo_lookback_min
            )["levels"]
        except Exception:  # noqa: BLE001 — anchoring is best-effort
            tpo_levels = []
        entry, sl, tp, target_note = smart_targets(
            base, trigger_tag.direction, entry, sl_base, cfg.rr,
            cfg.min_sl_atr, cfg.max_sl_atr, tpo_levels=tpo_levels,
            zones=zones, spread_price=spread_price,
            min_rr=cfg.tp_min_rr, max_tp_r=cfg.tp_max_r,
        )
        if tp is None:
            # poor geometry — the nearest opposing structure sits closer
            # than min_rr x risk: the market is predicted to hit the
            # barrier first
            trace.add("targets", False, target_note)
            _pulse_miss(pulse, trace)
            return Evaluation(None, trace.to_dict(), near_miss=True, pulse=pulse)
        realized_rr = abs(tp - entry) / max(abs(entry - sl), 1e-9)

    # D-041 — spread vs risk: entering at ask/exiting at bid costs one
    # spread; when that cost exceeds `max_spread_to_risk` of the stop
    # distance the trade is structurally unprofitable (noise-level stop).
    risk = abs(entry - sl)
    spread_risk_ok = risk > 0 and spread_price <= cfg.max_spread_to_risk * risk
    trace.add(
        "spread_risk",
        spread_risk_ok,
        f"spread ${spread_price:.2f} vs risk ${risk:.2f}"
        f" (max {cfg.max_spread_to_risk:.0%} of risk)",
    )
    if not spread_risk_ok:
        _pulse_miss(pulse, trace)
        return Evaluation(None, trace.to_dict(), near_miss=True, pulse=pulse)

    # D-061 — INSTITUTIONAL TRAP GATE (user directive: "কখন রিটেইলার
    # ট্রেডার ইন্সট্রিটিউনাল ট্রেডার এর কাছে ফেইল হয়েছো"): with ENTRY/SL
    # final, ask whether THIS trade is the retail side of an AMD
    # manipulation — a freshly reclaimed sweep against the direction, an
    # unswept liquidity pool inside the risk window, an opposing
    # displacement run, Judas timing. The user's trapped sell (signal on
    # the last red candle, filled a few pips lower, then several large
    # bullish candles) is exactly component A + C.
    trap: dict = {}
    try:
        from app.analysis.manipulation import trap_risk

        trap = trap_risk(
            base, trigger_tag.direction, entry, sl,
            state=amd or None, now=bar_close_time,
        )
        pulse["trap"] = {
            "risk": trap.get("risk", 0.0),
            "phase": trap.get("phase"),
            "reasons": trap.get("reasons", []),
        }
    except Exception:  # noqa: BLE001 — the trap gate is best-effort
        pass
    trap_risk_val = float(trap.get("risk", 0.0) or 0.0)
    if cfg.trap_filter and trap_risk_val >= cfg.trap_block_risk:
        trace.add(
            "trap_filter", False,
            f"institutional trap risk {trap_risk_val:.2f} >= "
            f"{cfg.trap_block_risk:.2f} — "
            + "; ".join(trap.get("reasons") or ["AMD structure against the trade"]),
        )
        _pulse_miss(pulse, trace)
        return Evaluation(None, trace.to_dict(), near_miss=True, pulse=pulse)
    trap_warned = trap_risk_val >= cfg.trap_warn_risk
    if trap_warned:
        trace.add(
            "trap_filter", True,
            f"warned — trap risk {trap_risk_val:.2f} (block at "
            f"{cfg.trap_block_risk:.2f}): "
            + "; ".join(trap.get("reasons") or ["AMD structure against the trade"]),
        )

    # D-064 — MARKET-STRUCTURE GUARD (user directive: "মার্কেট কোথায় গিয়ে
    # রেস্ট করে... কি এমন লজিক আছে যে মার্কেট এখন রিভার্স করবে?"): with
    # the signal direction final, ask the ladder — is this trade FADING a
    # mature run with no structural proof (no fresh CHoCH, no swept-
    # and-reclaimed pool)?
    #   - MOMENTUM trades (sfp / pullback) fading a mature run are the
    #     falling-knife entries the measured hazard table refuses —
    #     blocked, wait for the structure shift.
    #   - ZONE (location) trades keep the D-049 precedence ("সিগনাল মিস
    #     করা যাবে না" — the demand-zone BUY in a bearish HTF world):
    #     the location + rejection carries them, so a mature-run fade is
    #     VISIBLY tagged + discounted, never silently lost (the strict
    #     sweep gate remains counter_needs_sweep, off by default).
    # With-run trades on an EXTENDED run pay the chase penalty below
    # (the ~2 ATR rest eats a market-chase entry before the next leg).
    struct_adjust = 0.0
    if cfg.structure_guard and struct:
        from app.analysis.structure import reversal_evidence

        run_dir = struct.get("run_dir")
        run_len = int(struct.get("run") or 0)
        sig_dir = trigger_tag.direction
        with_run = (
            run_dir == "up" and sig_dir == "BUY"
        ) or (run_dir == "down" and sig_dir == "SELL")
        evidence = reversal_evidence(sig_dir, struct, amd.get("sweep"))
        location_trade = trigger == "zone"
        if not with_run and run_len >= cfg.structure_counter_legs \
                and not evidence:
            if location_trade:
                trace.add(
                    "structure_guard", True,
                    f"{sig_dir} zone fade of a {run_len}-leg {run_dir} run "
                    "with no CHoCH / sweep proof yet — location carries the "
                    "trade, confidence reduced (enable counter_needs_sweep "
                    "for the hard gate)",
                )
                struct_adjust -= cfg.structure_chase_penalty
            else:
                trace.add(
                    "structure_guard", False,
                    f"{sig_dir} fades a {run_len}-leg {run_dir} run with no "
                    "structural proof (no fresh CHoCH / swept pool) — "
                    "falling-knife fade refused, wait for the shift",
                )
                _pulse_miss(pulse, trace)
                return Evaluation(
                    None, trace.to_dict(), near_miss=True, pulse=pulse
                )
        if not with_run and run_len >= 2 and not evidence:
            # the run is not yet mature — the fade is early, not fatal:
            # visible warning + a modest confidence cost
            trace.add(
                "structure_guard", True,
                f"early fade — {run_len}-leg {run_dir} run intact, no "
                "CHoCH/sweep proof yet (confidence reduced)",
            )
            struct_adjust -= 0.05
        if with_run:
            phase = struct.get("phase")
            if phase == "extended" or run_len >= cfg.structure_exhaust_legs:
                trace.add(
                    "structure_guard", True,
                    f"with-trend on an extended run ({run_len} legs) — "
                    "chase risk: the measured ~2 ATR rest comes first "
                    "(confidence reduced; pending-at-magnet preferred)",
                )
                struct_adjust -= cfg.structure_chase_penalty
            elif phase == "resting":
                trace.add(
                    "structure_guard", True,
                    "with-trend at the REST zone — the exact down-rest-"
                    "continue entry (confidence boosted)",
                )
                struct_adjust += cfg.structure_rest_bonus

    # D-067 — the candle-battle gate (user directive: "কারা কাদের কে
    # ডোমেনেট করছে, কারা জিতেছে... রানিং ক্যান্ডেল এর রিয়েকশন"):
    # a signal that fires AGAINST a dominating candle-battle (the flow
    # AND the streak against it) pays confidence — the exact shape of
    # the user's trap report (SELL printed while buyers were stacking
    # bullish candles). Aligned domination earns a small bonus. A tug
    # of war costs nothing, and NOTHING is hard-blocked (D-049
    # "সিগনাল মিস করা যাবে না" — the trap gate owns hard blocks).
    flow_adjust = 0.0
    flow_note = ""
    if cfg.flow_guard and battle:
        b_state = battle.get("state")
        if b_state in ("buyers", "sellers"):
            streak = battle.get("streak") or {}
            streak_len = int(streak.get("len") or 0)
            dom_pct = float(
                battle.get("buy_pct" if b_state == "buyers" else "sell_pct") or 50.0,
            )
            against = (
                (b_state == "buyers" and trigger_tag.direction == "SELL")
                or (b_state == "sellers" and trigger_tag.direction == "BUY")
            )
            with_it = (
                (b_state == "buyers" and trigger_tag.direction == "BUY")
                or (b_state == "sellers" and trigger_tag.direction == "SELL")
            )
            if against and dom_pct >= cfg.flow_domination * 100.0 \
                    and streak_len >= cfg.flow_streak:
                flow_adjust -= cfg.flow_penalty
                flow_note = (
                    f"{trigger_tag.direction} fires into a dominating "
                    f"{b_state} battle — {round(dom_pct)}% of the flow, "
                    f"{streak_len} candles in a row for the {b_state} "
                    "(confidence reduced)"
                )
                trace.add("flow_guard", True, flow_note)
            elif with_it and dom_pct >= cfg.flow_domination * 100.0:
                flow_adjust += cfg.flow_bonus
                flow_note = (
                    f"{trigger_tag.direction} fires WITH the dominating "
                    f"{b_state} battle — {round(dom_pct)}% of the flow "
                    "pushing the trade's direction"
                )
                trace.add("flow_guard", True, flow_note)

    trace.add(
        "targets", True,
        f"TP predicted at {target_note} — realized rr {realized_rr:.2f}",
    )

    base_confidence = (
        1.0 * W_TREND
        + mtf_score * W_MTF
        + trigger_quality * W_TRIGGER
        + rsi_position(rsi_value, cfg, trigger_tag.direction) * W_RSI
        + (1.0 if session_ok else 0.0) * W_SESSION
        + atr_strength(atr_value, cfg) * W_ATR
    ) / max(W_TREND + W_MTF + W_TRIGGER + W_RSI + W_SESSION + W_ATR, 1e-9)
    # D-051 — the REAL-TIME whale pulse boost: big-player entries in the
    # trade direction make the signal MORE believable (user directive:
    # "আমি মনে করি সেই সিগন্যাল টাকে বেশি গুরুত্ব দেওয়া উচিত")
    whale_ok = any(
        f["name"] == "whale_pulse" and f["ok"] for f in factors
    ) if factors else False
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
    if whale_ok:
        # D-051 — big players in the trade direction: the signal itself
        # gets the extra weight (clamped to 1.0 below)
        confidence = min(confidence + cfg.whale_confidence_boost, 1.0)
    if trap_warned:
        # D-061 — the trade fires but the app shows WHY it is suspect:
        # confidence pays for the AMD structure stacked against it
        confidence = max(0.0, confidence - cfg.trap_conf_penalty)
    if struct_adjust:
        # D-064 — the structure ladder's verdict: chasing an extended
        # run costs confidence; the rest-zone continuation entry earns it
        confidence = max(0.0, min(1.0, confidence + struct_adjust))
    if flow_adjust:
        # D-067 — the candle battle's verdict: fighting a dominating
        # flow costs confidence; riding it earns a small bonus
        confidence = max(0.0, min(1.0, confidence + flow_adjust))
    trace_dict = trace.to_dict()
    trace_dict["trigger"] = trigger  # survives inside signals.trace JSON (D-041)
    trace_dict["confluence_factors"] = factors  # D-042 — Signal Analysis panel
    # D-061 — the market-context block the app surfaces on every signal
    # (phase, trap reasons, kill-zone / Judas timing, news verdict): the
    # user asked "এই বিষয় টা কিভাবে আমার অ্যাপ বুজবে। এবং আমিও দেখতে
    # পারবো" — it rides the payload (WS) AND the trace (DB persistence
    # keeps only trace, so the context lives there too).
    ses_ctx = (trap.get("session") or {})
    context = {
        "amd": {
            "phase": (amd.get("phase") if amd else None) or trap.get("phase"),
            "note": (amd.get("note") if amd else None) or trap.get("amd_note"),
            "sweep": amd.get("sweep") if amd else None,
            "displacement": amd.get("displacement") if amd else None,
        },
        "trap": {
            "risk": trap_risk_val,
            "warned": trap_warned,
            "reasons": trap.get("reasons") or [],
        },
        "session": {
            "name": session_name,
            "killzone": bool(ses_ctx.get("in_killzone")),
            "judas_window": bool(ses_ctx.get("judas_window")),
            "note": ses_ctx.get("note"),
        },
        "structure": {
            "run": struct.get("run") if struct else None,
            "run_dir": struct.get("run_dir") if struct else None,
            "phase": struct.get("phase") if struct else None,
            "p_reversal": struct.get("p_reversal") if struct else None,
            "action": struct.get("action") if struct else None,
            "choch": struct.get("choch_fresh") if struct else None,
            "magnets": (struct.get("magnets") or [])[:3] if struct else [],
        },
        "flow": {  # D-067 — the candle battle at signal time
            "state": battle.get("state") if battle else None,
            "buy_pct": battle.get("buy_pct") if battle else None,
            "sell_pct": battle.get("sell_pct") if battle else None,
            "streak": battle.get("streak") if battle else None,
            "net_atr": battle.get("net_atr") if battle else None,
            "wins": battle.get("wins") if battle else None,
            "verdict": battle.get("verdict") if battle else None,
            "note": flow_note or None,
        },
        "news": news.value,
    }
    trace_dict["context"] = context
    payload = {
        "direction": trigger_tag.direction,
        "entry": round(entry, 2),
        "sl": round(sl, 2),
        "tp": round(tp, 2),
        "rr": round(realized_rr, 2),
        "target_note": target_note,
        "confidence": round(min(max(confidence, 0.0), 1.0), 3),
        "trigger": trigger,
        "entry_type": entry_type,  # D-050 — "market" | "limit"
        "geometry": "drawing" if geo is not None else "legacy",  # D-057
        "market_ref": round(market_ref, 2),  # market at signal time
        "entry_note": entry_note,  # D-050 — the POI anchor description
        "trace": trace_dict,
        "context": context,  # D-061 — AMD phase / trap / session / news
        # D-067 — flow note rides the payload top level too (the signals
        # list renders it without digging into context)
        "flow_note": flow_note or None,
        "bar_time": base["time_utc"].iloc[-1],
        "session": session_name,
        "spread_points": spread_points,
    }
    # D-051 — the fired-signal summary for the strategy radar
    pulse["fired"] = {
        "direction": payload["direction"],
        "entry": payload["entry"],
        "sl": payload["sl"],
        "tp": payload["tp"],
        "entry_type": payload["entry_type"],
        "market_ref": payload["market_ref"],
        "confidence": payload["confidence"],
        "trigger": trigger,
        "whale": whale_ok,
        "trap": {  # D-061 — the fired trade's trap verdict on the radar
            "risk": trap_risk_val,
            "warned": trap_warned,
            "phase": (trap.get("phase") or (amd.get("phase") if amd else None)),
        },
        "flow": {  # D-067 — the fired trade's candle-battle verdict
            "state": (battle.get("state") if battle else None),
            "buy_pct": (battle.get("buy_pct") if battle else None),
            "sell_pct": (battle.get("sell_pct") if battle else None),
            "note": flow_note or None,
        },
    }
    _pulse_miss(pulse, trace)  # fills checks (+ no near-miss on a fire)
    pulse["near_miss"] = None
    return Evaluation(payload, trace_dict, near_miss=False, pulse=pulse)


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
        # D-050 — the budget SPLITS: FILLED/active signals count against
        # max_positions, PENDING (unfilled limit) signals count against
        # max_pending_signals. Unfilled pendings must never clog the flow
        # (user directive: "অটো ট্রেড ওপেন থাকুক বা না থাকুক সিগন্যাল আসবে" —
        # signals keep coming regardless).
        if self._in_cooldown(cfg, bar_open):
            await self._broadcast_pulse(
                symbol, tf, bar_close_time,
                {"price": round(float(closed_bar["c"]), 2),
                 "near_miss": f"cooldown — {cfg.cooldown_bars} bars after the last signal"},
            )
            return
        active_now = tracker.active
        if callable(active_now):  # duck-typed fakes may expose a method
            active_now = active_now()
        filled = [s for s in active_now if getattr(s, "status", "active") != "pending"]
        pending = [s for s in active_now if getattr(s, "status", "") == "pending"]
        if len(filled) >= max(int(cfg.max_positions), 1):
            await self._broadcast_pulse(
                symbol, tf, bar_close_time,
                {"price": round(float(closed_bar["c"]), 2),
                 "near_miss": f"position budget full ({len(filled)}/{cfg.max_positions})"},
            )
            return
        if len(pending) >= max(int(cfg.max_pending_signals), 1):
            await self._broadcast_pulse(
                symbol, tf, bar_close_time,
                {"price": round(float(closed_bar["c"]), 2),
                 "near_miss": (
                     f"pending budget full ({len(pending)}/"
                     f"{cfg.max_pending_signals}) — orders awaiting fills")},
            )
            return

        try:
            # D-049 — 1500 bars (24h of M1): the old 200-bar fetch starved
            # every depth-dependent feature — the TPO profile saw 3.3h
            # instead of 24h, PDH/PDL liquidity levels never existed, and
            # zone origins lived in a 2.7h window — which forced the TP
            # onto the fixed rr multiple ("SL/TP always the same ratio").
            base = await source.get_rates(
                symbol, cfg.timeframe,
                max(1500, cfg.sfp_lookback + 5, cfg.tpo_lookback_min + 10),
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
            # D-057 — M5/M15 frames carry the SAME bar counts the drawing
            # layer fetches (services/analysis.py BARS_PER_TF) so the
            # drawing-true geometry sees the very zones/liquidity the
            # chart draws; other TFs keep the light 100-bar fetch
            from app.analysis.setup_geometry import GEOMETRY_BARS

            bars = max(100, GEOMETRY_BARS.get(higher, 0))
            frame = await self._htf_frame(source, symbol, higher, bars)
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
        # D-051 — the strategy radar frame: every M1 close, signal or not,
        # tells the UI which strategy is doing what, within seconds
        await self._broadcast_pulse(symbol, tf, bar_close_time, ev.pulse)
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
                entry_type=payload.get("entry_type", "market"),
                market_ref=payload.get("market_ref"),
                # D-061 — the pre-fill displacement-guard anchor: the
                # tracker needs the ATR scale to recognize institutional
                # bodies printing against a WAITING limit order
                atr_ref=float(
                    atr_calc(base, cfg.atr_period) or 0.0
                ) or None,
            )
        )

    def _in_cooldown(self, cfg: EngineConfig, bar_open: datetime) -> bool:
        if self._last_signal_bar is None:
            return False
        elapsed = (bar_open - self._last_signal_bar).total_seconds()
        return elapsed < cfg.cooldown_bars * TIMEFRAME_MINUTES[cfg.timeframe] * 60

    async def _broadcast_pulse(
        self, symbol: str, tf: str, bar_close_time: datetime, pulse: dict | None
    ) -> None:
        """D-051 — strategy_pulse WS frame (user directive: "অ্যাপ এর
        প্রত্যেকটি স্টাডিজির ডাটা রিয়েল টাইমে সেকেন্ডের মধ্যে দেখাতে হবে").

        Broadcast is best-effort — a WS hiccup must never touch the
        engine's trading path.
        """
        if pulse is None:
            return
        try:
            from app.mt5.base import market_key

            # D-061 — pulses carry the MARKET KEY (XAUUSD), not the
            # broker-suffixed concrete symbol (XAUUSDm): the strategy
            # radar keys its state by the platform market name
            sym = market_key(symbol)
        except Exception:  # noqa: BLE001 — normalization must never break
            sym = symbol
        try:
            await self._hub.broadcast_all(
                "strategy_pulse",
                {**pulse, "symbol": sym, "tf": tf,
                 "ts": pulse.get("ts") or bar_close_time.isoformat()},
            )
        except Exception:  # noqa: BLE001
            logger.debug("strategy_pulse broadcast failed", exc_info=True)

    async def _log(self, level: str, message: str) -> None:
        logger.log(getattr(logging, level.upper(), logging.INFO), "%s", message)
        try:
            await self._hub.broadcast_all(
                "engine_log", {"level": level, "message": message}
            )
        except Exception:  # noqa: BLE001
            pass
