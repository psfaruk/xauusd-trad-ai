# XAUUSD AI Trading Platform (v1)

Single-tenant web platform for XAUUSD (gold) trading analysis connected to an
MT5 account at Exness. **Source of truth: [SPEC.md](./SPEC.md)** — the AI agent
builds phase by phase per SPEC §12. Deviations and ambiguity resolutions are
recorded in [DECISIONS.md](./DECISIONS.md).

## Phase status

| Phase | Scope | Status |
|-------|-------|--------|
| 0 | Scaffold & infra | ✅ done (ACs verified below) |
| 1 | Auth (Supabase) | ✅ done (ACs verified below) |
| 2 | MT5 connection + chart | ⬜ next |
| 3 | Signal engine + panel | ⬜ |
| 4 | Execution + risk | ⬜ |
| 5 | Hardening + deploy | ⬜ |

### Phase 0 — Acceptance Criteria verification

| AC (SPEC §12 Phase 0) | Result |
|---|---|
| uvicorn boots | ✅ `scripts/start_backend.sh`, port 8000 |
| `GET /api/health` → 200 | ✅ `{"status":"ok","version":"0.1.0","data_source":"mock","db":false}` |
| Frontend dev server renders placeholder | ✅ Vite on :3000, browser-verified (dark/gold dashboard skeleton, live backend pill) |
| pytest green on non-Windows machine | ✅ 23 passed, 0 failed (ruff clean; no `MetaTrader5` import at module load — covered by test) |
| schema.sql applied idempotently | ✅ verified against embedded Postgres (pgserver) incl. §8.6 seed + `auto_trade=false` |

### Phase 1 — Acceptance Criteria verification

| AC (SPEC §12 Phase 1) | Result |
|---|---|
| `/api/me` → 401 without token | ✅ live on :8000 and via the Vite proxy (`{"detail":"Not authenticated"}`) |
| JWT verification via `GET {SUPABASE_URL}/auth/v1/user` | ✅ httpx, verdict cached per token-hash 60s; 401 on rejection; 503 fail-closed when Supabase is missing/unreachable |
| Role from `profiles`, ADMIN_EMAILS promoted | ✅ startup promotion + defensive promotion on every lookup, admins never demoted; verified against embedded Postgres |
| admin vs viewer enforced by test | ✅ `require_admin` → 403 for viewers (test_auth.py); `/api/health` stays public |
| register/login/google/reset flows in browser | ⚠️ code complete (Login: email+password, sign-up, Google OAuth, forgot-password; ResetPassword: recovery email + set-new-password). End-to-end browser verification requires a real Supabase project (URL + anon key + Google provider + redirect URLs) — configure `frontend/.env` + `backend/.env`, then run through the flows. |

> Auth degraded-mode note: without Supabase env vars the API fails **closed**
> (401/503) — it never serves data without a verified token.

## Repository layout

```
xauusd-platform/
├── SPEC.md  DECISIONS.md  .env.example  README.md
├── Dockerfile  railway.json  .dockerignore   # Railway single-service deploy
├── frontend/    # React 18 + TS + Vite SPA
└── backend/     # FastAPI, DATA_SOURCE=mock|mt5
```

## Dev quickstart (mock mode — works on any OS, no MT5 needed)

### Backend (Python 3.11+)

```bash
cd backend
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt   # prod deps + pytest/ruff/pgserver
cp .env.example .env             # DATA_SOURCE=mock needs nothing else
uvicorn app.main:app --reload --port 8000
# health check: http://localhost:8000/api/health
# auth check:   http://localhost:8000/api/me -> 401 without a token
```

Without `DATABASE_URL` the backend boots in degraded mode (no persistence) and
logs a warning — see DECISIONS.md D-002. Point `DATABASE_URL` at Supabase
Postgres for full operation.

### Frontend (Node 18+)

```bash
cd frontend
npm install
cp .env.example .env
npm run dev                      # http://localhost:3000 (/api proxied to :8000)
```

### Tests / lint (backend)

```bash
cd backend
source .venv/bin/activate
pytest        # 33 tests; DB tests use TEST_DATABASE_URL/DATABASE_URL (Postgres)
              # or an embedded Postgres via pgserver; skip when neither exists
ruff check .
```

## Deploy to Railway (mock mode)

The repo ships a **single-service Docker image**: one uvicorn process serves
the REST API (`/api/*`), the WebSocket (`/ws`, Phase 2) **and** the built SPA
(`STATIC_DIR`, D-016) — same-origin, no CORS needed. It runs `DATA_SOURCE=mock`
(SPEC C1/C7: MetaTrader5 is Windows-only; real trading targets the Windows VPS
per SPEC §14).

1. **Create the project** — <https://railway.app> → New Project → *Deploy from
   GitHub repo* (root = repo root; the `Dockerfile` + `railway.json` are picked
   up automatically), or CLI:
   ```bash
   npm i -g @railway/cli && railway login
   railway init
   ```
2. **Add Postgres** — `railway add --database postgres` (or Dashboard →
   New → Database). Railway injects `DATABASE_URL` into the service when you
   reference it: `railway variables set DATABASE_URL="${{Postgres.DATABASE_URL}}"`
   (internal URL, no TLS needed — db.py also understands `?sslmode=require`).
   *Alternatively* point `DATABASE_URL` at your Supabase Postgres direct
   connection string — then signups also run the `handle_new_user` trigger.
3. **Set service variables** (Dashboard → Variables, or `railway variables set`):
   ```bash
   SUPABASE_URL=https://<ref>.supabase.co
   SUPABASE_ANON_KEY=<anon public key>
   ADMIN_EMAILS=you@example.com
   FERNET_KEY=<python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())">
   DATABASE_URL="${{Postgres.DATABASE_URL}}"
   DATA_SOURCE=mock
   # SPA build args (Railway passes service vars as Docker build args):
   VITE_SUPABASE_URL=https://<ref>.supabase.co
   VITE_SUPABASE_ANON_KEY=<anon public key>
   # leave VITE_API_URL empty -> same-origin /api + /ws
   ```
   Changing a `VITE_*` variable triggers a rebuild (the values are baked into
   the SPA at build time).
4. **Deploy** — `railway up` (or push to the connected branch).
5. **Generate a domain** — Settings → Networking → Generate Domain. Railway
   handles WebSockets on the same domain (needed from Phase 2).
6. **Supabase redirect URLs** — Supabase Dashboard → Authentication → URL
   Configuration: add `https://<your-app>.up.railway.app/**` to Redirect URLs
   so Google OAuth and password-recovery links land correctly.

Health check: `https://<your-app>.up.railway.app/api/health`. Auth check:
`/api/me` must answer 401 without a token.

## Safety

`auto_trade` defaults to OFF everywhere (SPEC §0.6). v1 targets a **demo**
account only (SPEC C8).
