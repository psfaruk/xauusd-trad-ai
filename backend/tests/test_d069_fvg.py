"""D-069 — the FVG importance + multi-timeframe model.

User directive: "মার্কেট স্ট্রাকচার এ চার্ট ড্রয়িং এ আমি দেখতে পাই, ছোট বড়
অনেক fvg তৈরি হয়, সব fvg গুরুত্ব পূর্ণ না। আমার অ্যাপ এই বিষয় টাকে কিভাবে
গুরুত্ব দিচ্ছে? Fvg হলেই কি এন্ট্রি সিগন্যাল আসে, নাকি এটা কয়েক টি fvg
একসাথে দেখলে মালটি টাইম ফ্রেমে এই বিষয় টা কিভাবে হ্যান্ডেল করে।"

Pinned here:
- detect_fvg telemetry (gap_atr / disp / fill_pct / fill_t-first-touch /
  full_t / filled);
- the importance gate: noise floor (>= 0.30 ATR), displacement floor
  (>= 0.40 ATR — net progress past the gap, no spike-round-trips);
- CE-aware freshness (a half-mitigated gap is NOT "untouched");
- full-fill window (legacy behavior preserved);
- HTF (M5/M15) institutional gaps join the ranked POI list;
- gap-in-gap stacking (both dedupe directions carry the flag) +
  premium/discount adjustment;
- the zone-retest trigger firing off a REAL FVG POI (an FVG alone is not
  enough — it must be real, quality-gated and rejected at);
- the target-ladder FILL WATERMARK (partially-filled gaps barrier at
  their untraded air, not the raw near edge);
- fvg_importance=False restores the pre-D-069 behavior exactly;
- the D-069 config block defaults.

All fixtures keep the frame's last-14-bar ATR at ~1.0 (settle bars have
range 1.0), so gap widths map straight onto gap_atr.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from app.analysis import smc
from app.analysis.context import _fvg_fill_span, smart_targets
from app.analysis.poi import (
    FVG_CE,
    FVG_MIN_ATR,
    FVG_MIN_DISP,
    FVG_STACK_BONUS,
    fvg_zone_quality,
    poi_zones,
)
from app.engine.config import DEFAULT_CONFIG, EngineConfig
from app.engine.engine import evaluate
from app.engine.zones import detect_zone_retest, zone_retest_note

T0 = datetime(2026, 9, 23, 9, 0, tzinfo=UTC)
COLS = ["time_utc", "o", "h", "l", "c", "v"]


def _m(i: int) -> datetime:
    return T0 + timedelta(minutes=i)


def _bar(i, o, h, low, c, v=100):
    return [_m(i), o, h, low, c, v]


def _quiet(rows, i0, n, price=4500.0):
    """n quiet bars (range 1.0 -> ATR ~1.0) at `price`."""
    for i in range(i0, i0 + n):
        rows.append(_bar(i, price, price + 0.5, price - 0.5, price + 0.02))
    return rows


def _settle(rows, i0, n, price, vary_high=False):
    """n settle bars at `price` (range 1.0, RSI-neutral alternating
    closes — flat closes drive RSI to 100/0 and would fail the zone
    RSI window). Highs may vary to avoid equal-high BSL pools."""
    for k in range(n):
        h = price + 0.5 + (0.3 * (k % 3) if vary_high else 0.0)
        c = price + (0.02 if k % 2 == 0 else -0.02)
        rows.append(_bar(i0 + k, price, h, price - 0.5, c))
    return rows


def _real_gap_tape(fill_frac: float = 0.0, fill_at_end: bool = True):
    """Quiet tape -> REAL bullish FVG [4500.5, 4501.3] minted with a
    1.6-ATR displacement -> optional partial/full fill -> settle above.

    fill_at_end puts the retest bar within the last 3 bars (the
    FRESH_FIRST_TOUCH window); otherwise mid-tape (stale discounted).
    """
    rows: list[list] = []
    _quiet(rows, 0, 40)
    # A (k-1) high 4500.5 / B impulse / C low 4501.3 -> gap [4500.5, 4501.3]
    rows.append(_bar(40, 4500.0, 4500.5, 4499.5, 4500.2))
    rows.append(_bar(41, 4500.2, 4502.0, 4500.0, 4501.8, v=400))
    rows.append(_bar(42, 4501.8, 4502.5, 4501.3, 4501.6))
    # disp = 4501.6 - 4500.0 = 1.6
    if fill_frac > 0.0 and not fill_at_end:
        depth = 0.8 * fill_frac
        dip = 4501.3 - depth
        rows.append(_bar(43, 4502.0, 4502.4, dip, 4502.1, v=300))
        _settle(rows, 44, 25, 4502.3)  # >20 min: old full fills must drop
    elif fill_frac > 0.0:
        _settle(rows, 43, 14, 4502.3)
        depth = 0.8 * fill_frac
        dip = 4501.3 - depth
        rows.append(_bar(57, 4502.0, 4502.4, dip, 4502.1, v=300))
        _settle(rows, 58, 2, 4502.3)
    else:
        _settle(rows, 43, 17, 4502.3)
    return pd.DataFrame(rows, columns=COLS)


def _drift_gap_tape():
    """A 0.35-ATR gap minted by a spike that round-trips (disp ~= gap) —
    passes the size floor, fails the displacement floor."""
    rows: list[list] = []
    _quiet(rows, 0, 40, price=4480.0)
    # A opens at its own high then sells off; B spikes; C closes at its low
    rows.append(_bar(40, 4480.95, 4480.95, 4480.30, 4480.40))
    rows.append(_bar(41, 4480.40, 4482.10, 4480.35, 4481.00, v=300))
    rows.append(_bar(42, 4481.00, 4481.60, 4481.30, 4481.31))
    # gap = [4480.95, 4481.30] = 0.35 wide; disp = 0.36 (< 0.40 floor)
    _settle(rows, 43, 25, 4482.0)
    return pd.DataFrame(rows, columns=COLS)


def _noise_gap_tape():
    """A 0.15-ATR gap — spread noise, never a POI (legacy minted it).
    Only 10 quiet bars (below the 12-minute TPO threshold) so nothing
    dedupes the noise gap away — the floor itself is what's on trial."""
    rows: list[list] = []
    _quiet(rows, 0, 10)
    rows.append(_bar(10, 4500.00, 4500.10, 4499.50, 4500.05))
    rows.append(_bar(11, 4500.05, 4500.60, 4500.00, 4500.50, v=200))
    rows.append(_bar(12, 4500.50, 4500.70, 4500.25, 4500.60))
    # gap = [4500.10, 4500.25] = 0.15 (0.15 ATR)
    _settle(rows, 13, 25, 4501.1)  # low 4500.6: unfilled, no new gap
    return pd.DataFrame(rows, columns=COLS)


