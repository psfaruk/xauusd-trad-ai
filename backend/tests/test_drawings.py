"""D-043 tests — professional auto-drawings + deep chart backfill.

Covers:
- build_drawings on the crafted bullish ICT tape: a BUY setup drawing with
  zone/entry/SL/TP/RR + factors, bounded object count, ISO times;
- the setup flips to "triggered" when a matching recent signal exists;
- robustness: empty/short frames, garbage recent_signals -> no crash;
- McpMarketFeed.bars high-water mark: a deep chart request is never served
  from (or shrunk back to) the engine's thin cache.
"""

from __future__ import annotations

import time as time_mod
from datetime import UTC, datetime, timedelta

import pandas as pd

from app.analysis.context import analyze_frame
from app.analysis.drawings import build_drawings
from app.mt5.mcp_market import BARS_CACHE_TTL_S, McpMarketFeed

from .test_confluence import _mk, bullish_ict_m1

# ------------------------------------------------------------- drawings


def wavy_frame(base: pd.Timestamp, tf_min: int, bars: int = 120,
               start: float = 90.0) -> pd.DataFrame:
    """Rising market with REAL fractal swings (6 up / 4 down legs): HH+HL
    structure (the plain sawtooth has no fractal points at all)."""
    rows = []
    prev_c = start
    price = start
    for i in range(bars):
        t = base - pd.Timedelta(minutes=tf_min * bars) + pd.Timedelta(minutes=tf_min * i)
        up = (i % 10) < 6
        step = 0.7 if up else -0.5
        c = price + step
        o = prev_c
        rows.append([t, o, max(o, c) + 0.15, min(o, c) - 0.15, c, 50])
        price, prev_c = c, c
    return _mk(rows)


def _snaps(frames: dict[str, pd.DataFrame]) -> dict[str, dict]:
    return {tf: analyze_frame(df) for tf, df in frames.items()}


def _frames() -> dict[str, pd.DataFrame]:
    """M1 bullish ICT tape; the SAME tape doubles as M5 (its OB zone holds
    the current price), wavy HH/HL frames above give the bullish bias."""
    base = pd.Timestamp("2025-01-06 12:00", tz="UTC")
    m1 = bullish_ict_m1()
    return {
        "M1": m1,
        "M5": m1,  # price sits inside this frame's bullish OB zone
        "M15": wavy_frame(base, 15, start=81.6, bars=120),
        "M30": wavy_frame(base, 30, start=81.6, bars=120),
        "H1": wavy_frame(base, 60, start=81.6, bars=120),
        "H4": wavy_frame(base, 240, start=81.6, bars=120),
        "D1": wavy_frame(base, 1440, start=81.6, bars=120),
    }


def _m5_demand_supply_around(price: float) -> pd.DataFrame:
    """D-057 — an M5 tape whose smc layer draws BOTH sides of the trade:
    a demand zone just under `price` (entry) and a supply zone above it
    (the drawn TP target). The setup box only exists when the chart
    draws a real target — no invented TPs."""
    t0 = pd.Timestamp("2025-01-06 11:00", tz="UTC")
    rows = []
    quiet = price - 3.4
    for i in range(20):  # ATR ~ 1.0, unique lows (no SSL pools nearby)
        t = t0 + pd.Timedelta(minutes=5 * i)
        c = quiet + 0.1 * (i % 3)
        rows.append([t, c - 0.1, c + 0.5, c - 0.6 - 0.01 * i, c + 0.1, 60])
    zlo, zhi = price - 0.7, price - 0.1          # demand zone band
    rows.append([t0 + pd.Timedelta(minutes=100), zhi - 0.2, zhi, zlo,
                 zlo + 0.1, 60])                 # down base candle
    rows.append([t0 + pd.Timedelta(minutes=105), zlo + 0.1, price + 2.7,
                 zlo + 0.15, price + 2.5, 60])   # up impulse
    slo, shi = price + 1.8, price + 2.5          # supply zone band
    rows.append([t0 + pd.Timedelta(minutes=110), slo + 0.05, shi, slo,
                 shi - 0.1, 60])                 # UP base candle (c > o)
    rows.append([t0 + pd.Timedelta(minutes=115), shi - 0.1, price + 2.6,
                 price - 0.8, price - 0.6, 60])  # down impulse
    legs = [
        (price - 0.4, price + 0.1, price - 0.5, price - 0.2),
        (price - 0.2, price + 0.2, price - 0.35, price - 0.1),
        (price - 0.1, price + 0.3, price - 0.25, price + 0.0),
        (price + 0.0, price + 0.4, price - 0.15, price + 0.1),
        (price + 0.1, price + 0.35, price - 0.05, price + 0.05),
        (price + 0.05, price + 0.3, price - 0.1, price + 0.0),
    ]
    for i, (o, h, low, c) in enumerate(legs):
        rows.append([t0 + pd.Timedelta(minutes=5 * (24 + i)), o, h, low, c, 60])
    return _mk(rows)


