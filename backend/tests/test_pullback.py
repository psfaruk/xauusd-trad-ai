"""D-041 pullback trigger + config upgrade tests."""

from __future__ import annotations

import pandas as pd
import pytest

from app.engine.config import EngineConfig, upgrade_legacy_payload
from app.engine.sfp import (
    PullbackSignal,
    build_levels_pullback,
    detect_pullback,
    pullback_quality,
)
from app.engine.trace import Trace

CFG = EngineConfig()


def m1_frame(closes, lows=None, highs=None):
    """M1 OHLC frame; default bar range = +/- 0.3 around the close."""
    rows = []
    base = pd.Timestamp("2025-01-06 10:00", tz="UTC")
    for i, c in enumerate(closes):
        low = lows[i] if lows else c - 0.3
        high = highs[i] if highs else c + 0.3
        rows.append([base + pd.Timedelta(minutes=i), c, high, low, c, 10])
    return pd.DataFrame(rows, columns=["time_utc", "o", "h", "l", "c", "v"])


class TestDetectPullbackBuy:
    def _buy_setup(self):
        """Rising tape (EMA20 rising), a pullback bar touching EMA, then a
        rejection candle: low at the EMA, close in the upper half, fat wick."""
        # steady uptrend so EMA20 rises cleanly
        closes = [100.0 + 0.25 * i for i in range(30)]
        # pullback bars: dip toward the EMA
        closes += [107.0, 106.6, 106.3]  # 3-bar pullback
        df = m1_frame(closes)
        # trigger bar: dips well through the EMA (which lags the trend) and
        # rejects hard — low 105.0, close near the high, fat lower wick
        i = len(df) - 1
        df.iloc[i, df.columns.get_indexer(["o", "h", "l", "c"])] = [
            106.3, 107.4, 104.7, 107.1,
        ]
        return df

    def test_rejection_detected(self):
        trace = Trace()
        trace.direction = "BUY"
        sig = detect_pullback(self._buy_setup(), CFG, trace)
        assert sig is not None, [c.value for c in trace.checks]
        assert sig.direction == "BUY"
        assert sig.entry == pytest.approx(107.1)
        check = [c for c in trace.checks if c.name == "pullback"][0]
        assert check.passed

    def test_no_pullback_without_dip(self):
        """Strong trend bar that never touches the EMA is not a pullback."""
        closes = [100.0 + 0.3 * i for i in range(34)]  # runaway trend
        df = m1_frame(closes)
        trace = Trace()
        trace.direction = "BUY"
        assert detect_pullback(df, CFG, trace) is None

    def test_doji_rejected_by_range_filter(self):
        """A tiny-range trigger bar is noise — the doji filter must refuse."""
        df = self._buy_setup()
        i = len(df) - 1
        df.iloc[i, df.columns.get_indexer(["o", "h", "l", "c"])] = [
            106.6, 106.65, 106.55, 106.6,
        ]
        trace = Trace()
        trace.direction = "BUY"
        assert detect_pullback(df, CFG, trace) is None


class TestDetectPullbackSell:
    def test_mirror_rejection_detected(self):
        closes = [130.0 - 0.25 * i for i in range(30)]
        closes += [123.0, 123.4, 123.7]  # pullback UP in a downtrend
        df = m1_frame(closes)
        i = len(df) - 1
        df.iloc[i, df.columns.get_indexer(["o", "h", "l", "c"])] = [
            123.7, 125.3, 122.6, 122.9,
        ]  # high pokes far above the EMA, closes near the low
        trace = Trace()
        trace.direction = "SELL"
        sig = detect_pullback(df, CFG, trace)
        assert sig is not None, [c.value for c in trace.checks]
        assert sig.direction == "SELL"


class TestPullbackLevels:
    def test_buy_levels_floored(self):
        sig = PullbackSignal(
            direction="BUY", entry=100.0, anchor=99.8,
            swing_extreme=99.9, wick_ratio=0.6, body_pos=0.8, atr=0.5,
        )
        entry, sl, tp = build_levels_pullback(sig, CFG)
        # swing (99.9 - 0.2*0.5 = 99.8) vs floor (100 - 1.5*0.5 = 99.25)
        assert sl == pytest.approx(99.25)
        risk = entry - sl
        assert risk >= CFG.min_sl_atr * 0.5 - 1e-9
        assert tp == pytest.approx(entry + CFG.rr * risk)

    def test_quality_bounds(self):
        strong = PullbackSignal("BUY", 100, 99, 99, 0.9, 0.95, 0.5)
        weak = PullbackSignal("BUY", 100, 99, 99, 0.0, 0.0, 0.5)
        assert pullback_quality(strong) == pytest.approx(0.6 * 0.9 + 0.4 * 0.95)
        assert pullback_quality(weak) == pytest.approx(0.0)


