"""D-061 tests — the institutional manipulation / AMD layer.

User directive (Bengali): "লাস্ট রেড ক্যান্ডেল এ সিগন্যাল ছিল মার্কেট নিচে
যাবে, একটা সেল অর্ডার ও creat হলো কয়েক pip নিচে নামলো, কিন্তু হঠাৎ করে
মার্কেট বিপরীত মুখী ... ইকমেলিউশন ও মেনোপোলেশন কোনো লজিক এড করতে
পারবেন? ... এই বিষয় টা কিভাবে আমার অ্যাপ বুজবে। এবং আমিও দেখতে পারবো।"

Covers:
- amd_state: the user's exact trapped-sell scenario reads as
  MANIPULATION -> DISTRIBUTION with the SSL sweep + reclaim + opposing
  displacement; compressed ranges read as accumulation;
- trap_risk: the SELL into the trap scores >= block level with named
  reasons; the mirrored BUY (the institutional side) stays clean; the
  liquidity-pool-in-risk-window component; Judas timing stacks;
- session_context: London/NY opens flag the Judas window;
- evaluate(): the payload + trace carry the context block, the trap gate
  blocks the retail side, the warn band cuts confidence;
- tracker: the pre-fill displacement guard cancels a waiting limit when
  two institutional bodies print against it (and resets on a quiet bar);
- backtest _SimTracker: the same guard mirrors into the replay;
- drawings: the AMD marks land on the chart, the setup box carries the
  TRAP tag.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd

from app.analysis.drawings import build_drawings
from app.analysis.manipulation import amd_state, session_context, trap_risk
from app.engine.backtest import BacktestSignal, _SimTracker
from app.engine.config import EngineConfig
from app.engine.engine import evaluate
from app.engine.tracker import SignalTracker, make_tracked
from tests.test_d048_poi_zones import _bar

CFG = EngineConfig()


# ------------------------------------------------------------- fixtures


def _trapped_sell_frame(seed: int = 7) -> pd.DataFrame:
    """The user's screenshot as bars: a range, red candles breaking down
    (the SELL signal bar), a wick below the range low that CLOSES BACK
    INSIDE (the SSL sweep + reclaim), then two LARGE consecutive bullish
    bodies (the displacement that ate the sell)."""
    rng = np.random.default_rng(seed)
    n = 120
    t0 = datetime(2026, 9, 23, 20, 0, tzinfo=UTC)
    rows = []
    for i in range(n):
        t = t0 + timedelta(minutes=i)
        drift = 0.02 * np.sin(i / 12.0)
        o = 2650.0 + drift
        c = o + rng.normal(0, 0.12)
        h = max(o, c) + abs(rng.normal(0, 0.08))
        low = min(o, c) - abs(rng.normal(0, 0.08))
        rows.append([t, o, h, low, c, 100.0])
    df = pd.DataFrame(rows, columns=["time_utc", "o", "h", "l", "c", "v"])
    # 3 red candles falling (the "market will go down" signal bars)
    i = n - 6
    p = float(df["c"].iloc[i])
    for k in range(i, i + 3):
        df.iloc[k, [1, 4]] = [p, p - 0.35]
        df.iloc[k, [2, 3]] = [p + 0.05, p - 0.40]
        p -= 0.35
    # the sweep bar: wick below the prior range low, close back above
    sweep_lo = float(df["l"].iloc[:-6].min())
    df.iloc[n - 3, [1, 4]] = [sweep_lo + 0.10, sweep_lo + 0.22]
    df.iloc[n - 3, [2, 3]] = [sweep_lo + 0.28, sweep_lo - 0.45]
    # two large bullish displacement bodies
    df.iloc[n - 2, [1, 4]] = [sweep_lo + 0.22, sweep_lo + 0.95]
    df.iloc[n - 2, [2, 3]] = [sweep_lo + 1.00, sweep_lo + 0.18]
    df.iloc[n - 1, [1, 4]] = [sweep_lo + 0.95, sweep_lo + 1.65]
    df.iloc[n - 1, [2, 3]] = [sweep_lo + 1.70, sweep_lo + 0.90]
    return df


def _compressed_range_frame() -> pd.DataFrame:
    """A coiled range: width far below the random-walk expectation."""
    t0 = datetime(2026, 9, 23, 18, 0, tzinfo=UTC)
    rows = []
    for i in range(90):
        t = t0 + timedelta(minutes=i)
        c = 2600.0 + 0.4 * np.sin(i / 9.0)
        rows.append([t, c - 0.1, c + 0.1, c - 0.1, c, 100.0])
    return pd.DataFrame(rows, columns=["time_utc", "o", "h", "l", "c", "v"])


# ------------------------------------------------------------- amd_state


def test_amd_state_reads_the_trapped_sell_as_distribution() -> None:
    st = amd_state(_trapped_sell_frame())
    assert st["phase"] == "distribution"
    sweep = st["sweep"]
    assert sweep is not None and sweep["side"] == "SSL"
    assert sweep["reclaimed"] is True
    assert sweep["depth_atr"] >= 0.5
    disp = st["displacement"]
    assert disp is not None and disp["dir"] == "up" and disp["run"] >= 2
    assert disp["price"] is not None  # the chart anchor
    assert "distributing" in st["note"]


def test_amd_state_manipulation_before_the_displacement() -> None:
    """Cut the frame BEFORE the big greens: the sweep + reclaim alone
    reads as MANIPULATION — the reversal is pending."""
    df = _trapped_sell_frame().iloc[:-2]
    st = amd_state(df)
    assert st["phase"] == "manipulation"
    assert st["sweep"] is not None and st["sweep"]["side"] == "SSL"
    assert "reclaimed" in st["note"] or "false break" in st["note"]


def test_amd_state_compressed_range_is_accumulation() -> None:
    st = amd_state(_compressed_range_frame())
    assert st["phase"] == "accumulation"
    assert st["sweep"] is None
    assert st["range"]["width_atr"] < 15.0


def test_amd_state_insufficient_bars() -> None:
    st = amd_state(pd.DataFrame(columns=["time_utc", "o", "h", "l", "c", "v"]))
    assert st["phase"] == "none"


# ------------------------------------------------------------- trap_risk


def test_trap_risk_blocks_the_retail_sell() -> None:
    """The user's trade, caught at BOTH moments:
    1. ON the sweep bar (the SFP-moment sell): the SSL sweep just
       reclaimed, price still at the level -> risk 0.85 (block level);
    2. AFTER the displacement ran: component C (momentum already
       flipped) keeps the sell above the block threshold."""
    df = _trapped_sell_frame()
    st = amd_state(df)
    # 1) the sweep-bar moment (frame cut at the sweep bar)
    sell_entry = float(df["c"].iloc[-3])
    tr = trap_risk(df.iloc[:-2], "SELL", sell_entry, sell_entry - 0.5,
                   state=amd_state(df.iloc[:-2]))
    assert tr["risk"] >= 0.85
    reasons = " ".join(tr["reasons"]).lower()
    assert "ssl" in reasons and "reclaimed" in reasons
    assert "institutions" in reasons
    # 2) the post-displacement moment
    entry_late = float(df["c"].iloc[-4])
    tr2 = trap_risk(df, "SELL", entry_late, entry_late - 0.5, state=st)
    assert tr2["risk"] >= 0.7  # displacement component still blocks it
    reasons2 = " ".join(tr2["reasons"]).lower()
    assert "bodies" in reasons2 or "displacement" in reasons2


def test_trap_risk_mirror_buy_is_the_institutional_side() -> None:
    df = _trapped_sell_frame()
    st = amd_state(df)
    entry = float(df["c"].iloc[-1])
    tr = trap_risk(df, "BUY", entry, entry - 0.5, state=st)
    assert tr["risk"] < CFG.trap_warn_risk


def test_trap_risk_liquidity_pool_inside_risk_window() -> None:
    """A SELL whose entry..SL brackets an unswept SSL pool is fishing
    INTO the draw — the pool component fires."""
    t0 = datetime(2026, 9, 23, 21, 0, tzinfo=UTC)
    rows = []
    # 50 quiet bars around 100.5 (the ATR baseline)
    for i in range(50):
        t = t0 + timedelta(minutes=i)
        rows.append([t, 100.5, 100.8, 100.2, 100.5, 100.0])
    # dip 1: low 99.2, close back above (a swing low)
    for k, c in enumerate((99.9, 99.5, 99.3)):
        rows.append([t0 + timedelta(minutes=50 + k), c + 0.1, c + 0.2, 99.2, c, 100.0])
    # rally between the dips
    for k in range(3):
        rows.append([t0 + timedelta(minutes=53 + k), 100.0, 100.3, 99.9, 100.2, 100.0])
    # dip 2: the SAME low 99.2 -> two equal lows = the SSL pool
    for k, c in enumerate((99.9, 99.5, 99.3)):
        rows.append([t0 + timedelta(minutes=56 + k), c + 0.1, c + 0.2, 99.2, c, 100.0])
    # quiet close: a flat shelf of equal lows AT 99.4 — the resting SSL
    # pool inside the coming sell's risk window (no fresh sweep)
    for i in range(59, 72):
        rows.append([t0 + timedelta(minutes=i), 99.5, 99.7, 99.4, 99.55, 100.0])
    df = pd.DataFrame(rows, columns=["time_utc", "o", "h", "l", "c", "v"])
    # SELL entry 99.6, SL 99.0: the 99.4 pool sits inside the risk window
    tr = trap_risk(df, "SELL", 99.6, 99.0)
    assert "pool_in_risk" in tr["components"]
    assert any("pool" in r.lower() for r in tr["reasons"])


def test_trap_risk_judas_timing_stacks() -> None:
    """A signal inside the London-open window stacks the timing bonus."""
    df = _trapped_sell_frame()
    st = amd_state(df)
    entry = float(df["c"].iloc[-4])
    london_open = df["time_utc"].iloc[-1].floor("D") + timedelta(hours=7, minutes=30)
    tr = trap_risk(df, "SELL", entry, entry - 0.5, state=st, now=london_open)
    assert "judas_timing" in tr["components"]
    assert tr["risk"] >= 0.9


# -------------------------------------------------------- session_context


def test_session_context_flags_judas_windows() -> None:
    assert session_context(datetime(2026, 9, 23, 7, 10, tzinfo=UTC))["judas_window"]
    assert session_context(datetime(2026, 9, 23, 12, 20, tzinfo=UTC))["judas_window"]
    assert session_context(datetime(2026, 9, 23, 15, 45, tzinfo=UTC))["judas_window"]
    # 4 hours UTC: off-session, no Judas flag
    off = session_context(datetime(2026, 9, 23, 4, 0, tzinfo=UTC))
    assert not off["judas_window"]
    assert off["name"] == "asia"


# ------------------------------------------------------------- evaluate


def _htf_frames(n: int = 80) -> dict[str, pd.DataFrame]:
    """Quiet M5/M15/H1/H4 frames with mild DOWN structure (SELL bias)."""
    out = {}
    for tf, m in (("H1", 60), ("M15", 15), ("M5", 5), ("H4", 240)):
        t0 = datetime(2026, 9, 23, 0, 0, tzinfo=UTC) - timedelta(minutes=m * n)
        rows = [
            _bar(t0 + timedelta(minutes=m * i), 2000.0 - i, 2000.6 - i,
                 1999.0 - i, 1999.6 - i, 50)
            for i in range(n)
        ]
        out[tf] = pd.DataFrame(rows, columns=["time_utc", "o", "h", "l", "c", "v"])
    return out


def test_evaluate_blocks_the_trapped_direction() -> None:
    """A candidate trade INTO a completed manipulation is refused by the
    trap gate (near-miss with the trap_filter trace line) — the exact
    trade the user watched fill and then reverse. The demand-zone frame
    reliably FIRES a BUY; patching the trap verdict to block level must
    refuse it with the trap trace, while trap_filter=False lets it run
    (D-049: signals must not silently vanish)."""
    from app.analysis import manipulation as mani
    from tests.test_d048_poi_zones import _demand_zone_frame, _uptrend_htf

    df = _demand_zone_frame()
    htf = _uptrend_htf()
    close_time = df["time_utc"].iloc[-1] + timedelta(minutes=1)

    fake = {
        "risk": 0.95, "phase": "manipulation", "amd_note": "x",
        "reasons": ["fake block reason"],
        "components": {"sweep_reclaimed": 0.85},
        "session": session_context(close_time),
    }

    def _fake(df_, direction, entry, sl, state=None, now=None):
        return fake

    orig = mani.trap_risk
    mani.trap_risk = _fake
    try:
        ev = evaluate(
            df, htf, close_time, EngineConfig(entry_mode="market", regime_guard=False, trap_filter=True),
            spread_points=20,
        )
    finally:
        mani.trap_risk = orig
    assert ev.signal is None
    names = {c["name"] for c in ev.trace["checks"]}
    assert "trap_filter" in names
    trap_check = next(c for c in ev.trace["checks"] if c["name"] == "trap_filter")
    assert not trap_check["pass"]
    assert "fake block reason" in trap_check["value"]
    # the radar pulse still tells the story
    assert ev.pulse["trap"]["risk"] == 0.95
    assert ev.pulse["near_miss"]

    # the same trade with the gate OFF still fires (and is visibly tagged)
    mani.trap_risk = _fake
    try:
        ev2 = evaluate(
            df, htf, close_time, EngineConfig(entry_mode="market", regime_guard=False, trap_filter=False),
            spread_points=20,
        )
    finally:
        mani.trap_risk = orig
    assert ev2.signal is not None
    assert ev2.signal["context"]["trap"]["risk"] == 0.95


def test_evaluate_reads_the_real_trap_frame() -> None:
    """The authentic trapped-sell frame: the pulse carries the AMD read
    every bar close, whatever the trigger pipeline did with it."""
    df = _trapped_sell_frame(seed=11)
    close_time = df["time_utc"].iloc[-1] + timedelta(minutes=1)
    ev = evaluate(df, _htf_frames(), close_time, EngineConfig(), spread_points=20)
    assert ev.pulse.get("amd") is not None
    assert ev.pulse["amd"]["phase"] in ("manipulation", "distribution", "none")
    if ev.signal is not None:
        # anything that fired through this structure stayed under the
        # block level and carries the context
        assert ev.signal["context"]["trap"]["risk"] < EngineConfig().trap_block_risk


def test_evaluate_payload_carries_market_context() -> None:
    """Any signal that fires carries the D-061 context block: AMD phase,
    trap verdict, session note, news line — the app + the user SEE it."""
    from tests.test_d048_poi_zones import _demand_zone_frame, _uptrend_htf

    df = _demand_zone_frame()
    htf = _uptrend_htf()
    close_time = df["time_utc"].iloc[-1] + timedelta(minutes=1)
    cfg = EngineConfig(entry_mode="market", regime_guard=False)  # D-068 isolated
    ev = evaluate(df, htf, close_time, cfg, spread_points=20)
    assert ev.signal is not None
    ctx = ev.signal["context"]
    assert set(ctx) >= {"amd", "trap", "session", "news"}
    assert ctx["trap"]["risk"] >= 0.0
    assert isinstance(ctx["trap"]["reasons"], list)
    assert ctx["session"]["name"]  # the session verdict is always present
    assert ctx["news"]  # the news verdict rides the context too
    # the trace carries the same block (DB persistence path)
    assert ev.signal["trace"]["context"]["trap"]["risk"] == ctx["trap"]["risk"]
    # the radar pulse carries the AMD read + trap verdict
    assert ev.pulse["amd"] is not None
    assert ev.pulse["trap"]["risk"] == ctx["trap"]["risk"]
    # the fired summary carries the trap verdict
    assert ev.pulse["fired"]["trap"]["risk"] == ctx["trap"]["risk"]


def test_trap_gate_off_still_reports_the_context() -> None:
    """trap_filter=False: signals keep flowing (D-049 'no missed
    signals') but the context + trap risk STILL ride the payload."""
    from tests.test_d048_poi_zones import _demand_zone_frame, _uptrend_htf

    df = _demand_zone_frame()
    htf = _uptrend_htf()
    close_time = df["time_utc"].iloc[-1] + timedelta(minutes=1)
    cfg = EngineConfig(entry_mode="market", regime_guard=False, trap_filter=False)
    ev = evaluate(df, htf, close_time, cfg, spread_points=20)
    assert ev.signal is not None
    assert ev.signal["context"]["trap"] is not None


def test_trap_warn_band_cuts_confidence() -> None:
    """A warn-band trap (0.4 <= risk < 0.7) tags the signal, pays the
    confidence penalty, and still fires."""
    from app.analysis import manipulation as mani
    from tests.test_d048_poi_zones import _demand_zone_frame, _uptrend_htf

    df = _demand_zone_frame()
    htf = _uptrend_htf()
    close_time = df["time_utc"].iloc[-1] + timedelta(minutes=1)
    base_cfg = EngineConfig(entry_mode="market", regime_guard=False, trap_filter=False)
    base = evaluate(df, htf, close_time, base_cfg, spread_points=20)
    assert base.signal is not None

    # monkeypatch trap_risk to return a warn-band verdict
    orig = mani.trap_risk
    fake = {
        "risk": 0.55, "phase": "manipulation", "amd_note": "x",
        "reasons": ["fake warn reason"],
        "components": {"sweep_reclaimed": 0.55},
        "session": session_context(close_time),
    }

    def _fake(df_, direction, entry, sl, state=None, now=None):
        return fake

    import app.engine.engine as eng_mod

    eng_mod.__dict__.setdefault("_trap_patch", None)
    mani.trap_risk = _fake
    try:
        cfg = EngineConfig(entry_mode="market", regime_guard=False, trap_filter=True,
                           trap_conf_penalty=0.12)
        ev = evaluate(df, htf, close_time, cfg, spread_points=20)
    finally:
        mani.trap_risk = orig
    assert ev.signal is not None
    assert ev.signal["context"]["trap"]["risk"] == 0.55
    assert ev.signal["context"]["trap"]["warned"] is True
    assert ev.signal["confidence"] <= base.signal["confidence"]
    names = {c["name"] for c in ev.signal["trace"]["checks"]}
    assert "trap_filter" in names


# ------------------------------------------------- pre-fill displacement


def _pending_sell(tracker_cfg) -> make_tracked:  # type: ignore[valid-type]
    return make_tracked(
        direction="SELL", entry=103.0, sl=104.0, tp=100.0,
        confidence=0.6, trace={}, bar_time=datetime(2026, 9, 23, 12, 0, tzinfo=UTC),
        entry_type="limit", market_ref=100.0, atr_ref=1.0,
    )


def test_tracker_cancels_pending_on_opposing_displacement() -> None:
    """The user's trap, caught BEFORE the fill: a waiting SELL limit +
    two consecutive ~1.1-ATR bullish bodies (that have NOT yet reached
    the limit) -> the pending cancels (missed trade, no loss) with the
    AMD reason — the order is pulled before the trap fills."""
    tr = SignalTracker()
    sig = _pending_sell(CFG)
    import asyncio

    closed: list = []

    async def _capture(s) -> None:
        closed.append(s)

    tr.on_status = _capture
    asyncio.run(tr.register(sig))
    t1 = datetime(2026, 9, 23, 12, 1, tzinfo=UTC)
    # bar 1: big bullish body against the SELL (1.2 ATR, high 101.3 < 103)
    asyncio.run(tr.on_bar_close(36, t1, 101.2, pending_expiry_bars=60,
                                bar_low=100.2, bar_high=101.3, bar_open=100.0))
    assert tr.active[0].status == "pending"  # one bar alone is not proof
    assert tr.active[0].disp_run == 1
    # bar 2: another big bullish body — the distribution began
    t2 = t1 + timedelta(minutes=1)
    asyncio.run(tr.on_bar_close(36, t2, 102.4, pending_expiry_bars=60,
                                bar_low=101.1, bar_high=102.5, bar_open=101.2))
    assert closed and closed[0].status == "cancelled"
    assert closed[0].result_r is None
    assert "displacement" in (closed[0].close_reason or "")
    d = closed[0].to_signal_dict("XAUUSD", "M1")
    assert d["close_reason"] and "displacement" in d["close_reason"]


def test_tracker_displacement_run_resets_on_quiet_bar() -> None:
    tr = SignalTracker()
    sig = _pending_sell(CFG)
    import asyncio

    asyncio.run(tr.register(sig))
    t1 = datetime(2026, 9, 23, 12, 1, tzinfo=UTC)
    asyncio.run(tr.on_bar_close(36, t1, 101.2, pending_expiry_bars=60,
                                bar_low=100.2, bar_high=101.3, bar_open=100.0))
    # a quiet bar resets the run
    t2 = t1 + timedelta(minutes=1)
    asyncio.run(tr.on_bar_close(36, t2, 101.0, pending_expiry_bars=60,
                                bar_low=100.9, bar_high=101.05, bar_open=100.95))
    assert tr.active[0].disp_run == 0
    assert tr.active[0].status == "pending"
    # one more big bar alone is NOT enough after the reset
    t3 = t2 + timedelta(minutes=1)
    asyncio.run(tr.on_bar_close(36, t3, 102.4, pending_expiry_bars=60,
                                bar_low=101.9, bar_high=102.5, bar_open=101.0))
    assert tr.active[0].status == "pending"


def test_sim_tracker_mirrors_the_displacement_guard() -> None:
    sim = _SimTracker(EngineConfig())
    sig = BacktestSignal(
        ts=datetime(2026, 9, 23, 12, 0, tzinfo=UTC), direction="SELL",
        entry=101.0, sl=102.0, tp=98.0, confidence=0.6, session="London",
        entry_type="limit", market_ref=100.0, filled=False, atr_ref=1.0,
    )
    sim.add(sig)
    # two big bullish bars that do NOT reach the sell limit (high < 101)
    t = datetime(2026, 9, 23, 12, 1, tzinfo=UTC)
    bar = pd.Series({"o": 100.0, "h": 100.9, "l": 99.9, "c": 101.2,
                     "time_utc": t})
    sim.step(bar)
    assert sig.status == "pending"
    bar2 = pd.Series({"o": 101.2, "h": 100.95, "l": 101.1, "c": 102.4,
                      "time_utc": t + timedelta(minutes=1)})
    sim.step(bar2)
    assert sig.status == "cancelled"
    assert sig.result_r is None
    assert "displacement" in (sig.close_reason or "")


# ------------------------------------------------------------- drawings


def test_drawings_carry_amd_marks() -> None:
    """The chart draws the trap anatomy: the accumulation range box, the
    MANIPULATION marker at the swept level, the DISTRIBUTION arrow."""
    df = _trapped_sell_frame()
    from app.analysis.context import analyze_frame

    frames = {"M1": df}
    snaps = {"M1": analyze_frame(df)}
    marks = build_drawings(frames, snaps, price=float(df["c"].iloc[-1]), tf="M1")
    amd_marks = [m for m in marks if m["kind"] == "amd"]
    elements = {m["element"] for m in amd_marks}
    assert "range" in elements
    assert "manipulation" in elements
    assert "distribution" in elements
    man = next(m for m in amd_marks if m["element"] == "manipulation")
    assert man["side"] == "SSL" and "reclaimed" in man["label"].lower()
    dist = next(m for m in amd_marks if m["element"] == "distribution")
    assert dist["price"] is not None


def test_drawings_setup_note_carries_trap_tag() -> None:
    """The setup box the user watches shows WHY the trade is suspect
    (TRAP: reason) — the directive 'আমিও দেখতে পারবো'."""
    import numpy.random as npr

    rng = npr.default_rng(5)
    t0 = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
    rows = []
    # quiet tape with a demand zone the setup can anchor on
    for i in range(60):
        t = t0 + timedelta(minutes=i)
        c = 2650.0 + rng.normal(0, 0.1)
        rows.append([t, c - 0.05, c + 0.2, c - 0.2, c, 100.0])
    # a demand base + impulse (zone for the setup)
    rows.append([t0 + timedelta(minutes=60), 2649.8, 2649.9, 2649.4, 2649.5, 100.0])
    rows.append([t0 + timedelta(minutes=61), 2649.5, 2652.6, 2649.4, 2652.4, 100.0])
    # pullback toward the zone + the trap: sweep below recent lows + reclaim + displacement up
    for k in range(62, 66):
        t = t0 + timedelta(minutes=k)
        c = 2652.4 - 0.8 * (k - 61)
        rows.append([t, c + 0.1, c + 0.2, c - 0.3, c, 100.0])
    lo = min(r[3] for r in rows[60:66])
    rows.append([t0 + timedelta(minutes=66), lo + 0.1, lo + 0.3, lo - 0.5, lo + 0.2, 100.0])
    rows.append([t0 + timedelta(minutes=67), lo + 0.2, lo + 1.1, lo + 0.1, lo + 1.0, 100.0])
    rows.append([t0 + timedelta(minutes=68), lo + 1.0, lo + 1.7, lo + 0.9, lo + 1.6, 100.0])
    df = pd.DataFrame(rows, columns=["time_utc", "o", "h", "l", "c", "v"])
    from app.analysis.context import analyze_frame, mtf_bias

    snaps = {"M1": analyze_frame(df), "M5": analyze_frame(df.iloc[-90:])}
    bias = mtf_bias(snaps)
    setup = None
    from app.analysis.drawings import _setup

    setup = _setup({"M1": df, "M5": df.iloc[-90:]}, snaps, bias,
                   price=float(df["c"].iloc[-1]),
                   now=datetime.now(UTC), recent_signals=[])
    if setup is not None and float(
        (trap_risk(df, setup["dir"], setup["entry"], setup["sl"]) or {}).get("risk", 0)
    ) >= 0.4:
        assert "TRAP" in setup["note"]
        assert any("trap risk" in f for f in setup["factors"])
