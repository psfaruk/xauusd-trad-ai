# Deep Backtest Record — Real MT5 Broker History

## Run 2026-09-27 (22:48 UTC)

| | |
|---|---|
| **Date** | Sunday 2026-09-27, 22:48 UTC |
| **Terminal** | MetaTrader 5 build 6230, Exness-MT5Trial6 |
| **Account** | 414350770 (demo, Exness Technologies Ltd), server connected, balance 407.42 USD |
| **Data path** | MCP bridge 127.0.0.1:22346 → `get_chart_history` (native broker M15 bars) — no exchange API, no random data, no fallback source |
| **Script** | `mini-services/market-service/backtest.ts` — byte-identical to the Task 7 commit `c349a57` (only file mode 644→755 changed); engine untouched |
| **Command** | `bun backtest.ts` (no CLI args; 90-day window anchored to `Date.now()`) |
| **Runtime** | 2.8 s per full run; run twice → **byte-identical output** (deterministic) |
| **History depth** | All 4 pairs returned first bar **2026-06-29** = the window start → full 90-day depth reached; **no retries needed** despite the terminal being rebuilt earlier today |

## Results (90 days, M15, 2026-06-29 → 2026-09-27)

| Pair | Broker symbol | Bars | Signals | Won / Lost / Expired | Win rate | Profit factor | Net R |
|---|---|---:|---:|---|---:|---:|---:|
| XAUUSD | XAUUSDm | 5,862 | 200 | 56 / 139 / 5 | 28.7% | 0.81 | **−24.46R** |
| BTCUSD | BTCUSDm | 8,640 | 258 | 97 / 152 / 9 | 39.0% | 1.28 | **+47.35R** |
| USOIL | USOILm | 5,858 | 216 | 79 / 131 / 6 | 37.6% | 1.21 | **+27.84R** |
| USTEC | USTECm | 5,856 | 196 | 60 / 133 / 3 | 31.1% | 0.90 | **−10.94R** |

Direction / entry mix: XAUUSD BUY 99 / SELL 101 · limit 44 / market 156 — BTCUSD BUY 134 / SELL 124 · limit 53 / market 205 — USOIL BUY 123 / SELL 93 · limit 50 / market 166 — USTEC BUY 103 / SELL 93 · limit 49 / market 147.

## Comparison vs prior session (Task 7, engine identical)

| Pair | Bars (prior) | Signals (prior) | WR (prior) | PF (prior) | Net R (prior) | Direction of change |
|---|---:|---:|---:|---:|---:|---|
| XAUUSD | 5,881 | 165 | 25.9% | 0.70 | n/a | more signals, WR +2.8pt, PF 0.70→0.81 (still losing) |
| BTCUSD | 8,640 | 233 | 35.7% | 1.11 | +20.04R | more signals, WR +3.3pt, PF 1.11→1.28, net +20.04→+47.35R |
| USOIL | 5,877 | 168 | 31.5% | 0.92 | n/a | more signals, WR +6.1pt, PF 0.92→1.21 (now profitable) |
| USTEC | 5,875 | 166 | 31.7% | 0.93 | n/a | similar signals, WR −0.6pt, PF 0.93→0.90 (still losing) |

**Why the numbers moved:** the window is a rolling `now − 90d`, so this run evaluates a *different* slice of history than Task 7's run (newest days added, oldest days dropped), and the terminal was rebuilt today with history re-synced from the Exness server. Bar counts agree with the prior run within 0.3% (5,856–5,862 vs 5,875–5,881 for the weekend-closed pairs; BTCUSD exactly 8,640 = 90×96 in both runs), confirming full and consistent depth. The engine (`backtest.ts`) is unchanged — these deltas are data-window effects, not code effects.

## Methodology (unchanged from Task 7)

- **Engine**: the SAME SFP (swing-failure-pivot) detector the live app uses — swing detection (depth 2), wick-reversal through pivot with ATR guard (wick ≥ 0.3×ATR), trend gate (EMA20 vs EMA50), session gate, RSI 30–70 gate; evaluated every other bar (stride 2).
- **Risk model**: SL beyond the swept pivot ± 0.2×ATR buffer, clamped to 0.8–1.2×ATR; TP at fixed RR 2; expiry after 24 bars (expired trades marked-to-market at the expiry close).
- **Deterministic limit fills**: limit entries only count if a later bar actually touches the entry price within 7 bars; market entries fill at the signal bar close. No randomness anywhere.
- **Pessimistic both-touch**: any bar that touches both SL and TP is scored as a **loss**.

## Honest verdict

- **BTCUSD and USOIL are profitable** on this 90-day window (PF 1.28 / 1.21; +47.35R / +27.84R). BTCUSD has now cleared PF > 1.1 on **both** independent 90-day windows tested.
- **XAUUSD loses on both windows** (PF 0.70 then 0.81; −24.46R today) — the platform's headline pair is currently the strategy's weakest. USTEC is borderline-negative on both (PF ~0.9).
- These are signal-level R multiples with zero fees/slippage/swap; live results will be worse. Treat PF ≈ 1.1 as break-even, not edge.
- Sunday context: XAUUSDm/USOILm/USTECm markets were closed at run time — history was served from broker/server storage as normal; BTCUSDm (24/7 crypto) is live.
