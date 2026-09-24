"""POI zone-retest trigger (D-048, D-049 rework — user directive).

"Best POI ZONE, SUPPLY ZONE, DEMAND ZONE এই গুলো তে সিগন্যাল দিতে হবে,
মিস করা যাবে না" — when price RETURNS to a quality point-of-interest
zone and REJECTS, that IS the entry. The trigger does not wait for the
other strategies to agree (user directive: "সব গুলো স্ট্রাটেজি একই সময়
AGREE নাও থাকতে পারে") — the location + the rejection carry the setup,
everything else only shapes confidence.

D-049 hard-coded retest rules (DEMAND / BUY — SUPPLY mirrors everything):

  1. ENGAGEMENT — the trigger bar traded INTO the band (low <= zone.hi),
     OR a bar inside `zone_retest_window` entered it AND the trigger bar
     is still CLOSE to the zone (close <= zone.hi + near_atr*ATR) — a
     bounce that already ran away is a MISSED entry, not a late one;
  2. REJECTION — the trigger bar CLOSED back above the zone high with a
     real rejection:
       * lower wick >= 35% of the bar range AND close above the zone
         MID (the zone gave way past its middle = weak), or
       * close in the top 30% of the bar range (strong body rejection), or
       * SWEEP + RECLAIM — the bar wicked THROUGH the zone's far edge
         (low < zone.lo) and closed back above the zone high: the
         classic stop-hunt reversal, rejection forced to >= 0.90;
  3. ENTRY PROXIMITY — a WINDOW-entered retest (the dip happened on an
     earlier bar) still counts only while the close stays near the band
     (<= zone.hi + near_atr*ATR): a bounce that already ran away is a
     MISSED entry, not a late one. A trigger bar that ITSELF traded into
     the zone needs no distance cap — its wick proves the engagement and
     a far closing price is strength (V-reversal), not chasing;
  4. ZONE INTACT — no close beyond the far edge since formation
     (broken zones are dropped upstream in poi_zones);
  5. QUALITY GATE — zone quality >= cfg.min_zone_quality when the zone
     direction agrees with (or is neutral to) the D-049 direction bias;
     a counter-trend reversal needs quality >= cfg.counter_trend_quality
     (D-049: 0.70 -> 0.58 — the old gate made BUY signals practically
     impossible during H1 downtrends, which is why the platform only
     ever sold) AND a strong rejection.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from app.engine.config import EngineConfig
from app.engine.indicators import atr as atr_calc
from app.engine.trace import Trace

#: rejection wick as a share of the bar range (hard gate)
REJECT_WICK_SHARE = 0.35
#: close position inside the bar range that alone counts as rejection
REJECT_BODY_POS = 0.70
#: sweep+reclaim rejection floor (the strongest zone entry)
SWEEP_RECLAIM_REJ = 0.90
#: counter-trend retests additionally need this rejection quality
COUNTER_REJECT_MIN = 0.50
#: D-049 — a window-entered zone still counts only if the trigger bar
#: stayed this close (in ATRs) to the band (current-bar entries are
#: exempt: the wick inside the zone IS the engagement)
NEAR_ZONE_ATR = 0.90
#: widened RSI windows for zone entries (D-049 — reversal buys off
#: demand zones print momentum fast; only absolute extremes are avoided)
ZONE_RSI_WINDOWS: dict[str, tuple[float, float]] = {
    "BUY": (20.0, 70.0),
    "SELL": (30.0, 80.0),
}


@dataclass(frozen=True)
class ZoneRetestSignal:
    """A POI zone retest that fired — the D-048/D-049 trigger payload."""

    direction: str  # "BUY" (demand) | "SELL" (supply)
    entry: float  # close of the trigger bar
    zone: dict  # the POI zone dict (side/source/lo/hi/quality/...)
    rejection: float  # 0..1 — how hard the trigger bar rejected
    quality: float  # 0..1 — the zone's POI quality
    atr: float
    counter_trend: bool  # zone direction against the direction bias
    sweep_reclaim: bool = False  # D-049 — stop-hunt through the zone
    sweep_extreme: float | None = None  # D-049 — the wick that swept


def rejection_quality(
    wick_share: float, body_pos: float, sweep_reclaim: bool = False
) -> float:
    """0..1 — trigger-bar rejection strength (wick + close position)."""
    base = max(0.0, min(1.0, 0.5 * wick_share / 0.6 + 0.5 * body_pos))
    if sweep_reclaim:
        base = max(base, SWEEP_RECLAIM_REJ)
    return base


def detect_zone_retest(
    base: pd.DataFrame,
    zones: list[dict],
    cfg: EngineConfig,
    bias_verdict: str,  # D-049 direction bias ("BUY" | "SELL" | "NEUTRAL")
) -> ZoneRetestSignal | None:
    """Scan the ranked POI zones for a live retest + rejection.

    Returns the BEST candidate (quality-first, with-bias preferred) or
    None. Deliberately does NOT write trace entries on the negative path
    — the arbitration in evaluate() may still fire sfp/pullback on the
    same bar and their trace must not carry a red zone entry.
    """
    if len(base) < 5:
        return None
    atr_val = atr_calc(base, cfg.atr_period)
    if atr_val <= 0:
        return None

    last = base.iloc[-1]
    o = float(last["o"])
    h = float(last["h"])
    low = float(last["l"])
    c = float(last["c"])
    rng = max(h - low, 1e-9)
    win = min(max(int(cfg.zone_retest_window), 1), 5)
    prev = base.iloc[-(win + 1) : -1]  # the bars before the trigger
    if len(prev) == 0:
        return None

    best: ZoneRetestSignal | None = None
    best_score = -1.0

    for z in zones:
        q = float(z.get("quality", 0.0))
        zlo, zhi = float(z["lo"]), float(z["hi"])
        zmid = (zlo + zhi) / 2.0

        if z["side"] == "demand":
            entered = low <= zhi
            entered_prev = bool((prev["l"].astype(float) <= zhi).any())
            near_zone = c <= zhi + NEAR_ZONE_ATR * atr_val
            closed_back = c > zhi
            wick_share = (min(o, c) - low) / rng
            body_pos = (c - low) / rng
            swept = low < zlo  # wicked through the far edge
            rejected = (
                (wick_share >= REJECT_WICK_SHARE and c > zmid)
                or body_pos >= REJECT_BODY_POS
                or (swept and closed_back)
            )
            if not (entered or (entered_prev and near_zone)):
                continue  # price never reached this zone — not a miss
            if not closed_back or not rejected:
                continue  # forming / no rejection — not a signal (yet)
            counter = bias_verdict == "SELL"
            d = "BUY"
        else:
            entered = h >= zlo
            entered_prev = bool((prev["h"].astype(float) >= zlo).any())
            near_zone = c >= zlo - NEAR_ZONE_ATR * atr_val
            closed_back = c < zlo
            wick_share = (h - max(o, c)) / rng
            body_pos = (h - c) / rng
            swept = h > zhi  # wicked through the far edge
            rejected = (
                (wick_share >= REJECT_WICK_SHARE and c < zmid)
                or body_pos >= REJECT_BODY_POS
                or (swept and closed_back)
            )
            if not (entered or (entered_prev and near_zone)):
                continue
            if not closed_back or not rejected:
                continue
            counter = bias_verdict == "BUY"
            d = "SELL"

        gate = cfg.counter_trend_quality if counter else cfg.min_zone_quality
        if q < gate:
            continue
        sweep_reclaim = bool(swept and closed_back)
        # D-056 — counter-trend zone reversals need the stop-hunt PROOF:
        # the bar wicked through the far edge and closed back (sweep +
        # reclaim). A plain bounce against the bias is a coin flip —
        # the backtest losers were exactly these.
        if counter and cfg.counter_needs_sweep and not sweep_reclaim:
            continue
        rej = rejection_quality(wick_share, body_pos, sweep_reclaim)
        if counter and rej < COUNTER_REJECT_MIN:
            continue

        score = q + 0.25 * rej - (0.05 if counter else 0.0)
        if score > best_score:
            best_score = score
            best = ZoneRetestSignal(
                direction=d,
                entry=c,
                zone=z,
                rejection=round(rej, 3),
                quality=q,
                atr=atr_val,
                counter_trend=counter,
                sweep_reclaim=sweep_reclaim,
                sweep_extreme=(low if z["side"] == "demand" else h)
                if sweep_reclaim else None,
            )

    return best


def zone_retest_note(sig: ZoneRetestSignal) -> str:
    """Human-readable trace line for a FIRED zone retest.

    D-069 — FVG-sourced zones carry their importance telemetry in the
    note: gap size in ATRs, fill depth (the CE read), gap-in-gap
    stacking and the premium/discount half — the same facts the
    SignalDetail panel shows, so the user can see WHY this gap mattered.
    """
    tag = "counter-trend" if sig.counter_trend else "with-trend"
    sweep = ", SWEEP+RECLAIM" if sig.sweep_reclaim else ""
    src = str(sig.zone.get("source", "?"))
    extra = ""
    if src == "fvg":
        bits: list[str] = []
        try:
            bits.append(f"{float(sig.zone.get('gap_atr', 0.0)):.2f} ATR gap")
        except (TypeError, ValueError):
            pass
        pct = sig.zone.get("fill_pct")
        if isinstance(pct, (int, float)) and pct > 0:
            bits.append(f"fill {float(pct):.0%}")
        if sig.zone.get("stacked"):
            bits.append(
                f"stacked in {sig.zone.get('htf_tf') or 'HTF'} gap"
            )
        elif sig.zone.get("htf_tf"):
            bits.append(f"born on {sig.zone['htf_tf']}")
        pd_pos = sig.zone.get("pd")
        if pd_pos in ("premium", "discount"):
            bits.append(str(pd_pos))
        if bits:
            extra = " (" + ", ".join(bits) + ")"
    return (
        f"{sig.direction} at {sig.zone['side'].upper()} POI "
        f"{sig.zone['lo']:.2f}-{sig.zone['hi']:.2f} "
        f"({src}{extra}, quality {sig.quality:.2f}, {tag}, "
        f"rejection {sig.rejection:.2f}{sweep})"
    )


def check_rsi_zone(
    base: pd.DataFrame, cfg: EngineConfig, trace: Trace
) -> tuple[bool, float]:
    """RSI check with the widened zone windows (direction set upstream)."""
    from app.engine.indicators import rsi

    value = rsi(base["c"], cfg.rsi_period)
    lo, hi = ZONE_RSI_WINDOWS[trace.direction or "BUY"]
    passed = lo <= value <= hi
    trace.add(
        "rsi", passed,
        f"{value:.1f} (zone window {lo:.0f}-{hi:.0f})",
    )
    return passed, value


def build_levels_zone(
    sig: ZoneRetestSignal, cfg: EngineConfig
) -> tuple[float, float]:
    """(entry, structural SL candidate) — SL beyond the zone's far edge.

    A sweep+reclaim entry anchors the stop beyond the SWEEP extreme
    (the actual invalidation), not just the zone edge. The floor/cap/
    TPO-anchor/liquidity-snap pass happens in smart_targets() exactly
    as for the other triggers.
    """
    z = sig.zone
    if sig.direction == "BUY":
        sl_base = float(z["lo"]) - cfg.sl_buffer_atr * sig.atr
        if sig.sweep_reclaim and sig.sweep_extreme is not None:
            # the stop-hunt wick IS the invalidation — anchor beyond it
            sl_base = min(sl_base, sig.sweep_extreme - cfg.sl_buffer_atr * sig.atr)
    else:
        sl_base = float(z["hi"]) + cfg.sl_buffer_atr * sig.atr
        if sig.sweep_reclaim and sig.sweep_extreme is not None:
            sl_base = max(sl_base, sig.sweep_extreme + cfg.sl_buffer_atr * sig.atr)
    return sig.entry, sl_base


def zone_trigger_quality(sig: ZoneRetestSignal) -> float:
    """0..1 — confidence contribution of a zone setup (location + rejection)."""
    return max(0.0, min(1.0, 0.55 * sig.quality + 0.45 * sig.rejection))


# ------------------------------------------------------------------ D-050
# POI pending-entry anchor (user directive):
#   "মার্কেট প্রাইস এখন 4513 যদি সেল সিগন্যাল আসে তখন পেন্ডিং অর্ডার creat
#    করতে হবে, এন্ট্রি প্রাইস 4520 বা তার আসে পাশে বসাতে হবে, আর যদি buy
#    signal আসে, তখন এন্ট্রি প্রাইস 4503 বা তার আসে পাশে অর্ডার বসাতে হবে,
#    এতে করে স্টপ লস হিট কম হবে ... মার্কেট এর সাপোর্ট জোন এর নিচ থেকে buy
#    order বসাবেন, আর মার্কেট এর রেসিসটেন্স এর উপর থেকে sell order বসাবেন,
#    এটাই হলো POI ZONE এর এন্ট্রি।"


def poi_pending_entry(
    direction: str,  # "BUY" | "SELL"
    market: float,  # current market reference (trigger-bar close — the M1 candle)
    zones: list[dict],  # ranked POI zones (app.analysis.poi.poi_zones)
    atr: float,
    cfg: EngineConfig,
    spread_price: float = 0.0,  # current spread in price units
    magnets: list[tuple[float, str]] | None = None,  # D-070 same-side
    #    structural magnets [(price, kind)] — EMA21/50, rest edges, FVG
    #    watermarks — the fallback anchor when no POI zone is in reach
) -> tuple[float, str] | None:
    """D-050/D-051/D-056/D-070 — the PENDING limit-entry price + note.

    BUY  -> a BUY LIMIT below the market anchored at the nearest DEMAND
            (support) zone: D-056 anchors at the zone's NEAR edge (the
            first-retest level where an intact zone rejects), never deeper
            than the user's 4-6 USD window.
    SELL -> a SELL LIMIT above the market anchored at the nearest SUPPLY
            (resistance) zone's NEAR edge.

    D-056 ROOT-CAUSE FIX (adverse selection): the D-051 code anchored a BUY
    limit at the zone's FAR edge (zlo) — that order only fills when price
    trades THROUGH the whole demand zone, i.e. exactly when the zone BREAKS.
    The fills were the failures: fill-rate 37%, filled-trade WR < 50% while
    the unfilled "misses" were the winners. The near edge is where a valid
    zone gets its FIRST/SECOND retest — the ICT entry itself.

    D-070 SCALP rework (the 'wrong entry' complaint, measured):
      * QUALITY-FIRST zone pick — the old code picked the NEAREST zone
        with quality >= 0.30, so a 0.31-quality band 0.4 ATR away beat a
        0.85-quality band 1.2 ATR away. The pick now scores
        quality - 0.25 x (dist / max_off): location still matters, but a
        weak zone can no longer shadow a strong one.
      * MAGNET FALLBACK — when no same-side zone sits in the window the
        entry anchors at the nearest same-side structural magnet
        (EMA21/EMA50, prior rest-zone edge, FVG fill watermark) instead
        of the old blind 4.5 USD offset into no-man's land (the 2-4 USD
        no-structure bucket lost 3/3 in the backtest autopsy).
      * NO ANCHOR -> REFUSE — `magnet_anchor` semantics: with no zone AND
        no magnet within the window the function returns None and the
        engine logs a visible near-miss ("no structural anchor") — a
        trade without a location is a guess, and guessing was exactly the
        "wrong entry point" the user reported.

    Geometry guards (combined ATR + USD):
    - minimum distance: max(entry_offset_atr*ATR, 2 spreads, entry_min_usd);
    - maximum distance: min(pending_max_atr*ATR, pending_max_usd) with a
      1.5x-minimum floor so the window is never empty;
    - zone must sit on the RIGHT side (demand below the market for BUY,
      supply above for SELL) with a quality >= 0.30.
    """
    min_off = max(
        cfg.entry_offset_atr * atr, 2.0 * spread_price, cfg.entry_min_usd
    )
    max_off = max(
        min(cfg.pending_max_atr * atr, cfg.pending_max_usd),
        1.5 * min_off,
    )
    want = "demand" if direction == "BUY" else "supply"

    best: tuple[float, float, dict] | None = None  # (score, anchor, zone)
    for z in zones:
        if z.get("side") != want:
            continue
        if float(z.get("quality", 0.0)) < 0.30:
            continue  # weak zone — not worth anchoring an entry to
        zlo, zhi = float(z["lo"]), float(z["hi"])
        # D-056 — NEAR edge: the level price touches FIRST on a retrace
        # (zhi for demand, zlo for supply). Retests of an intact zone
        # reject here; the far edge only trades on a break.
        near_edge = zhi if direction == "BUY" else zlo
        # pending must still clear the market by min_off (noise margin);
        # a near edge that hugs the market degrades to the pure offset
        anchor = (
            min(near_edge, market - min_off)
            if direction == "BUY" else
            max(near_edge, market + min_off)
        )
        dist = (market - anchor) if direction == "BUY" else (anchor - market)
        if dist < min_off:
            continue  # zone hugs the market — no margin to be had from it
        # D-070 — quality-first: a strong zone may sit a little deeper
        # and still win; the 0.25 weight keeps location relevant
        score = float(z.get("quality", 0.0)) - 0.25 * (dist / max_off)
        if best is None or score > best[0]:
            best = (score, anchor, z)

    if best is not None:
        _score, anchor, z = best
        dist = (market - anchor) if direction == "BUY" else (anchor - market)
        if dist <= max_off:
            where = "below" if direction == "BUY" else "above"
            entry = anchor
            note = (
                f"{where} {want} POI {z['lo']:.2f}-{z['hi']:.2f} "
                f"({z['source']}, q {float(z['quality']):.2f}) at near edge "
                f"{anchor:.2f} — {dist:.2f} USD from market"
            )
            return round(entry, 2), note
        if not cfg.magnet_anchor:
            # legacy escape (magnet_anchor=False) — the exact pre-D-070
            # D-051 clamp: stay inside the USD window, bookable
            entry = (market - max_off) if direction == "BUY" else (market + max_off)
            return round(entry, 2), (
                f"{want} POI {z['lo']:.2f}-{z['hi']:.2f} too deep "
                f"({dist:.2f} USD) — clamped to {max_off:.2f} USD"
                f" (cap {cfg.pending_max_usd:.1f})"
            )
        # D-070 — TOO DEEP for a scalp: no clamping into no-man's land
        # (the backtest autopsy says the deep clamped fills are exactly
        # the losers — 2-6 USD bucket: 0-17% WR). Fall through to the
        # magnets; the zone itself re-triggers when price actually
        # approaches it (the engagement rule needs price IN the band),
        # so nothing is missed, it is DEFERRED to the moment the zone
        # is live.

    # D-070 — MAGNET FALLBACK: no same-side POI zone within the window.
    # The next real structure the market is drawn to (EMA21/EMA50, a
    # prior REST-zone edge, an FVG fill watermark) anchors the pending;
    # the blind 4.5 USD offset into no-man's land is GONE (0/3 winners).
    if cfg.magnet_anchor and magnets:
        cands: list[tuple[float, str]] = []
        for price, kind in magnets:
            p = float(price)
            if not (p > 0):
                continue
            if direction == "BUY" and market - min_off >= p >= market - max_off:
                cands.append((market - p, f"{kind} magnet {p:.2f}"))
            elif direction == "SELL" and market + min_off <= p <= market + max_off:
                cands.append((p - market, f"{kind} magnet {p:.2f}"))
        if cands:
            cands.sort()  # nearest magnet first (highest fill probability)
            dist, tag = cands[0]
            entry = (market - dist) if direction == "BUY" else (market + dist)
            return round(entry, 2), (
                f"no {want} POI within {max_off:.2f} USD — anchored at the"
                f" nearest {tag} ({dist:.2f} USD from market)"
            )

    if cfg.magnet_anchor:
        # D-070 — refuse: neither a zone NOR a magnet is in reach; a
        # locationless pending is the exact "wrong entry point" the user
        # reported (the old code would fire it 4.5 USD into the void)
        return None

    # legacy escape (magnet_anchor=False): the pre-D-070 blind offset
    off = max(min(cfg.pending_target_usd, max_off), min_off)
    entry = (market - off) if direction == "BUY" else (market + off)
    note = (
        f"no {want} POI within {max_off:.2f} USD — "
        f"offset {off:.2f} USD (target {cfg.pending_target_usd:.1f})"
    )
    return round(entry, 2), note


def structural_magnets(
    base: pd.DataFrame,
    direction: str,
    market: float,
) -> list[tuple[float, str]]:
    """D-070 — same-side structural magnets the market is drawn to.

    The pending-entry fallback anchors: EMA21/EMA50 (the same pullback
    levels the structure ladder and the chart ribbon use), the edges of
    the most recent compressed REST zone (D-064), and FVG fill
    watermarks (D-069 — the untraded air beyond the watermark is the
    real inefficiency). Returns [(price, kind)] sorted by |market - p|.
    """
    out: list[tuple[float, str]] = []
    if base is None or len(base) < 55 or not market:
        return out
    try:
        from app.engine.indicators import ema as _ema

        closes = base["c"]
        for period, kind in ((21, "EMA21"), (50, "EMA50")):
            v = float(_ema(closes, period).iloc[-1])
            if v > 0:
                out.append((v, kind))
    except Exception:  # noqa: BLE001 — magnets are best-effort
        pass
    try:
        from app.analysis.structure import rest_zones

        for rz in rest_zones(base.iloc[-120:])[:2]:
            lo, hi = float(rz.get("lo", 0.0)), float(rz.get("hi", 0.0))
            if lo <= 0 or hi <= 0:
                continue
            # BUY anchors at the top of a rest box below price (the
            # box edge price retests first); SELL at the bottom of one
            # above — both edges are listed; the window check in
            # poi_pending_entry picks the reachable side
            out.append((hi, "REST edge"))
            out.append((lo, "REST edge"))
    except Exception:  # noqa: BLE001
        pass
    try:
        from app.analysis import smc as _smc

        for g in _smc.detect_fvg(base.iloc[-160:], max_gaps=8):
            if g.get("filled"):
                continue  # mitigated gaps are not magnets
            lo, hi = float(g["lo"]), float(g["hi"])
            width = max(hi - lo, 1e-9)
            pct = float(g.get("fill_pct", 0.0) or 0.0)
            if g.get("side") == "bullish":
                # watermark advances INTO the gap from the near edge (hi)
                wm = hi - pct * width
                out.append((wm, "FVG watermark"))
            else:
                wm = lo + pct * width
                out.append((wm, "FVG watermark"))
    except Exception:  # noqa: BLE001
        pass
    valid = [(p, k) for p, k in out if p > 0]
    return sorted(valid, key=lambda pk: abs(market - pk[0]))