def test_setup_drawing_on_bullish_ict_tape():
    frames = _frames()
    # D-057 — the M5 must draw BOTH the entry zone and a TP target
    frames["M5"] = _m5_demand_supply_around(float(frames["M1"]["c"].iloc[-1]))
    price = float(frames["M1"]["c"].iloc[-1])
    out = build_drawings(frames, _snaps(frames), price, [])
    assert len(out) <= 26
    setups = [d for d in out if d["kind"] == "setup"]
    assert len(setups) == 1
    s = setups[0]
    assert s["dir"] == "BUY"
    assert s["zone"][0] < s["zone"][1]
    assert s["sl"] < s["zone"][0]          # SL below the zone (app-controlled)
    assert s["tp"] > s["entry"]            # TP above entry for a BUY
    assert s["rr"] > 0
    assert s["status"] == "forming"
    assert s["factors"], "setup must carry its confluence notes"
    assert isinstance(s["t0"], str) and "T" in s["t0"]  # ISO time
    # every drawing with a time field carries an ISO string
    for d in out:
        for k in ("t0", "t1", "t", "t2"):
            if d.get(k) is not None:
                assert isinstance(d[k], str)


def test_setup_marks_triggered_with_recent_signal():
    frames = _frames()
    frames["M5"] = _m5_demand_supply_around(float(frames["M1"]["c"].iloc[-1]))
    price = float(frames["M1"]["c"].iloc[-1])
    recent = [{
        "direction": "BUY",
        "entry": price,
        "ts": (datetime.now(UTC) - timedelta(minutes=3)).isoformat(),
    }]
    out = build_drawings(frames, _snaps(frames), price, recent)
    setups = [d for d in out if d["kind"] == "setup"]
    assert setups and setups[0]["status"] == "triggered"
    assert "entry taken" in setups[0]["note"].lower()


def test_old_signal_does_not_mark_triggered():
    frames = _frames()
    price = float(frames["M1"]["c"].iloc[-1])
    stale = [{
        "direction": "BUY",
        "entry": price,
        "ts": (datetime.now(UTC) - timedelta(hours=6)).isoformat(),
    }]
    out = build_drawings(frames, _snaps(frames), price, stale)
    setups = [d for d in out if d["kind"] == "setup"]
    assert not setups or setups[0]["status"] == "forming"


def test_drawings_never_crash_on_thin_data():
    base = pd.Timestamp("2025-01-06 12:00", tz="UTC")
    thin = _mk([[base + pd.Timedelta(minutes=i), 100, 101, 99, 100.5, 5]
                for i in range(12)])
    out = build_drawings({"M1": thin}, {"M1": analyze_frame(thin)}, 100.5, [])
    assert isinstance(out, list)
    assert build_drawings({}, {}, 0.0, []) == []
    # garbage recent signals must not break anything
    assert build_drawings({}, {}, 100.0, [{"weird": True}, None, 42]) == []


def test_trendline_and_fib_from_htf_frames():
    """Swing-rich frames produce trendlines and a fib on the overlay."""
    frames = _frames()
    snaps = {tf: analyze_frame(df) for tf, df in frames.items()}
    price = float(frames["M5"]["c"].iloc[-1])
    out = build_drawings(frames, snaps, price, [])
    kinds = {d["kind"] for d in out}
    assert "trendline" in kinds
    fibs = [d for d in out if d["kind"] == "fib"]
    assert fibs, "swing-rich frames must yield a fib retracement"
    f = fibs[0]
    assert len(f["levels"]) == 5
    assert f["t0"] != f["t1"]


