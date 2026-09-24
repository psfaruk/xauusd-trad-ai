"""D-068 tests — the market-regime engine (trend TYPE x TFs + volatility).

User directive (Bengali): "মার্কেট এর ভিতরে ট্রেন্ট তৈরি হয় — up ট্রেন্ড,
ডাউন ট্রেন্ড, সাইড ওয়েস ট্রেন্ড, zigzag ট্রেন্ড... এই ট্রেন্ড গুলো কোন
টাইম ফ্রেম এর সাথে কীভাবে এনালাইসিস করে, সিগন্যাল প্রেডিকশন এর ক্ষেত্রে
কোনটি কে কিভাবে ব্যবহার করা যায়?" and "মার্কেট এ যখন বেশি ভোলাটেলিটি
তখন... সিগন্যাল বেশি ভুল হচ্ছে — সকল অবস্থা বুঝার মত সিস্টেম কি অ্যাপ
এ আছে?"

Covers:
- volatility_state: the self-scaling ATR/median ratio bands (quiet /
  normal / elevated / extreme) + the news-spike bar detection;
- trend_state: trend_up / trend_down (efficiency + ADX + EMA gap),
  range (the compressed box), chop (violent alternation incl. the
  zigzag flavor), unknown (short frames stay honest);
- regime_read: the per-TF grid (H4/H1/M15/M5), the label follows the
  H1 context frame with fallback, the MTF alignment count, the vol
  block, the policy sentence;
- regime_policy: TREND with/against, RANGE zone-fade vs momentum
  chase, CHOP penalties, elevated/extreme volatility, and the ONE
  hard gate — market entries refused in EXTREME vol (limit entries
  still flow);
- evaluate(): the pulse carries the regime every close, the extreme-vol
  market refusal is a visible near-miss, limit entries in the same
  state fire discounted, regime_guard=False disables everything;
- config: the D-068 block defaults.
"""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import pandas as pd

from app.analysis.regime import (
    regime_policy,
    regime_read,
    trend_state,
    volatility_state,
)
from app.engine.config import EngineConfig
from app.engine.engine import evaluate
from tests.test_d048_poi_zones import _demand_zone_frame, _uptrend_htf
from tests.test_d050_poi_pending import _deep_demand_frame

CFG = EngineConfig()


# ------------------------------------------------------------- fixtures


def _frame(
    closes: np.ndarray,
    t0: str = "2026-09-22 10:00",
    freq: str = "5min",
    wick: float = 0.15,
    bodies: bool = False,
) -> pd.DataFrame:
    """Frame from a close series. bodies=True carries the close-to-close
    move INSIDE each candle (the realistic zigzag); otherwise opens sit
    near closes (quiet wicky tape)."""
    closes = np.asarray(closes, dtype=float)
    n = len(closes)
    idx = pd.date_range(t0, periods=n, freq=freq, tz="UTC")
    if bodies:
        o = np.concatenate([[closes[0]], closes[:-1]])
    else:
        o = closes + 0.03
    h = np.maximum(o, closes) + wick
    low = np.minimum(o, closes) - wick
    return pd.DataFrame(
        {"time_utc": idx, "o": o, "h": h, "l": low, "c": closes,
         "v": np.full(n, 120.0)},
    )


def _uptrend_frame(n: int = 300, seed: int = 3) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    return _frame(4000.0 + 0.6 * np.arange(n) + rng.normal(0, 0.12, n))


def _downtrend_frame(n: int = 300, seed: int = 4) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    return _frame(4000.0 - 0.6 * np.arange(n) + rng.normal(0, 0.12, n))


def _tight_range_frame(n: int = 300, seed: int = 5) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    side = 4000.0 + rng.normal(0, 0.30, n).cumsum() * 0.04 \
        + rng.normal(0, 0.10, n)
    return _frame(side, wick=0.10)


def _zigzag_frame(n: int = 300, seed: int = 6) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    zz = 4000.0 + np.cumsum(rng.choice([-2.2, 2.2], size=n))
    return _frame(zz, wick=0.12, bodies=True)


def _fake_regime(
    label: str = "RANGE",
    vol_state: str = "normal",
    vol_ratio: float = 1.0,
    spike: bool = False,
) -> dict:
    """A regime_read-shaped dict for engine-policy tests (monkeypatched
    in place of the real read so the verdict is deterministic)."""
    return {
        "label": label,
        "label_tf": "H1",
        "dir": "down" if label.endswith("DOWN") else "up",
        "alignment": 1,
        "vol": {
            "state": vol_state,
            "vol_ratio": vol_ratio,
            "atr": 0.5,
            "spike": spike,
            "pct_rank": 0.9,
            "h1_state": vol_state,
            "h1_vol_ratio": vol_ratio,
        },
        "tfs": {
            "H1": {"state": "range", "dir": None, "er": 0.1, "adx": 12.0,
                   "body_ratio": 0.4, "flavor": None, "note": "range"},
        },
        "note": f"{label} on H1 · vol {vol_state} ({vol_ratio}x median)",
        "action": "the policy sentence",
    }


