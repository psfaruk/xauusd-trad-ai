"""FastAPI application entrypoint (SPEC §7.1).

Phase 2/3: market data (candles/WS streaming), MT5 connection manager with
the engine runtime (tick -> bar -> SFP signal), signal/config/stats/logs REST
routes. Every /api router (except /api/health) carries the CurrentUser guard;
/ws authenticates via ?token= query param. All routers mount BEFORE the SPA
catch-all.

Deployment (D-016): when STATIC_DIR points at a built SPA, it is served at "/"
with a history-mode fallback — the Railway single-service image.

Demo mode (D-018, D-033): DATA_SOURCE=mock is a LOCAL-DEV-ONLY mode gated
behind ALLOW_DEMO=1 — a deployment can never show synthetic prices. The
default (and Railway image) runs DATA_SOURCE=live: real-time gold prices
from five venue WebSocket streams, with NO demo fallback (D-033).
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse

from app.api import (
    routes_config,
    routes_logs,
    routes_market,
    routes_mt5,
    routes_signals,
    routes_stats,
    routes_trading,
    ws,
)
from app.auth import CurrentUser
from app.config import Settings, get_settings
from app.db import apply_schema, is_postgres_url, make_engine, promote_admins
from app.engine.config import ConfigRepo
from app.engine.repo import SignalRepo
from app.mt5.connection import ConnectionManager, SourceResolution, resolve_data_source
from app.services.external import ExternalMarketService
from app.services.news import NewsService
from app.services.ws_hub import WSHub

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("xauusd")


def _client_key(request: Request) -> str:
    """Rate-limit key: real client IP behind the Railway/proxy layer."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# SPEC §13: slowapi rate limits on auth-bearing REST routes.
