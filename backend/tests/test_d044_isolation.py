"""D-044 — institutional multi-user isolation tests.

The app is a public institutional product now:
1. every user gets an auto-provisioned PRACTICE plane (own balance,
   positions, risk settings — total per-user isolation);
2. the institution terminal's account/positions/history/orders are
   ADMIN-ONLY (regular users get 403, never the company's data);
3. /api/positions serves the USER's own plane only;
4. per-user risk settings persist and live-apply to the user's executor;
5. SL/TP on paper positions execute broker-like via check_stops();
6. news + COT services parse their sources and degrade gracefully.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from app.services.cot import CotService
from app.services.news import NewsService

# ---------------------------------------------------------------- helpers

def _mgr():
    """UserTradingManager over the deterministic mock market."""
    from unittest.mock import MagicMock

    from app.engine.config import ConfigRepo
    from app.mt5.mock_source import MockDataSource
    from app.services.trading import UserTradingManager

    public = MockDataSource()

    class _Repo(ConfigRepo):
        async def load(self, db=None):
            from app.engine.config import EngineConfig

            return EngineConfig(), False

    hub = MagicMock()
    hub.broadcast_user = MagicMock(side_effect=lambda *a, **k: asyncio.sleep(0))
    hub.broadcast_all = MagicMock(side_effect=lambda *a, **k: asyncio.sleep(0))
    hub.broadcast_admins = MagicMock(side_effect=lambda *a, **k: asyncio.sleep(0))
    mgr = UserTradingManager(
        settings=MagicMock(data_source="mock"),
        public_source=public,
        hub=hub,
        db_engine=None,
        config_repo=_Repo(),
        platform_manager=None,
    )
    return mgr, public


USER_A = "11111111-1111-1111-1111-111111111111"
USER_B = "22222222-2222-2222-2222-222222222222"


# ----------------------------------------------------- practice planes

async def test_ensure_plane_auto_provisions_isolated_accounts() -> None:
    """Every user gets their own plane + balance on first touch; the two
    accounts are fully independent (D-044 core requirement)."""
    mgr, _ = _mgr()
    from app.services.trading import DEMO_START_BALANCE

    pa = await mgr.ensure_plane(USER_A)
    pb = await mgr.ensure_plane(USER_B)
    assert pa is not pb
    assert pa.owner == USER_A and pb.owner == USER_B
    # independent balances
    ia = pa.source.account_info()
    ib = pb.source.account_info()
    assert ia["balance"] == DEMO_START_BALANCE
    assert ib["balance"] == DEMO_START_BALANCE
    # A trades — only A's balance moves
    from app.mt5.base import Order

    res = await pa.source.place_order(
        Order(symbol="XAUUSDm", side="BUY", volume=1.0, deviation=30, magic=0)
    )
    assert res.ok
    ia2 = pa.source.account_info()
    ib2 = pb.source.account_info()
    # floating P/L moves A's equity; B is untouched
    assert ia2["equity"] != ia["equity"] or ia2["balance"] == ia["balance"]
    assert ib2["balance"] == ib["balance"]
    assert ib2["equity"] == ib["equity"]
    # positions are per-plane
    pos_a = await mgr.positions(USER_A)
    pos_b = await mgr.positions(USER_B)
    assert len(pos_a) == 1
    assert len(pos_b) == 0


async def test_ensure_plane_is_idempotent() -> None:
    mgr, _ = _mgr()
    p1 = await mgr.ensure_plane(USER_A)
    p2 = await mgr.ensure_plane(USER_A)
    assert p1 is p2


async def test_set_auto_trade_arms_users_own_plane_only() -> None:
    mgr, _ = _mgr()
    await mgr.set_auto_trade(USER_A, True)
    assert mgr.plane(USER_A).executor.auto_trade is True
    assert mgr.plane(USER_B) is None  # B untouched


async def test_user_settings_override_engine_config() -> None:
    """Per-user money management: user settings merge over the global cfg."""
    from unittest.mock import MagicMock

    from app.engine.config import EngineConfig
    from app.services.trading import UserTradingManager

    base = EngineConfig(risk_percent=0.5, max_positions=3, rr=1.1)

    class _Repo:
        async def load(self, db=None):
            return base, False

    mgr = UserTradingManager(
        settings=MagicMock(data_source="mock"), public_source=MagicMock(),
        hub=MagicMock(), db_engine=None, config_repo=_Repo(),
    )

    # stored settings for A (simulated user_accounts row)
    async def _load(owner, _settings=None):
        if _settings is None:
            _settings = {"risk_percent": 2.0, "max_positions": 7}
        if owner == USER_A:
            return {
                "balance": 5000.0, "currency": "USD", "auto_trade": False,
                "settings": _settings,
            }
        return None

    mgr._load_account = _load  # type: ignore[method-assign]
    cfg_a = await mgr._user_cfg(USER_A)
    assert cfg_a.risk_percent == 2.0
    assert cfg_a.max_positions == 7
    assert cfg_a.rr == 1.1  # untouched field from the global config
    cfg_b = await mgr._user_cfg(USER_B)
    assert cfg_b.risk_percent == 0.5  # defaults


# ------------------------------------------------------------ SL/TP watcher

async def test_check_stops_closes_touched_positions() -> None:
    """Broker-like SL/TP execution on the paper plane (D-044)."""
    from app.mt5.base import Order
    from app.mt5.mock_source import MockDataSource

    src = MockDataSource()
    await src.connect({"server": "s", "login": "1", "password": "x"})
    # open a SELL far from price with a TP just above entry
    tick = await src.get_tick("XAUUSDm")
    res = await src.place_order(
        Order(symbol="XAUUSDm", side="SELL", volume=0.1, tp=tick.bid - 50.0,
              deviation=30, magic=0)
    )
    assert res.ok
    # nothing closed yet (TP far away)
    assert await src.check_stops() == []
    # open a BUY, then set its SL clearly ABOVE the current bid (already
    # breached for a BUY) — deterministic trigger for the watcher
    res2 = await src.place_order(
        Order(symbol="XAUUSDm", side="BUY", volume=0.1, deviation=30, magic=0)
    )
    assert res2.ok
    tick_now = await src.get_tick("XAUUSDm")
    from dataclasses import replace

    src._positions[-1] = replace(src._positions[-1], sl=tick_now.bid + 5.0)
    closed = await src.check_stops()
    assert len(closed) == 1
    assert closed[0]["kind"] == "sl"
    assert closed[0]["ticket"] == res2.ticket
    # position gone, balance realized
    positions = await src.get_positions()
    assert all(p.ticket != res2.ticket for p in positions)


def test_restore_positions_and_balance() -> None:
    from app.mt5.base import Position
    from app.mt5.mock_source import MockDataSource

    async def _go():
        src = MockDataSource()
        await src.connect({"server": "s", "login": "1", "password": "x"})
        now = datetime.now(tz=UTC)
        src.restore_positions(
            [
                Position(
                    ticket=555, symbol="XAUUSDm", side="BUY", volume=0.2,
                    price_open=4300.0, sl=4290.0, tp=4320.0, profit=0.0,
                    time=now,
                )
            ],
            next_ticket=900,
        )
        src.set_balance(7777.0)
        info = src.account_info()
        assert info["balance"] == 7777.0
        assert src._next_ticket >= 900
        assert len(src._positions) == 1

    asyncio.run(_go())


# ------------------------------------------------------------ news service

class _Resp:
    def __init__(self, status_code=200, data=None):
        self.status_code = status_code
        self._data = data

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Http:
    """Fake httpx client serving ForexFactory-shaped fixtures.

    D-052 fix: fixture dates are RELATIVE to now (the old fixed
    2026-09-22..25 dates fell out of the rolling [now-1h, now+7d] window
    the day after they were written, turning this test red for no reason).
    """

    def __init__(self, fail=False):
        self.fail = fail

    async def get(self, url, **kw):
        if self.fail:
            return _Resp(status_code=429, data=[])
        now = datetime.now(tz=UTC)
        in_ = lambda hours: (now + timedelta(hours=hours)).isoformat()  # noqa: E731
        if "ff_calendar_thisweek" in url:
            return _Resp(data=[
                {"title": "Fed Interest Rate Decision", "country": "USD",
                 "date": in_(2), "impact": "High",
                 "forecast": "4.25%", "previous": "4.50%"},
                {"title": "EU Summit", "country": "EUR",
                 "date": in_(26), "impact": "High"},
                {"title": "Core CPI m/m", "country": "USD",
                 "date": in_(48), "impact": "Medium"},
                {"title": "Low-impact thing", "country": "USD",
                 "date": in_(50), "impact": "Low"},
            ])
        if "ff_calendar_nextweek" in url:
            return _Resp(data=[])
        if "economic_calendar" in url:
            return _Resp(data=[
                {"date": in_(72), "country": "US",
                 "impact": "High", "event": "GDP q/q", "estimate": "2.1%",
                 "previous": "1.8%"}
            ])
        return _Resp(status_code=404, data=[])


def _client_factory(client):
    """http_factory pattern: a callable whose call returns a coroutine."""
    async def factory():
        return client
    return factory


async def test_news_multi_source_merge_and_filters() -> None:
    svc = NewsService(http_factory=_client_factory(_Http()), api_key="k")
    now = datetime.now(tz=UTC)
    start, end = now - timedelta(hours=1), now + timedelta(days=7)
    events = await svc.usd_high_impact(start, end)
    assert events is not None
    titles = [e["title"] for e in events]
    assert "Fed Interest Rate Decision" in titles
    assert "GDP q/q" in titles  # merged from FMP
    assert all("EU" not in t for t in titles)  # USD only
    assert all("Low-impact" not in t for t in titles)  # high impact only
    upcoming = await svc.upcoming(hours=24 * 8)
    assert any(e["impact"] == "medium" for e in upcoming)  # panel shows medium too


async def test_news_degrades_to_none_when_all_sources_fail() -> None:
    svc = NewsService(http_factory=_client_factory(_Http(fail=True)), api_key="k")
    now = datetime.now(tz=UTC)
    events = await svc.usd_high_impact(now, now + timedelta(hours=24))
    assert events is None  # honest unavailable — engine skips the check


# ------------------------------------------------------------ COT service

def test_cot_parses_cftc_rows() -> None:
    rows = [
        {"market_and_exchange_names": "GOLD - COMMODITY EXCHANGE INC.",
         "report_date_as_yyyy_mm_dd": "2026-09-15T00:00:00.000",
         "open_interest_all": "409899",
         "noncomm_positions_long_all": "258059",
         "noncomm_positions_short_all": "27721",
         "comm_positions_long_all": "56417",
         "comm_positions_short_all": "318138",
         "nonrept_positions_long_all": "47460",
         "nonrept_positions_short_all": "16077"},
        {"market_and_exchange_names": "GOLD - COMMODITY EXCHANGE INC.",
         "report_date_as_yyyy_mm_dd": "2026-09-08T00:00:00.000",
         "open_interest_all": "411227",
         "noncomm_positions_long_all": "261007",
         "noncomm_positions_short_all": "29047",
         "comm_positions_long_all": "54403",
         "comm_positions_short_all": "324677",
         "nonrept_positions_long_all": "52554",
         "nonrept_positions_short_all": "14240"},
    ]
    snap = CotService._build(rows)
    assert snap["report_date"] == "2026-09-15"
    assert snap["large_speculators"]["net"] == 258059 - 27721
    assert snap["large_speculators"]["net_change"] == (258059 - 27721) - (261007 - 29047)
    assert snap["commercial_hedgers"]["net"] == 56417 - 318138
    assert snap["bias"] in ("accumulating_short", "accumulating_long", "neutral")


async def test_cot_degrades_when_http_fails() -> None:
    class _FailHttp:
        async def get(self, url, **kw):
            raise RuntimeError("network down")

    svc = CotService(http_factory=_client_factory(_FailHttp()))
    assert await svc.snapshot() is None


# ------------------------------------------------------------ orderflow

def test_flow_stats_notional_and_delta() -> None:
    import numpy as np
    import pandas as pd

    from app.analysis.orderflow import flow_stats

    rng = np.random.default_rng(3)
    n = 1500
    closes = 4300 + np.cumsum(rng.standard_normal(n) * 0.6)
    df = pd.DataFrame({
        "time_utc": pd.date_range("2026-09-20", periods=n, freq="1min", tz="UTC"),
        "o": closes, "h": closes + 0.5, "l": closes - 0.5, "c": closes,
        "v": rng.integers(100, 800, n),
    })
    fs = flow_stats(df)
    assert fs["usd_24h"] > 0
    assert fs["usd_1h"] > 0
    assert 0.0 <= fs["buy_pct_1h"] <= 100.0
    assert fs["volume_24h"] > 0
    assert "bias" in fs


def test_confluence_has_d044_bonus_factors() -> None:
    import numpy as np
    import pandas as pd

    from app.analysis.context import CONFLUENCE_BONUS, build_confluence

    assert "delta_confirms" in CONFLUENCE_BONUS
    assert "flow_active" in CONFLUENCE_BONUS
    rng = np.random.default_rng(5)
    n = 400
    closes = 4300 + np.cumsum(rng.standard_normal(n) * 0.7)
    df = pd.DataFrame({
        "time_utc": pd.date_range("2026-09-20", periods=n, freq="1min", tz="UTC"),
        "o": closes, "h": closes + 0.6, "l": closes - 0.6, "c": closes,
        "v": rng.integers(100, 900, n),
    })
    factors = build_confluence(
        df, {"H1": df, "M5": df, "M15": df}, "BUY", float(df["c"].iloc[-1]),
        df["time_utc"].iloc[-1].to_pydatetime(),
    )
    names = [f["name"] for f in factors]
    assert "delta_confirms" in names
    assert "flow_active" in names


# ------------------------------------------------- D-046 settings-upsert SQL
# Production bug 2026-09-22: PUT /api/trading/settings 500'd on Postgres with
# "syntax error at or near ':'" — text() does NOT bind ':name' when it is
# immediately followed by a '::' cast, so ':s::jsonb' leaked into the SQL as
# a literal while ':o' became $1. These tests pin the correct CAST() form at
# the dialect-compile level (no live Postgres needed to catch this class).


def test_settings_upsert_sql_binds_all_params_under_asyncpg() -> None:
    """asyncpg dialect: every named bind must compile to $N — no leftovers."""
    import re

    from sqlalchemy import text
    from sqlalchemy.dialects.postgresql import asyncpg

    from app.services.trading import UPSERT_USER_SETTINGS_SQL

    stmt = text(UPSERT_USER_SETTINGS_SQL)
    compiled = stmt.compile(dialect=asyncpg.dialect())
    sql = compiled.string
    # both params converted to positional
    assert "$1" in sql and "$2" in sql
    # no un-converted ':name' bind survived into the SQL (the D-046 bug)
    leftover = re.findall(r"(?<![$\w]):[a-z_]+", sql)
    assert leftover == [], f"unbound named params leaked into SQL: {leftover}"
    # both bind params are declared
    assert sorted(stmt._bindparams.keys()) == ["o", "s"]


def test_settings_upsert_sql_compiles_for_sqlite_too() -> None:
    """The same statement must at least PARSE with named binds on sqlite
    (tests + local runs) — guards against dialect-specific regressions."""
    from sqlalchemy import text
    from sqlalchemy.dialects import sqlite

    from app.services.trading import UPSERT_USER_SETTINGS_SQL

    stmt = text(UPSERT_USER_SETTINGS_SQL)
    compiled = stmt.compile(dialect=sqlite.dialect())
    assert "CAST" in compiled.string
    assert sorted(stmt._bindparams.keys()) == ["o", "s"]


def test_no_adjacent_cast_bindparams_in_trading_sql() -> None:
    """Source sweep: no ':name::cast' may ever appear in trading.py —
    SQLAlchemy text() silently drops such binds (asyncpg leak class)."""
    import inspect
    import re

    import app.services.trading as trading_mod

    src = inspect.getsource(trading_mod)
    hits = re.findall(r":[a-z_]+\s*::[a-z_]+", src)
    assert hits == [], f"adjacent-cast bind params found (D-046 bug class): {hits}"


async def test_set_user_settings_executes_upsert_and_live_applies() -> None:
    """Full path: set_user_settings must actually EXECUTE the upsert against
    a DB (regression: the broken SQL was never exercised — db=None in all
    prior tests — so it shipped and 500'd only in production)."""
    import json
    from unittest.mock import MagicMock

    from app.services.trading import UPSERT_USER_SETTINGS_SQL, UserTradingManager

    executed: list[tuple[str, dict]] = []

    class _Conn:
        async def execute(self, stmt, params=None, *_a, **_k):
            executed.append((str(stmt), params or {}))
            return MagicMock()

    class _BeginCtx:
        async def __aenter__(self):
            return _Conn()

        async def __aexit__(self, *exc):
            return False

    class _Engine:
        def begin(self):
            return _BeginCtx()

    class _Repo:
        async def load(self, db=None):
            from app.engine.config import EngineConfig
            return EngineConfig(), False

    mgr = UserTradingManager(
        settings=MagicMock(data_source="mock"), public_source=MagicMock(),
        hub=MagicMock(), db_engine=_Engine(), config_repo=_Repo(),
    )
    mgr._load_account = lambda owner: None  # type: ignore[method-assign]

    clean = await mgr.set_user_settings(USER_A, {"risk_percent": 1.5, "bogus": 9})
    # validation dropped the unknown key, kept the good one
    assert clean == {"risk_percent": 1.5}
    # the upsert executed exactly once with the fixed SQL + serialized payload
    assert len(executed) == 1
    sql, params = executed[0]
    assert UPSERT_USER_SETTINGS_SQL in sql
    assert params["o"] == USER_A
    assert json.loads(params["s"]) == {"risk_percent": 1.5}
