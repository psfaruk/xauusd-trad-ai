# SPEC.md — XAUUSD AI Trading Web Platform (v1)

> Master build specification. The AI agent MUST follow this document.
> Build phase by phase. Do NOT skip ahead. Verify Acceptance Criteria before
> declaring any phase complete. Record any deviation in DECISIONS.md.

---

## 0. HOW TO USE THIS DOCUMENT (for the AI agent)

1. Work on exactly ONE phase at a time (see §12). Stop after each phase and report.
2. Every phase has **Acceptance Criteria** — all must pass before moving on.
3. Follow the tech stack in §4 exactly. No substitutions without logging in DECISIONS.md.
4. All code, comments, commit messages in English. Conventional commits
   (`feat:`, `fix:`, `chore:`), one commit per completed task.
5. If a requirement is ambiguous, choose the simplest option consistent with this
   spec, then record the decision in DECISIONS.md.
6. Safety rule: auto-trading code must default OFF everywhere. Never place a live
   order unless `engine_config.auto_trade == true` explicitly.

---

## 1. PRODUCT SUMMARY

A single-tenant web platform for XAUUSD (gold) trading analysis, connected to the
user's MT5 account at **Exness** broker.

Core features (v1):
- Login via Google OAuth and email/password, with password reset (Supabase Auth).
- Frontend connects/manages the MT5 connection (server, login, password).
- Live lightweight candlestick chart, multiple timeframes (M1…D1), real-time ticks.
- Server-side **signal engine**: analyzes data on every bar close and emits signals
  with a full **decision trace** (which data was checked, values, pass/fail).
- **Signal panel**: current + historical signals, each expandable to show its trace.
- Optional **auto-trading** on the connected MT5 account (default OFF) with risk
  management and kill switches.
- **3-dot menu** groups: settings, MT5 connection manager, auto-trade toggle,
  performance report, log viewer + CSV export, theme, logout.

Explicitly OUT of scope (v2 backlog, do not build now):
- Multi-user "bring your own MT5 account" agent-app model
- Backtesting UI, ML models
- CME futures orderflow (Databento/Rithmic)
- Telegram/email alerts, mobile apps

---

## 2. CRITICAL CONSTRAINTS (READ FIRST)

- **C1.** Python package `MetaTrader5` runs ONLY on Windows and only talks to an
  MT5 terminal installed on the SAME machine. Therefore: backend runs on a
  **Windows VPS**. Frontend is hosted separately (Vercel). DB/Auth on Supabase.
- **C2.** One MT5 terminal = one logged-in account. v1 = single account.
- **C3.** Exness symbol names vary by account type (`XAUUSD`, `XAUUSDm`,
  `XAUUSDz`...). NEVER hardcode. Discover via `mt5.symbols_get("*XAUUSD*")`,
  store the chosen symbol in `mt5_connections.symbol`.
- **C4.** MT5 server time is usually UTC+2/+3 (EET, DST-aware). Engine logic uses
  UTC only. On connect, detect broker UTC offset (compare latest tick time vs UTC),
  store it, re-detect once per day.
- **C5.** Filling mode differs per broker/symbol (IOC/FOK/RETURN). Read
  `symbol_info().filling_mode` and map to the order request. Never hardcode.
- **C6.** External free APIs have rate limits → all external calls go through an
  in-memory cache (TTL) + rate limiter. On external API failure: degrade
  gracefully (e.g., skip news filter, log warning). NEVER crash the engine.
- **C7. (Portability)** The dev machine may not be Windows. All engine/data code
  depends on a `DataSource` interface (§8.1) with two implementations:
  `MT5DataSource` (real) and `MockDataSource` (deterministic synthetic candles).
  Everything except real order placement must be fully testable with Mock.
  Selected via env var `DATA_SOURCE=mock|mt5`.
- **C8.** v1 target = **demo account only**. Live accounts only after Phase 5 QA.

---

## 3. ARCHITECTURE

