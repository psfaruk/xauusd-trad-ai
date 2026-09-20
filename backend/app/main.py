"""FastAPI application entrypoint (SPEC §7.1).

Phase 2/3: market data (candles/WS streaming), MT5 connection manager with
the engine runtime (tick -> bar -> SFP signal), signal/config/stats/logs REST
routes. Every /api router (except /api/health) carries the CurrentUser guard;
/ws authenticates via ?token= query param. All routers mount BEFORE the SPA
catch-all.

Deployment (D-016): when STATIC_DIR points at a built SPA, it is served at "/"
with a history-mode fallback — the Railway single-service image.

Demo mode (D-018): with DATA_SOURCE=mock the ConnectionManager auto-connects
at startup so the dashboard streams immediately (Phase 2 mock AC).
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
from app.mt5.connection import ConnectionManager, resolve_data_source
from app.services.external import ExternalMarketService
from app.services.news import NewsService, NullNewsService
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
    # D-030: DATA_SOURCE=live probes the free real-time provider chain once;
    # on total failure it degrades to mock so the dashboard always streams.
    app.state.hub = WSHub()
    if settings.fmp_api_key:
        app.state.news = NewsService(
            http_factory=lambda: _get_http(app),
            api_key=settings.fmp_api_key,
        )
    else:
        app.state.news = NullNewsService()
    app.state.config_repo = ConfigRepo()
    app.state.signals = SignalRepo(app.state.db_engine)
    source, effective_data_source = await resolve_data_source(
        settings, http_factory=lambda: _get_http(app)
    )
    app.state.data_source = effective_data_source
    app.state.mt5 = ConnectionManager(
        source=source,
        settings=settings,
        hub=app.state.hub,
        db_engine=app.state.db_engine,
        repo=app.state.signals,
        news_service=app.state.news,
        config_repo=app.state.config_repo,
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

    # --- D-018/D-030: mock + live auto-connect / mt5 stored-credential restore
    if effective_data_source in ("mock", "live"):
        try:
            if effective_data_source == "live":
                st = await app.state.mt5.connect(
                    {"server": "LiveMarket", "login": "REALTIME", "password": "none"}
                )
                feed = st.get("feed") or {}
                logger.info(
                    "live source auto-connected (D-030) — provider=%s price=%s",
                    feed.get("provider", "?"), feed.get("last_price", "?"),
                )
            else:
                await app.state.mt5.connect(
                    {"server": "MockServer", "login": "10000000", "password": "mock"}
                )
                logger.info("mock source auto-connected (D-018) — dashboard streams now")
        except Exception:  # noqa: BLE001 — never block boot
            logger.exception("%s auto-connect failed", effective_data_source)
    else:
        asyncio.create_task(app.state.mt5.try_restore())

    # --- Phase 4: arm the admin platform executor per persisted auto_trade;
    # restore user demo planes (multi-user agent).
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
        restored = await app.state.trading.try_restore_planes()
        if restored:
            logger.info("restored %d user trading plane(s)", restored)
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
    """Liveness probe (SPEC §7.1) — no auth required."""
    settings: Settings = app.state.settings
    return {
        "status": "ok",
        "version": app.version,
        "data_source": getattr(app.state, "data_source", None) or settings.data_source,
        "db": app.state.db_ok,
    }


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
