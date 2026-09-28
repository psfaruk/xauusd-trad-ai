# XAUUSD AI Trading Platform — 100% REAL Market Data

Institutional-grade gold/BTC/oil/Nasdaq AI trading platform with ICT/SMC
analytics, an SFP signal engine, and a practice trading plane.

> **DATA SOURCE — ALWAYS REAL, NEVER FABRICATED**
>
> Every price, candle, tick, spread, history bar, backtest bar and account
> number comes from a REAL venue through a three-rung source ladder
> (highest priority first, per market, fully automatic failover + recovery):
>
> 1. **Local MetaTrader 5 terminal** (Exness-MT5Trial6, login 414350770)
>    running under user-space Wine with its MCP server on 127.0.0.1:22346.
> 2. **AURUM Terminal remote MT5 bridge** (user-directed source:
>    `https://u1m7j8csutd1-d.space-z.ai`) — the SAME Exness terminal served
>    as JSON (`/api/symbols`, `/api/candles`): real-time broker bid/ask,
>    candles and tick volumes. Set `REMOTE_MT5_URL` to repoint it.
> 3. **Live web exchanges** (always-on last resort): Binance PAXG/USDT
>    (1 oz allocated LBMA gold) + BTC/USDT order books via sub-second
>    WebSocket, plus NYMEX WTI (CL=F) / CME Nasdaq-100 (NQ=F) futures.
>
> If a higher-priority source dies, markets fail over to the next rung
> within ~15s and upgrade back automatically when it returns. Closed
> markets simply stop ticking — data is never invented, and every status
> endpoint reports which venue actually powers each market.

## Public live-data API (this host as a data source)

Any external app can pull the live feed (no auth):

```
GET /api/public/quotes    → live bid/ask + market state per symbol
GET /api/public/candles?symbol=XAUUSD&tf=M15&limit=500 → real OHLCV bars
GET /api/health           → source ladder status (remote/web/terminal health)
```

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│ MetaTrader 5 terminal (Windows app, build 6231)                  │
│ Exness-MT5Trial6 · login 414350770 · real account balance        │
│ runs under user-space Wine (Linux sandbox)                       │
│ exposes its built-in MCP server on 127.0.0.1:22346 (bearer key)  │
└──────────────┬───────────────────────────────────────────────────┘
               │ JSON-RPC over HTTP (initialize → tools/call)
