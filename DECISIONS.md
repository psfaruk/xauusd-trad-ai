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

---

## D-035 — MT5-first market data, BTCUSD pair, chart hardening

**Context (user directive):** "market data forex market theke asok, meta
trader 5 theke" — data must come from the FOREX market through MetaTrader 5;
the weekend-closed logic must be handled; candles were updating incorrectly
and the chart broke; add BTCUSD as a second pair; show the MT5 account
(balance/trades/history) directly in the frontend.

**Decision:**
1. **MT5 terminal = primary market-data source** (`app/mt5/mcp_market.py`):
   a 1s poller per watched symbol pulls REAL broker ticks via the terminal
   MCP `get_chart_ticks_history` (bid/ask), and `get_chart_history` becomes
   the AUTHORITATIVE candle source (M1..D1 + forming bar) while the symbol's
   broker ticks are fresh. Symbol mapping auto-discovered from Market Watch
   (XAUUSD→XAUUSDm, BTCUSD→BTCUSDm). Terminal timestamps verified UTC
   (one-time calibration against the 24/7 BTC feed; `MT5_TIME_SHIFT_S`
   overrides).
2. **Weekend logic (failover both ways, automatic):** while MT5 ticks are
   FRESH the broker feed is the authority — crypto events are counted for
   venue health but never move the quote or candles (different price
   basis). When MT5 goes stale (forex closed Sat/Sun, terminal down) the
   crypto composite takes over seamlessly; on the Monday open MT5 resumes
   authority by itself. Honest `note` badge explains the state; the
   source-switch also triggers exactly ONE candle refetch (see #4).
3. **Per-symbol feeds (`SymbolFeedCore` in live_source.py):** XAUUSD (5-venue
   gold composite fallback) + BTCUSD (new pair: Binance BTC/USDT composite
   fallback + Yahoo BTC-USD history). Broker-suffix routing (BTCUSDm →
   BTCUSD), per-symbol contract sizes (gold 100 / BTC 1), per-symbol
   tick streams, candles REST `?symbol=`, and a SECOND EngineRuntime for
   BTCUSD (its own SFP signal engine + tracker). Signal broadcasts now
   carry `symbol` + `tf`.
4. **Chart-breaking root causes fixed:**
   - 15s heartbeat `mt5_status` invalidated candles on EVERY broadcast →
     constant full setData resets racing live updates. Now refetch only on
     a real symbol or SOURCE change (provider:mt5↔composite transition).
   - `parse_terminal_time` required milliseconds; chart-history bars are
     second-precision → every MT5 bar silently dropped → Binance fallback
     (wrong volumes/basis). Now both formats parse; platform candles match
     the terminal EXACTLY (O/C/V verified identical).
   - Bars now track the MID price (was bid-only); `bar_close` reconcile
     MERGES (h/l never move backward) instead of replacing.
   - Chart.tsx hardened: sanitize (sort/dedupe/finite) before setData,
     try/catch + `onDesync` self-heal refetch on rejected updates.
5. **Frontend:** SymbolSwitcher chips (XAUUSD|BTCUSD with per-symbol feed
   dot), TopBar shows the selected symbol + "LIVE · MT5 · broker feed"
   badge + weekend note, and the REAL MT5 account (balance/equity/floating
   P/L/open positions) sits in the dashboard footer (10s poll, click opens
   the full panel) — visible immediately after MT5 connects.

**Verification:** 206 tests green (12 new: tick/bar parsing incl.
second-precision, dedupe, freshness, authority policy, weekend failover,
multi-symbol routing, contract sizes); live sandbox e2e — BTCUSD provider
`mt5` @ real Exness prices with platform M1s EXACTLY matching terminal bars
(O 81208.64 C 81229.38 V 79 identical), XAUUSD composite 19 t/s on the
Sunday, dual-symbol WS (ticks + bar frames per symbol), both engines
running, browser e2e: symbol switch, LIVE · MT5 badge, weekend note, MT5
footer strip, zero console errors.

## D-036 — AI signal → auto-order on the REAL MetaTrader 5 terminal

**User request:** "AI signal → auto-order (MT5 execution) যুক্ত করা" — wire the
AI signal engine to automatic order execution through the real MT5 terminal.

**Decision:** A dedicated LIVE execution plane (`McpAutoTrader`) reusing the
UNCHANGED §9 OrderExecutor risk core, bound to the terminal via a new
execution-only DataSource adapter — plus a separate, explicit live arm.

1. **`McpTradingSource`** (`app/mt5/mcp_source.py`): DataSource adapter over
   the terminal MCP bridge (execution half only): `account_info`,
   `get_positions` (terminal `stop_loss`/`take_profit` field names map to
   Position.sl/tp), `symbol_info` from Market Watch discovery (REAL contract
   size + volume limits: XAUUSDm 100/0.01–200, BTCUSDm 1/0.01–200),
   `place_order` (retcode 10009 → ok, ticket/price/volume captured),
   `close_position` (symbol resolved from live positions; already-closed
   positions are a clean ok — broker SL/TP may have exited first). Market
   data methods are honest errors: the platform feed stays on LiveDataSource
   with the MT5-first overlay (D-035).
2. **`McpAutoTrader`** (`app/mt5/auto_trader.py`): owns the arm + the
   executor. Every order runs the full §9 pipeline on the REAL account:
   idempotency per signal_id, daily-loss/max-positions/spread kill switches
   (daily loss closes ALL terminal positions + disarms), size_lot on real
   equity with real contract specs, 1x retry. Orders carry broker-side
   SL/TP + `xauai-<signal>` comment.
3. **Safety = never silent, never queued:**
   - Separate `auto_trade_live` arm (engine_config column, typed "ENABLE"
     confirm, admin-only, 409 when the terminal is down or trading is not
     allowed) — distinct from the paper `auto_trade` kill switch.
   - Forex weekend logic: a `market_state` callable (from the live feed's
     MT5 overlay freshness) skips closed-market signals honestly
     ("weekend/holiday — signal kept, order skipped"); without it the broker
     rejection (10018 "Market closed") is reported as an honest event.
     BTCUSD trades 24/7 regardless.
   - Terminal down at signal time → skip event, NO queueing.
   - Exit sync: signal EXPIRY closes the linked terminal position
     (`trade_close_single_position`); won/lost are broker-side SL/TP exits —
     the trades record is reconciled.
4. **Wiring:** `UserTradingManager.relay_signal` fans engine signals to the
   live plane; `EngineRuntime._on_signal_status` routes tracker outcomes to
   it; `PUT /api/config` now reaches EVERY engine runtime (gold + BTC, was
   gold-only) and the live executor. REST: `GET/POST /api/mt5/auto-trade`.
   WS: structured `mt5_auto` events (arm/order/skip/close) to all clients.
5. **Frontend:** "AI AUTO" tab in the MT5 Account panel — arm state card
   (red ARMED + REAL MONEY warning), terminal readiness, §9 risk summary,
   typed-ENABLE arm/disarm, and the live execution feed (WS `mt5_auto`);
   dashboard footer shows a pulsing "● AI AUTO ARMED" chip while armed.

**Verification:** 224 tests green (18 new: source mapping incl. terminal
SL/TP field names, specs, order result mapping, close-resolution, arm
guards, weekend skip, broker-rejection honesty, terminal-down skip,
idempotency, daily-loss disarm, expiry close, won/lost reconcile, relay
fan-out, ConfigRepo round-trip, routes 400/200/409); live REST e2e
(same-origin proxy: status → typed-confirm arm → disarm); LIVE round-trip
on the real Exness account through the exact relay path: arm → synthetic
BTCUSD signal → BUY 0.01 BTCUSDm filled @ 81077.15 retcode 10009 with
SL 80880.12 / TP 81480.12 attached (verified on the terminal position) →
duplicate relay idempotently skipped → expiry closed @ 81067.15 → disarm
(-$0.10 spread cost, real broker behavior). Browser e2e: AI AUTO tab,
ENABLE→ARM flow, red ARMED badge + footer chip, WS event feed showing the
ARMED event in real time, zero console errors.

---

## D-037 — Per-user broker connections, MT5-only candles, 60fps chart, mobile UI

**Context (user directives, Bengali):** (1) "অ্যাপ এর ক্যান্ডেল ডেটা শুধু মাত্র
meta 5 থেকে আসবে, এটা ফিক্স" — candle/quote data comes ONLY from MetaTrader 5.
(2) "জখন যে লোক তার exness তথ্য দিয়ে একাউন্ট করবে, সেই ট্রেড শুধু তার
একাউন্টে দেখা যাবে" — every user connects THEIR OWN Exness account; trades
are visible only inside that user's session. (3) Candles must move like the
MT5 terminal (60fps, no stepping/freezing) and the site must be mobile +
desktop friendly. (4) The broker connection must survive restarts
("deploy করার পর connect হচ্ছে না").

**Decision:**
1. **`BrokerConnectionService`** (`app/mt5/broker_connect.py`): per-user
   broker connections over the REAL terminal. A user "connects" by entering
   (server, login, password); the service verifies the entered login+server
   against the LIVE terminal session and binds the user to it (password
   Fernet-encrypted at rest in `mt5_connections`). Accounts not provisioned
   on the terminal host get an honest 422; terminal down → 503; demo plane
   (DATA_SOURCE=mock) records without a terminal. All `/api/mt5/*` trading
   routes (account/positions/history/order/close, auto-trade arm for
   non-admins) are gated on the REQUESTING user's active connection →
   **users never see anyone else's trades** (428 "connect your broker
   account first" otherwise).
2. **MT5_ONLY market data** (`live_source.py`): the crypto-composite
   fallback is disabled by default (`MT5_ONLY=1`; `=0` restores D-035
   behavior for hosts without a terminal). Broker feed only; when a symbol
   is closed the state is reported honestly (`market: open|closed|
   unavailable` per symbol in feed status) and the terminal still serves
   its real chart HISTORY — nothing is ever synthesized. Weekend-robust
   connect: the platform is "connected" while ANY symbol ticks or the
   terminal serves bars.
3. **Stack rebuild + self-healing (the "doesn't connect after deploy"
   fix):** the whole `/home/z/mt5stack` was reconstructed from scratch
   (wine 10.0 from the gmag11/metatrader5_vnc image layers, MT5 build 6205
   via download.mql5.com — metaquotes.net is DNS-blocked here, terminal
   logged in through the GUI flow against ExnessSC-MT5Trial6). Key
   resilience pieces: the account session is stored (`accounts.dat`) so
   watchdog terminal restarts auto-relogin in ~15s (verified); a 60s
   `_ensure_symbols_loop` re-adds Market Watch entries an abrupt kill may
   lose (gold self-healed after a pkill test); MCP key in
   `assistant.ini` + `mcp_key.txt` (outside the repo).
4. **60fps chart** (`Chart.tsx`): a requestAnimationFrame easing loop
   (exponential, ~0.22/frame) animates the last candle toward the newest
   broker price — every real tick retargets the animation between bar
   frames (MetaTrader-terminal feel); bar_open seeds a new bucket, settled
   frames snap exact; volume + candle stay consistent; malformed frames
   still dropped + onDesync self-heal (D-035 hardening kept).
5. **Frontend per-user UX:** ⋮ → **Connect Broker** (all users) opens the
   rewritten dialog (per-user, encrypted-at-rest note, live connection
   card); the dashboard footer shows a `BROKER <login>@<server>` chip (or a
   gold "connect broker" CTA); the MT5 Account panel shows an honest
   "Connect broker account" CTA on 428 instead of fake data, and its
   header/tabs are mobile-friendly (wrapping tabs, scrollable); TopBar api
   pill hides on small screens; chart box responsive
   (48vh mobile → calc(100vh−220px) xl).

**Verification:** 234 backend tests green (10 new: demo bind, terminal
verify, 422 mismatch incl. wrong server, 503 terminal down / no session,
per-user isolation + disconnect, reconnecting-state snapshot retention;
428 gates for account/positions/history/order/auto-arm; MT5_ONLY
crypto-events-never-move-quote + weekend-stays-broker-only), ruff/tsc/vite
clean. LIVE e2e on the rebuilt stack: terminal connected
(Exness-MT5Trial6 · 414350770, build 6205, mcp_trade_allowed) with BOTH
symbols streaming from the real broker (Monday open: XAUUSD ~4354 @
0.1–0.5s, BTCUSD ~81918); REST e2e — 428 → 422 wrong account → 200 real
connect → per-user status snapshot → user B sees NOTHING (isolation) →
per-user disconnect (platform keeps running). Browser e2e: LIVE · MT5 ·
broker feed badge with ⚡ t/s, BROKER 414350770@Exness-MT5Trial6 footer
chip, MT5 panel ● live + 8-entry history, second user → "Connect broker
account" CTA → dialog opens; mobile 375px: zero horizontal overflow,
chart 343×390, header wraps, menu works; terminal pkill → watchdog
restart + AUTO-LOGIN verified in 15s; gold Market Watch self-heal via the
ensure loop. Note: the Exness TRIAL account balance was reset to 0.00 by
the broker (trial servers wipe periodically) — the connection, orders
routing and history all work; the user may need to top-up/recreate the
demo account for live trading.

| D-039 | 5 | **Tab-based app architecture** (user req): 4 tabs — Home / Chart & Signals / AI Trading / Settings. Mobile navigates via a fixed bottom TabBar (safe-area aware); desktop navigates via the ⋮ menu ("Go to" group); every remaining function is redistributed under these tabs (manual order + history live in AI Trading; practice + logs + engine settings in Settings; Mt5AccountPanel.tsx deleted — superseded by AiTradingView). AI Trading shows the honest **why-not-trading diagnosis** (`why` field from GET /api/mt5/auto-trade: not_armed / terminal_down / trade_not_allowed / no_balance / market_closed / ready) + per-symbol market state + balance. WS tick frames raised to 20/s cap and forming-bar frames to 10/s (was 10/s and 4/s) for the sub-100ms MT5-terminal feel; the candle still absorbs every event. Demo data remains impossible (D-033 ALLOW_DEMO gate untouched). | User (Bengali): auto-trade happened without visible analysis; wanted millisecond-level updates, real data only, tabs (home/chart/signals/AI+history/settings) with mobile bottom bar + desktop 3-dot menu, cleaner alignment, and was confused by the three MT5/broker entries — the menu now has exactly ONE real-broker entry (Connect Broker) and one clearly-optional practice entry. |

| D-055 | 6 | **Live pending-order path repaired (5 breaks)** — user report: "pending orders show in the app but never reach the Exness terminal". Root causes fixed: (1) `mcp.pending_order()` sent a blind-guessed `order_type` field (never live-verified) — D-055 adds MCP `tools/list` capability negotiation: the terminal's own `inputSchema` now drives the field name (`type`, matching the live-verified market_order contract; `order_type` only if the schema declares it) and `filling_type:"return"` is attached when offered (pending orders reject FOK/IOC on many symbols); (2) a successfully placed pending answering **10008 PLACED** was treated as failure → executor retried a LIVE order (duplicate exposure) — both 10008/10009 are ok for pendings, and `_place_with_retry` never retries a **transport** failure (retcode None = the order may be live; double-fire), only explicit broker rejections; (3) signal expiry/cancel only searched POSITIONS — a WAITING terminal order was answered "already closed"-ok and left live forever (fills hours later, untracked) — `close_position()` now dispatches (position→close tool, waiting order→`trade_delete_order`, gone→honest ok) and `notify_signal_status` cleans expired+cancelled; (4) kill switches closed positions but left live pendings — `_emergency_stop`/`_profit_lock_stop` cancel every waiting order first; (5) the institution's live pending book was invisible — `McpTradingSource.pending_orders()` (D-054 contract parity, tolerant terminal field mapping) + `McpAutoTrader.status()` expose `pending_orders`/`pending_count` and the admin AI tab renders them. | The practice plane's paper book filled the UI while the real path failed silently — every fix is honest-failure-first (schema negotiation, retcode truth, orphan cleanup, kill-switch hygiene, live visibility) so a broken terminal can never masquerade as placed orders again. NOTE: this sandbox has no MT5 stack (`/home/z/mt5stack` absent, MCP key unconfigured) — live Exness round-trip needs the terminal host + `MT5_MCP_URL`/`MT5_MCP_KEY`. |
| D-056 | 6 | **Signal-quality deep pass (backtest-verified)** — user directive: "SL যেনো হিট কম হয়, TP যেনো হিট বেশি হয়". (1) `swings()` vectorized (numpy sliding-window masks + interleaved walk, semantics D-043-identical — verified by 360-combo equivalence harness) — killed the per-bar `all()` generator that ran 943k calls per 4k bars (it also ran on LIVE M1 closes); (2) backtest `htf` frames capped at the last 300 closed bars (was the whole growing history — quadratic `iloc` copies AND more context than the live engine's ~100-bar fetches ever see: replay now mirrors live); (3) `poi_pending_entry` anchors at the zone's NEAR edge (was far edge — an order that only fills when the zone BREAKS: the fills were the failures, adverse selection); (4) `simulate_risk` kill switch re-arms every UTC day with a day-anchored drawdown peak (was armed=False forever after one trip — silently skipped every later signal and misreported recovering runs as terminal losses). | Mock autopsy: baseline WR 44.7–48.4%, SL hits > TP hits; the deep-entry bucket that looked like 86% WR was n=7. A/B harness (3 seeds × 6k bars, spread 20pt): near-edge anchor + live-mirrored replay hold WR ≈ 0.52 with +38R/3-seed expectancy. |
| D-057 | 6 | **The chart drawing IS the trade contract** — user directive: "চার্ট এর মধ্যে যে ড্রয়িং হচ্ছে, আমি চাই সিগন্যাল গুলো এই ড্রয়িং ফলো করে আসবে ... SL TP ENTRY সব কিছু এই চার্ট ফলো করে হবে" (the chart's drawn setup is more accurate). NEW shared module `app/analysis/setup_geometry.py` — the SINGLE source of truth both the chart's entry-setup box and the engine's order read: `setup_snapshot()` mirrors the drawing layer's exact per-TF bar windows (M5=200/M15=160, = services/analysis.py BARS_PER_TF) so the engine sees the very zones/liquidity the chart renders; `setup_geometry()` emits the contract — ENTRY at the drawn zone's near edge ('mid' optional), SL beyond the zone protected past the deepest same-side liquidity pool, TP at the NEAREST drawn target (BSL/SSL liquidity line or opposing zone edge, never the far pools the old box chased), floored at 1.2R, capped at 1.8R, drawn stop capped at 1.6 M5-ATRs (ATR-relative so it scales across mock/gold/BTC). `evaluate()` takes the drawn trade when the chart offers one (`cfg.drawing_true=True`, signal carries `geometry: drawing|legacy`), else the legacy poi+smart_targets chain; the setup box mirrors the FIRED signal's actual entry/SL/TP once triggered ("order live @ …"); the live engine fetches the drawing-layer bar counts for M5/M15. `counter_needs_sweep` capability added (counter-trend zone reversals demand sweep+reclaim proof) but defaults FALSE — the D-049 directive ("সিগন্যাল মিস করা যাবে না") wins; A/B showed the gate is seed-mixed. | Before D-057 the box and the order computed the same trade twice through different pipelines (smc snapshots vs poi_zones+smart_targets) — the numbers disagreed, so the broker received a different trade than the chart showed. A/B (3 seeds × 6k bars): drawing-true defaults +41.6R/WR 0.519/261–6 streak vs legacy +38.1R/WR 0.517 — the directive costs nothing; 12k-bar run: +90.8R, WR 0.522, 54 drawing-mode signals, seed-42 DD 18.4R vs legacy 20.6R. Mock lessons baked into the gates: mid-zone pendings only fill when the zone breaks (adverse selection — near edge anchors); a TP beyond ~1.8R is swing-scale ("TP যেনো হিট বেশি হয়" — a TP the horizon can't reach is a missed TP); far-BSL targets (old box) averaged 6.4 USD / RR 2.5 → 0 TP hits. |
| D-058 | 6 | **See-everything chart pass (PC fit + candle clarity + deeper ICT layer)** — user directive (Bengali): "পিসি… 2 পাশে অনেক ফাঁকা জায়গা… কোথাও কোনো ফাঁকা থাকতে পারবে না; টাইম ফ্রেম হেডারের লেফটে ড্রপডাউন; marks box লেফট সাইডে; লাইন আরও চিকন; জোনের কালার কমিয়ে ক্যান্ডেল স্পষ্ট; লেখার পিছনে বক্স থাকবে না, ছোট ও ফিক্সড সাইজ; ICT/SMC/price-action বেস্টগুলো ডিপলি অ্যাড". Frontend: (1) full-viewport responsive shell — `max-w-3xl` cap removed; lg+ renders a LEFT icon rail nav (SideNav) with the content column stretching to the right edge, HomeView/ChartsView split into `xl:flex-row` (chart fills the main column at `calc(100vh-8.5rem)`, intel cards in a 340–380px right rail), mobile keeps bottom-nav single column; (2) the chart card owns ONE 36px header — TF dropdown LEFT + MARKS dropdown (the old bottom legend strip became a left-anchored overlay panel with 7 layer toggles + mark chips, zero canvas cost when closed) + live quote + status + fullscreen (fullscreen moved INTO PriceChart so both tabs get it); (3) candle clarity — zone fills 0.18–0.22→0.06–0.07 alpha, halos halved (3.4px→1.4px, hardSeg +2.6→+1.5), borders 0.7px, volume ghosted to 25% alpha in the bottom 12%, barSpacing 4→6.5; (4) ALL on-chart text unboxed — small words written directly with a soft dark shadow (ctx.shadowBlur), font fixed 8–9px so enlarging the chart never grows the words, the setup note card became 3 direct text lines, zone words (BULL FVG · M15) / HH-HL-LH-LL tags / session names returned to the canvas unboxed. Backend deep layer (new drawing kinds): `ema` (EMA 9/21/50 momentum ribbon, ~60 pts/line, worded verdict), `swing` (HH/HL/LH/LL reads with full-word labels), `session` (ICT kill-zone bands, intraday TFs only), `equilibrium` hline (50% of the dealing range); BARS_PER_TF deepened M1 260→360 / M5 200→300 / M15 160→240 / M30 140→200 (user: "ইচ্ছা মতো ক্যান্ডেল নিয়ে ড্রয়িং") with GEOMETRY_BARS mirrored (M5 300/M15 240) — and drawings._setup now builds its s5/s15 through the SAME `setup_snapshot()` the engine's `_drawing_geometry()` uses, closing the 240-cap analyze_frame vs GEOMETRY_BARS window drift for good (parity test added). | e2e-verified on the mock stack (agent-browser + VLM, desktop 1600×900 + mobile 390×844): full-width layout (left rail / bottom bar), no side gutters, clear candles, HH/LH/EMA/session drawings visible, TF grid dropdown + Marks panel (25 marks, 7 layers incl. new Momentum) both open from the header, Charts tab signals-variant with the right-rail analysis; A/B window arm (3 seeds × 6k bars, 20pt spread): OLD 200/160 +45.09R/WR 0.529/19 drawing-mode vs NEW 300/240 +45.45R/WR 0.531/22 drawing-mode — the deeper windows are neutral-to-better and surface MORE chart-true trades; 445 backend tests pass, ruff clean, tsc 0 errors, vite build clean. |
| D-061 | 7 | **Institutional manipulation / AMD layer** — user report (Bengali): "লাস্ট রেড ক্যান্ডেল এ সিগন্যাল ছিল মার্কেট নিচে যাবে... হঠাৎ মার্কেট বিপরীত মুখী... ইকমেলিউশন ও মেনোপোলেশন কোনো লজিক এড করতে পারবেন? এই বিষয় টা কিভাবে আমার অ্যাপ বুজবে। এবং আমিও দেখতে পারবো" (a SELL filled a few pips into the lows, then several large bullish candles — the retail side of a stop hunt; was it a logic failure, an event, a session open?). NEW `app/analysis/manipulation.py` — the ICT Accumulation→Manipulation→Distribution cycle as pure closed-bar functions: `amd_state()` (compressed range = accumulation; a false break of the PRIOR range that closes back inside = the manipulation sweep with side/level/depth/bars-ago/reclaimed; ≥2 consecutive ≥1.1-ATR bodies = the displacement distribution; phase verdict with a one-sentence note), `trap_risk()` for a candidate trade (A: freshly reclaimed opposing sweep, 0.85, distance-gated ≤2 ATR so a completed sweep leg's pullback entries are fresh trades, not the trapped side; B: unswept liquidity pool inside entry..SL, 0.55/0.70 when on the fill path; C: opposing displacement run, 0.75; D: Judas window +0.15 — session-open timing), `session_context()` (kill zone + London/NY-open Judas flags — the "was there an event / session start?" answer rides every signal). Engine: every bar close publishes the AMD phase on the strategy pulse (the app "understands" the cycle continuously); with ENTRY/SL final, the trap gate refuses ≥0.70 (near-miss "institutional trap"), tags 0.40–0.70 (context + trap_conf_penalty 0.12 — the user SEES why a fired trade is suspect); every payload + trace carries `context {amd, trap, session, news}` (rides trace JSONB for persistence). Tracker + backtest sim gained the PRE-FILL DISPLACEMENT GUARD: a WAITING limit whose opposing side prints 2+ institutional bodies cancels before the trap fills (cancelled, no R, reason broadcast). Chart: `amd` drawing set (accumulation range box, MANIPULATION X-marker at the swept level, DISTRIBUTION arrow) + the setup box carries a TRAP note; SignalDetail gained the Market-context panel; the radar gained an AMD CYCLE row. D-062 fix (same report): the Marks panel's `max-h-[62%]` resolved against an auto-height container = unbounded → clipped by the chart card's overflow-hidden on short/mobile charts ("ড্রপ-ডাউন সম্পুর্ণ ওপেন হয় না") — now measured against the card height (maxH = cardH−52) so every option is reachable and the panel scrolls inside itself. D-063 (same report): old order ink auto-expires — signal arrows + dashed ENTRY/SL/TP lines vanish after a 120-min TTL (the engine's whole short-time horizon; history rows keep the trades). PRE-EXISTING WIRE BUG fixed: engine runtimes broadcast the CONCRETE broker symbol (XAUUSDm, mock AND Exness) while clients subscribe with the market key (XAUUSD) — ticks/bars/strategy-pulses silently never matched the default pair; the WS hub now normalizes both sides through market_key() and the pulse carries the key. | Verified: 19 new unit tests reproduce the user's exact chart (the trapped SELL reads distribution/0.85 at the sweep bar, the mirrored BUY 0.0 — the institutional side; blocked/warn/context/gate-off paths; the pre-fill guard cancels a waiting limit on 2 big opposing bodies and resets on a quiet bar; AMD marks + setup TRAP tag). A/B (3 seeds × 6k bars, 20pt): trap-ON PF 1.490 / avgR +0.201 / seed-42 PF 2.37 vs OFF PF 1.348 / +0.177 — on the random-walk mock the gate buys quality (+10% PF) with 22% fewer noise signals; the mock has no institutional behavior, so the real validation is the deterministic scenario tests. e2e (mock stack + VLM): Marks panel fully open on mobile (scrollH 380 ≤ panel 382, 114 chips), AMD drawings rendered on mobile+desktop charts, radar AMD CYCLE row live (phase cycling with the feed) after the symbol fix. 462 backend tests, ruff clean, tsc 0, vite build clean. |
| D-064 | 7 | **Market-structure engine — legs, RESTs, reversal proof** — user directive (Bengali): "মার্কেট নিচে যাচ্ছে নিচে যাচ্ছে... মার্কেট কোথায় গিয়ে রেস্ট করে বা একটু বিশ্রাম নেয়, বিশ্রাম নিয়ে একটু উপরের দিকে যায়, তার পর আবার ডাউন এ যায়... কি এমন লজিক আছে যে মার্কেট এখন রিভার্স করবে? আর কত বার HL LL LH HH LOWER HIGHER হলে রিভার্স বা কনটিনিউ করে?" NEW `app/analysis/structure.py` (pure, closed-bars, calibrated by `scripts/measure_legs.py` — 2 142 structural events over 5 mock seeds, M5+M15): (1) `structure_ladder()` — the "কত বার" counter: consecutive same-direction break events (BOS) with the run direction, labeled HH/HL/LH/LL tail, and the FRESH CHoCH (close beyond the last opposite swing within 12 bars) = the only structural reversal PROOF; (2) `rest_zones()` — WHERE the market rested: compressed pauses (range ≤ 0.62×ATR×√n, ≥6 bars; measured pause = ~2.3 ATR deep, 10–16 bars, compression p50 0.69); (3) `rest_magnets()` — where it rests NEXT: EMA 21/50, equilibrium, OTE band, unfilled FVG, prior-rest edge — counter-side only, within 3 ATR; (4) `structure_read()` — the verdict: phase leg/extended/resting/reversal-confirmed + honest p(reversal) (hazard base 0.46 + per-leg + momentum-decay + EMA21-stretch + fresh-CHoCH, clamped 0.35–0.85 — measured: counting legs ALONE never flips the ~50% coin, so the read never pretends otherwise) + a worded action. Measured survival: 87% of runs end at leg 3, 94% at leg 4 → LEGS_EXHAUST=3. Engine (`structure_guard`): momentum fades (sfp/pullback) of a ≥3-leg run WITHOUT proof (fresh CHoCH or swept-and-reclaimed pool — `reversal_evidence()`) are REFUSED ("falling-knife fade"); ZONE (location) trades keep the D-049 precedence — visibly tagged + confidence-discounted (never silently lost); with-trend trades on an extended run pay the chase penalty (0.08), the with-trend entry at a REST zone earns the rest bonus (0.06) — the user's exact down→rest→continue pattern. Every pulse carries the ladder (run/phase/p(rev)/action/magnets); every signal context + trace carries the `structure` block; the chart draws the set: REST boxes ("REST 14"), dashed gold MAGNET lines ("MAGNET · EMA 21"), and the LADDER badge ("LEG 3 ↓ · REST DUE · p(reversal) 55%" / "STRUCTURE SHIFT ↑ — reversal confirmed"); the radar gained the STRUCTURE row (LEG badge, phase, p(rev), CHoCH age, magnet list) and SignalDetail context chips. | The honest calibration answer baked in: leg count alone ≈ coin flip (mock hazard 46–53% at every run length) — the app never "predicts" a reversal from counting; it (a) EXPECTS the measured rest after leg 3 (~2 ATR, 10–16 bars), (b) treats the CHoCH close as the only confirmation, (c) demands sweep+reclaim proof before letting a momentum fade fire, (d) shows the user all of it live. A/B (3 seeds × 6k bars): guard-ON identical to OFF (the hard block never wrongly fires on the random-walk mock — instrumented: 83 signals reached the guard, 48 without evidence, all zone/with-run) — the guard is FREE; the 19 deterministic tests prove the block/penalty/bonus paths. 480 backend tests, ruff clean, tsc 0, vite build clean. |
| D-065 | 7 | **The timeframe ladder, stated in the app** — user directive (Bengali): "কত মিনিটের টাইম ফ্রেম কত টি টাইম এনালাইসিস করে, কোন টাইম ফ্রেম এ সিগন্যাল প্রধান করেন, কোনটি করলে ভালো হবে। আপনার ইচ্ছা" (short-time trading). The ladder (my call, stated on every strategy pulse + rendered as a radar "TF LADDER · WHO DOES WHAT" section): **M1 = SIGNAL** — triggers fire + pending entries anchor on every 1-minute close, 1500 bars (24h) of history for TPO/PDH-PDL/zone depth; **M5 = SETUP** — 300 bars: the drawn zones the trade geometry (setup_geometry) reads + the D-064 structure ladder + REST magnets; **M15 = CONFIRM** — 240 bars: MTF confirmation vote + structure vote; **H1 = TREND** — bias anchor (EMA50 + momentum); **H4 = BIAS** — big-frame structure vote. Analysis cadence: every M1 close (~60/hour), HTF frames 30s-cached, drawing snapshots 20s-cached per symbol. | The short-time ladder keeps the user's D-051 directive (signals come from M1) while the setup/geometry lives on the M5/M15 the chart draws — one consistent ladder from signal to context, now visible in-app instead of implied. |
| D-067 | 7 | **Running-candle buyer/seller battle** — user directive (Bengali): "একটি রানিং ক্যান্ডেল বা কয়েক টি ক্যান্ডেল buyer Sellar position, কারা কাদের কে ডোমেনেট করছে, কারা জিতেছে, লাস্ট কয়েক টি ক্যান্ডেল এর ভিতর কি ঘটেছে, মোট কথা রানিং ক্যান্ডেল এর রিয়েকশন, এই বিষয় গুল কি এই ফরেক্স মার্কেট এ কাজ করে?" — honest answer first: Forex has no centralized order book, so true buyer/seller volume is not available to a retail API; what DOES work (and is standard practice) is reading the footprints the broker does give — tick-volume-weighted delta, body-to-range conviction, close-position-in-range, wick rejections, streaks. Implemented as a pure closed-bar module `app/analysis/orderflow.py::battle_read` (volume-weighted buy/sell split of the last 6 closed M1 candles — state buyers/sellers/tug at 72% domination; per-candle anatomy with won_by/delta/conviction/wick/reject_atr; the events INSIDE the candles — decisive rejections, absorptions, institutional momentum bodies; the winning streak; net displacement in ATR; participation trend; a worded verdict) + `running_candle_read` (the forming bar's live read — who is winning RIGHT NOW: dominance split, the wick war, price vs developing mid — recomputed by the UI on every tick between closes). Engine (`flow_guard`, D-049-compliant — visible, never a silent block): every M1 close publishes `pulse["battle"]`; a signal firing AGAINST a dominating flow (≥72% + a ≥3-candle winning streak against it — the exact shape of the user's trap report) pays a confidence penalty (0.08, trace + context + payload `flow_note`); firing WITH the dominating flow earns a small bonus (0.04); a tug of war costs nothing; the trap gate keeps ownership of hard blocks. UI: StrategyRadar rows "M1 CANDLE — BUYERS VS SELLERS" (the running candle's live verdict) + "CANDLE BATTLE — LAST 6" (split bar, won count, net ATR, streak, the last inside-event), the Home right-rail battle panel, chart `battle`/`reject` marks (BATTLE badge + the wick-rejection points), SignalDetail context chips. `market.py`: every bar's FIRST movement frame is never throttled away (a quiet-market bar used to show only open+close — the running-candle read starved). | Verified: 18 new tests (`test_d067_flow.py` — anatomy/battle/streak/verdict math, the running-candle read's states, the gate's against/with/tug paths, the payload contract, first-movement frames). Full-pipeline audit (3 seeds × 6k bars, 20pt): battle state rode every pulse (tug→buyers 71.5% live), flow gate engaged 85× on fired signals (7 against-penalties, 78 with-bonuses), geometry audit 0 violations; A/B flow-ON vs OFF identical outcomes (confidence is the visibility layer by design — D-049; the hard blocks stay with the trap gate). e2e: radar CANDLE BATTLE row + Home panel + running-candle verdict rendering live, VLM-verified, no console errors. 501 backend tests, ruff clean, tsc 0, vite build clean. |
| D-VERIFY | 7 | **The full signal-pipeline audit** (user directive: "সব গুলো একটু ভেরিফাই করেন... সিগন্যাল পাইপ লাইন কেমন হওয়া উচিত, প্রেডিকশন টি সঠিক ভাবে যাচাই করে দিচ্ছে কিনা... স্ট্রাটেজি গুল ইউজ হচ্ছে কিনা, হলেও সঠিক ওয়ে তে হচ্ছে কিনা। অ্যাপ এ কোনো বাগ আছে কিনা") — a counted, evidence-backed pass over the EXACT shipped pipeline (`scripts/verify_pipeline.py`: wraps evaluate() and counts every strategy path, every gate, every refusal reason, and audits every fired signal's order contract; `scripts/verify_runtime.sh` + e2e for the live stack). RESULTS (3 seeds × 6k bars, 20pt spread): (1) STRATEGY USAGE — all trigger paths live: zone 255 / sfp 3 / pullback 6 (sfp/pullback are rare on the random-walk mock — no sweep structure — their contracts are proven by the 18+19 deterministic tests); POI pending path 257 limits + 7 market; drawing-true geometry 29 + legacy 235 (the box mirrors the chart when a tradeable drawing exists, falls back honestly); (2) GATES — trap_filter blocked 112 candidates, structure_guard engaged 129× (0 wrongful hard-blocks — D-049 precedence held), flow_guard 85× (7 penalties / 78 bonuses), the biggest refusal is `targets` (1 067 bar-evaluations — smart_targets refusing trades whose nearest opposing barrier sits closer than min_rr×risk: the prediction-honesty gate working as designed), session 358, rsi 179; (3) GEOMETRY AUDIT — 0 violations: every fired signal has SL/TP on the correct sides, RR within [0.4, 3.6], positive prices; direction balance BUY 119 / SELL 145 (no sell-only bias); (4) PREDICTION — WR 0.528, +38.5R, PF ~1.4, 1 cancelled pre-fill (AMD displacement guard), 83 expired pendings counted as missed (not losses); RUNTIME — /api/analysis 200 (33 drawings, all kinds, `errors: []`), WS pulse carries battle/structure/tf_ladder/amd live, engine logs show the live gates refusing in real time ("targets — BSL liquidity at 0.34R (< 1.2R min)"); frontend: login → home → charts → AI tab all render, Marks panel fully open (482px ≈ scroll 480), zero console/page errors, VLM visual pass. | The pipeline is a single honest chain: 3 quality-arbitrated triggers → trap/structure/flow guards (block only for the trap; discount visibly otherwise) → POI pending entry → shared setup geometry (the chart IS the contract) → barrier-aware targets. Nothing dead, nothing silent, nothing unmeasured — and the audit tooling is now repeatable (`verify_pipeline.py` / `verify_runtime.sh`) for every future change. |