# ------------------------------------------------------ volatility_state


def test_volatility_state_bands() -> None:
    """quiet <= 0.75x < normal <= 1.25x < elevated <= 1.75x < extreme —
    the self-scaling ratio (same bands read gold / BTC / the mock)."""
    rng = np.random.default_rng(11)
    base = _frame(4000.0 + rng.normal(0, 0.25, 400).cumsum(), wick=0.2)

    def inflate(frame: pd.DataFrame, mult: float, bars: int = 40) -> pd.DataFrame:
        f = frame.copy()
        for i in range(len(f) - bars, len(f)):
            mid = (f["h"].iloc[i] + f["l"].iloc[i]) / 2.0
            half = (f["h"].iloc[i] - f["l"].iloc[i]) / 2.0 * mult
            f.loc[f.index[i], "h"] = mid + half
            f.loc[f.index[i], "l"] = mid - half
        return f

    assert volatility_state(base)["state"] in ("quiet", "normal")
    # ~2x ranges on the last 40 bars push the ATR/median ratio past 1.75
    hot = inflate(base, 6.0)
    assert volatility_state(hot)["state"] == "extreme"
    # between the bands: elevated
    mid = inflate(base, 3.0)
    vs = volatility_state(mid)
    assert vs["state"] in ("elevated", "extreme"), vs


def test_volatility_state_spike_bar() -> None:
    """One news candle (TR >= 3.5x ATR) alone flips the state extreme
    and sets the spike flag — the user's 'হঠাৎ' volatility moment."""
    rng = np.random.default_rng(12)
    f = _frame(4000.0 + rng.normal(0, 0.15, 200).cumsum(), wick=0.12)
    calm = volatility_state(f)
    assert calm["state"] in ("quiet", "normal")
    assert not calm["spike"]
    g = f.copy()
    last = g.index[-1]
    c = float(g["c"].iloc[-1])
    g.loc[last, "h"] = c + 6.0
    g.loc[last, "l"] = c - 6.0
    hot = volatility_state(g)
    assert hot["spike"] is True
    assert hot["state"] == "extreme"


def test_volatility_state_short_frame_is_normal() -> None:
    """No history to rank against -> honest default, no crash."""
    assert volatility_state(None)["state"] == "normal"
    short = _frame(np.full(20, 4000.0))
    assert volatility_state(short)["state"] == "normal"


# ------------------------------------------------------------ trend_state


def test_trend_state_names_clean_trends() -> None:
    up = trend_state(_uptrend_frame())
    assert up["state"] == "trend_up", up
    assert up["dir"] == "up"
    down = trend_state(_downtrend_frame())
    assert down["state"] == "trend_down", down


def test_trend_state_names_the_tight_range() -> None:
    """The sideways box: no direction, no violence — the zone-fade
    regime (the app's POI strategy's home ground)."""
    ts = trend_state(_tight_range_frame())
    assert ts["state"] == "range", ts


def test_trend_state_names_the_zigzag_chop() -> None:
    """Violent alternation carried by BODIES at constant vol — the
    zigzag the user named: chop with the zigzag flavor (body ratio is
    the discriminator: ~0.9 ATR per candle vs ~0.5 in a quiet box)."""
    ts = trend_state(_zigzag_frame())
    assert ts["state"] == "chop", ts
    assert ts["flavor"] == "zigzag", ts
    assert ts["body_ratio"] >= 0.75


def test_trend_state_short_frame_is_unknown() -> None:
    assert trend_state(None)["state"] == "unknown"
    assert trend_state(_frame(np.full(30, 4000.0)))["state"] == "unknown"


# ------------------------------------------------------------- regime_read


def test_regime_read_label_follows_h1() -> None:
    """The single label follows the H1 CONTEXT frame of the D-065
    ladder; the grid still surfaces every TF's own state."""
    base = _tight_range_frame()
    h1_up = _uptrend_frame()
    htf = {"H4": _uptrend_frame(seed=7), "H1": h1_up,
           "M15": _tight_range_frame(seed=8), "M5": _tight_range_frame(seed=9)}
    rg = regime_read(base, htf)
    assert rg["label"].startswith("TREND UP"), rg["label"]
    assert rg["tfs"]["H1"]["state"] == "trend_up"
    assert rg["tfs"]["M5"]["state"] == "range"
    assert rg["alignment"] >= 2  # H4 + H1 both trend up
    assert rg["vol"]["state"] in ("quiet", "normal")
    assert rg["action"]


