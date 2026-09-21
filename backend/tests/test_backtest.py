"""Backtest module tests — determinism, stats math, sim tracker, resample."""

from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd
import pytest

from app.engine.backtest import (
    BacktestResult,
    BacktestSignal,
    _SimTracker,
    inject_sweep_series,
    load_mock_history,
    resample_ohlc,
    run_backtest,
    simulate_risk,
)
from app.engine.config import EngineConfig


def _m15(n=300, start="2025-01-06 07:00"):
    base = pd.Timestamp(start, tz="UTC")
    rows = []
    price = 100.0
    for i in range(n):
        c = price + (0.3 if i % 3 else -0.15)
        rows.append(
            [base + pd.Timedelta(minutes=15 * i), price, max(price, c) + 0.2,
             min(price, c) - 0.2, c, 10]
        )
        price = c
    return pd.DataFrame(rows, columns=["time_utc", "o", "h", "l", "c", "v"])


class TestResample:
    def test_m15_to_h1(self):
        df = _m15(8)  # exactly 2 hours
        h1 = resample_ohlc(df, 60, src_min=15)
        assert len(h1) == 2
        first = h1.iloc[0]
        assert first["o"] == df.iloc[0]["o"]
        assert first["c"] == df.iloc[3]["c"]
        assert first["h"] == df.iloc[0:4]["h"].max()
        assert first["l"] == df.iloc[0:4]["l"].min()
        assert first["v"] == 40

    def test_drops_incomplete_bucket(self):
        df = _m15(6)  # 1.5 hours -> only 1 full H1 bucket
        h1 = resample_ohlc(df, 60, src_min=15)
        assert len(h1) == 1


class TestSimTracker:
    def _sig(self):
        return BacktestSignal(
            ts=datetime(2025, 1, 6, 7, tzinfo=UTC), direction="BUY",
            entry=100.0, sl=99.0, tp=102.0, confidence=0.6, session="london",
        )

    def _bar(self, h, low, c, t="2025-01-06 07:15"):
        return {
            "time_utc": pd.Timestamp(t, tz="UTC"), "o": 100.0,
            "h": h, "l": low, "c": c, "v": 5,
        }

    def test_sl_hit_lost(self):
        tr = _SimTracker(EngineConfig())
        tr.open_sig = self._sig()
        out = tr.step(self._bar(100.5, 98.9, 99.5))
        assert out.status == "lost" and out.result_r == -1.0

    def test_tp_hit_won_2r(self):
        tr = _SimTracker(EngineConfig())
        tr.open_sig = self._sig()
        out = tr.step(self._bar(102.3, 100.1, 102.0))
        assert out.status == "won"
        assert out.result_r == pytest.approx(2.0)

    def test_both_touched_sl_first_pessimistic(self):
        tr = _SimTracker(EngineConfig())
        tr.open_sig = self._sig()
        out = tr.step(self._bar(102.5, 98.5, 101.0))  # hits both
        assert out.status == "lost"

    def test_expiry_r_math(self):
        cfg = EngineConfig(expiry_bars=3)
        tr = _SimTracker(cfg)
        sig = self._sig()
        tr.open_sig = sig
        out = None
        for i in range(3):
            out = tr.step(self._bar(100.4, 99.5, 100.5, t=f"2025-01-06 07:{15*(i+1):02d}"))
        assert out.status == "expired"
        assert out.result_r == pytest.approx(0.5)  # (100.5-100)/1.0

    def test_sell_mirror(self):
        tr = _SimTracker(EngineConfig())
        tr.open_sig = BacktestSignal(
            ts=datetime(2025, 1, 6, 7, tzinfo=UTC), direction="SELL",
            entry=100.0, sl=101.0, tp=98.0, confidence=0.6, session="london",
        )
        out = tr.step(self._bar(101.2, 99.9, 100.8))  # ask-side SL
        assert out.status == "lost"
        tr2 = _SimTracker(EngineConfig())
        s2 = BacktestSignal(
            ts=datetime(2025, 1, 6, 7, tzinfo=UTC), direction="SELL",
            entry=100.0, sl=101.0, tp=98.0, confidence=0.6, session="london",
        )
        tr2.open_sig = s2
        out2 = tr2.step(self._bar(99.5, 97.9, 98.2))  # TP
        assert out2.status == "won"
        assert out2.result_r == pytest.approx(2.0)


class TestRunBacktest:
    def test_deterministic(self):
        df = load_mock_history(5000, seed=42)
        r1 = run_backtest(df)
        r2 = run_backtest(df.copy())
        s1, s2 = r1.stats(), r2.stats()
        assert s1 == s2
        assert [s.ts for s in r1.signals] == [s.ts for s in r2.signals]

    def test_injected_series_produces_signals(self):
        df = load_mock_history(7000, seed=42)
        df = inject_sweep_series(df, every_bars=200)
        res = run_backtest(df)
        assert res.stats()["total_signals"] > 0
        # every signal carries sane levels
        for s in res.signals:
            if s.direction == "BUY":
                assert s.sl < s.entry < s.tp
            else:
                assert s.sl > s.entry > s.tp
            assert 0 < s.confidence <= 1
            assert s.session in ("london", "newyork", "off", "none") or s.session

    def test_no_lookahead_h1_context(self):
        """A signal's H1 trend must only use bars closed BEFORE the sweep."""
        df = inject_sweep_series(load_mock_history(7000, seed=42), every_bars=150)
        res = run_backtest(df)
        from app.engine.engine import closed_h1_asof

        h1 = resample_ohlc(df, 60, src_min=15)
        for s in res.signals[:5]:
            asof = closed_h1_asof(h1, s.ts + pd.Timedelta(minutes=1))
            assert len(asof) > 0
            # the last H1 bar closed at/before the sweep bar's close
            last_h1_close = (asof["time_utc"] + pd.Timedelta(hours=1)).iloc[-1]
            assert last_h1_close <= s.ts + pd.Timedelta(minutes=1)


