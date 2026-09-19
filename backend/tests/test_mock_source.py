"""MockDataSource behavior (SPEC §8.1, DECISIONS.md D-003/D-007)."""

import pandas as pd
import pytest

from app.mt5.base import Order
from app.mt5.mock_source import MockDataSource, SweepScenario, VolRegime
from tests.conftest import START, atr14, make_mock


async def test_same_seed_same_candles() -> None:
    a, b = make_mock(7), make_mock(7)
    a.advance_minutes(600)
    b.advance_minutes(600)
    fa = await a.get_rates("XAUUSDm", "M15", 50)
    fb = await b.get_rates("XAUUSDm", "M15", 50)
    pd.testing.assert_frame_equal(fa, fb)


async def test_different_seed_differs() -> None:
    a, b = make_mock(7), make_mock(8)
    a.advance_minutes(600)
    b.advance_minutes(600)
    fa = await a.get_rates("XAUUSDm", "M15", 50)
    fb = await b.get_rates("XAUUSDm", "M15", 50)
    assert not fa["c"].equals(fb["c"])


async def test_rates_shape_and_columns() -> None:
    m = make_mock(3)
    m.advance_minutes(300)
    df = await m.get_rates("XAUUSDm", "M15", 10)
    assert list(df.columns) == ["time_utc", "o", "h", "l", "c", "v"]
    assert len(df) == 10
    assert df["time_utc"].is_monotonic_increasing
    assert (df["h"] >= df[["o", "c"]].max(axis=1)).all()
    assert (df["l"] <= df[["o", "c"]].min(axis=1)).all()


async def test_get_rates_closed_bars_only() -> None:
    """D-003: the forming candle is never returned."""
    m = make_mock(5)
    # The mock ships 30 days of closed history before the anchor; the bucket
    # [anchor, anchor+15) is the forming one.
    df = await m.get_rates("XAUUSDm", "M15", 3)
    assert len(df) == 3
    assert df["time_utc"].iloc[-1] == START - pd.Timedelta(minutes=15)

    m.advance_minutes(7)  # mid-bucket: still forming
    df2 = await m.get_rates("XAUUSDm", "M15", 3)
    assert df2["time_utc"].iloc[-1] == START - pd.Timedelta(minutes=15)

    m.advance_minutes(8)  # exactly anchor+15: the bucket closed
    df3 = await m.get_rates("XAUUSDm", "M15", 3)
    assert df3["time_utc"].iloc[-1] == START
    assert len(df3) == 3


async def test_m15_is_aggregation_of_m1() -> None:
    m = make_mock(3)
    m.advance_minutes(1440)  # one full day of closed M1 bars
    m1 = await m.get_rates("XAUUSDm", "M1", 150)
    m15 = await m.get_rates("XAUUSDm", "M15", 2)
    assert len(m1) == 150 and len(m15) == 2
    for _, row in m15.iterrows():
        end = row["time_utc"] + pd.Timedelta(minutes=15)
        window = m1[(m1["time_utc"] >= row["time_utc"]) & (m1["time_utc"] < end)]
        assert len(window) == 15
        assert row["o"] == window["o"].iloc[0]
        assert row["h"] == window["h"].max()
        assert row["l"] == window["l"].min()
        assert row["c"] == window["c"].iloc[-1]
        assert row["v"] == window["v"].sum()


async def test_volatility_regimes_scale_ranges() -> None:
    lo = make_mock(11, base_vol=0.05)
    hi = make_mock(11, base_vol=1.0)
    lo.advance_minutes(600)
    hi.advance_minutes(600)
    lo_df = await lo.get_rates("XAUUSDm", "M15", 40)
    hi_df = await hi.get_rates("XAUUSDm", "M15", 40)
    assert hi_df["h"].sub(hi_df["l"]).mean() > 3 * lo_df["h"].sub(lo_df["l"]).mean()


async def test_regime_change_mid_history() -> None:
    # 30 days of history * 1440 minutes: switch sigma after 29 days (= 1 day
    # before the anchor).
    switch = 29 * 1440
    m = MockDataSource(
        seed=21,
        start_time=START,
        time_scale=0.0,
        vol_regimes=[VolRegime(0, 0.05), VolRegime(switch, 1.0)],
    )
    m.advance_minutes(1440)  # now = anchor + 1 day (regime active)
    df = await m.get_rates("XAUUSDm", "M15", 240)  # ~60h of closed buckets
    early = df[df["time_utc"] < START - pd.Timedelta(days=1)]
    late = df[df["time_utc"] >= START - pd.Timedelta(days=1)]
    assert len(early) >= 40 and len(late) >= 40
    assert late["h"].sub(late["l"]).mean() > 5 * early["h"].sub(early["l"]).mean()


async def test_tick_spread_and_time() -> None:
    m = make_mock(2, spread=0.30)
    m.advance_minutes(10.5)  # mid-minute
    tick = await m.get_tick("XAUUSDm")
    assert tick.ask - tick.bid == pytest.approx(0.30)
    assert tick.bid > 0
    assert tick.time == m._vnow()  # noqa: SLF001


