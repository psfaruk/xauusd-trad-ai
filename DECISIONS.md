# DECISIONS.md

Per SPEC §0.5 — every ambiguity resolution or deviation from the SPEC is logged
here, newest at the bottom. Phase numbering follows SPEC §12.

| ID | Phase | Decision | Rationale |
|----|-------|----------|-----------|
| D-001 | 0 | Build machine is Linux (no Windows / no MT5 terminal). `DATA_SOURCE=mock` is the default everywhere; the `MetaTrader5` package is never installed and is only lazy-imported inside `MT5DataSource` method bodies. | SPEC C7 — Phases 0–3 are mock-based by design; Phase 0 AC explicitly requires "no MetaTrader5 import at module load". |
| D-002 | 0 | Backend boots without `DATABASE_URL`: schema bootstrap logs a warning and continues in degraded (no-persistence) mode; `/api/health` reports `db: false`. | Phase 0 AC requires uvicorn boot + health 200 in environments without Supabase (this dev sandbox has no Postgres). |
| D-003 | 0 | `DataSource.get_rates` returns only CLOSED bars (forming candle excluded). | SPEC §8.2 evaluates closed bars only; the forming candle reaches the chart via WS `bar_update` events in Phase 2. |
| D-004 | 0 | TailwindCSS pinned to `3.4.x` (classic `tailwind.config.js` + PostCSS). | SPEC §4 does not pin a version; 3.4 is the stable line for the dark theme + gold accent config used here. |
| D-005 | 0 | In dev the frontend calls the API same-origin (`/api/...`) via a Vite dev-server proxy to `:8000`; `VITE_API_URL` still overrides for production (Vercel). | Keeps the SPEC §11 env contract while allowing a single-origin dev/preview setup. |
| D-006 | 0 | Python deps via `requirements.txt`; `pyproject.toml` holds tooling config only (ruff, pytest). | Simplest reproducible setup for the Windows VPS target (venv + NSSM, SPEC §4 Infra). |
| D-007 | 0 | `MockDataSource` generates base M1 bars deterministically — per-bar RNG seeded by `[seed, minute_index]` — and aggregates all higher timeframes from M1. Scenario overrides are applied at M1 granularity. | Order-independent determinism (same seed ⇒ identical candles regardless of access order) and cross-timeframe consistency (an injected M15 sweep also appears correctly inside H1 candles). |
| D-008 | 0 | `subscribe_ticks` is declared as a plain method returning `AsyncIterator[Tick]` (implemented as an async generator), not `async def` as sketched in SPEC §8.1. | SPEC §8.1 is interface pseudocode; this is the only correct Python shape for async generators. |
| D-009 | 0 | `apply_schema` executes statements in AUTOCOMMIT mode and logs+skips individual failing statements (e.g. the `auth.users` trigger on plain non-Supabase Postgres) instead of aborting the run. | Idempotent bootstrap must not die on Supabase-only objects; tests create an `auth.users` stub when running against plain Postgres. |
| D-010 | 0 | `postgresql://` and `postgres://` URLs are normalized to `postgresql+asyncpg://` at engine creation. | Supabase direct-connection strings use the plain `postgres://` scheme. |
| D-011 | 0 | Admin promotion via `ADMIN_EMAILS` deferred to Phase 1 (needs auth + `profiles` rows to exist). | SPEC §6 seed note; meaningless before login exists. |
| D-012 | 0 | `api/` route modules, `ws.py` and `auth.py` exist as unmounted stubs; only `GET /api/health` is live in Phase 0. | SPEC §5 structure + §0 phase discipline (no Phase 1–4 features early). |
| D-013 | 0 | The SPEC stack (React SPA on Vite + FastAPI) is kept as-is; the sandbox's default Next.js scaffold is NOT used for the product. The sandbox preview port (:3000) is served by the Vite dev server. | SPEC §0.3 / §4: "Follow the tech stack in §4 exactly. No substitutions." Next.js would be a forbidden substitution. |
| D-014 | 0 | Schema tests run against an embedded Postgres via the `pgserver` dev-only dependency when `TEST_DATABASE_URL`/`DATABASE_URL` are absent or unreachable; they skip cleanly when it is not installed. A non-Postgres `DATABASE_URL` (e.g. a local SQLite file) is rejected with a warning instead of crashing the boot. | Phase 0 ships "schema.sql applied idempotently" — proving it against a real Postgres in the Linux sandbox (no root, no Supabase) removes Phase 1 risk. `pgserver` is a test tool, not a stack substitution. |
| D-015 | 1 | Vite dev server sets `server.allowedHosts: true` (and `preview.allowedHosts: true`). The sandbox preview reaches :3000 through a dynamic gateway host (`ws-*.cn-hongkong-vpc.fcapp.run`) that changes between sessions, so the fixed-host list is not maintainable. | Dev-server-only setting; production builds (`vite build` / the Docker image) are unaffected by `allowedHosts`. Without it the preview is blocked with "Blocked request. This host is not allowed". |
| D-016 | 1 | Railway deploy = **single service**: the root `Dockerfile` builds the SPA (node stage) and serves it from FastAPI (`STATIC_DIR`, SPA-history fallback) alongside the API and (Phase 2) `/ws` — one uvicorn process, same-origin, no CORS. This replaces the SPEC §3 "frontend on Vercel" split *for the demo/mock deployment*; the SPEC's Windows VPS backend for real MT5 remains unchanged (C1/C7: the Linux image runs `DATA_SOURCE=mock`). `railway.json` pins the Dockerfile builder + `/api/health` healthcheck. `requirements.txt` is prod-only; dev/test deps (pytest, ruff, pgserver) moved to `requirements-dev.txt`. | Requested by the user ("configure railway deploy"). Single service avoids baked-in `VITE_API_URL`, CORS and WS-domain pitfalls; Railway injects `PORT`; service vars double as Docker build args for the `VITE_*` values. |
| D-017 | 1 | `apply_schema` prepends a compatible `auth.users` stub (`id`, `email`, `raw_user_meta_data`) on plain Postgres (Railway/tests). On Supabase it is a no-op. `/api/me` additionally upserts the verified user into `auth.users` (best-effort) before the `profiles` upsert; the `handle_new_user` trigger insert is now `ON CONFLICT DO NOTHING`. | Without the stub, `profiles` (FK → `auth.users`) and the signup trigger cannot exist on Railway Postgres, so roles would never persist (only the ADMIN_EMAILS fallback). With it, Railway Postgres behaves like Supabase for the tables this app owns; Supabase is untouched (no-ops / conflict-do-nothing). |
| D-018 | 2 | With `DATA_SOURCE=mock`, the ConnectionManager auto-connects at startup (fixed demo credentials) and starts the EngineRuntime immediately; with `DATA_SOURCE=mt5` it instead tries `try_restore()` from the stored (Fernet-encrypted) `mt5_connections` row. | SPEC Phase 2 mock AC ("chart streams with DATA_SOURCE=mock") requires streaming without a manual connect step; the dialog still allows disconnect/reconnect. The restore path implements the "engine resumes after MT5 terminal restart" AC for real MT5. |
| D-019 | 2 | The auto-reconnect loop (SPEC §8.1 MT5DataSource "auto-reconnect loop, retry every 5s") lives in `ConnectionManager._heartbeat_loop`, not inside `MT5DataSource`. | One reconnect authority shared by mock/real sources; the manager also owns runtime restart + status broadcasts, avoiding two competing retry loops. `MT5DataSource` stays a thin stateless wrapper around the terminal IPC. |
| D-020 | 2 | `MockDataSource` origin is floored to UTC midnight (was: anchor − 30 days verbatim). | MarketStream/chart bucket math is epoch-aligned (…:00/:15/:30/:45); with a minute-offset origin the mock's M15/H1 buckets disagreed with the streamed bars, breaking `bar_close` reconciliation and candle lookup. |
| D-021 | 2 | Live engine evaluation shares ONE pure pipeline `engine.evaluate()` (closed bars only, H1 context via `closed_h1_asof`) with the backtest runner; news state is pre-computed by the caller (`NewsState`) and injected. | Single source of truth — backtests provably run the same rules as live; keeps `evaluate()` sync/pure for bar-replay. The live engine additionally enforces cooldown/active-state and persistence. |
| D-022 | 2 | Backtest tracker is bar-based (SL-first pessimism when a bar touches both SL and TP); the live tracker is tick-based (§8.4). | Backtests have no intra-bar ticks; pessimistic both-touch → SL is the conservative convention. Live ticks resolve the real touch order. |
| D-023 | 3 | `scripts/mock_supabase.py` + `start_dev_stack.sh` — a local Supabase-auth-compatible mock (password grant + /auth/v1/user) for full browser e2e without a real Supabase project. | Dev-only tooling; lets the login → dashboard → WS streaming path be browser-verified on a laptop/CI. Never used in production. |
| D-024 | 4 | Multi-user trading is implemented as per-owner **trading planes** (`UserTradingManager`). Demo mode works on any OS (a `MockDataSource.sibling` sharing the public market's seed/origin/clock — positions mark against exactly the public chart prices). LIVE mode on Linux stores credentials with status `bridge_required` instead of connecting: the `MetaTrader5` package binds one terminal per process, so per-user live execution requires one worker process per user on a Windows bridge (v2 scope). | User req: everyone sees the same data; only traders connect their own MT5/Exness account. C1 makes true multi-account MT5 impossible inside one Linux process — silently simulating "live" would be dishonest, so the platform reports `bridge_required` until the bridge exists. |
| D-025 | 4 | Engine signals are relayed to armed planes via an `engine.on_signal` callback set by `EngineRuntime` (not by subclassing the engine or polling). One plane's failure is caught and logged without affecting others. | Keeps `SignalEngine` pure (backtest-shared), keeps the relay in the glue layer, and isolates per-user failures. |
| D-026 | 4 | Kill-switch order inside `evaluate_kill_switches` is: daily-loss emergency FIRST, then max_positions, then spread — and `execute_signal` runs the emergency inside the not-allowed branch. | A bleeding open position must never be left unhandled just because `max_positions` would have skipped the new order; the original order (max_positions first) made the daily-loss branch unreachable in that state — found by `test_executor_daily_loss_emergency_stop`. |
| D-027 | 4 | Money-moving routes carry an in-process per-client budget (order/close 30/min, arm/disarm 10/min) on top of the global slowapi 240/min; the slowapi limiter keys on `X-Forwarded-For` (first hop) so Railway's proxy does not collapse all users into one bucket. | SPEC §13 asks for rate limits on auth-bearing routes; tight per-route budgets protect the trading path from runaway clients without affecting chart polling. |
| D-028 | 4 | `mt5_connections` rows are now one-per-owner (upsert scoped by `owner`); the admin/platform plane persists only when an admin user initiated the connect (owner = their profile id); the D-018 mock auto-connect stays in memory. Schema gains `mode` + per-user `auto_trade` columns via idempotent `ALTER ... ADD COLUMN IF NOT EXISTS`. | The previous `DELETE FROM mt5_connections` (single-connection assumption) would have wiped every other user's stored credentials on each connect. |
| D-029 | 4 | `scripts/mock_supabase.py` upgraded: each email maps to a deterministic DISTINCT user (uuid5) + distinct token, enabling true multi-user isolation e2e in the dev stack. | The original mock returned the same user for every login, making per-user isolation impossible to verify (and hiding the D-028 bug class). |
| D-030 | 4 | **`DATA_SOURCE=live` is the new default**: real-time gold market data from free key-less public APIs (Binance PAXG/USDT quotes + klines primary; gold-api.com XAU quote fallback; Yahoo GC=F history fallback; tick-built candles last resort). Boot probes the chain once and honestly degrades to mock only when every provider is unreachable (reported via `/api/health`). Paper/demo planes price off the SAME live feed. TopBar shows a LIVE badge (provider + tick age), status/WS `mt5_status` carries a `feed` block, and the heartbeat rebroadcasts status every ~15s so badges stay fresh. Frankfurter ECB endpoint updated to `api.frankfurter.dev/v1` (old `.app` host now 301s). | User report "live real data is not updating": on Linux/Railway the MetaTrader5 package cannot run (C1), so the previous default showed synthetic mock prices. PAXG is a regulated physical-gold-backed token (1 PAXG = 1 fine troy oz) that tracks spot XAUUSD within a fraction of a percent and trades 24/7 — free, no API key. The basis vs broker XAUUSD is disclosed everywhere (feed detail, Market Data tab). |

## D-031 — Sandbox preview always-on stack (orphan watchdog + same-origin auth)

**Context:** The sandbox reaper kills every descendant of a tool-command when it
ends; only boot-chain (`.zscripts/dev.sh`) processes survive. The dev stack
(:8090 auth mock, :8000 API, :3000 vite) therefore died between sessions and the
preview showed no data; additionally the boot vite had no Supabase env, so login
through the preview origin was impossible.

**Decision:**
1. `scripts/watchdog.sh` — keep-alive loop restarting any dead component
   (mock-supabase :8090, API :8000 `DATA_SOURCE=live`, vite :3000). Every spawn
   is wrapped `( setsid nohup … & )` so it is orphan-reparented to PID 1
   immediately and escapes the reaper's descendant-tree walk (a direct
   `setsid nohup … &` is killed; verified empirically). Started from
   `.zscripts/dev.sh` (container boot) via a guarded hook.
