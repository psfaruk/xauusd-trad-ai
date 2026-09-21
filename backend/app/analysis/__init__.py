"""Market analysis package (D-042).

Three layers, all PURE functions on RATES_COLUMNS DataFrames
([time_utc, o, h, l, c, v], ascending, closed bars only) so the LIVE
engine, the BACKTEST runner and the /api/analysis endpoint all see
exactly the same numbers:

- indicators.py — the classic MT5/TradingView indicator suite
  (EMA/SMA, RSI, MACD, Bollinger, Stochastic, ADX/DI, ATR, VWAP, CCI,
  OBV, swing fractals, volume z-scores).
- smc.py — ICT / Smart-Money-Concepts: market structure (HH/HL/LH/LL +
  BOS/CHoCH), order blocks, fair value gaps, liquidity pools
  (equal highs/lows), supply & demand zones, premium/discount + OTE,
  kill zones.
- orderflow.py — institutional-activity estimation from broker tick
  volume: volume profile (POC/VAH/VAL), buy/sell delta proxy, whale
  events (momentum entries, liquidity sweeps/stop hunts, absorption).
- context.py — per-TF snapshot + MTF bias + the engine confluence
  builder (the single source of truth for "why this signal").
"""