def _htf_gap_frame(n_settle=30, price=4500.0):
    """M5-shaped frame (5-minute bars) with a REAL bullish M5 gap
    [price+0.4, price+2.3] (1.9 wide, M5 ATR ~1.0)."""
    t0 = T0 - timedelta(minutes=5 * 60)
    rows: list[list] = []
    for i in range(40):
        t = t0 + timedelta(minutes=5 * i)
        rows.append([t, price, price + 0.5, price - 0.5, price + 0.02, 100])
    t41 = t0 + timedelta(minutes=5 * 40)
    t42 = t0 + timedelta(minutes=5 * 41)
    t43 = t0 + timedelta(minutes=5 * 42)
    rows.append([t41, price, price + 0.4, price - 0.4, price + 0.2, 100])
    rows.append([t42, price + 0.2, price + 2.0, price + 0.1, price + 1.8, 500])
    rows.append([t43, price + 1.8, price + 2.5, price + 2.3, price + 2.4, 300])
    # M5 gap = [price+0.4, price+2.3]; disp = 2.4
    # settle low price+2.45 sits inside (C.high price+2.5, gap hi price+2.3)
    top = price + 3.45
    for i in range(44, 44 + n_settle):
        t = t0 + timedelta(minutes=5 * i)
        rows.append([t, top - 0.25, top, top - 1.0, top - 0.25, 100])
    return pd.DataFrame(rows, columns=COLS)