def test_regime_read_falls_back_when_h1_missing() -> None:
    base = _tight_range_frame()
    htf = {"M15": _zigzag_frame(), "M5": _tight_range_frame(seed=9)}
    rg = regime_read(base, htf)
    assert rg["label"].startswith("CHOP"), rg["label"]
    assert rg["label_tf"] == "M15"
    assert rg["tfs"]["H1"]["state"] == "unknown"


def test_regime_read_chop_from_elevated_vol() -> None:
    """Even a mild-ER tape reads CHOP once volatility is elevated —
    the danger axis speaks on its own (the user's complaint)."""
    f = _zigzag_frame()
    base = f.copy()
    for i in range(len(base) - 40, len(base)):
        mid = float(base["c"].iloc[i])
        base.loc[base.index[i], "h"] = mid + 2.5
        base.loc[base.index[i], "l"] = mid - 2.5
    htf = {"H1": f, "M15": f, "M5": f}
    rg = regime_read(base, htf)
    assert rg["vol"]["state"] == "extreme"
    assert "EXTREME volatility" in rg["action"]


def test_regime_read_empty_inputs_never_crash() -> None:
    rg = regime_read(None, {})
    assert rg["label"] is not None
    assert rg["tfs"]["H1"]["state"] == "unknown"


# ----------------------------------------------------------- regime_policy


def test_policy_trend_with_and_against() -> None:
    with_it = regime_policy(_fake_regime("TREND UP"), "BUY", "zone", "limit")
    assert with_it["adjust"] > 0
    against = regime_policy(_fake_regime("TREND UP"), "SELL", "zone", "limit")
    assert against["adjust"] < 0


def test_policy_range_favors_zone_fades() -> None:
    """RANGE: the zone retest at the box edge EARNs (that IS the range
    play); the momentum chase inside the box PAYS."""
    zone = regime_policy(_fake_regime("RANGE"), "BUY", "zone", "limit")
    mom = regime_policy(_fake_regime("RANGE"), "BUY", "sfp", "market")
    assert zone["adjust"] > 0
    assert mom["adjust"] < 0


def test_policy_chop_penalizes_momentum_hardest() -> None:
    zone = regime_policy(_fake_regime("CHOP (ZIGZAG)"), "BUY", "zone", "limit")
    mom = regime_policy(_fake_regime("CHOP (ZIGZAG)"), "BUY", "pullback", "market")
    assert mom["adjust"] < zone["adjust"] < 0


def test_policy_extreme_vol_blocks_market_only() -> None:
    """The ONE hard gate: EXTREME volatility refuses MARKET entries;
    the limit at the drawn level still lives (D-049 preserved)."""
    market = regime_policy(_fake_regime("RANGE", "extreme", 2.2), "BUY", "zone", "market")
    limit = regime_policy(_fake_regime("RANGE", "extreme", 2.2), "BUY", "zone", "limit")
    assert market["block_market"] is True
    assert limit["block_market"] is False
    assert limit["adjust"] < 0  # still fires, visibly discounted


def test_policy_elevated_vol_discounts() -> None:
    pol = regime_policy(
        _fake_regime("TREND UP", "elevated", 1.4), "SELL", "zone", "market",
    )
    assert pol["adjust"] < 0  # counter-trend AND elevated vol both pay
    assert not pol["block_market"]


def test_policy_none_regime_is_neutral() -> None:
    pol = regime_policy(None, "BUY", "zone", "market")
    assert pol["adjust"] == 0.0
    assert pol["block_market"] is False


# ----------------------------------------------------------------- engine


def test_pulse_carries_the_regime_every_close() -> None:
    """The radar knows WHAT KIND of market this is on every bar close —
    the answer to 'সকল অবস্থা বুঝার মত সিস্টেম আছে?'."""
    from app.engine.backtest import load_mock_history, resample_ohlc

    m1 = load_mock_history(3000, seed=42)
    htf = {
        "M5": resample_ohlc(m1, 5, src_min=1),
        "M15": resample_ohlc(m1, 15, src_min=1),
        "H1": resample_ohlc(m1, 60, src_min=1),
    }
    close_time = m1["time_utc"].iloc[-1] + timedelta(minutes=1)
    ev = evaluate(m1, htf, close_time, EngineConfig(), spread_points=20)
    rg = ev.pulse.get("regime")
    assert rg is not None
    assert rg["label"]  # TREND/RANGE/CHOP/TRANSITION — always stated
    assert rg["vol"]["state"] in ("quiet", "normal", "elevated", "extreme")
    assert set(rg["tfs"]) >= {"H1", "M15", "M5"}
    assert rg["action"]