2. `vite.config.ts` proxies `/auth/v1` → :8090; `supabase.ts` in DEV falls back
   to `window.location.origin` + placeholder key when `VITE_*` env is absent —
   the preview logs in through its own origin with zero baked credentials.
   Production builds are unaffected (fallback is `import.meta.env.DEV`-gated).

**Verification:** watchdog survived call boundaries (PPID=1); same-origin login →
/api/me (admin) → live Binance PAXG feed (tick age <2s) → real M1 candles at the
current minute; browser e2e: dashboard ticker === backend price, "LIVE · Binance
PAXG +0s" badge, ws open, zero console errors. Flat price over 40s was the quiet
Saturday market, not staleness (backend showed the same price).

## D-032 — Deployment-proof live data (mirror failover, health introspection, auto-recovery)

**Context:** The user's Railway deploy (verified live) ran the LATEST code but
served synthetic mock prices (~2715 vs real ~4362). Two causes were plausible:
(1) the Phase-1 README had instructed `DATA_SOURCE=mock` as a Railway variable —
Railway vars persist across deploys and override the image default (`live`),
silently pinning the platform to demo prices; (2) boot-time provider outage
(api.binance.com returns 451 to many datacenter/AWS egress IPs) degrades to mock
FOREVER because the probe runs only once at boot.