def _pd_gap_tape(history_price: float):
    """The crafted gap [4500.5, 4501.3] minted after deep history at
    `history_price` (a 25-bar quiet block + one jump bar): history BELOW
    puts the gap in the PREMIUM half of the range, history ABOVE puts it
    in the DISCOUNT half — identical gap geometry, opposite halves."""
    rows: list[list] = []
    _quiet(rows, 0, 25, price=history_price)
    if history_price < 4500.0:
        rows.append(_bar(25, history_price, 4500.5, history_price - 0.5,
                         4500.0, v=500))
    else:
        rows.append(_bar(25, history_price, history_price + 0.5, 4499.5,
                         4500.0, v=500))
    _quiet(rows, 26, 5)
    rows.append(_bar(31, 4500.0, 4500.5, 4499.5, 4500.2))
    rows.append(_bar(32, 4500.2, 4502.0, 4500.0, 4501.8, v=400))
    rows.append(_bar(33, 4501.8, 4502.5, 4501.3, 4501.6))
    _settle(rows, 34, 17, 4502.3)
    return pd.DataFrame(rows, columns=COLS)


def _uptrend_htf(n: int = 60) -> dict[str, pd.DataFrame]:
    """Rising H1/M15/M5 frames (BUY trend, EMA50 below price)."""
    out = {}
    for tf, m in (("H1", 60), ("M15", 15), ("M5", 5)):
        t0 = T0 - timedelta(minutes=m * n)
        rows = [
            [t0 + timedelta(minutes=m * i), 4200.0 + i, 4201.0 + i,
             4199.0 + i, 4200.5 + i, 50]
            for i in range(n)
        ]
        out[tf] = pd.DataFrame(rows, columns=COLS)
    return out


# ------------------------------------------------------- detect_fvg telemetry


class TestDetectFvgTelemetry:
    def test_gap_atr_and_disp_measured(self):
        df = _real_gap_tape()
        gaps = smc.detect_fvg(df)
        bull = [g for g in gaps if g["side"] == "bullish"
                and abs(g["lo"] - 4500.5) < 0.05
                and abs(g["hi"] - 4501.3) < 0.05]
        assert bull, f"expected the crafted gap in {gaps}"
        g = bull[0]
        assert g["gap"] == pytest.approx(0.8, abs=0.01)
        assert g["gap_atr"] == pytest.approx(0.8, abs=0.15)  # ATR ~1.0
        assert g["disp"] == pytest.approx(1.6, abs=0.3)

    def test_fill_pct_partial_and_first_touch(self):
        df = _real_gap_tape(fill_frac=0.3)
        gaps = smc.detect_fvg(df)
        g = next(g for g in gaps if abs(g["lo"] - 4500.5) < 0.05)
        assert g["filled"] is False           # legacy semantic: full fill only
        assert g["fill_pct"] == pytest.approx(0.3, abs=0.08)
        # fill_t = FIRST entry into the gap (the retest bar)
        assert pd.Timestamp(g["fill_t"]) == pd.Timestamp(_m(57))
        assert g["full_t"] is None

    def test_full_fill_semantics(self):
        df = _real_gap_tape(fill_frac=1.0)
        gaps = smc.detect_fvg(df)
        g = next(g for g in gaps if abs(g["lo"] - 4500.5) < 0.05)
        assert g["filled"] is True
        assert g["fill_pct"] == pytest.approx(1.0, abs=0.02)
        assert g["full_t"] is not None
        assert g["filled_pct"] == pytest.approx(1.0)

    def test_untouched_gap(self):
        df = _real_gap_tape(fill_frac=0.0)
        gaps = smc.detect_fvg(df)
        g = next(g for g in gaps if abs(g["lo"] - 4500.5) < 0.05)
        assert g["filled"] is False
        assert g["fill_pct"] == 0.0
        assert g["fill_t"] is None
        assert g["full_t"] is None