# ------------------------------------------------------ D-052 drawings


def test_d052_zone_drawings_with_full_word_labels():
    """Labeled zone boxes (S/D + OB + FVG) carry full-word labels."""
    frames = _frames()
    snaps = _snaps(frames)
    price = float(frames["M1"]["c"].iloc[-1])
    for tf in ("M1", "M5", "M15"):
        out = build_drawings(frames, snaps, price, [], tf=tf)
        zones = [d for d in out if d["kind"] == "zone"]
        assert zones, f"{tf}: swing-rich frames must yield zone boxes"
        for z in zones:
            assert z["lo"] < z["hi"]
            assert isinstance(z["label"], str) and len(z["label"]) > 4
            # full words, never cryptic 2-letter tags
            assert z["label"].split()[0] in (
                "Supply", "Demand", "Bullish", "Bearish", "Fair",
            )
            assert z["source_tf"] in ("M1", "M5", "M15", "M30", "H1", "H4", "D1")


def test_d052_every_tf_gets_its_own_drawing_set():
    """The per-TF contract: each view draws from its OWN frame — switching
    timeframes never wipes the overlay (user directive)."""
    frames = _frames()
    snaps = _snaps(frames)
    price = float(frames["M1"]["c"].iloc[-1])
    sets = {
        tf: build_drawings(frames, snaps, price, [], tf=tf)
        for tf in ("M1", "M5", "M15", "H1")
    }
    for tf, out in sets.items():
        assert out, f"{tf} view must carry drawings"
        # every mark is bounded (a pro chart never floods)
        assert len(out) <= 34
        # hlines survive on every view (no time anchor to lose)
        assert any(d["kind"] == "hline" for d in out)
    # the sets are distinct objects per view (not one shared list)
    assert sets["M1"] is not sets["M5"]


def test_d052_channel_with_median_line():
    """A swing-rich rising frame yields a channel with upper/lower/median."""
    base = pd.Timestamp("2025-01-06 12:00", tz="UTC")
    frames = {"M15": wavy_frame(base, 15, start=81.6, bars=120)}
    snaps = _snaps(frames)
    price = float(frames["M15"]["c"].iloc[-1])
    out = build_drawings(frames, snaps, price, [], tf="M15")
    channels = [d for d in out if d["kind"] == "channel"]
    assert channels, "wavy HH/HL frame must yield a channel"
    ch = channels[0]
    assert ch["label"] in ("Ascending Channel", "Descending Channel")
    for part in ("upper", "lower", "median"):
        assert ch[part]["t1"] and ch[part]["p1"] is not None


def test_d052_hline_labels_are_full_words():
    """No cryptic PDH/POC/BSL-only tags — every level label is worded."""
    frames = _frames()
    snaps = _snaps(frames)
    price = float(frames["M1"]["c"].iloc[-1])
    out = build_drawings(frames, snaps, price, [], tf="M1")
    hlines = [d for d in out if d["kind"] == "hline"]
    assert hlines
    # the label must LEAD with a full word (a parenthetical abbreviation
    # like "Point of Control (POC)" is fine; a bare "POC" is not)
    full_words = (
        "Previous", "Buy", "Sell", "Point", "Time",
    )
    for d in hlines:
        assert d["label"].split()[0] in full_words, f"{d['label']} not worded"
        assert len(d["label"]) > 6