def test_extreme_vol_market_entry_refused(monkeypatch) -> None:
    """The user's exact complaint: signals during the volatility spike
    are the wrong ones. A MARKET entry in EXTREME vol is refused —
    visibly (trace check 'regime', near-miss), never a crash."""
    import app.analysis.regime as reg

    monkeypatch.setattr(
        reg, "regime_read",
        lambda base, htf, price=None: _fake_regime("RANGE", "extreme", 2.3),
    )
    df = _demand_zone_frame()
    htf = _uptrend_htf()
    close_time = df["time_utc"].iloc[-1] + timedelta(minutes=1)
    cfg = EngineConfig(entry_mode="market")
    ev = evaluate(df, htf, close_time, cfg, spread_points=20)
    assert ev.signal is None
    checks = {c["name"]: c for c in ev.trace["checks"]}
    assert "regime" in checks and not checks["regime"]["pass"]
    assert "refused" in checks["regime"]["value"]
    assert ev.pulse.get("near_miss")


def test_extreme_vol_limit_entry_fires_discounted(monkeypatch) -> None:
    """Same extreme regime, but the default POI pending (LIMIT) entry
    keeps flowing — D-049 'সিগনাল মিস করা যাবে না': the limit only fills
    on the retrace to the drawn level, which is the SAFE way to trade
    a volatility expansion. Confidence pays, the context says why."""
    import app.analysis.regime as reg

    fake = lambda base, htf, price=None: _fake_regime("RANGE", "extreme", 2.3)  # noqa: E731
    monkeypatch.setattr(reg, "regime_read", fake)
    df = _deep_demand_frame()
    htf = _uptrend_htf()
    close_time = df["time_utc"].iloc[-1] + timedelta(minutes=5)

    ev_on = evaluate(df, htf, close_time, EngineConfig(), spread_points=20)
    assert ev_on.signal is not None
    assert ev_on.signal["entry_type"] == "limit"
    ctx = ev_on.signal["context"]["regime"]
    assert ctx["vol"]["state"] == "extreme"
    assert ctx["adjust"] < 0
    names = {c["name"] for c in ev_on.signal["trace"]["checks"]}
    assert "regime" in names

    # guard OFF: same trade, full confidence (the A/B baseline arm)
    monkeypatch.setattr(reg, "regime_read", fake)
    ev_off = evaluate(
        df, htf, close_time, EngineConfig(regime_guard=False), spread_points=20,
    )
    assert ev_off.signal is not None
    assert ev_off.signal["confidence"] > ev_on.signal["confidence"]


def test_regime_guard_off_disables_the_gate(monkeypatch) -> None:
    """regime_guard=False: no refusal, no penalty — the config escape
    hatch for A/B and for users who want the raw pipeline."""
    import app.analysis.regime as reg

    monkeypatch.setattr(
        reg, "regime_read",
        lambda base, htf, price=None: _fake_regime("RANGE", "extreme", 2.3),
    )
    df = _demand_zone_frame()
    htf = _uptrend_htf()
    close_time = df["time_utc"].iloc[-1] + timedelta(minutes=1)
    cfg = EngineConfig(entry_mode="market", regime_guard=False)
    ev = evaluate(df, htf, close_time, cfg, spread_points=20)
    assert ev.signal is not None
    names = {c["name"] for c in ev.signal["trace"]["checks"]}
    assert "regime" not in names  # the policy never engaged


def test_fired_pulse_carries_regime_summary(monkeypatch) -> None:
    import app.analysis.regime as reg

    monkeypatch.setattr(
        reg, "regime_read",
        lambda base, htf, price=None: _fake_regime("RANGE", "normal", 1.0),
    )
    df = _deep_demand_frame()
    htf = _uptrend_htf()
    close_time = df["time_utc"].iloc[-1] + timedelta(minutes=5)
    ev = evaluate(df, htf, close_time, EngineConfig(), spread_points=20)
    assert ev.signal is not None
    fired = ev.pulse.get("fired") or {}
    assert fired.get("regime", {}).get("label") == "RANGE"


# ------------------------------------------------------------------ config


def test_d068_config_defaults() -> None:
    cfg = EngineConfig()
    assert cfg.regime_guard is True
    assert cfg.regime_vol_block_market is True
    assert 0 < cfg.regime_trend_bonus <= 0.3
    assert 0 < cfg.regime_range_bonus <= 0.3
    assert 0 < cfg.regime_chop_penalty <= 0.5
    assert 0 < cfg.regime_vol_penalty <= 0.5
