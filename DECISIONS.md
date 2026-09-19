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