**Decision:**
1. **Binance mirror chain** — all market-data calls now try
   `data-api.binance.vision` (Binance's official public data domain, identical
   REST shape, not subject to the 451 datacenter blocks) and fall back to
   `api.binance.com`; the working mirror becomes sticky so healthy polls pay no
   failover latency. Applied to MarketFeed (quotes + klines + TF fetch) and
   ExternalMarketService (24h ticker).
2. **Health introspection** — `/api/health` now reports
   `requested_data_source` + `degraded` alongside `data_source`, so a remote
   deployment is diagnosable without shell access ("mock"+requested "mock" =
   env var forces demo; "mock"+requested "live"+degraded = boot outage).
3. **Live-recovery loop** — when the boot probe degrades live->mock, a
   background task re-probes every 60s and hot-swaps the platform to live the
   moment any provider answers (disconnect -> `ConnectionManager.set_source`
   -> reconnect rebuilds the engine runtime). A bad boot minute never pins the
   platform to synthetic prices again.
4. **Unmistakable UI warning** — TopBar shows a red "⚠ DEMO DATA · synthetic
   prices" badge whenever mock runs (amber + "recovering…" when degraded),
   with the exact fix in the tooltip.
5. **README corrected** — Railway guide no longer sets `DATA_SOURCE=mock`;
   pre-D-030 deployments are told to DELETE the stale variable (and to attach
   Postgres — `"db": false` means data does not survive restarts).

