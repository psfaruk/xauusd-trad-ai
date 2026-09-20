"""Tiny Supabase-auth-compatible mock for local browser e2e (dev only).

Implements the minimum supabase-js v2 surface:
- POST /auth/v1/token?grant_type=password -> session (any email/password)
- GET  /auth/v1/user                     -> user for any issued Bearer token
- POST /auth/v1/token?grant_type=refresh_token -> refreshed session
- POST /auth/v1/signup                   -> new distinct user per email

Each EMAIL maps to a deterministic, DISTINCT user id (uuid5) and a distinct
access token — so multi-user isolation (trading planes, WS user targeting)
is verifiable end-to-end in the dev stack. Run: python scripts/mock_supabase.py
(port 8090).
"""

from __future__ import annotations

import hashlib
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

# email -> user dict (created on first login/signup)
USERS: dict[str, dict] = {}
# token -> email
TOKENS: dict[str, str] = {}


def _user_for(email: str) -> dict:
    email = (email or "user@example.com").strip().lower()
    if email not in USERS:
        USERS[email] = {
            "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"mock-supabase:{email}")),
            "email": email,
            "user_metadata": {"full_name": email.split("@")[0].title()},
            "aud": "authenticated",
            "role": "authenticated",
        }
    return USERS[email]


def _token_for(email: str) -> str:
    email = (email or "user@example.com").strip().lower()
    token = f"good-token-{hashlib.sha1(email.encode()).hexdigest()[:12]}"
    TOKENS[token] = email
    return token


def _session(email: str) -> dict:
    return {
        "access_token": _token_for(email),
        "token_type": "bearer",
        "expires_in": 3600,
        "expires_at": 9999999999,
        "refresh_token": f"refresh-{hashlib.sha1(email.encode()).hexdigest()[:8]}",
        "user": _user_for(email),
    }


@app.post("/auth/v1/token")
async def token(request: Request):
    body = await request.json()
    grant = request.query_params.get("grant_type", "password")
    if grant == "refresh_token":
        rt = str(body.get("refresh_token", ""))
        # any refresh token -> resolve back to the issuing email deterministically
        for email in USERS:
            if _session(email)["refresh_token"] == rt:
                return _session(email)
        return {"error": "invalid refresh token"}
    if grant == "password" and not body.get("email"):
        return {"error": "missing email"}
    return _session(str(body.get("email", "")))


@app.get("/auth/v1/user")
async def user(request: Request):
    authz = request.headers.get("authorization", "")
    if not authz.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="invalid token")
    token = authz.removeprefix("Bearer ")
    email = TOKENS.get(token)
    if email is None:
        raise HTTPException(status_code=401, detail="invalid token")
    return _user_for(email)


@app.get("/auth/v1/settings")
async def settings():
    return {
        "external": {"email": True},
        "disable_signup": False,
        "mailer_autoconfirm": True,
        "phone_autoconfirm": True,
        "sms_provider": "",
    }


@app.post("/auth/v1/signup")
async def signup(request: Request):
    body = await request.json()
    return _session(str(body.get("email", "")))


@app.post("/auth/v1/logout")
async def logout():
    return {}


@app.get("/auth/v1/authorize")
async def authorize():
    return {"error": "oauth not supported in mock"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8090)
