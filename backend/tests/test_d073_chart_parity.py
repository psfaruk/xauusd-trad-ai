"""D-073 — chart parity + the entry-proximity directive.

User report (Bengali): "দেখো মার্কেট কোথায় আর এন্ট্রি সিগন্যাল কোথায় গিয়ে
পড়ে। এই জন্য মনে হচ্ছে সিগন্যাল কম আসছে, আর প্রফিট ও হয় না।" — the
engine's drawing-true reach (1.5 M5-ATRs) booked entries at zones up to
2x farther than anything the chart's own setup box draws (0.75), and
NOTHING capped the drawn pending's distance from the market. Covered:
- the default reach == the chart box's SETUP_NEAR_ATR (parity);
- upgrade_legacy_d073 moves untouched 1.5 rows, preserves custom values;
- _drawing_geometry defers (None) when the drawn entry sits beyond the
  pending window — the legacy chain then anchors nearer or refuses
  visibly, instead of booking a limit deep into no-man's land;
- the near-zone contract itself is unchanged (the d057 suite pins it).
"""

from datetime import UTC, datetime

from app.analysis.setup_geometry import SETUP_NEAR_ATR, setup_snapshot
from app.engine.config import EngineConfig, upgrade_legacy_d073
from app.engine.engine import _drawing_geometry
from tests.test_d057_drawing_true import _m5_with_drawn_zones, _quiet_m1

# ------------------------------------------------------------- config parity

def test_setup_near_atr_default_is_chart_parity() -> None:
    """The engine books orders at the zones the chart actually draws —
    0.75 M5-ATRs, not the old 2x-wider 1.5 window."""
    assert EngineConfig().setup_near_atr == SETUP_NEAR_ATR == 0.75


def test_upgrade_d073_moves_untouched_default() -> None:
    raw = {"setup_near_atr": 1.5}
    out, moved = upgrade_legacy_d073(raw)
    assert out["setup_near_atr"] == 0.75
    assert moved == ["setup_near_atr"]


def test_upgrade_d073_preserves_custom_values() -> None:
    """A user-chosen reach is a choice — it stays put forever."""
    out, moved = upgrade_legacy_d073({"setup_near_atr": 1.2})
    assert out["setup_near_atr"] == 1.2
    assert moved == []
    out, moved = upgrade_legacy_d073({"tp_min_rr": 1.0})  # field absent
    assert "setup_near_atr" not in out
    assert moved == []


def test_upgrade_d073_tolerates_garbage() -> None:
    out, moved = upgrade_legacy_d073({"setup_near_atr": "not-a-number"})
    assert out["setup_near_atr"] == "not-a-number"
    assert moved == []
    out, moved = upgrade_legacy_d073(None)
    assert out is None and moved == []


# ------------------------------------------- the drawn-entry pending-window cap

def test_drawing_geometry_defers_deep_drawn_entry() -> None:
    """A drawn entry beyond the pending window is DEFERRED (None), not
    booked: the legacy chain anchors nearer (magnet/POI window) or
    refuses visibly. The old code booked the far limit — the exact
    'entry lands where the market never went' the user reported."""
    m5 = _m5_with_drawn_zones()
    snap = setup_snapshot(m5, "M5")
    demands = [z for z in snap["zones"] if z["side"] == "demand"]
    assert demands, "fixture must mint a demand zone"
    z = demands[0]
    htf = {"M5": m5, "M15": _quiet_m1()}
    # market ~1.8 USD above the zone's near edge: inside a WIDE reach
    # (old-style 4 ATR) but beyond the tight pending window
    market = round(float(z["hi"]) + 1.8, 2)
    tight = EngineConfig(setup_near_atr=4.0, pending_max_usd=1.0)
    geo = _drawing_geometry(
        _quiet_m1(), htf, "BUY", market=market, cfg=tight, spread_price=0.20,
    )
    assert geo is None  # the cap deferred it


def test_drawing_geometry_books_within_pending_window() -> None:
    """Sanity: the same wide-reach setup WITH a wide pending window still
    books the drawn trade — the cap defers only the deep bucket."""
    m5 = _m5_with_drawn_zones()
    snap = setup_snapshot(m5, "M5")
    demands = [z for z in snap["zones"] if z["side"] == "demand"]
    z = demands[0]
    htf = {"M5": m5, "M15": _quiet_m1()}
    market = round(float(z["hi"]) + 1.8, 2)
    wide = EngineConfig(setup_near_atr=4.0, pending_max_usd=6.0)
    geo = _drawing_geometry(
        _quiet_m1(), htf, "BUY", market=market, cfg=wide, spread_price=0.20,
    )
    assert geo is not None
    assert geo["entry"] == round(float(z["hi"]), 2)  # the drawn near edge
    assert geo["entry_type"] == "limit"


def test_drawing_geometry_default_reach_rejects_far_zone() -> None:
    """At the shipped 0.75-reach a market 1.8 USD off the zone draws no
    setup at all (setup_geometry returns None before the engine cap) —
    the chart and the engine now agree on what is 'at the market'."""
    m5 = _m5_with_drawn_zones()
    snap = setup_snapshot(m5, "M5")
    demands = [z for z in snap["zones"] if z["side"] == "demand"]
    z = demands[0]
    htf = {"M5": m5, "M15": _quiet_m1()}
    market = round(float(z["hi"]) + 1.8, 2)
    geo = _drawing_geometry(
        _quiet_m1(), htf, "BUY", market=market, cfg=EngineConfig(),
        spread_price=0.20,
    )
    assert geo is None


def test_drawing_geometry_default_still_books_near_zone() -> None:
    """The old d057 contract survives the parity change: price basically
    AT the zone books the drawn trade at the market with the drawn
    SL/TP contract (the market branch — dist below the noise margin)."""
    m5 = _m5_with_drawn_zones()
    snap = setup_snapshot(m5, "M5")
    demands = [z for z in snap["zones"] if z["side"] == "demand"]
    z = demands[0]
    htf = {"M5": m5, "M15": _quiet_m1()}
    market = round(float(z["hi"]) + 0.15, 2)
    geo = _drawing_geometry(
        _quiet_m1(), htf, "BUY", market=market, cfg=EngineConfig(),
        spread_price=0.20,
    )
    assert geo is not None
    assert geo["entry_type"] == "market"  # at the zone -> fill now
    assert abs(geo["entry"] - market) < 0.01
    assert geo["sl"] < float(z["lo"])    # the drawn contract intact
    assert geo["tp"] > geo["entry"]


def _unused_ts_guard() -> datetime:
    """Keeps the UTC/datetime import meaningful for fixture reuse."""
    return datetime(2026, 9, 25, tzinfo=UTC)