┌──────────────▼───────────────────────────────────────────────────┐
│ market-service (bun, port 3003)                                  │
│  • providers.ts — local MT5 MCP client (the top rung):           │
│    get_marketwatch_symbols, get_chart_history,                   │
│    get_trading_account_info, get_trading_open_positions          │
│  • remotefeed.ts — AURUM Terminal remote MT5 bridge (rung 2):    │
│    1s /api/symbols poll (all symbols) + 10s M1 candle sync       │
│  • webfeed.ts — live web exchanges (rung 3): Binance WS ticks +  │
│    klines, Yahoo CL=F/NQ=F quotes/bars (rate-limit backoff)      │
│  • engine.ts — source ladder per market (failover ≤15s, upgrade  │
│    probes every 60s); SFP signal engine; real backtest on the    │
│    active source; honest closed-market detection                 │
│  • REST /api/* + socket.io (ticks, bars, signals, pulses)        │
│  • PUBLIC data API: /api/public/quotes, /api/public/candles      │
└──────────────┬───────────────────────────────────────────────────┘
               │ HTTP ?XTransformPort=3003 + socket.io (gateway)
┌──────────────▼───────────────────────────────────────────────────┐
│ Next.js 16 frontend (port 3000, single / route)                  │
│  React 19 + Tailwind 4 + lightweight-charts; login, dashboard,   │
│  charts (ICT/SMC drawings), AI trading tab, settings + bridge    │
└──────────────────────────────────────────────────────────────────┘
```

## Markets (Exness broker symbols)

| Platform symbol | MT5 symbol   | Digits | Notes                          |
| --------------- | ------------ | ------ | ------------------------------ |
| XAUUSD          | `XAUUSDm`    | 3      | Gold, 100 oz contract          |
| BTCUSD          | `BTCUSDm`    | 2      | Bitcoin — trades 24/7          |
| USOIL           | `USOILm`     | 3      | WTI crude, 1000 bbl contract   |
| USTEC           | `USTECm`     | 2      | Nasdaq-100 CFD                 |

FX reference legs for the DXY panel: `EURUSDm`, `USDJPYm`, `GBPUSDm`,
`USDCADm`, `USDSEKm`, `USDCHFm` — all added to the terminal Market Watch.

## Honest market states

Crypto trades 24/7. FX/metals/oil have sessions and weekend breaks. The
broker's own quote timestamp decides: when it goes stale (>120 s) the market
shows **CLOSED**, the last real quote stays on record with its real age, and
**no tick is ever fabricated**.

## Run

```bash
# 1. MetaTrader 5 terminal (Windows natively, or Wine on Linux) logged into
#    Exness-MT5Trial6 with the MCP server enabled on 127.0.0.1:22346
#    (put the bearer key at /home/z/mt5-stack/mcp_key.txt or adjust the path
#    in mini-services/market-service/src/providers.ts)

# 2. market-service
cd mini-services/market-service
bun install
bun run dev            # port 3003

# 3. frontend
cd ../..
bun install
bun run dev            # port 3000
```

## Cloud Deployment (Railway)

The repo deploys to [Railway](https://railway.app) as **one service running both
processes**: the Next.js frontend (UI + the `/api/*` REST proxy) on the
Railway-injected `$PORT`, and `market-service` (bun + socket.io) on `:3003`
behind it — supervised by `railway-start.sh`. A **`Dockerfile`** is included
(Railway auto-detects it) for a deterministic build environment; `railway.json`
remains as the Nixpacks fallback config (healthcheck on `/`, ON_FAILURE
restarts).

> **⚠️ RAM requirement — the #1 cause of "deploy fail":**
> `next build` peaks at **~0.7–0.8 GB RSS** for this app (webpack profile with
> memory optimizations; Turbopack would need ~1.1 GB). On a **512 MB** plan the
> build step itself is **OOM-killed and the deploy fails before the app ever
> boots**. Set the service to **at least 1 GB — 2 GB recommended** (Railway →
> service Settings → Memory) *before* deploying. Runtime idles well under that
> (Next standalone ~150 MB + market-service ~100–200 MB).

**Real-time data on the cloud — always on, never fabricated.** The
market-service walks a data-source ladder per market, so no Wine/MT5 terminal
is required in the container:

1. local MT5 MCP bridge (`MT5_MCP_URL`, default `127.0.0.1:22346` — absent on
   cloud, skipped automatically);
2. the **AURUM remote MT5 bridge** (`REMOTE_MT5_URL`, default
   `https://u1m7j8csutd1-d.space-z.ai`) — real Exness broker ticks;
3. **live web exchanges** — Binance order-book WebSocket (PAXG 1oz-gold, BTC,
   sub-second) + NYMEX WTI / CME Nasdaq futures.

It auto-upgrades back up the ladder whenever a higher rung recovers, and the
public JSON API (`/api/public/quotes`, `/api/public/candles`) is served from
whatever rung is live.

Deploy steps:

1. Push this repo to GitHub (main branch).
2. Railway → **New Project** → deploy the repo (Dockerfile is picked up
   automatically; `railway.json` is used if the builder is pinned to Nixpacks).
3. **Set service memory to ≥1 GB (2 GB recommended) — see the warning above.**
4. Service variables — **all optional**, the defaults already stream live data:
   - `REMOTE_MT5_URL` / `REMOTE_MT5_PORT` — the remote MT5 bridge to prefer
     over web exchanges (default: the AURUM terminal).
   - `MT5_MCP_URL` + `MT5_MCP_KEY` — only if you run your own MT5 terminal on
     a VPS and tunnel its MCP bridge.
   - `NEXT_PUBLIC_MARKET_WS_URL` — only if you host market-service separately;
     without it the frontend uses the same origin it was served from.
     `NEXT_PUBLIC_*` values are **inlined at build time** — change them, then
     redeploy.
5. Deploy and verify `GET /api/health` on the Railway domain — it reports the
   active `data_source` (e.g. `remote-mt5` or `web-live`) honestly.

If the terminal-side rungs are unreachable the app still boots and honestly
reports the state in its status endpoints and bridge diagnostics — **no price
is ever fabricated** (see the data-source mandate above). Local/dev stays one
command (`bun run dev`) in the sandbox where the terminal, market-service and
gateway auto-start.

## Backtest on REAL broker history

```bash
cd mini-services/market-service
bun run backtest.ts
```

Runs the platform's SFP engine over ~90 days of **native broker M15 bars**
fetched directly from the terminal for all four pairs and prints honest
per-pair stats (signals, win rate, profit factor, total R).

Example output (2026-06-29 → 2026-09-27):

```
XAUUSD  5,881 broker M15 bars · 165 signals · WR 25.9% · PF 0.70
BTCUSD  8,640 broker M15 bars · 233 signals · WR 35.7% · PF 1.11 · +20.04R
USOIL   5,877 broker M15 bars · 168 signals · WR 31.5% · PF 0.92
USTEC   5,875 broker M15 bars · 166 signals · WR 31.7% · PF 0.93
```

## Practice trading plane

The UI trades on a $10,000 **practice plane** marked-to-market on the real
MT5 quotes (orders fill at the broker's live bid/ask). The practice plane is
intentionally separate from the real Exness account, whose live balance /
equity / open positions are displayed read-only from the terminal.

## Repository layout

```
src/                     Next.js 16 app (single / route)
  app/                   layout, page (SPA shell)
  components/            PriceChart, Dashboard, Login, nav, signal UI, …
  views/                 HomeView, ChartsView, AiView, SettingsView
  lib/                   api client, socket.io client, auth, markets
  state/                 feed store (ticks/bars/pulses)
mini-services/
  market-service/        bun + socket.io backend (the MT5-only engine)
    src/providers.ts     ← the ONLY data source module (MCP client)
    src/engine.ts        real-time engine + SFP signals + backtest seed
    src/tape.ts          candle store fed by broker bars
    src/analysis.ts      ICT/SMC analytics + indicators
    src/trading.ts       practice planes
    src/index.ts         REST API + socket.io wiring
    backtest.ts          deep 90-day real-history backtest
prisma/ db/              data layer
Caddyfile                gateway (XTransformPort routing)
```

The earlier Vite + FastAPI implementation is preserved in the git history
(see commits before the Next.js port).
