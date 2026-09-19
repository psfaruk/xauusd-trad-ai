"""FastAPI application entrypoint (Phase 0 skeleton, SPEC §7.1).

Only GET /api/health is live. Route modules exist as stubs and are mounted in
Phases 1–4 (DECISIONS.md D-012).
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import Settings, get_settings
from app.db import init_db
from app.mt5.connection import ConnectionManager, create_data_source

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("xauusd")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = get_settings()
    app.state.settings = settings
    app.state.db_ok = await init_db(settings.database_url)
    app.state.mt5 = ConnectionManager(create_data_source(settings))
    logger.info(
        "startup complete: data_source=%s db=%s", settings.data_source, app.state.db_ok
    )
    yield
    await app.state.mt5.shutdown()
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