def test_d052_structure_and_sweep_drawings():
    """BOS/CHoCH chips + sweep lines use full-word labels with anchors."""
    frames = _frames()
    snaps = _snaps(frames)
    price = float(frames["M1"]["c"].iloc[-1])
    out = build_drawings(frames, snaps, price, [], tf="M1")
    kinds = {d["kind"] for d in out}
    # structure events exist on the ICT tape (it has breaks)
    if "structure" in kinds:
        s = next(d for d in out if d["kind"] == "structure")
        assert s["label"].split()[0] in ("BOS", "CHoCH")
        assert "Break of Structure" in s["label"] or "Change of Character" in s["label"]
    # sweeps (when present) are worded
    for d in (x for x in out if x["kind"] == "sweep"):
        assert d["label"] in ("Liquidity Sweep High", "Liquidity Sweep Low")
        assert d["side"] in ("high", "low")
    # arrows (when present) carry a direction + worded label
    for d in (x for x in out if x["kind"] == "arrow"):
        assert d["dir"] in ("up", "down")
        assert len(d["label"]) > 6


def test_d052_drawings_window_is_bounded():
    """Time-anchored marks sit in the recent 80-150 candles of the view."""
    from app.analysis.drawings import DRAW_WINDOW_BARS

    assert 80 <= DRAW_WINDOW_BARS <= 150
    frames = _frames()
    snaps = _snaps(frames)
    price = float(frames["M1"]["c"].iloc[-1])
    out = build_drawings(frames, snaps, price, [], tf="M15")
    base = frames["M15"]
    n = min(DRAW_WINDOW_BARS, len(base))
    t_start = pd.Timestamp(base["time_utc"].iloc[-n])
    for d in out:
        if d["kind"] in ("zone",) and d.get("t"):
            assert pd.Timestamp(d["t"]) >= t_start - pd.Timedelta(minutes=1)
        if d["kind"] in ("structure", "sweep") and d.get("t"):
            assert pd.Timestamp(d["t"]) >= t_start - pd.Timedelta(minutes=1)


def test_d053_drawing_state_marks_faded_marks():
    """D-053 — fadeable marks carry an explicit state so the frontend can
    render broken trendlines / tested order blocks THIN (user directive:
    faded drawings get thin text + thin lines, active ones stay hard)."""
    frames = _frames()
    snaps = _snaps(frames)
    price = float(frames["M1"]["c"].iloc[-1])
    for tf in ("M1", "M5", "M15"):
        out = build_drawings(frames, snaps, price, [], tf=tf)
        for d in out:
            if d["kind"] == "trendline":
                assert d["state"] in ("active", "faded")
                assert (d["state"] == "faded") == bool(d.get("broken"))
            if d["kind"] == "zone" and d["side"] in ("ob_bull", "ob_bear"):
                assert d["state"] in ("active", "faded")
                assert (d["state"] == "faded") == d["label"].endswith("· tested")


async def test_d052_analysis_service_serves_per_tf_drawings():
    """The /api/analysis snapshot carries drawings_by_tf — one drawing set
    per timeframe, so the frontend overlay never goes blank on a TF
    switch (user directive: "টাইম ফ্রম পরিবর্তন করলেও ড্রয়িং নষ্ট হবে না")."""
    from app.services.analysis import AnalysisService

    class _Src:
        platform_symbols = ["XAUUSD"]

        async def get_rates(self, symbol, tf, limit):
            return _frames().get(tf)

        def get_tick(self, symbol):
            return None

    svc = AnalysisService(ttl_s=0.0)
    payload = await svc.get(_Src(), "XAUUSD", [])
    assert payload["drawings_by_tf"], "per-TF drawing sets must be present"
    for tf in ("M1", "M5", "M15", "M30", "H1", "H4", "D1"):
        assert tf in payload["drawings_by_tf"], f"{tf} view missing"
        marks = payload["drawings_by_tf"][tf]
        assert isinstance(marks, list)
        assert any(d["kind"] == "hline" for d in marks), f"{tf} levels"
    # the legacy default field stays backward compatible (M1 marks)
    assert payload["drawings"] == payload["drawings_by_tf"]["M1"]


# ------------------------------------------------- analyze_frame regression


def test_analyze_frame_no_recursion_and_full_snapshot():
    """D-043 regression: vwap()'s session branch used to recurse forever —
    analyze_frame (i.e. EVERY /api/analysis request) died with
    RecursionError. It must return the full snapshot instead."""
    import sys

    frames = _frames()
    old_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(old_limit)  # default limit must be plenty
    try:
        for _tf, df in list(frames.items()):
            snap = analyze_frame(df)
            assert snap["ok"] is True
            assert snap["indicators"]["vwap"] > 0
            assert -1e9 < snap["indicators"]["rsi"] < 200
            assert snap["atr"] > 0
    finally:
        sys.setrecursionlimit(old_limit)