# --------------------------------------------------------- the importance gate


class TestFvgImportanceGate:
    def test_noise_floor_drops_small_gaps(self):
        df = _noise_gap_tape()
        zones = poi_zones(df)
        fvgs = [z for z in zones if z["source"] == "fvg"]
        assert not any(
            z for z in fvgs if (z["hi"] - z["lo"]) < FVG_MIN_ATR * 1.2
        ), f"noise gap became a POI: {fvgs}"
        # legacy path (pre-D-069) DID mint it — the A/B baseline proof
        legacy = poi_zones(df, cfg=EngineConfig(fvg_importance=False))
        assert any(
            z for z in legacy if z["source"] == "fvg"
            and (z["hi"] - z["lo"]) < 0.2
        ), "legacy path must still mint the noise gap (A/B baseline)"

    def test_displacement_floor_drops_round_trip_gaps(self):
        df = _drift_gap_tape()
        gaps = smc.detect_fvg(df)
        crafted = [g for g in gaps if abs(g["lo"] - 4480.95) < 0.05]
        assert crafted, "fixture must mint the drift gap"
        g = crafted[0]
        # size floor passes, displacement floor fails
        assert g["gap_atr"] >= FVG_MIN_ATR
        assert g["disp"] < FVG_MIN_DISP
        zones = poi_zones(df)
        assert not [
            z for z in zones if z["source"] == "fvg"
            and abs(z["lo"] - 4480.95) < 0.05
        ], "spike-and-round-trip gap must not become a POI"

    def test_real_gap_becomes_high_quality_poi(self):
        df = _real_gap_tape()
        zones = poi_zones(df)
        real = [z for z in zones if z["source"] == "fvg"
                and abs(z["lo"] - 4500.5) < 0.05]
        assert real, f"real gap missing: {[(z['source'], z['lo']) for z in zones]}"
        z = real[0]
        assert z["side"] == "demand"
        assert z["quality"] >= 0.60
        assert z["gap_atr"] == pytest.approx(0.8, abs=0.15)
        assert z["disp"] == pytest.approx(1.6, abs=0.3)
        assert z["fill_pct"] == 0.0
        assert z["stacked"] is False
        # this tape's gap sits at the center of its own range -> no pd
        # adjustment (the discount/premium behavior has dedicated tapes)
        assert z["pd"] == "mid"


# ------------------------------------------------------------- CE freshness


class TestCeFreshness:
    def _zone(self, fill_frac: float) -> dict:
        df = _real_gap_tape(fill_frac=fill_frac)
        zones = poi_zones(df)
        return next(z for z in zones if z["source"] == "fvg"
                    and abs(z["lo"] - 4500.5) < 0.05)

    def test_ce_intact_retest_beats_ce_broken(self):
        intact = self._zone(0.3)
        broken = self._zone(0.6)
        # exact model: 0.30 weight x (0.85 first-touch - 0.45 stale)
        assert intact["quality"] - broken["quality"] == pytest.approx(
            0.30 * (0.85 - 0.45), abs=0.03
        )

    def test_ce_boundary_is_fifty_percent(self):
        assert FVG_CE == 0.5

    def test_ce_broken_zone_discounted_not_dropped(self):
        z = self._zone(0.6)
        intact = self._zone(0.3)
        assert 0.45 <= z["quality"] < intact["quality"]

    def test_full_fill_old_window_drops_zone(self):
        # a gap fully filled long ago stops being a POI (legacy rule)
        df = _real_gap_tape(fill_frac=1.0, fill_at_end=False)
        zones = poi_zones(df)
        assert not [
            z for z in zones if z["source"] == "fvg"
            and abs(z["lo"] - 4500.5) < 0.05
        ]


# --------------------------------------------------------------- HTF + stack


