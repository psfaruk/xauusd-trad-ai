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
| 2 | MT5 connection + chart | ✅ done (ACs verified below) |
| 3 | Signal engine + panel | ✅ done (ACs verified below) |
| 4 | Execution + risk | ⬜ next |
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

### Phase 2 — Acceptance Criteria verification

| AC (SPEC §12 Phase 2) | Result |
|---|---|
| Chart streams with `DATA_SOURCE=mock` | ✅ browser-verified end-to-end (login → dashboard → `/ws` open → live tick stream 2710.75→2711.34, forming bars via `bar_open`/`bar_update`); pytest `test_ws_market_stream` covers the same path headlessly |
| Gaps heal on reconnect | ✅ WS client (1s→30s backoff) re-subscribes + invalidates the candles query on `open`; server reconciles every `bar_close` against authoritative `get_rates` |
| Candles backfill `/api/candles` | ✅ all TFs M1..D1 (closed bars only, ascending, epoch-UTC) |
| Symbol auto-discovery (C3) | ✅ `*XAUUSD*` discovery — mock: `XAUUSDm`; real: shortest match (Exness suffix variants) |
| Connection manager: Fernet storage, heartbeat, auto-reconnect | ✅ encrypted `mt5_connections` row (when `FERNET_KEY` set), 5s heartbeat, reconnect restarts the runtime; verified by route tests |
| Broker UTC offset (C4) | ✅ detected at connect (30-min quantized), refreshed 2×/day; `get_rates` converts server→UTC; verified with a fake +2h Exness-style terminal |
| Real MT5 on Windows VPS | ⚠️ code complete + fake-MetaTrader5-tested on Linux (closed-bars-only, UTC conversion, FOK/IOC/RETURN filling, order mapping); live Exness demo verification needs the Windows VPS (C1) |

### Phase 3 — Acceptance Criteria verification

| AC (SPEC §12 Phase 3) | Result |
|---|---|
| Engine emits a signal with complete trace on injected sweep | ✅ `test_injected_sweep_emits_signal_with_full_trace` — 7 checks (trend_h1/sfp_sweep/rsi/atr/session/news/spread), SL/TP = wick ± 0.2·ATR, TP = 2R, confidence weights sum to 1.0 |
| No signal ever from a forming candle | ✅ `test_no_signal_from_forming_candle` (engine evaluates closed bars only) |
| Tracker transitions active→won/lost/expired | ✅ tick-based SL/TP + bar-close expiry with R math (won=+2R, lost=−1R, expired=±R fraction) |
| e2e pytest covers the full path | ✅ 134 tests green (indicators/sfp/filters hand-computed fixtures, engine e2e, market-stream reconciliation, WS routes, fake-MT5, backtest determinism) |
| SignalPanel renders trace ✓/✗ + history chips | ✅ SignalCard + TraceList + history with status/R chips, stats row (WR/total R/PF) |
| SettingsDialog edits engine_config (validated) | ✅ admin PUT `/api/config` (pydantic-validated, live engine reload) + auto-trade toggle with typed `ENABLE` confirmation |
| Backtest verification | ✅ `python -m app.engine.backtest` — mock seed 42, 2825 M15 bars: pure random-walk 22 sigs (WR 31.8%, PF 0.93 — noise, as expected); injected-SFP 26 sigs (**WR 46.2%, PF 1.71, +10.0R**) — the engine finds real sweeps and the RR-2 math holds |

## How the frontend connects to MetaTrader 5 (architecture)

The frontend **never talks to MT5 directly** — MT5 is a Windows terminal behind
the backend:

```
React SPA ── REST /api/candles · /api/mt5/* ──► FastAPI backend
          ── WS   /ws?token=…  (tick/bar/signal events)
                                                    │
                              DataSource (C7) ┌─────┴──────┐
                                DATA_SOURCE=mock │ MockDataSource (any OS, demo)
                                DATA_SOURCE=mt5  │ MT5DataSource  → MetaTrader5 pkg → Exness terminal (Windows only, C1)
```

1. Admin opens **MT5 connect** in the TopBar → `POST /api/mt5/connect`
   `{server, login, password, terminal_path?}`.
2. Backend `ConnectionManager.connect()` initializes the terminal, discovers the
   symbol (`*XAUUSD*` → e.g. `XAUUSDm`), stores the password Fernet-encrypted,
   and starts the **EngineRuntime**: tick loop → bar builder → engine + tracker.
3. The dashboard subscribes `/ws` `{channel:"market", symbol, tf}` and receives
   `tick` / `bar_open` / `bar_update` / `bar_close` events; signals arrive as
   global `signal` / `signal_update` events; `/api/candles` backfills history.
4. On Railway (Linux) the app runs `DATA_SOURCE=mock` (C1: MetaTrader5 is
   Windows-only) — real MT5 streaming requires the backend on a Windows VPS
   with the Exness terminal installed (README → Deploy).

## Repository layout

```
xauusd-platform/
├── SPEC.md  DECISIONS.md  .env.example  README.md
├── Dockerfile  railway.json  .dockerignore   # Railway single-service deploy
├── frontend/    # React 18 + TS + Vite SPA
└── backend/     # FastAPI, DATA_SOURCE=mock|mt5
```

## Backtest the engine (verification harness)

```bash
cd backend
source .venv/bin/activate

# deterministic 30-day mock history (2880 M15 bars)
python -m app.engine.backtest --bars 2880

# craft an SFP sweep every 150 bars — proves the engine fires on real patterns
python -m app.engine.backtest --bars 2880 --inject-every 150

# your own data: CSV with columns time_utc,o,h,l,c,v (e.g. MT5 export)
python -m app.engine.backtest --csv xauusd_m15.csv --json out.json --csv-out signals.csv
```

The backtest replays bars through the SAME `engine.evaluate()` pipeline as the
live engine (no lookahead — H1 context is sliced per bar-close), then simulates
the §8.4 tracker pessimistically (a bar touching both SL and TP counts as SL).
Stats: win rate, expectancy, profit factor, max drawdown (R), by-session split.

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