def test_vwap_session_anchor_matches_last_day_only():
    from app.analysis import indicators as ind

    base = pd.Timestamp("2025-01-06 22:00", tz="UTC")
    # day A: 30 bars at ~100, day B (last 10 bars): ~200
    rows = []
    for i in range(30):
        t = base + pd.Timedelta(minutes=i)
        rows.append([t, 100, 101, 99, 100, 10])
    day_b = base + pd.Timedelta(hours=2)
    for i in range(10):
        t = day_b + pd.Timedelta(minutes=i)
        rows.append([t, 200, 201, 199, 200, 10])
    df = _mk(rows)
    v = ind.vwap(df, period=0)
    assert 195 < v < 205  # anchored to day B only, NOT the whole frame
    assert 99 < ind.vwap(df, period=40) < 205  # rolling window mixes days



class _BarsClient:
    """Returns only `n` rows no matter how many were requested (mimics a
    bridge serving a thin window), tracking the requested limit."""

    def __init__(self, n: int) -> None:
        self.n = n
        self.requests: list[int] = []
        self._i = 0

    def bars(self, symbol, period, dt_from, dt_to, limit=1000):
        self.requests.append(int(limit))
        now = time_mod.time()
        return [
            {
                "time": datetime.fromtimestamp(
                    now - (self.n - k) * 60, tz=UTC
                ).strftime("%Y.%m.%d %H:%M:%S"),
                "open": 100.0 + k, "high": 101.0 + k, "low": 99.0 + k,
                "close": 100.5 + k, "tick_volume": 5,
            }
            for k in range(self.n)
        ]


async def test_bars_high_water_never_shrinks():
    """A 200-bar engine fetch, then a 1200-bar chart request: the deep
    request must trigger a bigger fetch, and a later small engine fetch
    must NOT shrink the cached window back down."""
    c = _BarsClient(n=1200)
    feed = McpMarketFeed(client=c, watch=["XAUUSD"])
    feed._bars_refetch[("XAUUSD", "M1")] = 0.0

    # engine fetch (small) — establishes a thin 200-bar cache
    closed, _ = await feed.bars("XAUUSD", "M1", 200)
    assert len(closed) == 200
    assert c.requests[-1] == 210  # want + 10

    # chart fetch (deep) inside the cache TTL: a fresh-but-THIN cache must
    # not silently satisfy it — the high-water mark forces the bigger call
    closed, _ = await feed.bars("XAUUSD", "M1", 1200)
    assert len(closed) == 1200
    assert c.requests[-1] == 1210

    # later small fetch inside the TTL: pure cache hit (no bridge call),
    # and the deep window is still intact (callers slice what they need)
    n_req = len(c.requests)
    closed, _ = await feed.bars("XAUUSD", "M1", 200)
    assert len(c.requests) == n_req
    assert len(closed) == 1200


async def test_bars_partial_response_never_shrinks_cache():
    """A bridge hiccup returning a PARTIAL window must not replace a
    deeper cached window."""
    c = _BarsClient(n=1200)
    feed = McpMarketFeed(client=c, watch=["XAUUSD"])
    await feed.bars("XAUUSD", "M1", 1200)  # deep cache established
    assert len(feed._bars[("XAUUSD", "M1")][0]) >= 1200

    # force a refetch that returns only 30 rows (bridge degraded)
    feed._bars[("XAUUSD", "M1")] = (
        feed._bars[("XAUUSD", "M1")][0],
        time_mod.monotonic() - BARS_CACHE_TTL_S - 1.0,
    )
    feed._bars_refetch[("XAUUSD", "M1")] = 0.0
    c.n = 30
    closed, _ = await feed.bars("XAUUSD", "M1", 1200)
    assert len(feed._bars[("XAUUSD", "M1")][0]) >= 1200  # never shrunk
    assert len(closed) >= 1200  # merged serve still deep enough
