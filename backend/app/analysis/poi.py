"""POI — Point-of-Interest zone engine (D-048, user directive).

"Best POI ZONE, SUPPLY ZONE, DEMAND ZONE — এই গুলো তে সিগন্যাল দিতে হবে,
মিস করা যাবে না" — signals must fire AT these zones; none may be missed.

Unifies every zone concept the platform computes into ONE ranked list of
points of interest:

  source "sd"   supply/demand base zones (swing base + displacement)
  source "ob"   order blocks (institutional entry footprints)
  source "fvg"  fair value gaps (unfilled / freshly-filled inefficiencies)
  source "tpo"  time-at-price levels (D-047 — prices where the market
                SPENT TIME become marked S/R)
  source "pd"   prior-day high/low (classic liquidity draws)

Each zone carries a normalized 0..1 QUALITY built from hard-coded
weights: impulse strength of the origin move, freshness (unretested
full, FIRST retest near-full — that is the entry), age decay,
time-at-price reinforcement and HTF origin (M15 structure = bigger
players). The signal engine's zone-retest trigger (app/engine/zones.py)
consumes this list: price returning to a QUALITY POI zone and rejecting
IS the entry — no other strategy must agree at that moment (user
directive: "সব গুলো স্ট্রাটেজি একই সময়ে AGREE নাও থাকতে পারে").

Pure functions on closed bars only — no lookahead, no I/O.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from app.analysis import smc
from app.analysis.indicators import atr as atr_last
from app.analysis.tpo import tpo_profile_cached

#: base-TF bars scanned for zone origins (D-049: 160 -> 480 — 8 hours
#: of zone formation on M1; the old 160-bar window only saw 2.7h and
#: starved the ranked list of anything but the freshest zones)
POI_WINDOW = 480
#: zone age (minutes) up to which quality stays full
AGE_FULL_MIN = 240.0
#: zone age (minutes) at which the age component bottoms out (24h)
AGE_ZERO_MIN = 1440.0
#: TPO minutes inside the band that count as full reinforcement
TPO_FULL_MINUTES = 30.0
#: TPO minutes below this add nothing (noise)
TPO_MIN_MINUTES = 8.0
#: FVG fills older than this (minutes) stop being POIs
FVG_FILL_WINDOW_MIN = 20.0
# ------------------------------------------------------------- D-069 FVG
# (user directive: "ছোট বড় অনেক fvg তৈরি হয়, সব fvg গুরুত্ব পূর্ণ না" —
#  many small and large FVGs form, NOT all of them matter)
#: hard noise floor — a gap below this fraction of the frame's OWN ATR
#: is spread noise and NEVER becomes a POI (self-scaling: M1 gaps are
#: measured on M1-ATR, M15 gaps on M15-ATR)
FVG_MIN_ATR = 0.30
#: the 3-bar displacement (in ATRs) that must have minted the gap —
#: note disp >= gap mathematically for every FVG (close[k+1] sits above
#: the gap, open[k-1] below it), so a floor ABOVE FVG_MIN_ATR is what
#: gives the rule teeth: the leg must carry NET PROGRESS past the gap
#: itself, not a spike-and-round-trip
FVG_MIN_DISP = 0.40
#: gap width that earns the full size component (in frame ATRs)
FVG_SIZE_FULL_ATR = 1.00
#: displacement that earns the full displacement component (in ATRs)
FVG_DISP_FULL_ATR = 1.00
#: consequent encroachment — once price has traded this deep into the
#: gap the inefficiency is considered mitigated (freshness drops)
FVG_CE = 0.50
#: partial fill, CE intact, NOT recent — discounted freshness
FVG_FILL_STALE = 0.65
#: FVG-specific quality weights (size + disp + fresh + age + tpo = 1.0)
FVG_W_SIZE = 0.35
FVG_W_DISP = 0.15
FVG_W_FRESH = 0.30
FVG_W_AGE = 0.15
FVG_W_TPO = 0.05
#: gap-in-gap bonus — a base-TF gap stacked inside a same-side HTF gap
#: (the multi-timeframe alignment the chart shows as nested boxes)
FVG_STACK_BONUS = 0.10
#: premium/discount adjustment — a demand gap in the discount half of
#: the recent range (buy low) / a supply gap in the premium half (sell
#: high) earns +, the wrong half pays -
FVG_PD_ADJ = 0.05
#: HTF frames whose FVGs join the ranked POI list (institutional gaps —
#: the same frames the chart drawings layer renders)
FVG_HTF_TFS = ("M5", "M15")
#: hard-coded quality weights (sum of the first four = 1.0; HTF is a bonus)
W_IMPULSE = 0.35
W_FRESH = 0.30
W_AGE = 0.15
W_TPO = 0.10
W_HTF_BONUS = 0.10
#: freshness component values
FRESH_UNTOUCHED = 1.0
FRESH_FIRST_TOUCH = 0.85  # the retest happening RIGHT NOW — the ICT entry
FRESH_STALE = 0.45
#: a close this far beyond the far edge (in ATRs) breaks the zone
BROKEN_TOL_ATR = 0.10


def _clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else (1.0 if x > 1.0 else x)


def _age_q(t: Any, last_t: Any) -> float:
    """Age component: full inside 4h, decaying to 0.15 by 24h."""
    try:
        age = (pd.Timestamp(last_t) - pd.Timestamp(t)).total_seconds() / 60.0
    except (TypeError, ValueError):
        return 0.0
    if age <= AGE_FULL_MIN:
        return 1.0
    if age >= AGE_ZERO_MIN:
        return 0.15
    return 1.0 - 0.85 * (age - AGE_FULL_MIN) / (AGE_ZERO_MIN - AGE_FULL_MIN)


def _impulse_q(impulse_atr: float | None) -> float:
    """Impulse component: 1.0x ATR -> 0.0, 3.0x ATR -> 1.0."""
    if not impulse_atr or impulse_atr <= 0:
        return 0.25  # sources without displacement (TPO/PD) get a neutral fill
    return _clamp01((impulse_atr - 1.0) / 2.0)


def _freshness_q(zone: dict, recent_from: Any) -> float:
    """Freshness component from mitigation/fill timestamps."""
    if not zone.get("mitigated") and not zone.get("filled"):
        return FRESH_UNTOUCHED
    mt = zone.get("mit_t") or zone.get("fill_t")
    if mt is None:
        return FRESH_STALE
    try:
        if pd.Timestamp(mt) >= pd.Timestamp(recent_from):
            return FRESH_FIRST_TOUCH
    except (TypeError, ValueError):
        pass
    return FRESH_STALE


def _tpo_minutes(levels: list[dict], lo: float, hi: float) -> float:
    """How many TPO minutes sit inside the [lo, hi] band."""
    total = 0.0
    for lv in levels or []:
        p = float(lv.get("price", 0.0))
        if lo - 0.5 <= p <= hi + 0.5:
            total += float(lv.get("minutes", 0.0))
    return total


def zone_quality(
    *,
    impulse_atr: float | None,
    fresh_q: float,
    age_q: float,
    tpo_minutes: float,
    htf: bool,
) -> float:
    """0..1 — the hard-coded POI quality model (D-048).

    impulse 0.35 — displacement that created the zone (institutional size)
    fresh   0.30 — untested zones + the FIRST retest score highest
    age     0.15 — fresh origins matter; day-old zones decay
    tpo     0.10 — the market also SPENT TIME at this level (reinforcement)
    htf    +0.10 — zone originates on M15 structure (bigger timeframe)
    """
    tpo_q = 0.0
    if tpo_minutes >= TPO_MIN_MINUTES:
        tpo_q = _clamp01(tpo_minutes / TPO_FULL_MINUTES)
    q = (
        W_IMPULSE * _impulse_q(impulse_atr)
        + W_FRESH * _clamp01(fresh_q)
        + W_AGE * _clamp01(age_q)
        + W_TPO * tpo_q
    )
    if htf:
        q += W_HTF_BONUS
    return round(_clamp01(q), 3)


def _fvg_freshness(g: dict, recent_from: Any) -> float:
    """CE-aware FVG freshness (D-069 — the ICT consequent-encroachment
    rule the old model could not see: any gap price had not FULLY
    filled scored "untouched" 1.0, so a half-mitigated gap was as fresh
    as a virgin one).

      untouched            FRESH_UNTOUCHED 1.00
      filling NOW, CE ok   FRESH_FIRST_TOUCH 0.85 — the retest IS the
                           entry
      partial, CE ok, old  FVG_FILL_STALE 0.65 — still tradeable
      past CE (>= 0.50)    FRESH_STALE 0.45 — the gap is mitigated
      fully filled         FRESH_STALE 0.45 (only fills younger than
                           FVG_FILL_WINDOW_MIN survive the drop below)
    """
    pct = float(g.get("fill_pct", 0.0) or 0.0)
    if pct <= 0.0:
        return FRESH_UNTOUCHED
    if pct >= FVG_CE:
        return FRESH_STALE
    ft = g.get("fill_t")
    try:
        if ft is not None and pd.Timestamp(ft) >= pd.Timestamp(recent_from):
            return FRESH_FIRST_TOUCH
    except (TypeError, ValueError):
        pass
    return FVG_FILL_STALE


def fvg_zone_quality(
    *,
    gap_atr: float,
    disp: float,
    fresh_q: float,
    age_q: float,
    tpo_minutes: float,
    htf: bool,
) -> float:
    """0..1+ — the D-069 FVG importance model (un-clamped: the caller
    adds the stack/pd adjustments, then clamps).

    size  0.35 — gap width in frame-ATRs (FVG_MIN_ATR -> 0,
                   FVG_SIZE_FULL_ATR -> 1): the inefficiency must be
                   REAL before anything else matters
    disp  0.15 — the displacement leg that minted the gap (ICT: gaps
                   born of drifts are noise, gaps born of displacement
                   are institutional footprints)
    fresh 0.30 — CE-aware (see _fvg_freshness)
    age   0.15 — the same age model every other zone uses
    tpo   0.05 — time-at-price reinforcement (small: gaps are momentum
                   artifacts, not rest levels)
    htf  +0.10 — born on M5/M15 (the institutional frames)
    """
    size_q = _clamp01(
        (gap_atr - FVG_MIN_ATR) / (FVG_SIZE_FULL_ATR - FVG_MIN_ATR)
    )
    disp_q = _clamp01(disp / FVG_DISP_FULL_ATR)
    tpo_q = 0.0
    if tpo_minutes >= TPO_MIN_MINUTES:
        tpo_q = _clamp01(tpo_minutes / TPO_FULL_MINUTES)
    q = (
        FVG_W_SIZE * size_q
        + FVG_W_DISP * disp_q
        + FVG_W_FRESH * _clamp01(fresh_q)
        + FVG_W_AGE * _clamp01(age_q)
        + FVG_W_TPO * tpo_q
    )
    if htf:
        q += W_HTF_BONUS
    return q


def _pd_position(side: str, lo: float, hi: float,
                 range_lo: float, range_hi: float) -> str:
    """Range half the gap sits in ("premium" / "discount" / "mid").

    Demand below the range midpoint = discount (the half BUYs belong
    to); supply above it = premium (the half SELLs belong to).
    """
    try:
        mid = (float(range_lo) + float(range_hi)) / 2.0
        zmid = (float(lo) + float(hi)) / 2.0
    except (TypeError, ValueError):
        return "mid"
    if abs(zmid - mid) <= 0.10 * max(range_hi - range_lo, 1e-9):
        return "mid"
    if zmid < mid:
        return "discount"
    return "premium"


_FVG_MEMO: dict[tuple, list[dict]] = {}


def _htf_fvgs(tf: str, frame: pd.DataFrame, max_gaps: int) -> list[dict]:
    """detect_fvg on an HTF frame, memoized on content identity.

    HTF frames only change when their own bar closes (closed-bars-only
    slices), so (tf, last-timestamp, length) tracks content within ONE
    symbol's series — but the engine evaluates multiple markets (XAUUSD
    + BTCUSD) whose M5/M15 frames share timestamps and lengths, so the
    key also carries the frame's first/last closes (price-series
    identity — two symbols never coincide there).
    """
    try:
        key = (
            tf, str(pd.Timestamp(frame["time_utc"].iloc[-1])),
            int(len(frame)), int(max_gaps),
            round(float(frame["c"].iloc[0]), 4),
            round(float(frame["c"].iloc[-1]), 4),
        )
    except Exception:  # noqa: BLE001 — memo must never break analysis
        key = None
    if key is not None:
        hit = _FVG_MEMO.get(key)
        if hit is not None:
            return hit
    out = smc.detect_fvg(frame, max_gaps=max_gaps)
    if key is not None:
        if len(_FVG_MEMO) > 8:
            _FVG_MEMO.clear()
        _FVG_MEMO[key] = out
    return out


def _broken(
    closes: np.ndarray,
    side: str,
    lo: float,
    hi: float,
    tol: float,
    start_i: int,
) -> bool:
    """A CLOSE beyond the far edge (± tolerance) invalidates the zone.

    Vectorized (D-048): this scan runs for every candidate zone on every
    bar close — the pandas .iloc loop it replaced dominated backtest time.
    """
    if start_i >= len(closes):
        return False
    seg = closes[start_i:]
    if side == "demand":
        return bool((seg < lo - tol).any())
    return bool((seg > hi + tol).any())


def _origin_index(t_arr: np.ndarray, t: Any) -> int:
    """Row position of the zone origin (+2: the move needs the next bar).

    D-049 bug fix: the old `frame.index.get_indexer([Timestamp])` ran
    against the RESET (integer) index and ALWAYS returned -1 — every
    zone's broken-scan silently started at row 2, not at its origin.
    D-049 perf: `t_arr` is the frame's time column converted ONCE per
    poi_zones call (the per-zone to_numpy conversion this replaced was
    31% of the whole backtest runtime).
    """
    i = int(np.searchsorted(t_arr, t))
    if i < len(t_arr) and t_arr[i] == t:
        return i + 2
    return 2  # not found (never expected) — conservative early start


def _cfg_flag(cfg: Any, name: str, default: bool) -> bool:
    """Duck-typed config read (EngineConfig-like; None = hard-coded)."""
    try:
        return bool(getattr(cfg, name, default))
    except Exception:  # noqa: BLE001 — config reads must never break POIs
        return default


def _cfg_num(cfg: Any, name: str, default: float) -> float:
    try:
        v = float(getattr(cfg, name, default))
    except (TypeError, ValueError):
        return default
    return v if v == v else default  # NaN guard


#: TF minute lengths for the frame-relative FVG fill window
_FVG_TF_MIN = {"M1": 1.0, "M5": 5.0, "M15": 15.0}


def poi_zones(
    base: pd.DataFrame,
    htf: dict[str, pd.DataFrame] | None = None,
    max_zones: int = 24,
    cfg: Any = None,  # EngineConfig-like (D-069 knobs); None = constants
) -> list[dict]:
    """Ranked POI zone list (highest quality first), all sides mixed.

    `base` = closed bars of the trigger TF (M1). `htf` optional — when it
    carries an "M15" frame its S/D zones + order blocks join the list as
    HTF zones (quality bonus, institutional timeframe). Zones are deduped
    (same-side overlaps keep the higher-quality band) and broken zones are
    dropped — a close beyond the far edge means the zone is invalidated.

    D-069 (user directive: "ছোট বড় অনেক fvg তৈরি হয়, সব fvg গুরুত্ব
    পূর্ণ না") — FVGs go through the importance model: hard noise floor
    (>= FVG_MIN_ATR of the frame's own ATR), displacement floor, CE-aware
    freshness, and M5/M15 HTF gaps join the ranked list (gap-in-gap
    stacking bonus + premium/discount adjust). `cfg.fvg_importance=False`
    restores the pre-D-069 behavior exactly (A/B escape hatch).
    """
    out: list[dict] = []
    if base is None or len(base) < 30:
        return out
    frame = base.iloc[-POI_WINDOW:].reset_index(drop=True)
    atr_val = atr_last(frame, 14) or 1e-9
    last_t = frame["time_utc"].iloc[-1]
    closes_np = frame["c"].to_numpy(dtype=float)
    # ONE datetime-array conversion per call — _origin_index used to
    # re-convert the whole column for EVERY candidate zone (31% of the
    # backtest runtime lived here)
    t_np = frame["time_utc"].to_numpy()
    broken_tol = BROKEN_TOL_ATR * atr_val
    # the "first touch" window: a mitigation in the last 3 bars is the
    # retest happening RIGHT NOW (the classic zone entry)
    recent_from = frame["time_utc"].iloc[-3] if len(frame) >= 3 else last_t

    # TPO profile once — reinforcement minutes for every zone band
    # (D-049: memoized — evaluate/confluence/POI share ONE profile per bar)
    try:
        prof = tpo_profile_cached(base, lookback_minutes=1440)
        tpo_levels: list[dict] = prof.get("levels", [])
    except Exception:  # noqa: BLE001 — reinforcement is best-effort
        tpo_levels = []

    def _add(
        side: str,
        source: str,
        t: Any,
        lo: float,
        hi: float,
        impulse: float | None,
        htf_origin: bool,
        mitigated: bool = False,
        mit_t: Any = None,
        filled: bool = False,
        fill_t: Any = None,
        quality: float | None = None,
        extra: dict | None = None,
        skip_broken: bool = False,
    ) -> None:
        if not skip_broken and _broken(
            closes_np, side, float(lo), float(hi), broken_tol,
            _origin_index(t_np, t),
        ):
            return
        zone = {
            "side": side,
            "source": source,
            "t": t,
            "lo": float(lo),
            "hi": float(hi),
            "impulse": float(impulse) if impulse else None,
            "mitigated": mitigated,
            "mit_t": mit_t,
            "filled": filled,
            "fill_t": fill_t,
            "htf": htf_origin,
        }
        zone["quality"] = (
            quality if quality is not None else zone_quality(
                impulse_atr=impulse,
                fresh_q=_freshness_q(zone, recent_from),
                age_q=_age_q(t, last_t),
                tpo_minutes=_tpo_minutes(tpo_levels, float(lo), float(hi)),
                htf=htf_origin,
            )
        )
        if extra:
            zone.update(extra)
        out.append(zone)

    # --- 1/2. base-TF supply/demand zones + order blocks (D-049: caps
    # raised so the deeper 480-bar window actually contributes zones)
    for z in smc.detect_supply_demand(frame, max_zones=8):
        _add(z["side"], "sd", z["t"], z["lo"], z["hi"],
             z.get("impulse"), False)
    for ob in smc.detect_order_blocks(frame, max_zones=8):
        side = "demand" if ob["side"] == "bullish" else "supply"
        _add(side, "ob", ob["t"], ob["lo"], ob["hi"],
             ob.get("impulse"), False, ob["mitigated"], ob.get("mit_t"))

    # --- 3. fair value gaps — D-069 importance model (user directive:
    # "ছোট বড় অনেক fvg তৈরি হয়, সব fvg গুরুত্ব পূর্ণ না" — not every
    # gap matters). Legacy path (fvg_importance=False) keeps the exact
    # pre-D-069 code (A/B escape hatch).
    fvg_on = _cfg_flag(cfg, "fvg_importance", True)
    fvg_htf_on = fvg_on and _cfg_flag(cfg, "fvg_htf_enabled", True)
    fvg_min_atr = _cfg_num(cfg, "fvg_min_atr", FVG_MIN_ATR)
    fvg_min_disp = _cfg_num(cfg, "fvg_min_disp", FVG_MIN_DISP)
    fvg_stack = _cfg_num(cfg, "fvg_stack_bonus", FVG_STACK_BONUS)
    fvg_pd = _cfg_num(cfg, "fvg_pd_adjust", FVG_PD_ADJ)
    # base-TF full-fill window — flat 20 min = the exact legacy value
    base_fill_win = FVG_FILL_WINDOW_MIN
    range_lo = float(frame["l"].min())
    range_hi = float(frame["h"].max())

    # base-TF gap candidates FIRST (floors + full-fill window), so the
    # stacking check can run BOTH ways: whichever same-side pair wins
    # the dedupe below carries the gap-in-gap flag — the zone the user
    # sees on the chart is ONE nested level, not two
    base_cands: list[tuple[dict, str, float, float]] = []  # (g, side, atr, disp)
    for g in smc.detect_fvg(frame, max_gaps=24 if fvg_on else 10):
        if not fvg_on:
            # legacy path — the exact pre-D-069 code (full_t restores
            # the old full-fill fill_t semantics for the window check)
            if g.get("filled"):
                ft = g.get("full_t") or g.get("fill_t")
                try:
                    age = (pd.Timestamp(last_t) - pd.Timestamp(ft)
                           ).total_seconds() / 60.0
                except (TypeError, ValueError):
                    age = 1e9
                if age > base_fill_win:
                    continue
            side = "demand" if g["side"] == "bullish" else "supply"
            gap_atr = (g["hi"] - g["lo"]) / atr_val
            _add(side, "fvg", g["t"], g["lo"], g["hi"],
                 1.0 + 2.0 * _clamp01(gap_atr), False,
                 g.get("filled", False), g.get("full_t") or g.get("fill_t"))
            continue
        # D-069 — the importance gate: real size + real displacement
        gap_atr = float(g.get("gap_atr", 0.0) or 0.0)
        disp = float(g.get("disp", 0.0) or 0.0)
        if gap_atr < fvg_min_atr or disp < fvg_min_disp:
            continue  # spread noise / spike-round-trip — never a POI
        if g.get("filled"):
            ft = g.get("full_t") or g.get("fill_t")
            try:
                age = (pd.Timestamp(last_t) - pd.Timestamp(ft)
                       ).total_seconds() / 60.0
            except (TypeError, ValueError):
                age = 1e9
            if age > base_fill_win:
                continue  # full fills older than the window stop being POIs
        side = "demand" if g["side"] == "bullish" else "supply"
        base_cands.append((g, side, gap_atr, disp))

    # HTF (M5/M15) institutional gaps. Self-scaled: each frame's gaps
    # are floored/weighted on that frame's OWN ATR, so a 0.30-ATR M15
    # gap is a real institutional inefficiency whatever the volatility.
    htf_fvg_bands: list[dict] = []
    if fvg_htf_on:
        for tf in FVG_HTF_TFS:
            hf = (htf or {}).get(tf)
            if hf is None or len(hf) < 30:
                continue
            h_frame = hf.iloc[-POI_WINDOW:].reset_index(drop=True)
            h_closes = h_frame["c"].to_numpy(dtype=float)
            h_t_np = h_frame["time_utc"].to_numpy()
            h_atr = atr_last(h_frame, 14) or atr_val
            h_tol = BROKEN_TOL_ATR * h_atr
            tf_min = _FVG_TF_MIN.get(tf, 15.0)
            h_recent = (
                h_frame["time_utc"].iloc[-3]
                if len(h_frame) >= 3 else h_frame["time_utc"].iloc[-1]
            )
            fill_win = max(FVG_FILL_WINDOW_MIN, 2.0 * tf_min)
            h_range_lo = float(h_frame["l"].min())
            h_range_hi = float(h_frame["h"].max())
            for g in _htf_fvgs(tf, h_frame, 12):
                gap_atr = float(g.get("gap_atr", 0.0) or 0.0)
                disp = float(g.get("disp", 0.0) or 0.0)
                if gap_atr < fvg_min_atr or disp < fvg_min_disp:
                    continue  # noise on the institutional frame too
                side = "demand" if g["side"] == "bullish" else "supply"
                g_lo, g_hi = float(g["lo"]), float(g["hi"])
                # invalidation, dual-clock (both start AFTER the 3-candle
                # pattern completes — a scan starting inside the forming
                # impulse would falsely break the gap with its own M1
                # closes): HTF closes on the HTF clock, M1 closes from
                # the pattern's completion minute
                if _broken(
                    h_closes, side, g_lo, g_hi, h_tol,
                    _origin_index(h_t_np, g["t"]),
                ):
                    continue
                try:
                    done_at = pd.Timestamp(g["t"]) + pd.Timedelta(
                        minutes=2 * tf_min
                    )
                    m1_start = int(np.searchsorted(t_np, done_at))
                except (TypeError, ValueError):
                    m1_start = len(closes_np)
                if _broken(
                    closes_np, side, g_lo, g_hi, broken_tol, m1_start
                ):
                    continue
                htf_fvg_bands.append({
                    "side": side, "lo": g_lo, "hi": g_hi, "tf": tf,
                })
                if g.get("filled"):
                    ft = g.get("full_t") or g.get("fill_t")
                    try:
                        age = (
                            pd.Timestamp(last_t) - pd.Timestamp(ft)
                        ).total_seconds() / 60.0
                    except (TypeError, ValueError):
                        age = 1e9
                    if age > fill_win:
                        continue
                # gap-in-gap, HTF side: a same-side base gap nested inside
                # this band makes THIS zone the stacked level
                stacked = any(
                    b_side == side
                    and float(bg["lo"]) <= g_hi and float(bg["hi"]) >= g_lo
                    for bg, b_side, _a, _d in base_cands
                )
                pos = _pd_position(
                    side, g_lo, g_hi, h_range_lo, h_range_hi
                )
                pd_adjust = (
                    fvg_pd
                    if (side == "demand" and pos == "discount")
                    or (side == "supply" and pos == "premium")
                    else (-fvg_pd if pos in ("premium", "discount") else 0.0)
                )
                q = round(_clamp01(fvg_zone_quality(
                    gap_atr=gap_atr,
                    disp=disp,
                    fresh_q=_fvg_freshness(g, h_recent),
                    age_q=_age_q(g["t"], last_t),
                    tpo_minutes=_tpo_minutes(tpo_levels, g_lo, g_hi),
                    htf=True,
                ) + pd_adjust + (fvg_stack if stacked else 0.0)), 3)
                _add(
                    side, "fvg", g["t"], g_lo, g_hi,
                    None, True, g.get("filled", False),
                    g.get("full_t") or g.get("fill_t"),
                    quality=q, skip_broken=True,
                    extra={
                        "gap_atr": round(gap_atr, 2),
                        "disp": round(disp, 2),
                        "fill_pct": round(
                            float(g.get("fill_pct", 0.0) or 0.0), 2),
                        "stacked": bool(stacked),
                        "htf_tf": tf,
                        "pd": pos,
                    },
                )

    # base-TF zones (the candidates survived the floors + window above)
    for g, side, gap_atr, disp in base_cands:
        g_lo, g_hi = float(g["lo"]), float(g["hi"])
        # gap-in-gap, base side: nested inside a same-side HTF band
        stack_hit = next(
            (b for b in htf_fvg_bands
             if b["side"] == side and g_lo <= b["hi"] and g_hi >= b["lo"]),
            None,
        )
        stacked = stack_hit is not None
        pos = _pd_position(side, g_lo, g_hi, range_lo, range_hi)
        pd_adjust = (
            fvg_pd
            if (side == "demand" and pos == "discount")
            or (side == "supply" and pos == "premium")
            else (-fvg_pd if pos in ("premium", "discount") else 0.0)
        )
        q = round(_clamp01(fvg_zone_quality(
            gap_atr=gap_atr,
            disp=disp,
            fresh_q=_fvg_freshness(g, recent_from),
            age_q=_age_q(g["t"], last_t),
            tpo_minutes=_tpo_minutes(tpo_levels, g_lo, g_hi),
            htf=False,
        ) + (fvg_stack if stacked else 0.0) + pd_adjust), 3)
        _add(
            side, "fvg", g["t"], g_lo, g_hi,
            None, False, g.get("filled", False),
            g.get("full_t") or g.get("fill_t"),
            quality=q,
            extra={
                "gap_atr": round(gap_atr, 2),
                "disp": round(disp, 2),
                "fill_pct": round(float(g.get("fill_pct", 0.0) or 0.0), 2),
                "stacked": bool(stacked),
                "htf_tf": stack_hit["tf"] if stack_hit else None,
                "pd": pos,
            },
        )

    # --- 4. TPO time-at-price levels (both sides, band = ±half bucket)
    if tpo_levels:
        last_close = float(frame["c"].iloc[-1])
        for lv in tpo_levels:
            if float(lv.get("minutes", 0.0)) < 12.0:
                continue
            p = float(lv["price"])
            side = "demand" if p <= last_close else "supply"
            _add(side, "tpo", last_t, p - 0.25, p + 0.25, None, False)

    # --- 5. prior-day high/low liquidity draws
    liq = smc.detect_liquidity(frame)
    for lv in liq.get("levels", []):
        if lv.get("tag") not in ("PDH", "PDL"):
            continue
        p = float(lv["price"])
        side = "supply" if lv["kind"] == "BSL" else "demand"
        _add(side, "pd", lv.get("t", last_t), p - 0.30, p + 0.30, None, False)

    # --- 6. HTF (M15) zones — institutional timeframe, quality bonus
    m15 = (htf or {}).get("M15")
    if m15 is not None and len(m15) >= 30:
        htf_frame = m15.iloc[-POI_WINDOW:].reset_index(drop=True)
        htf_closes = htf_frame["c"].to_numpy(dtype=float)
        htf_t_np = htf_frame["time_utc"].to_numpy()
        htf_atr = atr_last(htf_frame, 14) or atr_val
        htf_tol = BROKEN_TOL_ATR * htf_atr
        for z in smc.detect_supply_demand(htf_frame, max_zones=8):
            if _broken(htf_closes, z["side"], float(z["lo"]), float(z["hi"]),
                       htf_tol, _origin_index(htf_t_np, z["t"])):
                continue
            _add(z["side"], "sd", z["t"], z["lo"], z["hi"],
                 z.get("impulse"), True)
        for ob in smc.detect_order_blocks(htf_frame, max_zones=8):
            side = "demand" if ob["side"] == "bullish" else "supply"
            if _broken(htf_closes, side, float(ob["lo"]), float(ob["hi"]),
                       htf_tol, _origin_index(htf_t_np, ob["t"])):
                continue
            _add(side, "ob", ob["t"], ob["lo"], ob["hi"],
                 ob.get("impulse"), True, ob["mitigated"], ob.get("mit_t"))

    # dedupe: same-side overlapping bands keep the higher-quality zone
    ranked = sorted(out, key=lambda z: z["quality"], reverse=True)
    kept: list[dict] = []
    for z in ranked:
        clash = any(
            o["side"] == z["side"]
            and z["lo"] <= o["hi"] and z["hi"] >= o["lo"]
            for o in kept
        )
        if not clash:
            kept.append(z)
    return kept[:max_zones]