```
┌───────────────────────────────────────────────┐
│ FRONTEND (React SPA, hosted on Vercel)        │
│ Login • Chart • Signal Panel • Dot Menu       │
└──────────────┬────────────────────────────────┘
               │ HTTPS REST  +  WSS realtime
┌──────────────▼────────────────────────────────┐
│ BACKEND (FastAPI, single process, Windows VPS)│
│  ├─ auth.py        (verify Supabase JWT)      │
│  ├─ api/           (REST routes + WS hub)     │
│  ├─ engine/        (signal engine, tracker,   │
│  │                  executor, risk)           │
│  ├─ mt5/           (DataSource: MT5 / Mock)   │
│  └─ services/      (news calendar, ext data)  │
└───┬───────────────┬───────────────────────────┘
    │               │
┌───▼──────────┐ ┌──▼───────────────────────────┐
│ SUPABASE     │ │ MT5 TERMINAL (Exness demo)   │
│ Postgres +   │ │  ▲ same machine as backend   │
│ Auth (Google,│ │ MetaTrader5 Python package   │
│ email,reset) │ └──────────────────────────────┘
└──────────────┘
    ▲
    │ (cached, rate-limited)
┌───┴──────────────────────────────┐
│ EXTERNAL: FMP economic calendar, │
│ yfinance (backup/history data)   │
└──────────────────────────────────┘
```

Data flow:
1. **Tick loop** (MT5 or Mock) → WS broadcast `tick` + `bar_update` → chart.
2. **Bar-close detector** (per timeframe) → `engine.on_bar_close(tf)` → if signal:
   insert into `signals` (with trace JSON) → WS broadcast `signal` → panel.
3. **Result tracker**: for each `active` signal, on every tick compare price to
   SL/TP → update status (`won`/`lost`/`expired`) → WS broadcast `signal_update`.
4. **Executor** (only if auto_trade ON): risk sizing → `order_send` → insert
   `trades` row linked to signal → account updates via WS.

---

## 4. TECH STACK (exact — do not substitute)

**Frontend** (`frontend/`)
- React 18 + TypeScript + Vite
- `lightweight-charts` **^4.1** (v4 API: `createChart`, `chart.addCandlestickSeries()`,
  `series.update()`, `series.createPriceLine()`. Do NOT use v5.)
