"""D-049 — sell-bias fix, structure-predicted SL/TP, user trade budget.

User directive: "শুধু মাত্র sell সিগন্যাল দিচ্ছে ... SL ও TP সমান রেশিও
দিচ্ছে কেনো? এটা হিসাব করে প্রেডিকশন করতে হবে, মার্কেট এই প্রাইস লেভেল এ
গেলে SL অথবা TP হিট করবে। ইউজার শুধু ট্রেডিং ব্যালেন্স, PIP, প্রতিদিন
কত গুলো ট্রেড প্লেস হবে অটো এটা কন্ট্রোল করবে।"
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from app.analysis import indicators as ind
from app.analysis.context import smart_targets
from app.engine.bias import direction_bias
from app.engine.config import EngineConfig
from app.engine.engine import evaluate

# ------------------------------------------------------------ frame builders

def _bar(t, o, h, low, c, v=100):
    return [t, o, h, low, c, v]


def _cols():
    return ["time_utc", "o", "h", "l", "c", "v"]


def _frame(rows):
    return pd.DataFrame(rows, columns=_cols())


def _trend_frame(n: int = 80, start: float = 4200.0, step: float = 1.0,
                 t0=None, minutes: int = 60):
    """Monotonic rising (step>0) or falling (step<0) HTF frame."""
    t0 = t0 or datetime(2026, 9, 22, 0, 0, tzinfo=UTC)
    rows = [
        _bar(t0 + timedelta(minutes=minutes * i),
             start + step * i, start + step * i + 1.0,
             start + step * i - 1.0, start + step * i + 0.5, 50)
        for i in range(n)
    ]
    return _frame(rows)


def _zigzag_frame(n: int = 96, start: float = 4200.0, direction: str = "BUY",
                  t0=None, minutes: int = 60):
    """Trending frame WITH real swings (4 with-trend bars + 2 pullback
    bars, repeating) so detect_structure sees HH/HL (or LH/LL) — a pure
    monotonic ramp has zero fractal swings and reads 'balanced', and a
    1-bar pullback is invisible to the right=2 fractal window."""
    t0 = t0 or datetime(2026, 9, 22, 0, 0, tzinfo=UTC)
    rows = []
    price = start
    for i in range(n):
        with_trend = (i % 6) < 4
        if direction == "BUY":
            c = price + (2.0 if with_trend else -1.0)
        else:
            c = price - (2.0 if with_trend else -1.0)  # pullback goes UP
        rows.append(_bar(t0 + timedelta(minutes=minutes * i),
                         price, max(price, c) + 0.5, min(price, c) - 0.5,
                         c, 50))
        price = c
    return _frame(rows)


def _bias_htf(direction: str = "BUY") -> dict[str, pd.DataFrame]:
    """HTF set that votes `direction` on every source (zigzag swings so
    the structure votes register)."""
    return {
        "H1": _zigzag_frame(96, direction=direction),
        "M15": _zigzag_frame(96, direction=direction, minutes=15),
        "M5": _zigzag_frame(96, direction=direction, minutes=5),
        "H4": _zigzag_frame(96, direction=direction, minutes=240),
    }


# ------------------------------------------------------------------- bias

class TestDirectionBias:
    def test_all_sources_buy(self):
        cfg = EngineConfig()
        verdict, score, _ = direction_bias(_bias_htf("BUY"), cfg)
        assert verdict == "BUY" and score > 0.8

    def test_all_sources_sell(self):
        cfg = EngineConfig()
        verdict, score, _ = direction_bias(_bias_htf("SELL"), cfg)
        assert verdict == "SELL" and score < -0.8

    def test_conflicting_sources_are_neutral(self):
        """The D-049 fix: H1 EMA up but H4/M15 structure down -> NEUTRAL —
        the old single-EMA gate would have locked the engine into BUY."""
        cfg = EngineConfig()
        htf = _bias_htf("SELL")          # everything bearish ...
        htf["H1"] = _zigzag_frame(96, direction="BUY")  # ... except H1
        # H1 EMA +0.30, H1 structure +0.20 (rising), momentum +0.10
        # H4 -0.20, M15 -0.20 -> total +0.20 -> NEUTRAL
        verdict, score, _ = direction_bias(htf, cfg)
        assert verdict == "NEUTRAL"
        assert -0.30 < score < 0.30

    def test_neutral_bias_allows_zone_both_sides(self):
        """Under NEUTRAL neither side counts as counter-trend — a demand
        BUY trades at the WITH-trend quality gate (the sell-only bias fix).
        """
        cfg = EngineConfig()
        htf = _bias_htf("SELL")
        htf["H1"] = _zigzag_frame(96, direction="BUY")
        from tests.test_d048_poi_zones import _demand_zone_frame

        df = _demand_zone_frame()
        from app.analysis.poi import poi_zones
        from app.engine.zones import detect_zone_retest

        verdict, _, _ = direction_bias(htf, cfg)
        assert verdict == "NEUTRAL"
        sig = detect_zone_retest(df, poi_zones(df), cfg, verdict)
        assert sig is not None and sig.direction == "BUY"
        assert sig.counter_trend is False  # NEUTRAL -> with-trend gate


# ------------------------------------------------------- smart_targets v2

class TestSmartTargetsV2:
    def _quiet(self, price=4300.0, n=120):
        """Flat tape (equal highs/lows everywhere — fine for SL-side
        tests; the TP side is covered by _rising)."""
        t0 = datetime(2026, 9, 22, 9, 0, tzinfo=UTC)
        rows = [
            _bar(t0 + timedelta(minutes=i), price - 0.05, price + 0.15,
                 price - 0.20, price, 100)
            for i in range(n)
        ]
        return _frame(rows)

    def _rising(self, n=120, start=4300.0):
        """Slowly rising tape: no equal-high clusters above, no TPO
        levels above (each bucket is visited < 15 min) — a clean empty
        target ladder for BUY (a flat tape has BSL pools everywhere and
        the geometry check correctly refuses to trade)."""
        t0 = datetime(2026, 9, 22, 9, 0, tzinfo=UTC)
        rows = [
            _bar(t0 + timedelta(minutes=i), start + 0.08 * i,
                 start + 0.08 * i + 0.12, start + 0.08 * i - 0.12,
                 start + 0.08 * i, 100)
            for i in range(n)
        ]
        return _frame(rows)

    def test_tp_predicted_at_opposing_zone(self):
        """TP parks just in front of the nearest opposing supply zone —
        NOT at a fixed rr multiple (the equal-ratio fix)."""
        base = self._rising()
        zones = [{"side": "supply", "lo": 4302.0, "hi": 4302.6,
                  "quality": 0.8, "source": "sd"}]
        entry, sl, tp, note = smart_targets(
            base, "BUY", 4300.0, 4299.2, rr=1.6, min_sl_atr=1.5,
            max_sl_atr=3.5, zones=zones,
        )
        a = ind.atr(base, 14)
        risk = entry - sl
        assert risk > 0
        # parked just BELOW the supply near edge
        assert tp == pytest.approx(4302.0 - 0.10 * a, abs=0.02)
        assert "supply zone" in note
        assert abs(tp - entry) / risk >= 1.2  # min rr honored

    def test_barrier_before_min_rr_skips_trade(self):
        """A supply zone sitting at 0.5R means the market is PREDICTED to
        hit the barrier before profit -> tp=None (geometry rejects it)."""
        base = self._rising()
        zones = [{"side": "supply", "lo": 4300.4, "hi": 4300.9,
                  "quality": 0.8, "source": "sd"}]
        entry, sl, tp, note = smart_targets(
            base, "BUY", 4300.0, 4299.2, rr=1.6, min_sl_atr=1.5,
            max_sl_atr=3.5, zones=zones,
        )
        assert tp is None
        assert "poor geometry" in note

    def test_spread_floor_widens_the_stop(self):
        """D-049 — the stop must survive 2.5x the spread: with a $0.22
        spread the floor is $0.55, NOT the 1.5-ATR floor (~$0.30 on a
        quiet tape)."""
        base = self._quiet()
        _, sl_no_spread, _, _ = smart_targets(
            base, "BUY", 4300.0, 4299.7, rr=1.6, min_sl_atr=1.5,
            max_sl_atr=3.5,
        )
        _, sl_spread, _, _ = smart_targets(
            base, "BUY", 4300.0, 4299.7, rr=1.6, min_sl_atr=1.5,
            max_sl_atr=3.5, spread_price=0.22,
        )
        assert 4300.0 - sl_spread == pytest.approx(0.55, abs=1e-6)
        assert sl_spread < sl_no_spread  # wider stop under a real spread

    def test_tpo_anchor_uses_nearest_level(self):
        """D-049 bug fix — the old code anchored past the FURTHEST strong
        TPO level (min for BUY), ballooning the stop; the NEAREST level
        beyond the SL is the correct anchor."""
        base = self._quiet()
        # SL candidate 4299.2; nearest strong support 4299.0, another at
        # 4298.0 — the anchor must sit just past 4299.0, not 4298.0
        tpo = [
            {"price": 4299.0, "minutes": 40.0, "side": "support",
             "strength": 1.0},
            {"price": 4298.0, "minutes": 30.0, "side": "support",
             "strength": 0.9},
        ]
        a = ind.atr(base, 14)
        _, sl, _, _ = smart_targets(
            base, "BUY", 4300.0, 4299.2, rr=1.6, min_sl_atr=1.5,
            max_sl_atr=3.5, tpo_levels=tpo,
        )
        assert sl == pytest.approx(4299.0 - 0.15 * a, abs=0.02)

    def test_no_targets_falls_back_to_rr(self):
        base = self._rising()
        entry, sl, tp, note = smart_targets(
            base, "BUY", 4300.0, 4299.5, rr=1.6, min_sl_atr=1.5,
            max_sl_atr=3.5,
        )
        risk = entry - sl
        assert tp == pytest.approx(entry + 1.6 * risk, abs=0.02)
        assert "no target" in note


# ------------------------------------------------------- evaluate() wiring

class TestEvaluateD049:
    def _demand_frame(self):
        # the D-048 crafted demand-zone tape (rally to 4305 + gradual
        # overlapping descent — no opposing structure right above entry)
        from tests.test_d048_poi_zones import _demand_zone_frame

        return _demand_zone_frame()

    def test_signal_carries_rr_and_target_note(self):
        df = self._demand_frame()
        htf = _bias_htf("BUY")
        close_time = df["time_utc"].iloc[-1] + timedelta(minutes=1)
        ev = evaluate(
            df, htf, close_time,
            EngineConfig(entry_mode="market", regime_guard=False),  # D-068 isolated
            spread_points=20,
        )
        assert ev.signal is not None, ev.trace["checks"]
        assert "rr" in ev.signal and ev.signal["rr"] >= 1.2
        assert isinstance(ev.signal["target_note"], str) and ev.signal["target_note"]
        # trace tells the story of the PREDICTED target
        names = {c["name"]: c for c in ev.signal["trace"]["checks"]}
        assert names["targets"]["pass"] is True
        assert "TP predicted" in names["targets"]["value"]

    def test_buy_signal_in_sell_htf_via_zone(self):
        """THE headline D-049 test: with every higher timeframe BEARISH, a
        quality demand-zone retest still produces a BUY signal (the old
        engine could only SELL here — the user's complaint)."""
        df = self._demand_frame()
        htf = _bias_htf("SELL")  # H4/H1/M15/M5 all falling
        close_time = df["time_utc"].iloc[-1] + timedelta(minutes=1)
        cfg = EngineConfig(counter_trend_quality=0.58, entry_mode="market", regime_guard=False)  # D-068 isolated
        ev = evaluate(df, htf, close_time, cfg, spread_points=20)
        assert ev.signal is not None, ev.trace["checks"]
        assert ev.signal["direction"] == "BUY"
        assert ev.signal["trigger"] == "zone"
        trace_names = [c["name"] for c in ev.signal["trace"]["checks"]]
        assert "direction_bias" in trace_names

    def test_neutral_bias_blocks_trend_triggers_allows_zone(self):
        """NEUTRAL: sfp/pullback need a direction, zone-only fires."""
        df = self._demand_frame()
        htf = _bias_htf("SELL")
        htf["H1"] = _trend_frame(80, step=1.0)  # conflicts -> NEUTRAL
        close_time = df["time_utc"].iloc[-1] + timedelta(minutes=1)
        cfg = EngineConfig(entry_mode="market", regime_guard=False)  # D-068 isolated
        ev = evaluate(df, htf, close_time, cfg, spread_points=20)
        assert ev.signal is not None
        assert ev.signal["trigger"] == "zone"  # only the zone can fire


# ------------------------------------------------------------ daily budget

class TestDailyTradeBudget:
    def _executor(self, max_trades: int = 2):
        from app.engine.executor import OrderExecutor, TradeRepo

        class _Src:
            async def get_positions(self):
                return []

            def account_info(self):
                return {"equity": 1000.0}

            async def symbol_info(self, symbol):
                return None

            async def place_order(self, order):
                from app.mt5.base import OrderResult

                return OrderResult(
                    ok=True, ticket=1, price=100.0, retcode=0, comment="filled"
                )

        cfg = EngineConfig(max_trades_per_day=max_trades)
        return OrderExecutor(_Src(), cfg, TradeRepo(None)), cfg

    @pytest.mark.asyncio
    async def test_budget_blocks_after_limit(self):
        ex, cfg = self._executor(max_trades=2)
        ex.arm(True)
        sig = {"id": "s1", "direction": "BUY", "entry": 100.0,
               "sl": 99.0, "tp": 101.0, "spread_points": 10}
        r1 = await ex.execute_signal(sig, "XAUUSD", 0.01)
        r2 = await ex.execute_signal({**sig, "id": "s2"}, "XAUUSD", 0.01)
        assert r1 is not None and r2 is not None
        r3 = await ex.execute_signal({**sig, "id": "s3"}, "XAUUSD", 0.01)
        assert r3 is None
        # D-052 — the completed budget turns the AI OFF automatically
        # (user directive: any completed limit -> button auto-off)
        assert "daily signal limit" in (ex.last_skip_reason or "")
        assert ex.auto_trade is False

    @pytest.mark.asyncio
    async def test_skips_do_not_consume_budget(self):
        """Disarmed skips never count; the first real order still fires."""
        ex, cfg = self._executor(max_trades=1)
        ex.arm(False)
        await ex.execute_signal(
            {"id": "x", "direction": "BUY", "entry": 100.0, "sl": 99.0,
             "tp": 101.0, "spread_points": 10}, "XAUUSD", 0.01,
        )
        ex.arm(True)
        r = await ex.execute_signal(
            {"id": "y", "direction": "BUY", "entry": 100.0, "sl": 99.0,
             "tp": 101.0, "spread_points": 10}, "XAUUSD", 0.01,
        )
        assert r is not None
