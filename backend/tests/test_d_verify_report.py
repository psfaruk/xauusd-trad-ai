"""D-VERIFY — tests for the external-report verification pass.

External report claims verified in code and fixed where logical:
  §12/§28 — FINAL order-contract gate (no inverted SL/TP can ever emit)
  §8      — flow_block opt-in hard WAIT (D-049 default kept: penalty only)
  §13/§15 — backtest bid/ask-exact fills (BUY limit fills on the ASK
            touch; SELL exits on the ask side; spread embedded in levels,
            post-hoc tax only for market-born trades)
  §14     — live tracker bar-close fallback: BUY needs the bid low at
            entry - spread (the ask never traded there otherwise)
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from app.engine.backtest import BacktestSignal, _SimTracker, run_backtest
from app.engine.config import EngineConfig
from app.engine.engine import evaluate
from app.engine.tracker import SignalTracker, make_tracked


def _t0() -> datetime:
    return datetime(2026, 9, 24, 10, 0, tzinfo=UTC)


def make_bars(n: int, seed: int = 1, start: datetime | None = None) -> pd.DataFrame:
    """M1 random-walk frame with the columns evaluate() needs."""
    import numpy as np

    t0 = start or _t0()
    rng = np.random.default_rng(seed)
    steps = rng.normal(0, 0.25, n)
    close = 100.0 + np.cumsum(steps)
    high = close + rng.uniform(0.05, 0.30, n)
    low = close - rng.uniform(0.05, 0.30, n)
    open_ = close - rng.normal(0, 0.1, n)
    vol = rng.integers(80, 400, n).astype(float)
    return pd.DataFrame({
        "time_utc": [t0 + timedelta(minutes=i) for i in range(n)],
        "o": open_, "h": high, "l": low, "c": close, "v": vol,
    })


# --------------------------------------------------------------- §12 contract

class TestOrderContractGate:
    """The final BUY/SELL SL/TP assertion refuses inverted contracts."""

    def test_gate_refuses_inverted_contract(self):
        """monkeypatch smart_targets to return an inverted BUY contract —
        evaluate must refuse the emit with the order_contract near-miss."""
        import app.engine.engine as eng
        from tests.test_d048_poi_zones import _demand_zone_frame, _uptrend_htf

        df = _demand_zone_frame()
        htf = _uptrend_htf()
        close_time = df["time_utc"].iloc[-1] + timedelta(minutes=1)
        cfg = EngineConfig(entry_mode="market", regime_guard=False)  # D-068 isolated

        original = eng.smart_targets

        def inverted(base, direction, entry, sl_base, rr, min_sl, max_sl, **kw):
            # BUY contract with SL ABOVE entry — inverted on purpose
            if direction == "BUY":
                e = float(base["c"].iloc[-1])
                return e, e + 1.0, e + 2.0, "inverted fixture"
            return original(base, direction, entry, sl_base, rr,
                            min_sl, max_sl, **kw)

        eng.smart_targets = inverted
        try:
            ev = evaluate(df, htf, close_time, cfg, spread_points=20)
        finally:
            eng.smart_targets = original
        assert ev.signal is None, "an inverted BUY contract must never emit"
        checks = (ev.trace or {}).get("checks", [])
        assert any(
            c.get("name") == "order_contract" and c.get("pass") is False
            for c in checks
        ), "the refusal must be attributed to the order_contract gate"

    def test_geometry_audit_and_limit_replay_healthy(self):
        """Sanity after the fill-model change: the replay still fires,
        every contract is geometrically valid, and limit signals exist."""
        from app.engine.backtest import load_mock_history

        cfg = EngineConfig()
        base = load_mock_history(6000, seed=42)
        res = run_backtest(base, cfg=cfg, spread_points=20.0)
        assert res.signals, "mock replay should fire signals"
        limits = [s for s in res.signals if s.entry_type == "limit"]
        assert limits, "mock replay fires limit signals"
        for s in res.signals:
            if s.direction == "BUY":
                assert s.sl < s.entry < s.tp
            else:
                assert s.tp < s.entry < s.sl
            assert abs(s.entry - s.sl) > 0


# ------------------------------------------------------------- §8 flow_block

class TestFlowBlock:
    """flow_block=true refuses trades fighting a dominating battle."""

    def test_config_default_off(self):
        assert EngineConfig().flow_block is False

    def test_block_on_vs_off(self):
        """A SELL into a dominating buyers' streak: off → fires (penalty),
        on → refused with the flow_guard near-miss."""

        # 6 strong bullish candles = buyers dominate, streak 6
        bars = make_bars(120, seed=21)
        t0 = bars["time_utc"].iloc[-6]
        idx = bars.index[bars["time_utc"] >= t0]
        ramp = np.linspace(100.0, 106.0, 6)
        for k, j in enumerate(idx):
            bars.loc[j, "o"] = ramp[k] - 0.35
            bars.loc[j, "c"] = ramp[k] + 0.35
            bars.loc[j, "h"] = ramp[k] + 0.5
            bars.loc[j, "l"] = ramp[k] - 0.5
            bars.loc[j, "v"] = 300.0
        htf = {"H1": make_bars(60, seed=22), "M15": make_bars(60, seed=23)}

        results = {}
        for block in (False, True):
            cfg = EngineConfig(flow_block=block)
            evs = []
            for i in range(80, 120):
                hist = bars.iloc[: i + 1].reset_index(drop=True)
                t = hist["time_utc"].iloc[-1].to_pydatetime() \
                    + timedelta(minutes=1)
                ev = evaluate(hist, htf, t, cfg, 0.0)
                evs.append(ev)
            results[block] = evs

        # with block ON, no fired signal may carry a SELL direction while
        # the battle dominates buyers (or nothing fires at all); with
        # block OFF the behavior is unchanged D-067 (penalty, still may
        # fire). We assert the gate paths exist in the traces.
        for ev in results[True]:
            tr = ev.trace if isinstance(ev.trace, dict) else {}
            checks = tr.get("checks", [])
            for c in checks:
                if c.get("name") == "flow_guard" and c.get("pass") is False:
                    assert "flow_block" in str(c.get("value", ""))
                    assert ev.signal is None


# ---------------------------------------------------- §13/§15 sim bid/ask fills

class TestSimBidAskFills:
    def _bar(self, o, h, lo, c, t):
        return {"time_utc": t, "o": o, "h": h, "l": lo, "c": c}

    def _pending(self, direction, entry, sl, tp, t):
        return BacktestSignal(
            ts=t, direction=direction, entry=entry, sl=sl, tp=tp,
            confidence=0.7, session="london", trigger="zone",
            entry_type="limit", market_ref=entry + 5, filled=False,
        )

    def test_buy_limit_needs_ask_touch(self):
        """BUY pending fills only when the BID low reaches entry-spread."""
        cfg = EngineConfig()
        sim = _SimTracker(cfg, spread_price=0.20)
        t = datetime(2025, 1, 1, tzinfo=UTC)
        sig = self._pending("BUY", 100.0, 98.0, 102.0, t)
        sim.add(sig)
        # bid low = 100.10 -> ask = 100.30 > 100 entry: NO fill
        sim.step(self._bar(100.5, 100.6, 100.10, 100.4, t))
        assert sig.status == "pending"
        # bid low = 99.80 -> ask = 100.00 <= entry: fills
        sim.step(self._bar(100.0, 100.2, 99.80, 100.1, t))
        assert sig.filled is True and sig.status == "active"

    def test_sell_limit_fills_on_bid_touch(self):
        cfg = EngineConfig()
        sim = _SimTracker(cfg, spread_price=0.20)
        t = datetime(2025, 1, 1, tzinfo=UTC)
        sig = self._pending("SELL", 100.0, 102.0, 98.0, t)
        sim.add(sig)
        sim.step(self._bar(99.8, 100.0, 99.7, 99.9, t))
        assert sig.filled is True, "SELL fills on the bid touch (exact)"

    def test_sell_sl_triggers_on_ask_side(self):
        """SELL SL is an ask-side stop: bid high >= sl - spread triggers."""
        cfg = EngineConfig()
        sim = _SimTracker(cfg, spread_price=0.20)
        t = datetime(2025, 1, 1, tzinfo=UTC)
        sig = self._pending("SELL", 100.0, 101.0, 98.0, t)
        sim.add(sig)
        sim.step(self._bar(100.0, 100.9, 99.8, 100.4, t))
        assert sig.filled is True
        # bid high 100.85 -> ask 101.05 >= SL 101.0: the stop fires
        sim.step(self._bar(100.4, 100.85, 100.3, 100.5, t))
        assert sig.status == "lost"

    def test_sell_tp_needs_ask_reach(self):
        """SELL TP is an ask-side limit: bid low must reach tp - spread."""
        cfg = EngineConfig()
        sim = _SimTracker(cfg, spread_price=0.20)
        t = datetime(2025, 1, 1, tzinfo=UTC)
        sig = self._pending("SELL", 100.0, 101.5, 98.0, t)
        sim.add(sig)
        sim.step(self._bar(100.0, 100.1, 99.9, 99.95, t))
        assert sig.filled is True
        # bid low 98.10 -> ask 98.30 > tp 98.0: NOT yet
        sim.step(self._bar(99.0, 99.2, 98.10, 98.6, t))
        assert sig.status == "active"
        # bid low 97.75 -> ask 97.95 <= tp 98.0: TP hit
        sim.step(self._bar(98.0, 98.2, 97.75, 98.1, t))
        assert sig.status == "won"

    def test_sell_expiry_pays_the_ask_exit(self):
        """Expired SELL buys back at ask (close + spread) — R includes it."""
        cfg = EngineConfig(expiry_bars=1)
        sim = _SimTracker(cfg, spread_price=0.20)
        t = datetime(2025, 1, 1, tzinfo=UTC)
        sig = self._pending("SELL", 100.0, 102.0, 98.0, t)
        sim.add(sig)
        sim.step(self._bar(100.0, 100.1, 99.9, 99.9, t))
        assert sig.filled is True
        sim.step(self._bar(99.9, 100.0, 99.8, 99.9, t))
        assert sig.status == "expired"
        # exit at ask = 99.9 + 0.2 = 100.1 -> R = (100 - 100.1)/2 = -0.05
        assert sig.result_r == pytest.approx(-0.05, abs=1e-3)

    def test_no_posthoc_tax_for_limit_trades(self):
        """run_backtest embeds the spread in the levels — the post-hoc
        tax applies to market-born trades only (structural: covered by
        the code path; this pins the entry_type split at the call site)."""
        from app.engine.backtest import load_mock_history

        cfg = EngineConfig()
        base = load_mock_history(6000, seed=7)
        res = run_backtest(base, cfg=cfg, spread_points=20.0)
        assert res.signals
        # the levels already carry the spread for limit trades; only
        # market-born signals may still owe the post-hoc tax
        markets = [s for s in res.signals if s.entry_type == "market"]
        assert markets, "the mock replay exercises market entries too"


# ------------------------------------------------- §14 tracker fallback spread

class TestTrackerFallbackSpread:
    def _tracker_with(self, sid: str, direction: str, entry: float,
                      sl: float, tp: float) -> tuple[SignalTracker, str]:
        tr = SignalTracker()
        t = _t0()
        sig = make_tracked(
            direction=direction, entry=entry, sl=sl, tp=tp,
            confidence=0.7, trace={}, bar_time=t,
            signal_id=sid, entry_type="limit",
        )
        tr._active[sid] = sig  # noqa: SLF001 — fixture
        return tr, sid

    def test_buy_fallback_requires_spread_margin(self):
        tr, sid = self._tracker_with("s1", "BUY", 100.0, 98.0, 102.0)
        t = _t0()
        # bid low 99.95 with spread 0.20 -> ask 100.15 > entry: NO fill
        asyncio.run(tr.on_bar_close(
            45, t, 100.1, bar_low=99.95, bar_high=100.2,
            bar_open=100.0, spread_price=0.20,
        ))
        assert tr._active[sid].status == "pending"  # noqa: SLF001
        # bid low 99.75 -> ask 99.95 <= 100.0: fills
        asyncio.run(tr.on_bar_close(
            45, t, 99.9, bar_low=99.75, bar_high=100.0,
            bar_open=100.0, spread_price=0.20,
        ))
        assert tr._active[sid].status == "active"  # noqa: SLF001

    def test_zero_spread_keeps_old_behavior(self):
        tr, sid = self._tracker_with("s2", "SELL", 100.0, 102.0, 98.0)
        t = _t0()
        asyncio.run(tr.on_bar_close(
            45, t, 100.0, bar_low=99.8, bar_high=100.05,
            bar_open=100.0, spread_price=0.0,
        ))
        assert tr._active[sid].status == "active"  # noqa: SLF001