async def test_subscribe_ticks_yields() -> None:
    m = make_mock(2, tick_interval=0.01)
    m.advance_minutes(5)
    got = []
    async for tick in m.subscribe_ticks("XAUUSDm"):
        got.append(tick)
        break  # first tick is enough
    assert got and got[0].ask > got[0].bid


async def test_connect_orders_positions_lifecycle() -> None:
    m = make_mock(2)
    assert m.discover_symbols() == ["XAUUSDm"]
    assert not await m.is_connected()
    info = await m.connect({"server": "Exness-MT5Trial", "login": "12345"})
    assert info["server"] == "Exness-MT5Trial"
    assert info["balance"] == 10_000.0
    assert await m.is_connected()

    result = await m.place_order(
        Order(symbol="XAUUSDm", side="BUY", volume=0.10, sl=2640.0, tp=2680.0, magic=234000)
    )
    assert result.ok and result.ticket and result.retcode == 10009
    positions = await m.get_positions()
    assert len(positions) == 1
    assert positions[0].side == "BUY" and positions[0].volume == 0.10

    await m.disconnect()
    assert not await m.is_connected()


async def test_unknown_timeframe_rejected() -> None:
    m = make_mock(2)
    m.advance_minutes(60)
    with pytest.raises(ValueError):
        await m.get_rates("XAUUSDm", "M3", 10)


async def test_sweep_injection_buy() -> None:
    m = make_mock(9)
    m.advance_minutes(3 * 1440)
    rec = m.inject_sweep(SweepScenario(direction="BUY", tf="M15", wick_atr_mult=0.5))
    m.advance_minutes(15)  # close the crafted bucket

    df = await m.get_rates("XAUUSDm", "M15", 25)
    last = df.iloc[-1]
    assert last["time_utc"] == rec.bucket_time
    prior = df.iloc[:-1].tail(20)
    swept_level = prior["l"].min()

    assert last["l"] < swept_level, "wick must sweep below the 20-bar low"
    assert last["c"] > swept_level, "close must pull back above the swept level"
    lower_wick = min(last["o"], last["c"]) - last["l"]
    assert lower_wick >= 0.3 * atr14(prior), "wick must exceed 0.3*ATR (SPEC 8.2)"
    # Crafted candle values are exactly what the scenario planned.
    assert last["l"] == pytest.approx(rec.candle["l"])
    assert last["c"] == pytest.approx(rec.candle["c"])

    # Cross-TF consistency: M1 bars inside the bucket aggregate to the same low.
    m1 = await m.get_rates("XAUUSDm", "M1", 15)
    assert m1["l"].min() == pytest.approx(last["l"])
    assert m1["o"].iloc[0] == pytest.approx(last["o"])
    assert m1["c"].iloc[-1] == pytest.approx(last["c"])


async def test_sweep_injection_sell() -> None:
    m = make_mock(12)
    m.advance_minutes(2 * 1440)
    rec = m.inject_sweep(SweepScenario(direction="SELL", tf="M15", wick_atr_mult=0.5))
    m.advance_minutes(15)

    df = await m.get_rates("XAUUSDm", "M15", 25)
    last = df.iloc[-1]
    prior = df.iloc[:-1].tail(20)
    swept_level = prior["h"].max()

    assert last["h"] > swept_level
    assert last["c"] < swept_level
    upper_wick = last["h"] - max(last["o"], last["c"])
    assert upper_wick >= 0.3 * atr14(prior)
    assert rec.candle["h"] == pytest.approx(last["h"])


def test_scenario_validation() -> None:
    with pytest.raises(ValueError):
        SweepScenario(direction="HOLD")
    with pytest.raises(ValueError):
        SweepScenario(direction="BUY", offset_bars=0)  # must target an open bucket
    with pytest.raises(ValueError):
        SweepScenario(direction="BUY", tf="M17")


def test_injection_targets_open_bucket_only() -> None:
    """Overriding an already-closed/generated bucket is impossible by design."""
    m = make_mock(9)
    m.advance_minutes(3 * 1440)
    df_before = m._rates_sync("M15", 3)  # noqa: SLF001
    m.inject_sweep(SweepScenario(direction="BUY", tf="M15", offset_bars=1))
    m.advance_minutes(15)
    df_after = m._rates_sync("M15", 3)  # noqa: SLF001
    # Buckets [B-1, B] were closed before injection and must be untouched;
    # only the new bucket B+1 (the crafted one) differs.
    before = df_before.iloc[-2:].reset_index(drop=True)
    after = df_after.iloc[:-1].reset_index(drop=True)
    pd.testing.assert_frame_equal(before, after)
    # The crafted bucket is indeed new.
    assert df_after["time_utc"].iloc[-1] == df_before["time_utc"].iloc[-1] + pd.Timedelta(
        minutes=15
    )
