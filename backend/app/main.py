"""FastAPI application entrypoint (SPEC §7.1).

Phase 1: public `GET /api/health`, authenticated `GET /api/me` (Supabase JWT
via app.auth). Route modules from SPEC §5 mount in Phases 2–4 (D-012) — every
new /api router MUST carry `dependencies=[Depends(get_current_user)]` (or
per-route guards) so the "all /api except /health require Bearer JWT" rule
from §7.1 stays true, and must be mounted BEFORE the SPA catch-all.

Deployment (D-016): when STATIC_DIR points at a built SPA, it is served at "/"
with a history-mode fallback — the Railway single-service image.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.auth import CurrentUser
from app.config import Settings, get_settings
from app.db import apply_schema, is_postgres_url, make_engine, promote_admins
from app.mt5.connection import ConnectionManager, create_data_source

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("xauusd")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = get_settings()
    app.state.settings = settings

    # --- Database: persistent engine (auth role lookups; Phase 2+ state) ---
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

    # --- Shared httpx client (Supabase auth checks; Phase 2+ external APIs) ---
    app.state.http = httpx.AsyncClient(timeout=httpx.Timeout(10.0))

    app.state.mt5 = ConnectionManager(create_data_source(settings))
    logger.info(
        "startup complete: data_source=%s db=%s", settings.data_source, app.state.db_ok
    )
    yield
    await app.state.http.aclose()
    await app.state.mt5.shutdown()
    if app.state.db_engine is not None:
        await app.state.db_engine.dispose()
    logger.info("shutdown complete")


app = FastAPI(
    title="XAUUSD AI Trading Platform",
    version=get_settings().app_version,
    lifespan=lifespan,
)

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
        "data_source": settings.data_source,
        "db": app.state.db_ok,
    }


@app.get("/api/me")
async def me(user: CurrentUser) -> dict:
    """Profile + role (SPEC §7.1). Requires a valid Supabase Bearer JWT."""
    return user


class SPAStaticFiles(StaticFiles):
    """Serve the built SPA with history-mode fallback to index.html."""

    async def get_response(self, path: str, scope):  # type: ignore[override]
        response = await super().get_response(path, scope)
        if response.status_code == 404:
            response = await super().get_response("index.html", scope)
        return response


# Railway single-service deploy (D-016): serve the built SPA from the API
# process. Mounted LAST — /api routes (and the future /ws) always win.
_static_dir = _settings.static_dir
if _static_dir and Path(_static_dir).is_dir():
    app.mount("/", SPAStaticFiles(directory=_static_dir, html=True), name="spa")
    logger.info("serving SPA from %s", _static_dir)
