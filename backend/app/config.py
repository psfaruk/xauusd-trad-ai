"""Application settings (SPEC §11), loaded from environment / .env."""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All values optional in Phase 0 — the app boots in degraded mode without them."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Supabase / DB
    database_url: str | None = None
    supabase_url: str | None = None
    supabase_anon_key: str | None = None

    # Security
    fernet_key: str | None = None
    admin_emails: str = ""

    # Engine / data (SPEC C7; live = free real-time APIs, DECISIONS.md D-030)
    data_source: Literal["mock", "mt5", "live"] = "live"
    live_poll_seconds: float = 2.0
    fmp_api_key: str | None = None
    mt5_terminal_path: str | None = None

    # HTTP
    cors_origins: str = "http://localhost:3000,http://localhost:5173"
    app_version: str = "0.1.0"

    # Deployment: directory of the built SPA (Railway single-service image,
    # DECISIONS.md D-016). When set and present, FastAPI serves it at "/".
    static_dir: str | None = None

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def admin_email_list(self) -> list[str]:
        return [e.strip().lower() for e in self.admin_emails.split(",") if e.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
