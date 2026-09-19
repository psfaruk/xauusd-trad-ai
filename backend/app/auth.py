"""Supabase JWT verification and role guards (SPEC §7.1, Phase 1).

The backend never validates JWT signatures itself — it forwards the bearer
token to `GET {SUPABASE_URL}/auth/v1/user` (httpx) and caches the verdict per
token-hash for 60s (SPEC §12 Phase 1). Roles live in `profiles`;
ADMIN_EMAILS are promoted at startup (db.promote_admins) and re-applied
defensively on every lookup (never demoting an existing admin).

Degraded behaviour (SPEC C6 spirit — fail closed, never crash):
- Supabase missing / unreachable  -> 503 AuthUnavailable
- DB unreachable                  -> role falls back to the ADMIN_EMAILS check
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Annotated

import httpx
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import text

from app.config import get_settings

logger = logging.getLogger("xauusd.auth")

CACHE_TTL_S = 60.0  # SPEC §12 Phase 1: cache per token-hash, 60s TTL
_cache: dict[str, tuple[float, dict | None]] = {}
_bearer = HTTPBearer(auto_error=False)

# Dependency singletons (ruff B008): Annotated is resolved by FastAPI at
# request time — see get_current_user / require_admin below.
CredsDep = Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]


def clear_auth_cache() -> None:
    """Drop all cached token verdicts (test helper)."""
    _cache.clear()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class AuthUnavailable(HTTPException):
    """503 — Supabase missing/unreachable. Fail closed: never let requests through."""

    def __init__(self, detail: str = "Auth backend unavailable") -> None:
        super().__init__(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail)


async def verify_supabase_token(http: httpx.AsyncClient, token: str) -> dict | None:
    """Verify a bearer token against Supabase; result cached 60s per token-hash.

    Returns the Supabase user dict on success, None on rejection (both cached).
    Raises AuthUnavailable when Supabase is not configured or unreachable.
    """
    key = _token_hash(token)
    now = time.monotonic()
    hit = _cache.get(key)
    if hit is not None and hit[0] > now:
        return hit[1]

    settings = get_settings()
    if not settings.supabase_url or not settings.supabase_anon_key:
        raise AuthUnavailable("Supabase not configured (SUPABASE_URL / SUPABASE_ANON_KEY)")

    try:
        resp = await http.get(
            f"{settings.supabase_url.rstrip('/')}/auth/v1/user",
            headers={
                "apikey": settings.supabase_anon_key,
                "Authorization": f"Bearer {token}",
            },
        )
    except httpx.HTTPError as exc:
        logger.warning("Supabase auth check failed: %s", exc)
        raise AuthUnavailable() from exc

    user: dict | None = None
    if resp.status_code == 200:
        try:
            body = resp.json()
        except ValueError:
            body = None
        if isinstance(body, dict) and body.get("id"):
            user = body
    else:
        logger.info("Supabase rejected token: HTTP %d", resp.status_code)

    _cache[key] = (now + CACHE_TTL_S, user)
    return user


async def resolve_role(request: Request, user_id: str, email: str | None) -> str:
    """Resolve the role from `profiles` (SPEC §7.1).

    - missing profile row -> created with the ADMIN_EMAILS-derived role
    - ADMIN_EMAILS member -> always promoted to admin
    - existing admin is never demoted
    Falls back to the email check when the DB is unavailable (degraded mode).
    """
    settings = request.app.state.settings
    email_norm = (email or "").strip().lower()
    role = "admin" if email_norm in settings.admin_email_list else "viewer"

    engine = getattr(request.app.state, "db_engine", None)
    if engine is None:
        return role
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    insert into profiles (id, display_name, role)
                    values (:uid, :name, :role)
                    on conflict (id) do update
                        set role = case
                            when profiles.role = 'admin' then 'admin'
                            else :role end
                    """
                ),
                {"uid": user_id, "name": email_norm or None, "role": role},
            )
            stored = (
                await conn.execute(
                    text("select role from profiles where id = :uid"), {"uid": user_id}
                )
            ).scalar()
        return stored or role
    except Exception as exc:  # noqa: BLE001 — degrade, never crash (C6 spirit)
        logger.warning("role lookup failed, using fallback '%s': %s", role, exc)
        return role


async def get_current_user(
    request: Request,
    creds: CredsDep = None,
) -> dict:
    """Dependency for every /api route except /api/health (SPEC §7.1)."""
    if creds is None or not creds.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated"
        )
    http: httpx.AsyncClient | None = getattr(request.app.state, "http", None)
    if http is None:
        raise AuthUnavailable()
    user = await verify_supabase_token(http, creds.credentials)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token"
        )
    user_id = str(user["id"])
    email = user.get("email")
    role = await resolve_role(request, user_id, email)
    metadata = user.get("user_metadata") or {}
    return {
        "id": user_id,
        "email": email,
        "display_name": metadata.get("full_name") or email,
        "role": role,
    }


CurrentUser = Annotated[dict, Depends(get_current_user)]


async def require_admin(user: CurrentUser) -> dict:
    """Dependency for admin-only routes (mt5/connect, PUT /config, auto-trade…)."""
    if user["role"] != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Admin role required"
        )
    return user


AdminUser = Annotated[dict, Depends(require_admin)]