limiter = Limiter(key_func=_client_key, default_limits=["240/minute"])


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = get_settings()
    app.state.settings = settings

    # --- Database: persistent engine (auth role lookups; signals/config state)
    app.state.db_engine = None
    app.state.db_ok = False
    if is_postgres_url(settings.database_url):
        engine = make_engine(settings.database_url)
        try:
            app.state.db_ok = await apply_schema(engine)
            app.state.db_engine = engine
            promoted = await promote_admins(engine, settings.admin_email_list)
            if promoted:
                logger.info("promoted %d ADMIN_EMAILS user(s) to admin", promoted)
        except Exception as exc:  # noqa: BLE001 — boot must never crash (C6 spirit)
            logger.warning("DB init failed — running in degraded mode: %s", exc)
            await engine.dispose()
    else:
        logger.warning("DATABASE_URL not set — running without persistence (degraded mode)")

    # --- Shared httpx client (Supabase auth checks; external APIs)
    app.state.http = httpx.AsyncClient(timeout=httpx.Timeout(10.0))

    # --- Free external reference data (user req #6): Binance PAXG + ECB FX
    app.state.external = ExternalMarketService(
        http_factory=lambda: _get_http(app)
    )

    # --- Services: WS hub, news, repos, connection manager + engine runtime.
    # D-033: DATA_SOURCE=live is the only production mode — resolve returns a
    # LiveDataSource even when no provider answers the boot probe; the feed
    # keeps retrying (WS venue workers + REST loop) and NEVER substitutes
    # demo prices.
    app.state.hub = WSHub()
    # D-044 — news is ALWAYS on: ForexFactory needs no key; FMP adds
    # coverage when a key exists. High-impact USD events gate the engine
    # (news_blackout_min) and feed the Economic Events panel.
    app.state.news = NewsService(
        http_factory=lambda: _get_http(app),
        api_key=settings.fmp_api_key,
    )
    app.state.config_repo = ConfigRepo()
    app.state.signals = SignalRepo(app.state.db_engine)
    # D-042 — ICT/SMC analysis snapshots for the chart overlays + strip
    # D-044 — + COT institutional positioning + news panel data
    from app.services.analysis import AnalysisService
    from app.services.cot import CotService

    app.state.cot = CotService(http_factory=lambda: _get_http(app))
    app.state.analysis = AnalysisService(
        news_service=app.state.news, cot_service=app.state.cot
    )
    resolution = await resolve_data_source(
        settings, http_factory=lambda: _get_http(app)
    )
    source, effective_data_source = resolution.source, resolution.effective
    app.state.data_source = effective_data_source
    app.state.source_resolution = resolution  # D-032
    app.state.mt5 = ConnectionManager(
        source=source,
        settings=settings,
        hub=app.state.hub,
        db_engine=app.state.db_engine,
        repo=app.state.signals,
        news_service=app.state.news,
        config_repo=app.state.config_repo,
    )

    # --- D-037: per-user broker connections over the REAL terminal (frontend
    # "Connect broker" flow — credentials verified against the live terminal
    # session, Fernet-encrypted at rest, per-user trade isolation).
    from app.mt5.broker_connect import BrokerConnectionService

    app.state.broker_connect = BrokerConnectionService(
        db_engine=app.state.db_engine,
        settings=settings,
        demo_mode=(effective_data_source == "mock"),
    )

    # --- Phase 4: per-user trading planes (agent architecture) + the admin's
    # own platform executor (armed by engine_config.auto_trade).
    from app.engine.executor import OrderExecutor, TradeRepo
    from app.services.trading import UserTradingManager

    app.state.trades_repo = TradeRepo(app.state.db_engine)
    app.state.trading = UserTradingManager(
        settings=settings,
        public_source=app.state.mt5.source,
        hub=app.state.hub,
        db_engine=app.state.db_engine,
        config_repo=app.state.config_repo,
        platform_manager=app.state.mt5,
    )
    app.state.mt5.trading_manager = app.state.trading  # runtime relay hook

    # --- Structured logging -> logs table (best-effort, C6)
    from app.api.routes_logs import DBLogHandler

    root = logging.getLogger()
    if not any(isinstance(h, DBLogHandler) for h in root.handlers):
        root.addHandler(DBLogHandler(lambda: app.state.db_engine))

    # --- D-018/D-030/D-033/D-050: mock + live auto-connect / mt5 stored-credential
    # restore. Live connect waits for the first REAL tick (WS or REST) — on
    # total outage it raises, the status shows the no-feed state, and the
    # ConnectionManager heartbeat retries connect() forever. NO demo data.
    # D-050 — "অটো ট্রেড ওপেন থাকুক বা না থাকুক সিগন্যাল আসবে": the signal
    # engine must NEVER depend on the boot instant (or on broker/auto-trade
    # state). When the auto-connect fails at boot the heartbeat keeps
    # retrying the stored credentials every ~5s, so the engine runtime —
    # and with it SIGNALS — start the moment a provider answers.
    if effective_data_source in ("mock", "live"):
        boot_creds = (
            {"server": "LiveMarket", "login": "REALTIME", "password": "none"}
            if effective_data_source == "live"
            else {"server": "MockServer", "login": "10000000", "password": "mock"}
        )
        try:
            if effective_data_source == "live":
                st = await app.state.mt5.connect(boot_creds)
                feed = st.get("feed") or {}
                logger.info(
                    "live source auto-connected (D-030) — provider=%s price=%s",
                    feed.get("provider", "?"), feed.get("last_price", "?"),
                )
            else:
                await app.state.mt5.connect(boot_creds)
                logger.info("mock source auto-connected (D-018) — dashboard streams now")
        except Exception:  # noqa: BLE001 — never block boot
            logger.exception(
                "%s auto-connect failed — status shows NO FEED until a "
                "provider answers (no demo fallback, D-033); heartbeat "
                "retries every ~5s (D-050)",
                effective_data_source,
            )
            # D-050 — start the retry loop NOW (creds are stored pre-attempt
            # inside connect()); signals resume without a restart.
            app.state.mt5.ensure_heartbeat()
    else:
        asyncio.create_task(app.state.mt5.try_restore())

    # --- Phase 4: arm the admin platform executor per persisted auto_trade;
    # restore user demo planes (multi-user agent).
    # --- D-036: the REAL-terminal auto-executor (AI signal -> MT5 order).
    try:
        cfg0, auto0 = await app.state.config_repo.load(app.state.db_engine)
        platform_executor = OrderExecutor(
            source=app.state.mt5.source,
            cfg=cfg0,
            repo=app.state.trades_repo,
            hub=app.state.hub,
            owner=None,
        )
        if auto0:
            platform_executor.arm(True)
        app.state.trading.attach_platform_executor(platform_executor)
        if auto0:
            logger.warning("platform auto_trade=TRUE at boot — executor ARMED")

        from app.mt5.auto_trader import McpAutoTrader
        from app.mt5.mcp_source import McpTradingSource

        def _market_state(plat: str) -> tuple[bool, str]:
            """Broker-market-open check from the live feed's MT5 overlay
            (weekend/holiday logic — gold closed Sat/Sun, BTC 24/7)."""
            market = getattr(source, "market", None)
            mcp = getattr(market, "mcp", None)
            if mcp is None:
                return True, "market state unknown (no MT5 overlay)"
            try:
                if mcp.fresh(plat):
                    return True, "broker ticking"
                return False, "weekend/holiday — no fresh broker ticks"
            except Exception:  # noqa: BLE001 — never block trading on the check
                return True, "market state unknown"

        app.state.mt5_auto = McpAutoTrader(
            source=McpTradingSource(),
            cfg=cfg0,
            repo=app.state.trades_repo,
            hub=app.state.hub,
            config_repo=app.state.config_repo,
            db_engine=app.state.db_engine,
            market_state=_market_state,
        )
        app.state.trading.attach_live_auto_trader(app.state.mt5_auto)
        if await app.state.mt5_auto.restore():
            logger.warning(
                "LIVE MT5 AUTO-TRADE armed from persisted state — AI signals"
                " will place REAL orders"
            )
        restored = await app.state.trading.try_restore_planes()
        if restored:
            logger.info("restored %d user trading plane(s)", restored)
        # D-044 — every persisted practice plane comes back at boot (armed
        # accounts keep executing AI signals even before the user opens the app)
        practice = await app.state.trading.restore_practice_planes()
        if practice:
            logger.info("restored %d practice plane(s)", practice)
    except Exception:  # noqa: BLE001 — trading plane boot issues must not kill API
        logger.exception("trading plane init failed")

    logger.info(
        "startup complete: data_source=%s db=%s", effective_data_source, app.state.db_ok
    )
    yield
    await app.state.http.aclose()
    await app.state.trading.shutdown()
    await app.state.mt5.shutdown()
    if app.state.db_engine is not None:
        await app.state.db_engine.dispose()
    logger.info("shutdown complete")


