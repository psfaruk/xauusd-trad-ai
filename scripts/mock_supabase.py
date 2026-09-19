"""Tiny Supabase-auth-compatible mock for local browser e2e (dev only).

Implements the minimum supabase-js v2 surface:
- POST /auth/v1/token?grant_type=password -> session (any email/password)
- GET  /auth/v1/user                     -> user for Bearer good-token
- POST /auth/v1/token?grant_type=refresh_token -> refreshed session
Run: python scripts/mock_supabase.py  (port 8090)
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

USER = {
    "id": "11111111-1111-1111-1111-111111111111",
    "email": "trader@example.com",
    "user_metadata": {"full_name": "Test Trader"},
    "aud": "authenticated",
    "role": "authenticated",
}
SESSION = {
    "access_token": "good-token",
    "token_type": "bearer",
    "expires_in": 3600,
    "expires_at": 9999999999,
    "refresh_token": "refresh-1",
}


@app.post("/auth/v1/token")
async def token(request: Request):
    body = await request.json()
    grant = request.query_params.get("grant_type", "password")
    if grant == "password" and not body.get("email"):
        return {"error": "missing email"}
    return {**SESSION, "user": USER}


@app.get("/auth/v1/user")
async def user(request: Request):
    authz = request.headers.get("authorization", "")
    if authz != "Bearer good-token":
        from fastapi import HTTPException

        raise HTTPException(status_code=401, detail="invalid token")
    return USER


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
    return {**SESSION, "user": {**USER, "email": body.get("email", USER["email"])}}


@app.post("/auth/v1/logout")
async def logout():
    return {}


@app.get("/auth/v1/authorize")
async def authorize():
    return {"error": "oauth not supported in mock"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8090)