class TestStats:
    def _result(self, statuses_rs):
        sigs = [
            BacktestSignal(
                ts=datetime(2025, 1, 6, 10 + i, tzinfo=UTC), direction="BUY",
                entry=100, sl=99, tp=102, confidence=0.5, session="london",
                status=s, result_r=r,
            )
            for i, (s, r) in enumerate(statuses_rs)
        ]
        res = BacktestResult(
            cfg=EngineConfig(), bars_tested=100,
            date_from=datetime(2025, 1, 6, tzinfo=UTC),
            date_to=datetime(2025, 1, 10, tzinfo=UTC),
            signals=sigs,
        )
        return res.stats()

    def test_stats_math(self):
        s = self._result([("won", 2.0), ("won", 2.0), ("lost", -1.0), ("expired", 0.5)])
        assert s["total_signals"] == 4
        assert s["won"] == 2 and s["lost"] == 1 and s["expired"] == 1
        assert s["win_rate"] == pytest.approx(2 / 3, abs=1e-4)
        assert s["total_r"] == pytest.approx(3.5)
        assert s["expectancy"] == pytest.approx(3.5 / 4, abs=1e-4)
        assert s["profit_factor"] == pytest.approx(4.5, abs=1e-4)  # gains 4.5 / pains 1.0
        assert s["max_drawdown_r"] == pytest.approx(1.0)  # -1 dip after two wins

    def test_empty_stats(self):
        s = self._result([])
        assert s["total_signals"] == 0
        assert s["win_rate"] is None and s["profit_factor"] is None


# ------------------------------------------------------- Phase 4 risk sim


class TestRiskSimulation:
    """§9 executor simulation — the sizing/kill math verified end-to-end."""

    @staticmethod
    def _result(statuses_rs, risk_percent=0.5):
        sigs = [
            BacktestSignal(
                ts=datetime(2025, 1, 6, 10 + i, tzinfo=UTC), direction="BUY",
                entry=100.0, sl=98.0, tp=104.0, confidence=0.5, session="london",
                status=s, result_r=r,
            )
            for i, (s, r) in enumerate(statuses_rs)
        ]
        return BacktestResult(
            cfg=EngineConfig().model_copy(update={"risk_percent": risk_percent}),
            bars_tested=100,
            date_from=datetime(2025, 1, 6, tzinfo=UTC),
            date_to=datetime(2025, 1, 10, tzinfo=UTC),
            signals=sigs,
        )

    def test_risk_sim_pnl_matches_manual_calculation(self):
        """SPEC §12 Phase 4 AC: stats numbers match manual calculation."""
        res = self._result([("won", 2.0), ("lost", -1.0)])
        risk = simulate_risk(res, start_equity=10_000.0)
        # manual: equity 10_000, risk 0.5% -> $50; sl_dist 2 -> lots 0.25
        # win: +2R -> +2*2*100*0.25 = +100  -> equity 10_100
        # loss: -1R -> lots 10_100*0.005/(2*100)=0.2525 -> floor 0.25
        #      -1*2*100*0.25 = -50 -> equity 10_050
        assert risk.trades_taken == 2
        first = risk.per_trade[0]
        assert first["lots"] == pytest.approx(0.25)
        assert first["pnl_usd"] == pytest.approx(100.0)
        assert first["equity_after"] == pytest.approx(10_100.0)
        assert risk.final_equity == pytest.approx(10_050.0)
        assert risk.net_pl == pytest.approx(50.0)

    def test_risk_sim_kill_switch_fires_and_skips_rest(self):
        """Daily-loss kill switch verified in simulation (SPEC §12 Phase 4 AC)."""
        # 3 losses of -1R each with wide SL distance so losses compound
        sigs = [
            BacktestSignal(
                ts=datetime(2025, 1, 6, 10 + i, tzinfo=UTC), direction="BUY",
                entry=100.0, sl=90.0, tp=120.0, confidence=0.5, session="london",
                status="lost", result_r=-1.0,
            )
            for i in range(4)
        ]
        cfg = EngineConfig().model_copy(update={"risk_percent": 5.0, "daily_max_loss_pct": 3.0})
        res = BacktestResult(
            cfg=cfg, bars_tested=100,
            date_from=datetime(2025, 1, 6, tzinfo=UTC),
            date_to=datetime(2025, 1, 10, tzinfo=UTC),
            signals=sigs,
        )
        risk = simulate_risk(res, start_equity=10_000.0)
        # each loss: 5% of equity -> -5% -> after loss 1 the drawdown from the
        # peak is already 5% >= 3% -> kill switch on the next signal
        assert risk.kill_switch_events == 1
        assert risk.trades_taken == 1
        assert risk.signals_skipped_after_kill == 3
        assert risk.final_equity == pytest.approx(9_500.0)  # only one -5% trade
        kill = next(t for t in risk.per_trade if t.get("event") == "KILL_SWITCH")
        assert kill["loss_pct"] >= 3.0

    def test_risk_sim_no_trades_when_all_active(self):
        res = self._result([("active", None)])
        risk = simulate_risk(res)
        assert risk.trades_taken == 0
        assert risk.lots_min is None
        assert risk.final_equity == 10_000.0