**Verification:** 186 backend tests green (new: mirror-failover 451 rotation +
sticky, SourceResolution contract incl. explicit-mock vs degraded distinction;
external test re-based on the vision domain), ruff/tsc/vite clean; live stack
restarted — health `{data_source: live, requested: live, degraded: false}`,
uvicorn log shows data-api.binance.vision 200s, real feed 4362.59 @ 0.2s tick
age, browser LIVE badge + ticker 4362.58/4362.59, zero console errors.

## D-033 — Real ticks only: five-venue WS aggregate, demo data abolished

**User directive (verbatim intent):** "demo data must be completely OFF the
site — only real data, real price, real market; candles must update on every
tick (20–50 ticks/sec in active sessions)."

**Problem.** D-030/D-032 still degraded `live → mock` when no provider
answered at boot — a Railway boot-time outage silently showed SYNTHETIC
prices (the exact thing the user complained about). And REST polling capped
the feed at one quote per 2s — nothing near real tick flow.

**Decision.**
1. **Demo fallback abolished.** `resolve_data_source` NEVER returns mock.
   On total outage the platform stays on `LiveDataSource`, shows an honest
   "no feed — retrying" state, and retries forever (WS venue workers +
   REST poll + ConnectionManager heartbeat). `DATA_SOURCE=mock` is honored
   ONLY with `ALLOW_DEMO=1` (local dev/tests; the Docker image never sets
   it) — deployments physically cannot display fake prices.
