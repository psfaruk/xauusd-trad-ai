"""D-076 — multi-market platform tests (USOIL + USTEC + per-pair lots).

User directives (Bengali, verbatim):
- "আরও দুইটি পেয়ার অ্যাড করবেন সেটি হলো USOIL ও USTEC(×100) ... এই পেয়ার
  গুলো ও ঠিক অন্য পেয়ার এর মতো অটো সিগন্যাল চার্ট ড্রয়িং, সব কিছু হবে।"
- "আর সব পেয়ার গুলো একটি ড্রপ ডাউন বক্সে থাকবে" (frontend, tested via
  the markets module contract in test_livesetup.mjs).
- "ইউজার চাইলে প্রত্যেক পেয়ার এর জন্য আলাদা করে লট সাইজ ও অন্যান্য
  বিষয়গুলো সেটাপ করতে পারবে।"
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.engine.config import DEFAULT_CONFIG, EngineConfig, upgrade_legacy_d076
from app.engine.engine import SignalEngine
from app.engine.executor import size_lot
from app.mt5.base import MARKETS, market_key, market_scale, market_spec
from app.mt5.mock_source import MockDataSource

# ------------------------------------------------------------ market_key

@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("XAUUSD", "XAUUSD"),
        ("XAUUSDm", "XAUUSD"),
        ("BTCUSDm", "BTCUSD"),
        ("USOIL", "USOIL"),
        ("USOILm", "USOIL"),       # 5-char base + suffix (the D-076 fix)
        ("USTEC", "USTEC"),
        ("USTECm", "USTEC"),
        ("USTECmicro", "USTEC"),
        ("usoil", "USOIL"),
        ("USTEC.x", "USTEC"),
        ("ETHUSD", "ETHUSD"),      # unknown symbol: legacy rule, untouched
    ],
)
def test_market_key_five_char_bases(raw: str, want: str) -> None:
    """USOILm/USTECm normalize to their market keys (pre-D-076 the suffix
    rule required >=6 base chars and produced 'USOILM' — matched nothing)."""
    assert market_key(raw) == want


def test_markets_registry_covers_all_four() -> None:
    assert set(MARKETS) == {"XAUUSD", "BTCUSD", "USOIL", "USTEC"}
    for mk, spec in MARKETS.items():
        assert spec.key == mk
        assert spec.point > 0
        assert spec.contract_size > 0
        assert spec.digits >= 0
        assert spec.mock_sigma > 0
        assert spec.mock_spread > 0
    # lot-sizing honesty: oil 1000 bbl, index 100 units (user's ×100)
    assert MARKETS["USOIL"].contract_size == 1000.0
    assert MARKETS["USTEC"].contract_size == 100.0
    assert market_spec("USTECm").point == 0.1
    assert market_spec("USOILm").point == 0.01
    assert market_scale("XAUUSD") == 1.0  # gold: zero behavior change


# ------------------------------------------------------------ mock tapes

async def test_mock_multimarket_tapes() -> None:
    src = MockDataSource(
        start_time=datetime(2026, 1, 5, 12, 0, tzinfo=UTC), time_scale=0
    )
    src.advance_minutes(400)
    gold = await src.get_rates("XAUUSDm", "M15", 2)
    btc = await src.get_rates("BTCUSDm", "M15", 2)
    oil = await src.get_rates("USOIL", "M15", 2)
    tec = await src.get_rates("USTECm", "M15", 2)
    # four DIFFERENT price levels (no cross-market contamination)
    assert 2000 < float(gold["c"].iloc[-1]) < 3200
    assert 20000 < float(btc["c"].iloc[-1]) < 120000
    assert 20 < float(oil["c"].iloc[-1]) < 200
    assert 10000 < float(tec["c"].iloc[-1]) < 30000
    # per-market symbol info (lot math honest)
    assert src.symbol_info("USOIL").contract_size == 1000.0
    assert src.symbol_info("USTEC").point == 0.1
    assert src.symbol_info("XAUUSDm").contract_size == 100.0
    # the platform list drives the engine runtimes
    assert set(src.platform_symbols) == {"XAUUSD", "BTCUSD", "USOIL", "USTEC"}
    # pattern-respecting discovery: legacy gold callers still get gold
    assert src.discover_symbols("*XAUUSD*") == ["XAUUSD"]  # market key (platform contract)
    # deterministic tapes: same seed -> identical prices (sibling planes)
    twin = MockDataSource(
        start_time=datetime(2026, 1, 5, 12, 0, tzinfo=UTC), time_scale=0
    )
    twin.advance_minutes(400)
    oil2 = await twin.get_rates("USOIL", "M15", 2)
    assert float(oil2["c"].iloc[-1]) == float(oil["c"].iloc[-1])


async def test_mock_gold_tape_unchanged() -> None:
    """The gold tape must stay byte-identical to the pre-D-076 generator
    (rng [42, minute]) — every gold test/fixture depends on it."""
    src = MockDataSource(
        start_time=datetime(2026, 1, 5, 12, 0, tzinfo=UTC), time_scale=0
    )
    src.advance_minutes(120)
    df = await src.get_rates("XAUUSDm", "M1", 3)
    # pre-refactor values reproduced by the identical rng stream
    closes = [float(v) for v in df["c"].tolist()]
    assert len(closes) == 3
    # cross-check the raw tape array against the deterministic generator
    # (minute indexes count from the ORIGIN — 30 days before the anchor)
    import numpy as np

    last = src._last_closed_minute(src._vnow())
    for i, m in enumerate(range(last - 2, last + 1)):
        rng = np.random.default_rng([42, m])
        z = rng.standard_normal(4)
        prev = float(src._c[m - 1]) if m > 0 else 2650.0
        assert abs(closes[i] - (prev + 0.35 * float(z[0]))) < 1e-9


# ------------------------------------------------------- engine market cfg

class _Hub:
    def broadcast_all(self, *a, **k):  # noqa: ANN002
        return None


class _Repo:
    def insert(self, *a, **k):  # noqa: ANN002
        return None


async def test_signal_engine_market_cfg_scaling() -> None:
    engine = SignalEngine(cfg=DEFAULT_CONFIG, hub=_Hub(), repo=_Repo())
    # gold: the raw config, untouched
    assert engine._market_cfg("XAUUSDm") is DEFAULT_CONFIG
    view = engine._market_cfg("USOIL")
    assert view is not DEFAULT_CONFIG
    s = market_scale("USOIL")
    assert abs(view.entry_min_usd - DEFAULT_CONFIG.entry_min_usd * s) < 1e-9
    assert abs(view.pending_max_usd - DEFAULT_CONFIG.pending_max_usd * s) < 1e-9
    # spread budget scales by the market's spread width (BTC fix)
    bt = engine._market_cfg("BTCUSD")
    assert bt.max_spread_points > DEFAULT_CONFIG.max_spread_points * 10
    # apply_config invalidates the cached views
    new_cfg = DEFAULT_CONFIG.model_copy(update={"entry_min_usd": 2.0})
    await engine.apply_config(new_cfg)
    fresh = engine._market_cfg("USOIL")
    assert abs(fresh.entry_min_usd - 2.0 * s) < 1e-9


def test_size_lot_symbol_lot_override() -> None:
    cfg = DEFAULT_CONFIG.model_copy(
        update={"risk_mode": "fixed", "fixed_lot": 0.05}
    )
    # fallback: fixed_lot
    base = size_lot(cfg, 10_000.0, 3.0)
    assert base.lots == 0.05
    # per-market override wins
    oil = size_lot(cfg, 10_000.0, 3.0, symbol_lot=0.2)
    assert oil.lots == 0.2
    # percent mode ignores the per-market lot (risk formula governs)
    pct = DEFAULT_CONFIG.model_copy(update={"risk_mode": "percent"})
    a = size_lot(pct, 10_000.0, 3.0, symbol_lot=0.2)
    b = size_lot(pct, 10_000.0, 3.0)
    assert abs(a.lots - b.lots) < 1e-9


# --------------------------------------------------------------- executor

def test_executor_set_symbol_lots_normalizes() -> None:
    from app.engine.executor import OrderExecutor

    class _Src:  # duck-typed minimal source
        async def account_info(self):
            return {"equity": 10_000.0}

    ex = OrderExecutor(source=_Src(), cfg=DEFAULT_CONFIG, repo=_Repo(),
                       hub=_Hub(), owner=None)
    assert ex._symbol_lots == {}
    ex.set_symbol_lots({"usoil": 0.1, "USTEC": "bad", "XAUUSD": -1, "BTCUSD": 0.02})
    # lowercase key normalized; non-numeric + non-positive dropped
    assert ex._symbol_lots == {"USOIL": 0.1, "BTCUSD": 0.02}


# ---------------------------------------------------------------- config

def test_d076_defaults_four_markets() -> None:
    assert DEFAULT_CONFIG.signal_symbols == [
        "XAUUSD", "BTCUSD", "USOIL", "USTEC",
    ]


def test_upgrade_legacy_d076() -> None:
    # untouched D-051 default row -> 4 markets
    raw = {"signal_symbols": ["XAUUSD", "BTCUSD"]}
    out, moved = upgrade_legacy_d076(raw)
    assert out["signal_symbols"] == ["XAUUSD", "BTCUSD", "USOIL", "USTEC"]
    assert moved
    # a user-customized list stays FOREVER
    custom = {"signal_symbols": ["XAUUSD"]}
    out2, moved2 = upgrade_legacy_d076(custom)
    assert out2["signal_symbols"] == ["XAUUSD"]
    assert not moved2
    # already-upgraded row: idempotent
    out3, moved3 = upgrade_legacy_d076(dict(out))
    assert not moved3


def test_config_auto_trade_subset_accepts_new_markets() -> None:
    cfg = EngineConfig(
        signal_symbols=["XAUUSD", "BTCUSD", "USOIL", "USTEC"],
        auto_trade_symbols=["USOIL", "USTEC"],
    )
    assert cfg.auto_trade_symbols == ["USOIL", "USTEC"]


# ------------------------------------------------------------ live source

async def test_live_source_terminal_only_markets() -> None:
    from app.mt5.live_source import MarketFeed

    feed = MarketFeed(enable_ws=False)
    assert set(feed.feeds) == {"XAUUSD", "BTCUSD", "USOIL", "USTEC"}
    oil = feed.feeds["USOIL"]
    tec = feed.feeds["USTEC"]
    # no composite providers — the terminal MCP is the only source
    assert oil.spec.binance_symbol == ""
    assert tec.spec.binance_symbol == ""
    assert oil._resolve_venues() == ()
    assert tec._resolve_venues() == ()
    # honest degraded state (no MT5 overlay, no composite: never fake data)
    ok = await oil.poll_once()
    assert ok is False
    assert oil.provider == "degraded"
    assert "MetaTrader 5" in oil.provider_detail or "terminal" in oil.provider_detail
    # symbol routing handles 5-char broker suffixes
    assert feed.feed_for("USOILm").spec.key == "USOIL"
    assert feed.feed_for("USTECmicro").spec.key == "USTEC"
