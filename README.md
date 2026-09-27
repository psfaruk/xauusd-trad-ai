# XAUUSD AI Trading Platform — 100% MetaTrader 5 Data

Institutional-grade gold/BTC/oil/Nasdaq AI trading platform with ICT/SMC
analytics, an SFP signal engine, and a practice trading plane.

> **DATA SOURCE — MANDATORY, PERMANENT, NO EXCEPTIONS**
>
> Every price, candle, tick, spread, history bar, backtest bar and account
> number in this app comes **only** from the user's real **MetaTrader 5**
> terminal (Exness-MT5Trial6, login 414350770, build 6231). There is no
> Binance, no Yahoo, no ECB, no simulated/random fallback anywhere in the
> codebase. If the terminal is unreachable, prices go stale and the status
> honestly reports `disconnected` — data is never invented.

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
│  • providers.ts — the ONLY data path: get_marketwatch_symbols,   │
│    get_chart_history, get_trading_account_info,                  │
│    get_trading_open_positions (MCP tools)                        │
│  • engine.ts — 1s Market Watch poll (all symbols, one round      │
│    trip) + 5s per-market M1 sync; SFP signal engine; real        │
│    backtest on broker history; honest closed-market detection    │
│  • REST /api/* + socket.io (ticks, bars, signals, pulses)        │
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