class TestHtfAndStacking:
    def test_m5_gap_joins_ranked_list(self):
        base = _real_gap_tape()
        m5 = _htf_gap_frame(price=4497.0)  # below the M1 range, no overlap
        zones = poi_zones(base, {"M5": m5})
        htf_fvgs = [z for z in zones if z["source"] == "fvg" and z.get("htf_tf")]
        assert any(z["htf_tf"] == "M5" for z in htf_fvgs), \
            f"no M5 gap POI: {[(z['source'], z.get('htf_tf'), z['lo']) for z in zones]}"
        z = next(z for z in htf_fvgs if z["htf_tf"] == "M5")
        assert z["htf"] is True
        assert z["side"] == "demand"
        assert z["gap_atr"] == pytest.approx(1.9, abs=0.3)

    def test_htf_disabled_escapes(self):
        base = _real_gap_tape()
        m5 = _htf_gap_frame(price=4497.0)
        zones = poi_zones(base, {"M5": m5},
                          cfg=EngineConfig(fvg_htf_enabled=False))
        assert not [z for z in zones if z["source"] == "fvg" and z.get("htf_tf")]

    def test_gap_in_gap_stacks_whichever_wins_dedupe(self):
        # base gap [4500.5, 4501.3] sits INSIDE the M5 gap band
        # [4499.9, 4501.8] -> one nested level, stacked either way
        base = _real_gap_tape()
        m5 = _htf_gap_frame(price=4499.5)
        zones = poi_zones(base, {"M5": m5})
        band = [z for z in zones if z["source"] == "fvg"
                and z["lo"] < 4502.0]
        assert len(band) == 1, "the nested pair must dedupe to ONE zone"
        z = band[0]
        assert z["stacked"] is True
        assert z["htf_tf"] == "M5"
        # the bonus: quality = the unstacked single-gap model + 0.10
        solo = poi_zones(base, None)
        ref = next(x for x in solo if x["source"] == "fvg"
                   and abs(x["lo"] - 4500.5) < 0.05)
        assert z["quality"] >= min(1.0, ref["quality"] + FVG_STACK_BONUS) - 0.02

    def test_opposite_side_does_not_stack(self):
        base = _real_gap_tape()  # demand base gap
        m5 = _htf_gap_frame(price=4497.0)  # demand M5 gap, no overlap
        zones = poi_zones(base, {"M5": m5})
        z = next(z for z in zones if z["source"] == "fvg"
                 and abs(z["lo"] - 4500.5) < 0.05)
        assert z["stacked"] is False


# ------------------------------------------------------- premium / discount


class TestPremiumDiscount:
    def test_discount_earns_premium_pays(self):
        # history above -> the gap sits in the DISCOUNT half (buy low)
        disc = poi_zones(_pd_gap_tape(history_price=4520.0))
        z_disc = next(z for z in disc if z["source"] == "fvg"
                      and abs(z["lo"] - 4500.5) < 0.05
                      and abs(z["hi"] - 4501.3) < 0.05)
        assert z_disc["pd"] == "discount"
        # history below -> the same gap sits in the PREMIUM half (buy high)
        prem = poi_zones(_pd_gap_tape(history_price=4480.0))
        z_prem = next(z for z in prem if z["source"] == "fvg"
                      and abs(z["lo"] - 4500.5) < 0.05
                      and abs(z["hi"] - 4501.3) < 0.05)
        assert z_prem["pd"] == "premium"
        # identical geometry (same gap, untouched, young, no TPO in band)
        # — exactly the two adjustments apart (+0.05 / -0.05)
        assert z_disc["gap_atr"] == pytest.approx(z_prem["gap_atr"], abs=0.02)
        assert (z_disc["quality"] - z_prem["quality"]) == pytest.approx(
            0.10, abs=0.02
        )


# ------------------------------------------------- the zone-retest trigger path