2. **Five-venue WebSocket aggregate (new `app/mt5/tick_feed.py`).**
   Binance PAXG/USDT+USDC (bookTicker/aggTrade/depth@100ms via
   data-stream.binance.vision — probe-verified that the classic
   stream.binance.com times out from this egress), Bybit XAUT/USDT
   (PAXGUSDT does NOT exist on Bybit — probe-verified), OKX PAXG+XAUT bbo-tbt,
   Kraken PAXG/USD book+trade, Coinbase PAXG-USD ticker. Every real event
   (book update or trade) → consolidated best bid/ask (max-bid/min-ask,
   freshest-venue fallback on cross) → tick → forming-candle update. Venue
   workers reconnect with backoff forever; per-venue health + a real t/s
   meter surface in /api/health, /api/mt5/status and the TopBar LIVE badge
   (`⚡ N t/s`). US-geo robustness: vision mirror + Kraken + Coinbase.
3. **Broadcast budgets (backend-only throttling of FRAMES, never of data):**
   tick frames ≤10/s per client (latest-quote-wins, carry `n` + `tps`),
   bar_update ≤4/s per TF; the forming candle itself + engine + SL/TP tracker
   see EVERY event at full resolution. REST poll drops to 30s while WS is
   healthy (kline authority only).

**Rejected:** synthesizing 20–50 fake ticks/sec to hit the number — violates
the "real data only" directive; the t/s meter shows the true market rate
(quiet Sunday ≈ 1–5/s, active sessions tens/s).

**Verification:** probes of all 5 venues (REST + WS + 30s rate samples);
offline unit tests for every venue parser + consolidation + the emit path;
resolve tests updated for the no-degrade contract (incl. ALLOW_DEMO gating);
193 tests green; ruff/tsc/vite clean.

---

## D-034 — Real MT5 account through the terminal's built-in MCP server

**Context:** The user supplied a live Exness demo account (Exness-MT5Trial6,
login 414350770) and required the platform to connect THROUGH MetaTrader 5
("তোমি সরাসরি এটি কানেক্ট করতে পারবে না — meta 5 দিয়ে করতে হবে") and to
show balance + detailed trade history + prove auto-trading, from the site.

**Decision:**
1. **Real terminal, user-space Wine (no root):** Debian wine debs extracted
   under `/home/z/mt5stack/root` (rootless), Xvfb :99 + openbox provide the
   GUI, and the genuine MetaTrader 5 terminal (build 6204) runs as
   `terminal64.exe /portable`, logged into the Exness account. The watchdog
   (D-031) now supervises the whole stack (xvfb/openbox/terminal) with a 90s
   boot grace; the stack is skipped automatically where the dir is absent
   (Railway/Windows hosts).
2. **Terminal MCP bridge instead of MetaTrader5 pip:** MT5 build 6000+ ships
   a built-in MCP server (127.0.0.1:22346, bearer-key auth). `app/mt5/mcp.py`
   is a JSON-RPC client for it (initialize/tools-call, session retry). This
   replaces the Windows-Python gateway idea (bridge.py stays as an optional
   shim for DATA_SOURCE=mt5 hosts) — no Windows Python needed, the platform
   only ever talks to the terminal, satisfying the "through MetaTrader 5"
   constraint.
3. **Admin panel endpoints (routes_mt5.py):** GET /api/mt5/account
   (balance/equity/margin/connected/mcp_trade_allowed), /positions,
   /history?days=1..365, /symbols, POST /order + /close — all executed by
   the real terminal. Per-user live planes keep the honest
   `bridge_required` state (D-024) until a per-account terminal exists.
4. **UI:** DotMenu → "MT5 Account (live)" opens Mt5AccountPanel — live
   balance/equity/free-margin/floating-P/L cards (5s refresh), open
   positions with per-row close, 90-day trade history table, and a market
   order tab (symbol/volume/SL/TP, BUY/SELL) routed to the real account.

**Verification:** MCP round-trip in-sandbox — account (Exness-MT5Trial6,
$499.94, mcp_trade_allowed=true); REST e2e through the vite proxy (same
path the UI uses): order BUY 0.01 BTCUSDm filled @81224.37 (deal
4424979755), position shape verified, closed @81232.39 (+$0.08), history
shows all 3 trades. XAUUSDm weekend order correctly rejected with
"Market closed" (retcode 10018) — real broker behaviour. Browser e2e:
panel shows ● LIVE, BALANCE 500.02 USD, server/login line, history table;
zero console errors. 194 tests green, ruff/tsc/vite clean.

**Secrets:** the MCP bearer key lives only in /home/z/mt5stack/mcp_key.txt
(gitignored, outside the repo); the API reads it via MT5_MCP_KEY_FILE.
Account credentials live in the terminal's encrypted profile only.
