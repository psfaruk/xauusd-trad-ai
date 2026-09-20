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
| 4 | Execution + risk | ✅ done (ACs verified below) |
| 5 | Hardening + deploy | ✅ done (security checklist below) |
| — | **Live real-time data** (D-030) | ✅ `DATA_SOURCE=live` default — free real-time gold APIs (Binance PAXG), verified end-to-end |

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

### Phase 4 — Acceptance Criteria verification

| AC (SPEC §12 Phase 4) | Result |
|---|---|
| Correct lot per formula (unit-test the math) | ✅ `test_lot_sizing_*` — `lots = risk/(sl_dist × 100)`, floor to volume_step, clamp to broker limits, fixed-lot mode, degenerate-SL guard; risk-sim test reproduces a hand-computed 0.25-lot / +$100 / −$50 sequence exactly |
| SL/TP attached, magic set, zero duplicates per signal | ✅ executor attaches signal SL/TP + `magic=cfg.magic` + `comment=xauai-{signal_id}`; `test_executor_idempotent_per_signal` proves the second relay is skipped (trades-table pre-check) |
| Daily-loss kill switch verified in simulation | ✅ `test_executor_daily_loss_emergency_stop` (close-all + disarm + CRITICAL log — incl. the fix that the emergency fires even when max_positions would skip) and `test_risk_sim_kill_switch_fires_and_skips_rest` (backtest replay: 5% loss ≥ 3% limit → 1 trade taken, 3 skipped) |
| Kill switch disables auto-trade | ✅ per-user arm and the platform arm both flip to false; AccountStrip shows the live arm state |
| Stats numbers match manual calculation | ✅ risk-sim P/L = result_r × sl_distance × contract × lots — unit-tested to the cent |
| PerfReport UI | ✅ SignalPanel → performance tab (WR/expectancy/PF/maxDD-R/total-R/by-session from `/api/stats`) |
| LogViewer + CSV export | ✅ `/api/logs` + `export.csv` (authorized blob download); level filter + 10s live tail |
| Auto-trade toggle with typed confirmation | ✅ platform (admin, `/api/config/auto-trade`) AND per-user (`/api/trading/auto-trade`) — both require `confirm:"ENABLE"`; browser-verified ARM/disarm |
| Multi-user trading planes (user req #1/#5 agent flow) | ✅ `UserTradingManager` — per-owner isolated planes, Fernet-encrypted per-owner credentials, masked logins (`10••••34`), WS events user-targeted (`test_user_plane_connect_and_isolation`, browser WS isolation e2e: owner receives `trading_account`, other user receives NOTHING) |
| Trades table linkage | ✅ `trades.signal_id` ↔ signals; history shows AI-signal vs manual source chips |
| Free external data APIs (user req #6) | ✅ `/api/market/external` — Binance PAXG (live-verified $4,361) + Frankfurter/ECB FX, all fail-soft (C6); Market Data tab renders references live |
| 3-dot menu grouped functions (user req #3) | ✅ Trading / Analysis / Settings / Account groups, outside-click + Esc; browser-verified |

### Phase 5 — Hardening (security checklist §13)

| Item | Result |
|---|---|
| MT5 passwords Fernet-encrypted, never logged, masked on return | ✅ per-owner encryption; only `login_masked` ever leaves the backend (SPEC §13); tests assert the raw login never appears in responses |
| All routes JWT-guarded except `/api/health`; admin-only routes enforced | ✅ mt5 connect/disconnect, PUT /config, platform auto-trade → admin; trading routes owner-scoped |
| slowapi rate limits | ✅ 240/min global per client IP (X-Forwarded-For aware) + 30/min order/close + 10/min arm budgets; real-app 429 test |
| Input validation on write endpoints | ✅ pydantic bodies everywhere (trading connect/order/auto-trade, config PUT rejects unknown keys + sane ranges) |
| No secrets in git | ✅ `.env` gitignored; only `.env.example` committed |
| 48h unattended run | ⚠️ needs a deployed environment (Railway/Windows VPS) — watchdogs, heartbeats, reconnect loops and degraded-mode fallbacks are in place and tested; the long-run soak is the only remaining operational check |

## How the frontend connects to MetaTrader 5 (architecture)

The frontend **never talks to MT5 directly** — MT5 is a Windows terminal behind
the backend:

```
React SPA ── REST /api/candles · /api/mt5/* ──► FastAPI backend
          ── WS   /ws?token=…  (tick/bar/signal events)
                                                    │
                              DataSource (C7) ┌─────┴──────────────────────────┐
                                DATA_SOURCE=live │ LiveDataSource — FREE real-time gold APIs (DEFAULT, D-030)
                                                  │   Binance PAXG/USDT → gold-api.com → Yahoo GC=F
                                DATA_SOURCE=mock │ MockDataSource (synthetic demo, any OS)
                                DATA_SOURCE=mt5  │ MT5DataSource  → MetaTrader5 pkg → Exness terminal (Windows only, C1)
```

**Real-account trading via the terminal's built-in MCP server (D-034).** The
backend also speaks to a LIVE MetaTrader 5 terminal directly — on the sandbox
host the genuine terminal (build 6000+) runs under user-space Wine
(`/home/z/mt5stack`, no root needed) logged into the Exness account, and its
built-in MCP server (`127.0.0.1:22346`, bearer-key auth) serves account info,
positions, history, symbols and ORDER EXECUTION (`app/mt5/mcp.py`). The 3-dot
menu → **MT5 Account (live)** panel shows real balance / equity / floating
P/L, open positions (with one-click close), the 90-day trade history and a
market-order tab — every order is executed by the real MetaTrader 5 terminal,
never simulated. The watchdog supervises Xvfb + openbox + the terminal and
skips the whole stack automatically where it is not installed (e.g. Railway,
which keeps serving real-time market DATA only).

**MT5 = the primary MARKET-DATA source too (D-035).** While the broker feed
for a symbol is ticking (market open), quotes AND candles come from the real
forex market through the terminal (1s tick poll + authoritative M1..D1 chart
history). When forex closes (weekend) or the terminal is down, the platform
transparently falls back to the 24/7 crypto composite and switches back to
the broker feed automatically at the Monday open — the badge always shows
which source is live (`LIVE · MT5 · broker feed` vs `LIVE · crypto composite`).

**AI signal → auto-order on the REAL account (D-036).** The AI engines
(XAUUSD + BTCUSD, M15 SFP pipeline) can now place REAL orders automatically
through the MetaTrader 5 terminal: ⋮ → **MT5 Account (live)** → **AI AUTO**
tab → type `ENABLE` → ARM. Every armed signal runs the unchanged §9 risk
core against the real account (lot size from real equity + the signal's SL
distance, idempotent per signal, max-positions/spread/daily-loss kill
switches — daily loss closes everything and disarms), sends the order with
broker-side SL/TP + an `xauai-<signal>` comment, and broadcasts a live
`mt5_auto` event feed (orders, honest skips — e.g. forex weekends — and
closes). Signal expiry closes the terminal position; won/lost reconcile
automatically (broker-side SL/TP). The arm is a separate, explicit switch
from the paper `auto_trade` toggle, persists in `engine_config`, is refused
honestly (409) when the terminal is offline, and shows as a red pulsing
`● AI AUTO ARMED` chip in the dashboard footer while active. Orders are
NEVER queued: terminal down or market closed → the signal is kept and the
skip is reported. **BTCUSD is a full second pair** (own chart, ticks,
candles, signal engine, trading); XAUUSD ↔ BTCUSD switch with the instrument
chips next to the timeframe switcher, and the real MT5 account balance /
equity / open positions are always visible in the dashboard footer.

**LIVE mode (default, D-030/D-033/D-035) — MT5 terminal first, crypto
composite 24/7.** The backend pushes EVERY real market event the instant it
happens — tens of ticks per second in active sessions; the forming candle
absorbs all of them:

- **Five-venue WS aggregate (D-033, primary)** — Binance `PAXG/USDT`+
  `PAXG/USDC` (bookTicker/aggTrade/depth@100ms via `data-stream.binance.vision`,
  geo-robust), Bybit `XAUT/USDT`, OKX `PAXG/USDT`+`XAUT/USDT`, Kraken `PAXG/USD`,
  Coinbase `PAXG-USD`. PAXG/XAUT each redeem for exactly 1 troy ounce of
  physical gold, so all five venues price the same underlying as spot XAUUSD.
  Consolidated best bid/ask across venues; per-venue health + a real
  **ticks/sec meter** surface in the TopBar LIVE badge and `/api/health`.
- **Binance `PAXG/USDT` REST** (quotes + authoritative `klines` OHLCV when WS
  is silent).
- **gold-api.com XAU spot** (quote fallback when Binance is geo-blocked).
- **Yahoo Finance `GC=F`** (history fallback — COMEX gold futures candles).
- **Tick-built candles** (last resort — bars aggregate live from quotes).

**NO demo fallback (D-033, user directive).** If every provider is unreachable
the platform shows an honest "no feed / retrying" state and keeps retrying
forever — it NEVER substitutes synthetic prices. Demo data exists only for
local development behind `ALLOW_DEMO=1` (never set in the Docker image), so a
deployment physically cannot display fake prices. The active provider + price
age + t/s rate is shown in the TopBar **LIVE badge**, the MT5 status (`feed`
block) and the **Market Data tab** (basis vs spot XAU is disclosed there too).
Demo/paper trading planes fill at the SAME live prices.

1. Admin opens **MT5 connect** in the 3-dot menu (Settings → Platform MT5) →
   `POST /api/mt5/connect` `{server, login, password, terminal_path?}`.
2. Backend `ConnectionManager.connect()` initializes the terminal, discovers the
   symbol (`*XAUUSD*` → e.g. `XAUUSDm`), stores the password Fernet-encrypted,
   and starts the **EngineRuntime**: tick loop → bar builder → engine + tracker.
3. The dashboard subscribes `/ws` `{channel:"market", symbol, tf}` and receives
   `tick` / `bar_open` / `bar_update` / `bar_close` events; signals arrive as
   global `signal` / `signal_update` events; `/api/candles` backfills history.
4. On Railway (Linux) the app runs `DATA_SOURCE=live` (D-030): real-time gold
   data from free APIs. Real MT5 *execution* works wherever the MT5 terminal
   runs — natively on Windows, or rootless under Wine on Linux (D-034,
   `/home/z/mt5stack`) — with the terminal's MCP server bridging the platform
   to the broker. Where no terminal exists (Railway), trading endpoints
   honestly report the bridge as offline instead of simulating.

## Multi-user trading: the agent architecture (Phase 4)

Everyone watches the SAME public market feed (candles/ticks/signals). A user
who wants to trade connects their OWN MT5/Exness account and gets an isolated
**trading plane**:

```
                        ┌── EngineRuntime ── SFP signal ──┐
public feed (admin MT5)─┤                                  ▼ relay
                        └── WS broadcast (everyone)   UserTradingManager
                                                          │
                             ┌────────────────────────────┼────────────────────┐
                             ▼                            ▼                    ▼
                     user A's plane               user B's plane        admin platform plane
                     (demo sibling mock)          (demo sibling mock)   (the admin's own account)
                     OrderExecutor §9             OrderExecutor §9      OrderExecutor §9
                     kill switches + lots         …                     auto_trade flag
                     trades(owner=A)              trades(owner=B)       trades(owner=admin)
                             │                            │
                             └── WS trading_account / trading_log → ONLY that user
```

- **Demo mode (works everywhere incl. Railway):** the plane is a paper account
  ($10,000) priced off the SAME deterministic market as the public chart
  (`MockDataSource.sibling` — identical seed/origin/clock, so positions mark
  against exactly the candles everyone sees).
- **Live mode:** credentials are stored encrypted with status
  `bridge_required` on Linux — per-user LIVE execution needs the Windows MT5
  bridge (one worker process per user, v2 scope — DECISIONS D-024). The
  platform NEVER silently simulates live trading.
- **Copy-trading agent:** when a user arms auto-trade (typed `ENABLE`), every
  engine signal is executed on their plane with THEIR equity via the same
  §9 sizing + kill switches — idempotent per signal_id.
- **Isolation:** credentials, positions, trades and WS events are strictly
  owner-scoped; logins are masked (`10••••34`) and passwords are never
  returned, logged, or shared between planes.

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

## Deploy to Railway (live mode — real-time data)

The repo ships a **single-service Docker image**: one uvicorn process serves
the REST API (`/api/*`), the WebSocket (`/ws`, Phase 2) **and** the built SPA
(`STATIC_DIR`, D-016) — same-origin, no CORS needed. It runs `DATA_SOURCE=live`
(D-030): REAL-TIME gold prices stream from free key-less public APIs (Binance
PAXG primary). Real MT5 *execution* still targets the Windows VPS per SPEC §14
(C1: the MetaTrader5 package is Windows-only).

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
   # DATA_SOURCE defaults to "live" in the image (D-030) — you can omit it.
   # SPA build args (Railway passes service vars as Docker build args):
   VITE_SUPABASE_URL=https://<ref>.supabase.co
   VITE_SUPABASE_ANON_KEY=<anon public key>
   # leave VITE_API_URL empty -> same-origin /api + /ws
   ```
   Changing a `VITE_*` variable triggers a rebuild (the values are baked into
   the SPA at build time).

   ⚠️ **Deployed BEFORE D-030 (Sept 2026)? REMOVE the stale `DATA_SOURCE=mock`
   variable** — Railway variables persist across deploys and OVERRIDE the
   image default, which silently keeps the platform on synthetic demo prices
   (~2715) while the code is fully live-capable. Same for a missing
   `DATABASE_URL`: `/api/health` then reports `"db": false` (in-memory data
   only — trades/signals/config do not survive restarts).
4. **Deploy** — `railway up` (or push to the connected branch).
5. **Generate a domain** — Settings → Networking → Generate Domain. Railway
   handles WebSockets on the same domain (needed from Phase 2).
6. **Supabase redirect URLs** — Supabase Dashboard → Authentication → URL
   Configuration: add `https://<your-app>.up.railway.app/**` to Redirect URLs
   so Google OAuth and password-recovery links land correctly.

Health check: `https://<your-app>.up.railway.app/api/health`. Auth check:
`/api/me` must answer 401 without a token.

## Safety

`auto_trade` defaults to OFF everywhere (SPEC §0.6). The D-036 live
auto-execution arm (`auto_trade_live`) is a SEPARATE switch, also defaults
to OFF, requires the admin to type `ENABLE`, and is refused while the
terminal is offline. Kill switches (daily loss −3%, max positions, max
spread) guard every real order; the daily-loss emergency closes all
terminal positions and disarms automatically. v1 targets a **demo**
account only (SPEC C8).