class TestFvgTriggerPath:
    def _trigger_tape(self):
        """A real demand FVG whose near edge price RETESTS with a wick
        rejection (the D-048/D-049 zone entry, on an FVG POI)."""
        rows: list[list] = []
        _quiet(rows, 0, 40)
        rows.append(_bar(40, 4500.0, 4500.5, 4499.5, 4500.2))
        rows.append(_bar(41, 4500.2, 4502.0, 4500.0, 4501.8, v=400))
        rows.append(_bar(42, 4501.8, 4502.5, 4501.3, 4501.6))
        # rally away: RSI-balanced alternating closes (4502.5 / 4502.0 —
        # a flat close series would pin RSI at 100 and fail the window)
        # with slowly DESCENDING highs: no swing-high BSL pools (which
        # vetoed the TP at 1.12R) and no fresh break of the prior range
        # high (which the D-061 trap reads as a reclaimed BSL sweep —
        # the opposing side for a BUY)
        for k in range(12):
            p = 4502.3
            h = p + 0.5 - 0.02 * k
            c = p + 0.2 if k % 2 == 0 else p - 0.3
            rows.append(_bar(43 + k, p, h, p - 0.5, c))
        # trigger: dips into the gap (low 4501.0 = 37% deep, CE intact),
        # closes back above the gap top with a long lower wick
        rows.append(_bar(55, 4502.3, 4502.5, 4501.0, 4502.4, v=350))
        return pd.DataFrame(rows, columns=COLS)

    def test_zone_trigger_fires_on_real_fvg(self):
        base = self._trigger_tape()
        htf = _uptrend_htf()
        close_time = base["time_utc"].iloc[-1] + timedelta(minutes=1)
        ev = evaluate(base, htf, close_time, EngineConfig(), spread_points=20.0)
        sig = ev.signal
        assert sig is not None, ev.trace
        zone_checks = [c for c in ev.trace["checks"]
                       if c["name"] == "zone_retest"]
        assert zone_checks and zone_checks[0]["pass"]
        note = str(zone_checks[0]["value"])
        assert "fvg" in note and "ATR gap" in note
        assert sig["direction"] == "BUY"
        assert sig["sl"] < sig["entry"] < sig["tp"]

    def test_noise_gap_never_fires(self):
        df = _noise_gap_tape()
        htf = _uptrend_htf()
        close_time = df["time_utc"].iloc[-1] + timedelta(minutes=1)
        ev = evaluate(df, htf, close_time, EngineConfig(), spread_points=20.0)
        zone_checks = [c for c in ev.trace["checks"]
                       if c["name"] == "zone_retest"]
        # no FVG-sourced zone signal (the tape's TPO level may legitimately
        # retest — it is a different, quality-gated zone source)
        assert not (
            zone_checks and "fvg" in str(zone_checks[0]["value"])
        )

    def test_note_carries_telemetry(self):
        base = self._trigger_tape()
        zones = poi_zones(base)
        sig = detect_zone_retest(base, zones, EngineConfig(), "BUY")
        assert sig is not None
        note = zone_retest_note(sig)
        assert "ATR gap" in note
        assert "fill" in note  # the retest depth
        assert "quality" in note


# ------------------------------------------------- the target-ladder watermark


class TestTargetLadderWatermark:
    def test_fill_span_math(self):
        assert _fvg_fill_span({"lo": 100.0, "hi": 110.0, "fill_pct": 0.5}) \
            == pytest.approx(5.0)
        assert _fvg_fill_span({"lo": 100.0, "hi": 110.0, "fill_pct": 0.0}) == 0.0
        assert _fvg_fill_span({"lo": 100.0, "hi": 110.0, "fill_pct": 1.0}) == 0.0
        assert _fvg_fill_span({"lo": 100.0, "hi": 110.0}) == 0.0  # no telemetry

    def test_partial_gap_barriers_at_watermark(self):
        # BUY at 98; supply gap [100, 110] half-filled -> the honest
        # barrier is 105.0 (the untraded air), NOT the raw edge 100.0
        base = _real_gap_tape()
        zone = {"side": "supply", "source": "fvg", "lo": 100.0, "hi": 110.0,
                "fill_pct": 0.5}
        entry, sl, tp, note = smart_targets(
            base, "BUY", 98.0, 90.0, 1.6, 1.5, 3.5,
            zones=[zone], min_rr=1.2, max_tp_r=3.0,
        )
        assert tp is not None, note
        assert tp == pytest.approx(105.0 - 0.08, abs=0.2)
        assert "105" in note  # the note states the watermark level

    def test_untouched_gap_barriers_at_near_edge(self):
        base = _real_gap_tape()
        zone = {"side": "supply", "source": "fvg", "lo": 100.0, "hi": 110.0,
                "fill_pct": 0.0}
        entry, sl, tp, note = smart_targets(
            base, "BUY", 95.0, 90.0, 1.6, 1.5, 3.5,
            zones=[zone], min_rr=1.2, max_tp_r=3.0,
        )
        assert tp is not None, note
        assert tp == pytest.approx(100.0 - 0.08, abs=0.2)

    def test_non_fvg_zone_unchanged(self):
        base = _real_gap_tape()
        zone = {"side": "supply", "source": "sd", "lo": 100.0, "hi": 110.0,
                "fill_pct": 0.5}  # telemetry present but source is sd
        entry, sl, tp, note = smart_targets(
            base, "BUY", 95.0, 90.0, 1.6, 1.5, 3.5,
            zones=[zone], min_rr=1.2, max_tp_r=3.0,
        )
        assert tp is not None, note
        assert tp == pytest.approx(100.0 - 0.08, abs=0.2)  # raw near edge