class TestLegacyConfigUpgrade:
    """D-041 — a pre-M1 engine_config row is upgraded automatically."""

    LEGACY = {
        "timeframe": "M15", "trend_tf": "H1",
        "ema_fast": 20, "ema_slow": 50, "trend_ema": 50,
        "rsi_period": 14, "rsi_buy_min": 40.0, "rsi_buy_max": 65.0,
        "rsi_sell_min": 35.0, "rsi_sell_max": 60.0,
        "atr_period": 14, "min_atr": 0.8,
        "sfp_lookback": 20, "sfp_wick_atr_ratio": 0.3,
        "sl_buffer_atr": 0.2, "rr": 2.0, "expiry_bars": 20, "cooldown_bars": 3,
        "sessions": [{"name": "london", "utc": [7, 16]}],
        "news_blackout_min": 30, "max_spread_points": 35,
        "risk_mode": "percent", "risk_percent": 1.0, "fixed_lot": 0.02,
        "max_positions": 1, "daily_max_loss_pct": 3.0, "magic": 234000,
    }

    def test_upgrade_changes_strategy_keeps_risk(self):
        up, changed = upgrade_legacy_payload(dict(self.LEGACY))
        assert changed is True
        assert up["timeframe"] == "M1"
        assert up["confirm_tfs"] == ["M5", "M15"]
        assert up["rr"] == 1.1
        assert up["min_sl_atr"] == 1.5
        assert up["min_confluence"] == 2          # D-048: honest smc indexing gate
        assert up["max_positions"] == 3           # D-042 multi-entry default
        # user-customized risk/session values survive
        assert up["risk_percent"] == 1.0
        assert up["fixed_lot"] == 0.02
        assert up["sessions"] == [{"name": "london", "utc": [7, 16]}]
        cfg = EngineConfig.model_validate(up)
        assert cfg.timeframe == "M1" and cfg.min_tf_agree == 1
        assert cfg.bias_tfs == ["H4"] and cfg.smc_enabled is True

    def test_modern_payload_untouched(self):
        modern = EngineConfig().model_dump()
        up, changed = upgrade_legacy_payload(modern)
        assert changed is False
        assert up == modern

    def test_tf_ordering_validation(self):
        with pytest.raises(ValueError):
            EngineConfig(timeframe="M15", confirm_tfs=["M5"])  # M5 < M15
        with pytest.raises(ValueError):
            EngineConfig(confirm_tfs=["H4"])  # H4 >= trend_tf H1
        with pytest.raises(ValueError):
            EngineConfig(min_tf_agree=3)  # can never be satisfied


class TestConfigRepoJsonbDict:
    """D-043 regression: asyncpg/SQLAlchemy returns jsonb as a dict, but
    ConfigRepo.load used to json.loads() it unconditionally (TypeError ->
    silent fallback to code defaults -> saved settings lost on restart)."""

    class _Result:
        def __init__(self, row):
            self._row = row

        def first(self):
            return self._row

    class _ConnCtx:
        def __init__(self, row):
            self._row = row

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def execute(self, *_a, **_kw):
            return TestConfigRepoJsonbDict._Result(self._row)

    class _Engine:
        def __init__(self, row):
            self._row = row

        def connect(self):
            return TestConfigRepoJsonbDict._ConnCtx(self._row)

    @pytest.mark.asyncio
    async def test_dict_payload_loads(self):
        from app.engine.config import ConfigRepo

        payload = EngineConfig(rr=1.4, min_confluence=5).model_dump()
        repo = ConfigRepo()
        cfg, auto = await repo.load(self._Engine((payload, True)))  # dict row
        assert cfg.rr == 1.4 and cfg.min_confluence == 5
        assert auto is True

    @pytest.mark.asyncio
    async def test_string_payload_still_loads(self):
        import json as _json

        from app.engine.config import ConfigRepo

        payload = _json.dumps(EngineConfig(rr=1.2).model_dump())
        repo = ConfigRepo()
        cfg, _ = await repo.load(self._Engine((payload, False)))  # str row
        assert cfg.rr == 1.2

    @pytest.mark.asyncio
    async def test_garbage_payload_falls_back(self):
        from app.engine.config import ConfigRepo

        repo = ConfigRepo()
        cfg, _ = await repo.load(self._Engine((42, False)))  # nonsense type
        assert cfg == ConfigRepo()._mem_config