- TailwindCSS (dark theme default, gold accent #d4af37)
- `@supabase/supabase-js` (auth flows)
- `@tanstack/react-query` for REST; native `WebSocket` for realtime

**Backend** (`backend/`, Python 3.11+)
- fastapi, uvicorn[standard], pydantic v2, pydantic-settings
- `MetaTrader5` (install only on Windows; code must import lazily so non-Windows
  dev machines can run with MockDataSource)
- pandas, numpy (indicators)
- SQLAlchemy 2 async + asyncpg → Supabase Postgres via `DATABASE_URL`
- httpx (external APIs + Supabase token verification)
- cryptography (Fernet — encrypt MT5 passwords at rest)
- slowapi (basic REST rate limit), pytest, ruff

**Infra**
- Supabase: Auth (Google + email + reset) + Postgres
- Frontend hosting: Vercel
- Backend: Windows VPS (2–4GB RAM), run uvicorn as a service (NSSM)

---

## 5. REPOSITORY STRUCTURE

```
xauusd-platform/
├── SPEC.md  DECISIONS.md  .env.example  README.md
├── frontend/
│   └── src/
│       ├── main.tsx  App.tsx  types.ts
│       ├── lib/       supabase.ts  api.ts  ws.ts
│       ├── pages/     Login.tsx  ResetPassword.tsx  Dashboard.tsx
│       └── components/
│           TopBar.tsx  DotMenu.tsx  Chart.tsx  TimeframeSwitcher.tsx
│           SignalPanel.tsx  SignalCard.tsx  TraceList.tsx
│           AccountStrip.tsx  Mt5ConnectDialog.tsx  SettingsDialog.tsx
│           PerfReport.tsx  LogViewer.tsx
└── backend/
    ├── app/
    │   ├── main.py  config.py  auth.py  db.py  schema.sql
    │   ├── mt5/      base.py  mt5_source.py  mock_source.py  connection.py
    │   ├── engine/   indicators.py  sfp.py  filters.py  engine.py
    │   │             trace.py  tracker.py  executor.py  risk.py
    │   ├── services/ news.py  extdata.py
    │   └── api/      routes_mt5.py  routes_market.py  routes_signals.py
    │                 routes_config.py  routes_stats.py  routes_logs.py  ws.py
    └── tests/        test_indicators.py  test_sfp.py  test_filters.py
                      test_risk.py  test_auth.py  test_engine_e2e.py
```

---

## 6. DATABASE SCHEMA (`schema.sql`, applied idempotently on backend start)

```sql
create table if not exists profiles (
  id uuid primary key references auth.users(id) on delete cascade,
  display_name text,
  role text not null default 'viewer' check (role in ('admin','viewer')),
  created_at timestamptz not null default now()
);

create table if not exists mt5_connections (
  id uuid primary key default gen_random_uuid(),
  owner uuid not null references profiles(id),
  server text not null,
  login text not null,
  enc_password text not null,              -- Fernet-encrypted
  terminal_path text,
  symbol text,                             -- discovered, e.g. XAUUSDm
  status text not null default 'disconnected',
  last_heartbeat timestamptz,
  created_at timestamptz not null default now()
);

create table if not exists engine_config (
  id int primary key default 1 check (id = 1),   -- single row
  config jsonb not null,
  auto_trade boolean not null default false,
  updated_by uuid references profiles(id),
  updated_at timestamptz not null default now()
);

create table if not exists signals (
  id uuid primary key default gen_random_uuid(),
  ts timestamptz not null,
  symbol text not null,
  tf text not null default 'M15',
  direction text not null check (direction in ('BUY','SELL')),
  entry double precision not null,
  sl double precision not null,
  tp double precision not null,
  confidence double precision not null,
  trace jsonb not null,
  status text not null default 'active'
    check (status in ('active','won','lost','expired','cancelled')),
  result_r double precision,
  closed_at timestamptz,
  created_at timestamptz not null default now()
);
create index if not exists signals_ts_idx on signals (ts desc);

create table if not exists trades (
  id uuid primary key default gen_random_uuid(),
  signal_id uuid references signals(id),
  owner uuid references profiles(id),
  ticket bigint unique,
  side text not null,
  volume double precision not null,
  price_open double precision not null,
  sl double precision, tp double precision,
  price_close double precision,
  profit double precision,
  opened_at timestamptz, closed_at timestamptz
);

create table if not exists logs (
  id bigint generated always as identity primary key,
  ts timestamptz not null default now(),
  level text not null,
  source text not null,
  message text not null,
  meta jsonb
);
create index if not exists logs_ts_idx on logs (ts desc);

-- Auto-create profile on signup (Supabase trigger)
create or replace function public.handle_new_user()
returns trigger language plpgsql security definer as $$
begin
  insert into public.profiles (id, display_name)
  values (new.id, coalesce(new.raw_user_meta_data->>'full_name', new.email));
  return new;
end $$;
drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function public.handle_new_user();
```

Seed one `engine_config` row with §8.6 defaults. Seed admin role for the owner's
email (env `ADMIN_EMAILS`, applied at startup).

---

## 7. API CONTRACT

### 7.1 REST (all under `/api`, Bearer Supabase JWT unless noted)

| Method | Path | Role | Purpose |
|---|---|---|---|
| GET | `/health` | none | liveness |
| GET | `/me` | auth | profile + role |
| POST | `/mt5/connect` | admin | `{server, login, password, terminal_path?}` → discovers symbol, connects, stores encrypted |
| POST | `/mt5/disconnect` | admin | shutdown cleanly |
| GET | `/mt5/status` | auth | `{status, symbol, account: {balance, equity, currency}, broker_time_utc_offset}` |
| GET | `/candles?tf=M15&limit=500` | auth | OHLCV array for chart backfill |
| GET | `/signals?limit=100&status=` | auth | signal list (with trace) |
| GET | `/signals/{id}` | auth | single signal full detail |
| GET | `/config` | auth | engine_config |
| PUT | `/config` | admin | update config JSON (validated) |
| POST | `/config/auto-trade` | admin | `{enabled, confirm:"ENABLE"}` (typed confirmation required) |
| GET | `/positions` | auth | open MT5 positions |
| GET | `/stats?days=30` | auth | win rate, expectancy, PF, max DD, by-session breakdown |
| GET | `/logs?limit=200&level=` | auth | recent logs |
| GET | `/logs/export.csv` | auth | CSV download |

### 7.2 WebSocket (`/ws`, token as query param `?token=...`)

Client → Server:
```json
{"type":"subscribe","channel":"market","symbol":"XAUUSDm","tf":"M15"}
{"type":"unsubscribe","channel":"market"}
```

Server → Client (event types):
```json
{"type":"tick","symbol":"XAUUSDm","bid":2695.12,"ask":2695.32,"ts":1737000000000}
{"type":"bar_open","symbol":"XAUUSDm","tf":"M15","candle":{"t":1737000000,"o":2695.1,"h":2695.4,"l":2694.9,"c":2695.2,"v":12}}
{"type":"bar_update","symbol":"XAUUSDm","tf":"M15","candle":{...}}   // live forming candle
{"type":"bar_close","symbol":"XAUUSDm","tf":"M15","candle":{...}}
{"type":"signal","signal":{"id":"...","ts":"...","direction":"BUY","entry":2695.4,"sl":2691.8,"tp":2702.6,"confidence":0.72,"trace":[...]}}
{"type":"signal_update","id":"...","status":"won","result_r":2.0,"closed_at":"..."}
{"type":"account","balance":1000.0,"equity":1004.2,"positions":[...]}
{"type":"mt5_status","status":"connected","symbol":"XAUUSDm"}
{"type":"engine_log","level":"info","message":"...","ts":"..."}
```

WS rules: heartbeat ping/pong every 15s; client auto-reconnect with exponential
backoff (1s→2s→…→30s cap); on reconnect, client re-subscribes and refetches
candles REST to heal gaps.

---

## 8. SIGNAL ENGINE SPEC

### 8.1 DataSource interface (`mt5/base.py`)

```python
class DataSource(ABC):
    async def connect(self, creds) -> dict            # account info
    async def disconnect(self) -> None
    async def is_connected(self) -> bool
    async def get_rates(self, symbol, tf, count) -> pd.DataFrame  # o,h,l,c,v,time_utc
    async def get_tick(self, symbol) -> Tick          # bid, ask, time
    async def subscribe_ticks(self, symbol) -> AsyncIterator[Tick]
    def discover_symbols(self, pattern="*XAUUSD*") -> list[str]
    async def place_order(self, order: Order) -> OrderResult   # MT5 impl only
    async def get_positions(self) -> list[Position]            # MT5 impl only
```

- `MockDataSource`: deterministic seeded random-walk candle generator with
  injectable volatility regimes + a `step_seconds` speed multiplier (for tests).
  Must be able to **craft scenarios** (inject a sweep bar) for engine tests.
- `MT5DataSource`: thin async wrapper (run blocking MT5 calls in executor);
  auto-reconnect loop (retry every 5s, log every attempt).

### 8.2 Strategy v1 (exact rules)

Base timeframe **M15**, context timeframe **H1**. Evaluate ONLY on M15 bar close
(never on forming candles).

**BUY signal — ALL must pass:**
1. **Trend (H1):** last H1 close > H1 EMA(50).
2. **SFP sweep (M15):** last closed candle: `low < min(low of previous 20 bars)`
   AND `close > that min` AND `lower_wick >= 0.3 × ATR(14)`.
3. **RSI(14):** `40 <= RSI <= 65`.
4. **ATR filter:** `ATR(14) >= min_atr` (default 0.8 USD).
5. **Session filter:** bar time (UTC) inside London `[07:00–16:00]` or NY
   `[13:00–20:00]` (configurable).
6. **News filter:** no high-impact USD event within ±30 min (configurable).
   Source: FMP economic calendar, cached 15 min. If calendar unavailable →
   skip check, log warning.
7. **State:** no active signal in same direction; cooldown of 3 bars since last
   signal; spread ≤ max_spread_points (default 35).

**SELL:** mirror of everything (trend below EMA, sweep of highs, upper wick,
RSI `35 <= RSI <= 60`).

**Levels:**
- `SL = sweep_extreme ∓ 0.2 × ATR` (buffer beyond the wick)
- `TP = entry ± 2.0 × |entry − SL|` (RR configurable, default 1:2)

**Confidence** = weighted pass score:
trend 0.30 + sfp_quality 0.30 (wick_ratio vs threshold) + rsi_position 0.20 +
session 0.10 + atr_strength 0.10. Store 0–1.

### 8.3 Decision trace (stored in `signals.trace`, rendered in UI)

```json
{
  "direction": "BUY",
  "checks": [
    {"name":"trend_h1","pass":true,"value":"close 2694.2 > EMA50 2688.7"},
    {"name":"sfp_sweep","pass":true,"value":"low 2689.9 swept min 2690.4; wick 1.12 >= 0.3*ATR(0.94)"},
    {"name":"rsi","pass":true,"value":48.2},
    {"name":"atr","pass":true,"value":0.94},
    {"name":"session","pass":true,"value":"london"},
    {"name":"news","pass":true,"value":"next USD event in 4.2h"},
    {"name":"spread","pass":true,"value":22}
  ],
  "params": {"ema_trend":50,"sfp_lookback":20,"wick_atr":0.3,"rr":2.0}
}
```

### 8.4 Lifecycle & result tracking

- `active` → tracker checks each tick: bid/ask touches SL → `lost` (r = −1);
  touches TP → `won` (r = +rr); else after `expiry_bars` (default 20) →
  `expired` with `result_r = (close − entry)/risk` signed by direction.
- With a linked trade (auto-traded), final status comes from the deal history
  (actual profit / initial risk).

### 8.5 Engine pseudocode

```python
async def on_bar_close(tf: str):
    if tf != cfg.timeframe: return
    if in_cooldown() or active_signal_exists(): return
    m15 = await source.get_rates(symbol, "M15", 200)
    h1  = await source.get_rates(symbol, cfg.trend_tf, 100)
    trace = Trace()
    if not check_trend(h1, cfg, trace):        return broadcast_none(trace)
    sig = detect_sfp(m15, cfg, trace)          # None | "BUY" | "SELL" + levels
    if not sig:                                return broadcast_none(trace)
    if not check_rsi(m15, cfg, trace):         return broadcast_none(trace)
    if not check_atr(m15, cfg, trace):         return broadcast_none(trace)
    if not check_session(now_utc, cfg, trace): return broadcast_none(trace)
    if not await check_news(trace):            return broadcast_none(trace)
    signal = build_signal(sig, trace)          # entry=close, SL/TP per §8.2
    await db.insert_signal(signal); await ws.broadcast("signal", signal)
    tracker.register(signal)
    if cfg.auto_trade: await executor.execute(signal)
```

### 8.6 `engine_config.config` defaults (JSONB)

```json
{
  "timeframe":"M15","trend_tf":"H1",
  "ema_fast":20,"ema_slow":50,"trend_ema":50,
  "rsi_period":14,"rsi_buy_min":40,"rsi_buy_max":65,"rsi_sell_min":35,"rsi_sell_max":60,
  "atr_period":14,"min_atr":0.8,
  "sfp_lookback":20,"sfp_wick_atr_ratio":0.3,
  "sl_buffer_atr":0.2,"rr":2.0,"expiry_bars":20,"cooldown_bars":3,
  "sessions":[{"name":"london","utc":[7,16]},{"name":"newyork","utc":[13,20]}],
  "news_blackout_min":30,"max_spread_points":35,
  "risk_mode":"percent","risk_percent":0.5,"fixed_lot":0.01,
  "max_positions":1,"daily_max_loss_pct":3.0,"magic":234000
}
```

---

## 9. EXECUTION & RISK (Phase 4)

- Runs only when `auto_trade == true` AND MT5 connected. Otherwise no-op.
- **Lot sizing (XAUUSD):** contract size usually 100 oz → `lots = risk_amount /
  (sl_distance × 100)`, then round DOWN to `volume_step`, clamp to
  `[volume_min, volume_max]`. `risk_amount = equity × risk_percent/100`
  (or `fixed_lot`).
- **Order:** market order with SL/TP attached, deviation 30 points, filling mode
  from `symbol_info`, magic = cfg.magic, comment `xauai-{signal_id}`.
- **Idempotency:** check `trades` by `signal_id` before sending; never duplicate.
- **Kill switches (checked before EVERY order):**
  1. `max_positions` reached → skip
  2. Daily realized+floating loss ≥ `daily_max_loss_pct` → close all positions,
     set `auto_trade=false`, log CRITICAL, WS broadcast `engine_log`
  3. Current spread > `max_spread_points` → skip
- All order results logged with retcode; failed orders retried max 1×.

---

## 10. FRONTEND SPEC

### Layout (Dashboard)

```
┌ TopBar: logo | XAUUSDm ●connected | balance/equity | 👤 | ⋯ ┐
├──────────────────────────────────┬──────────────────────────┤
│ CHART (lightweight-charts)       │ SIGNAL PANEL             │
│ [M1 M5 M15 M30 H1 H4 D1]         │ ┌ Active SignalCard ───┐ │
│ live ticks update forming candle │ │ ▲ BUY 2695.40        │ │
│ optional SL/TP price lines       │ │ SL 2691.80 TP 2702.60│ │
│                                  │ │ TraceList ✓✓✓✓✓✓✓    │ │
│                                  │ └──────────────────────┘ │
│                                  │ History list (status     │
│                                  │ chips, click → trace)    │
├──────────────────────────────────┴──────────────────────────┤
│ AccountStrip: balance | equity | open positions | engine ⏻  │
└─────────────────────────────────────────────────────────────┘
```

### Chart requirements
- `createChart` dark theme; `addCandlestickSeries()`; data = REST `/candles`
  then `series.update()` per `bar_update`; new `bar_open` starts new bar.
  Time = UNIX **seconds**, ascending, unique.
- Timeframe switcher refetches + resubscribes WS.
- SL/TP horizontal `createPriceLine()` for the active signal (toggle in menu).

### DotMenu items (top-right ⋯)
MT5 Connection… | Auto-trade toggle (OFF default, admin, typed confirm) |
Settings… (maps to engine_config fields, grouped: Strategy / Filters / Risk) |
Performance Report | Logs | Export CSV | Theme | Logout

### Performance Report
Cards: Win rate, Expectancy (R), Profit factor, Max drawdown, Total R, signal
count. Table: breakdown by session and direction. Only closed signals.

### Auth pages
- `/login`: Google button (`signInWithOAuth({provider:"google"})`), email/password,
  "Forgot password?" link.
- `/reset-password`: handles recovery token (PKCE, `detectSessionInUrl`), new
  password form.
- Guarded routes redirect to `/login` when no session.

Design: Tailwind, dark default, gold accent, Inter font, mobile: chart on top,
panel below (stacked).

---

## 11. ENVIRONMENT VARIABLES

```ini
# frontend/.env
VITE_SUPABASE_URL=...
VITE_SUPABASE_ANON_KEY=...
VITE_API_URL=https://api.example.com
VITE_WS_URL=wss://api.example.com/ws

# backend/.env
DATABASE_URL=postgresql+asyncpg://...supabase...   # direct connection
SUPABASE_URL=...
SUPABASE_ANON_KEY=...                              # for token verification
FERNET_KEY=...                                     # encrypt MT5 passwords
ADMIN_EMAILS=you@example.com
DATA_SOURCE=mock                                   # mock | mt5
FMP_API_KEY=...                                    # economic calendar (optional)
MT5_TERMINAL_PATH=                                 # optional
```

---

## 12. BUILD PHASES

### Phase 0 — Scaffold & infra (~0.5d)
Repo init, Vite frontend scaffold, FastAPI skeleton, `schema.sql` applied,
`GET /api/health`, MockDataSource with seeded candle generator + scenario
injection, pytest + ruff pass, DECISIONS.md created.
**AC:** uvicorn boots; health 200; frontend dev server renders placeholder;
`pytest` green on non-Windows machine (no MetaTrader5 import at module load).

### Phase 1 — Auth (~1d)
Supabase Auth wired: Google OAuth, email login, registration, password reset
flow end-to-end. Backend JWT verification: call `GET {SUPABASE_URL}/auth/v1/user`
with the bearer token (cache per token-hash, 60s TTL). Role from `profiles`
(ADMIN_EMAILS promoted at startup). Protected `/api/me`.
**AC:** register/login/google/reset all work in browser; `/api/me` → 401 without
token; admin vs viewer enforced by test.

### Phase 2 — MT5 connection + market data + chart (~2d)
Connection manager (§7.1 mt5 routes) with Fernet-encrypted password storage,
symbol discovery, heartbeat + auto-reconnect. Tick loop → WS broadcast.
`/candles` backfill. Frontend Dashboard: chart + TF switcher + live updates +
status pill + Mt5ConnectDialog.
**AC (mock):** chart streams with `DATA_SOURCE=mock`, gaps heal on reconnect.
**AC (real):** on Windows VPS + Exness demo: candles match MT5 terminal within
1 tick; symbol auto-discovered; reconnects within 10s after terminal restart.

### Phase 3 — Signal engine + panel (~2d)
Engine per §8 (bar-close scheduler, trace, tracker, WS events). Frontend
SignalPanel: active SignalCard, TraceList (✓/✗ per check), history with status
chips. SettingsDialog edits engine_config (validated). Unit tests: indicators,
sfp (pure sweep / close-beyond fake break / quiet market fixtures), filters.
**AC:** engine on MockSource (accelerated + injected sweep scenario) emits a
signal with complete trace; tracker transitions active→won/lost/expired
correctly; e2e pytest covers the full path; no signal ever from forming candle.

### Phase 4 — Execution + risk + menu functions (~2d)
Executor + risk (§9), kill switches, trades table linkage, AccountStrip +
`account` WS events, PerfReport (stats endpoint + UI), LogViewer + CSV export,
auto-trade toggle with typed confirmation.
**AC:** on MT5 demo with auto_trade ON: correct lot per formula (unit-test the
math), SL/TP attached, magic set, zero duplicates per signal; daily-loss kill
switch verified in simulation; stats numbers match manual calculation over ≥20
signals.

### Phase 5 — Hardening + deploy (~1–2d)
Security checklist (§13) pass; structured logging to `logs` table + console;
README deploy guide (Windows VPS: install Exness MT5 terminal, Python 3.11,
NSSM service for uvicorn; Vercel deploy; Supabase config incl. Google OAuth
client); final QA checklist below.
**AC:** 48h unattended run on demo: zero crashes, WS reconnects survive VPS
network blips, engine resumes after MT5 terminal restart; all tests green.

### Final manual QA checklist
- [ ] Candles match MT5 terminal on 3 timeframes
- [ ] Signal trace renders all 7 checks with real values
- [ ] Auto-trade OFF by default after fresh deploy
- [ ] Kill switch disables auto-trade on simulated daily loss
- [ ] Log CSV export downloads
- [ ] Google login + password reset on fresh browser
- [ ] All REST endpoints 401 without token; admin routes 403 for viewer

---

## 13. SECURITY CHECKLIST

- MT5 passwords: Fernet-encrypted at rest; NEVER logged; NEVER returned to
  frontend after save (masked as `••••`).
- Supabase service key only in backend env; anon key only in frontend.
- All routes require JWT except `/health`; admin-only: mt5 connect/disconnect,
  PUT /config, auto-trade toggle.
- HTTPS/WSS only in production; CORS restricted to the frontend domain.
- slowapi rate limits on auth-bearing REST routes.
- No secrets in git; `.env.example` committed, real `.env` gitignored.
- Input validation via pydantic on every write endpoint (esp. config PUT —
  reject unknown keys, enforce sane ranges).

---

## 14. KNOWN RISKS & FALLBACKS

| Risk | Mitigation |
|---|---|
| MetaTrader5 pkg = Windows-only | MockDataSource (C7) |
| Economic calendar API down/outdated | Skip news filter + warn; manual blackout times in config |
| Broker DST shift breaks session filter | Re-detect UTC offset daily (C4) |
| Exness spread spike at daily rollover | Spread guard skips entries |
| VPS reboot / terminal crash | NSSM auto-restart + engine auto-reconnect loop |
| WS client disconnects | Backoff reconnect + REST gap-heal |

---

## 15. v2 BACKLOG (DO NOT BUILD NOW)

Multi-user agent-app (credentials never leave user's machine), copy-trading,
CME orderflow via Databento/Rithmic, backtesting UI, ML confidence model,
Telegram alerts, PAXG weekend feed.