# --------------------------------------------------- pulse + config contracts


class TestContracts:
    def test_pulse_zone_carries_fvg_telemetry(self):
        base = _real_gap_tape()
        htf = {**_uptrend_htf(), "M5": _htf_gap_frame(price=4497.0)}
        close_time = base["time_utc"].iloc[-1] + timedelta(minutes=1)
        ev = evaluate(base, htf, close_time, EngineConfig(), 20.0)
        assert ev.pulse is not None
        fvg_rows = [z for z in ev.pulse.get("zones", [])
                    if z.get("source") == "fvg"]
        assert fvg_rows, ev.pulse.get("zones")
        assert all("gap_atr" in z and "fill_pct" in z for z in fvg_rows)
        assert any(z.get("tf") for z in fvg_rows)  # the M5-born zone visible

    def test_config_defaults(self):
        cfg = EngineConfig()
        assert cfg.fvg_importance is True
        assert cfg.fvg_min_atr == FVG_MIN_ATR == 0.30
        assert cfg.fvg_min_disp == FVG_MIN_DISP == 0.40
        assert cfg.fvg_htf_enabled is True
        assert cfg.fvg_stack_bonus == 0.10
        assert cfg.fvg_pd_adjust == 0.05
        assert "fvg_importance" not in DEFAULT_CONFIG  # code defaults only

    def test_fvg_zone_quality_bounds(self):
        q_hi = fvg_zone_quality(
            gap_atr=1.2, disp=1.5, fresh_q=1.0, age_q=1.0,
            tpo_minutes=0.0, htf=True,
        )
        q_lo = fvg_zone_quality(
            gap_atr=0.31, disp=0.41, fresh_q=0.45, age_q=0.15,
            tpo_minutes=0.0, htf=False,
        )
        assert q_hi >= 0.95
        assert q_lo < 0.45


# --------------------------------------------------- the A/B legacy escape


class TestLegacyAbPath:
    def test_legacy_restores_old_zone_model(self):
        df = _noise_gap_tape()
        legacy = poi_zones(df, cfg=EngineConfig(fvg_importance=False))
        noise = [z for z in legacy if z["source"] == "fvg"
                 and (z["hi"] - z["lo"]) < 0.2]
        assert noise, "legacy path must mint the noise gap"
        z = noise[0]
        assert "gap_atr" not in z      # no D-069 telemetry on legacy zones
        assert "stacked" not in z
        assert z["impulse"] is not None  # the old impulse mapping survives

    def test_quality_model_weights_sum(self):
        from app.analysis.poi import (
            FVG_W_AGE,
            FVG_W_DISP,
            FVG_W_FRESH,
            FVG_W_SIZE,
            FVG_W_TPO,
        )
        assert FVG_W_SIZE + FVG_W_DISP + FVG_W_FRESH + FVG_W_AGE + FVG_W_TPO \
            == pytest.approx(1.0)