app = FastAPI(
    title="XAUUSD AI Trading Platform",
    version=get_settings().app_version,
    lifespan=lifespan,
)

# SPEC §13 — rate limiting (default 240/min per client IP on REST routes)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, lambda req, exc: JSONResponse(
    status_code=429,
    content={"detail": f"rate limit exceeded: {exc.detail}"},
))
app.add_middleware(SlowAPIMiddleware)

_settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=_settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
async def health() -> dict:
    """Liveness probe (SPEC §7.1) — no auth required.

    D-033: `data_source` reports what RUNS plus the live-feed state —
    provider, tick rate (tps) and per-venue stream health — so a remote
    deployment is fully diagnosable without shell access. Demo data can
    never silently appear: it requires ALLOW_DEMO=1 (local dev only).
    """
    settings: Settings = app.state.settings
    resolution: SourceResolution | None = getattr(
        app.state, "source_resolution", None
    )
    effective = getattr(app.state, "data_source", None) or settings.data_source
    out: dict = {
        "status": "ok",
        "version": app.version,
        "data_source": effective,
        "requested_data_source": (
            resolution.requested if resolution else settings.data_source
        ),
        "degraded": False,
        "db": app.state.db_ok,
    }
    # D-033: feed introspection (provider, tps, venue health)
    try:
        source = app.state.mt5.source
        feed_status = getattr(source, "feed_status", None)
        if callable(feed_status):
            out["feed"] = feed_status()
    except Exception:  # noqa: BLE001 — health must never fail
        pass
    return out


@app.get("/api/me")
async def me(user: CurrentUser) -> dict:
    """Profile + role (SPEC §7.1). Requires a valid Supabase Bearer JWT."""
    return user


# --- Feature routers (all guarded per-route via CurrentUser deps) ---
app.include_router(routes_mt5.router)
app.include_router(routes_market.router)
app.include_router(routes_signals.router)
app.include_router(routes_config.router)
app.include_router(routes_stats.router)
app.include_router(routes_logs.router)
app.include_router(routes_trading.router)
app.include_router(ws.router)


async def _get_http(app: FastAPI) -> httpx.AsyncClient:
    return app.state.http


class SPAStaticFiles(StaticFiles):
    """Serve the built SPA with history-mode fallback to index.html.

    Newer Starlette raises HTTPException(404) from get_response instead of
    returning a 404 response — catch it and retry with index.html.
    """

    async def get_response(self, path: str, scope):  # type: ignore[override]
        try:
            response = await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404:
                raise
            response = await super().get_response("index.html", scope)
        if response.status_code == 404:
            response = await super().get_response("index.html", scope)
        return response


# Railway single-service deploy (D-016): serve the built SPA from the API
# process. Mounted LAST — /api routes and /ws always win.
_static_dir = _settings.static_dir
if _static_dir and Path(_static_dir).is_dir():
    app.mount("/", SPAStaticFiles(directory=_static_dir, html=True), name="spa")
    logger.info("serving SPA from %s", _static_dir)
