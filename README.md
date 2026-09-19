# XAUUSD AI Trading Platform (v1)

Single-tenant web platform for XAUUSD (gold) trading analysis connected to an
MT5 account at Exness. **Source of truth: [SPEC.md](./SPEC.md)** — the AI agent
builds phase by phase per SPEC §12. Deviations and ambiguity resolutions are
recorded in [DECISIONS.md](./DECISIONS.md).

## Phase status

| Phase | Scope | Status |
|-------|-------|--------|
| 0 | Scaffold & infra | ✅ done (ACs verified) |
| 1 | Auth (Supabase) | ⬜ next |
| 2 | MT5 connection + chart | ⬜ |
| 3 | Signal engine + panel | ⬜ |
| 4 | Execution + risk | ⬜ |
| 5 | Hardening + deploy | ⬜ |

## Repository layout

```
xauusd-platform/
├── SPEC.md  DECISIONS.md  .env.example  README.md
├── frontend/    # React 18 + TS + Vite SPA (Vercel target)
└── backend/     # FastAPI (Windows VPS target), DATA_SOURCE=mock|mt5
```

## Dev quickstart (mock mode — works on any OS, no MT5 needed)

### Backend (Python 3.11+)

```bash
cd backend
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # DATA_SOURCE=mock needs nothing else
uvicorn app.main:app --reload --port 8000
# health check: http://localhost:8000/api/health
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
pytest                           # schema test auto-skips without a reachable Postgres
ruff check .
```

## Safety

`auto_trade` defaults to OFF everywhere (SPEC §0.6). v1 targets a **demo**
account only (SPEC C8).
