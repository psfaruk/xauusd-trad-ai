"""Supabase JWT verification and role guards — implemented in Phase 1 (SPEC §7.1).

Phase 1 will verify the bearer token against `GET {SUPABASE_URL}/auth/v1/user`
(httpx, cached per token-hash, 60s TTL) and read the role from `profiles`.
"""

from fastapi import HTTPException, status


async def get_current_user(token: str | None = None) -> dict:
    """Dependency placeholder — returns 401-style error until Phase 1."""
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Authentication is implemented in Phase 1",
    )


async def require_admin(user: dict | None = None) -> dict:
    """Dependency placeholder — admin-only routes (SPEC §13)."""
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Authentication is implemented in Phase 1",
    )
