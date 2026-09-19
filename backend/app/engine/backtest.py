"""Backtest runner (verification harness for the SFP engine).

Runs the EXACT same `engine.evaluate()` pipeline as the live engine over a
historical M15 series (bars replayed one-by-one, H1 context derived with
`closed_h1_asof` to prevent lookahead), then simulates the SPEC §8.4 tracker:

- SL/TP from bar highs/lows; when a bar touches BOTH, SL wins (pessimistic),
- expiry after `expiry_bars` M15 closes -> result_r from the closing price,
- spread applied as an entry cost via `spread_cost_r` (optional, default 0).

Sources of data:
- `--source mock`  deterministic MockDataSource history (default seed),
- `--csv file.csv` OHLCV bars (time_utc, o, h, l, c, v) e.g. MT5 export.

CLI: python -m app.engine.backtest [--bars N] [--csv path] [--inject-every N]
                                    [--json out.json] [--csv-out out.csv]
Prints a human-readable report and writes machine-readable artifacts.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from app.engine.config import EngineConfig
from app.engine.engine import NewsState, closed_h1_asof, evaluate
from app.mt5.base import TIMEFRAME_MINUTES


@dataclass
class BacktestSignal:
    ts: datetime
    direction: str
    entry: float
    sl: float
    tp: float
    confidence: float
    session: str
    status: str = "active"
    result_r: float | None = None
    exit_ts: datetime | None = None


@dataclass
class BacktestResult:
    cfg: EngineConfig
    bars_tested: int
    date_from: datetime | None
    date_to: datetime | None
    signals: list[BacktestSignal] = field(default_factory=list)

    def stats(self) -> dict:
        closed = [s for s in self.signals if s.status != "active"]
        won = [s for s in closed if s.status == "won"]
        lost = [s for s in closed if s.status == "lost"]
        expired = [s for s in closed if s.status == "expired"]
        rs = [s.result_r for s in closed if s.result_r is not None]
        gains = sum(r for r in rs if r > 0)
        pains = abs(sum(r for r in rs if r < 0))
        curve = peak = max_dd = 0.0
        for r in rs:
            curve += r
            peak = max(peak, curve)
            max_dd = max(max_dd, peak - curve)
        by_session: dict[str, dict] = {}
        for s in closed:
            slot = by_session.setdefault(
                s.session, {"signals": 0, "won": 0, "lost": 0, "expired": 0}
            )
            slot["signals"] += 1
            if s.status in slot:
                slot[s.status] += 1
        return {
            "bars_tested": self.bars_tested,
            "date_from": self.date_from.isoformat() if self.date_from else None,
            "date_to": self.date_to.isoformat() if self.date_to else None,
            "total_signals": len(self.signals),
            "won": len(won),
            "lost": len(lost),
            "expired": len(expired),
            "active": len(self.signals) - len(closed),
            "win_rate": round(len(won) / (len(won) + len(lost)), 4) if (won or lost) else None,
            "avg_r": round(sum(rs) / len(rs), 4) if rs else None,
            "total_r": round(sum(rs), 4) if rs else None,
            "expectancy": round(sum(rs) / len(rs), 4) if rs else None,
            "profit_factor": (
                round(gains / pains, 4) if pains > 0 else (None if gains == 0 else float("inf"))
            ),
            "max_drawdown_r": round(max_dd, 4),
            "by_session": by_session,
        }


def resample_ohlc(m15: pd.DataFrame, dst_min: int, src_min: int = 15) -> pd.DataFrame:
    """Aggregate the M15 frame into bigger buckets (dst_min % 15 == 0).

    The trailing bucket is dropped when incomplete (fewer than dst_min/src_min
    bars) so backtests never see a half-formed higher-timeframe bar.
    """
    from app.mt5.base import RATES_COLUMNS

    if dst_min % src_min != 0:
        raise ValueError("resample target must be a multiple of the source tf")
    df = m15.set_index("time_utc")
    agg = df.resample(f"{dst_min}min", label="left", closed="left").agg(
        {"o": "first", "h": "max", "l": "min", "c": "last", "v": "sum"}
    )
    agg = agg.dropna(subset=["o", "c"])
    # drop trailing incomplete bucket (only the last one can be incomplete)
    counts = df.resample(f"{dst_min}min", label="left", closed="left").size()
    if len(counts) and counts.iloc[-1] < dst_min // src_min and len(agg):
        agg = agg.iloc[:-1]
    return agg.reset_index()[RATES_COLUMNS]


class _SimTracker:
    """Bar-based §8.4 simulation (SL-first pessimism)."""

    def __init__(self, cfg: EngineConfig) -> None:
        self.cfg = cfg
        self.open_sig: BacktestSignal | None = None
        self.bars_held = 0

    def step(self, bar: pd.Series) -> BacktestSignal | None:
        """Advance one M15 bar; return the signal if it closed this bar."""
        sig = self.open_sig
        if sig is None:
            return None
        self.bars_held += 1
        high, low, close = float(bar["h"]), float(bar["l"]), float(bar["c"])
        if sig.direction == "BUY":
            if low <= sig.sl and high >= sig.tp:
                sig.status, sig.result_r = "lost", -1.0  # pessimistic
            elif low <= sig.sl:
                sig.status, sig.result_r = "lost", -1.0
            elif high >= sig.tp:
                sig.status = "won"
                sig.result_r = round(
                    (sig.tp - sig.entry) / (sig.entry - sig.sl), 4
                )
            elif self.bars_held >= self.cfg.expiry_bars:
                sig.status = "expired"
                sig.result_r = round((close - sig.entry) / (sig.entry - sig.sl), 4)
            else:
                return None
        else:
            if high >= sig.sl and low <= sig.tp:
                sig.status, sig.result_r = "lost", -1.0
            elif high >= sig.sl:
                sig.status, sig.result_r = "lost", -1.0
            elif low <= sig.tp:
                sig.status = "won"
                sig.result_r = round(
                    (sig.entry - sig.tp) / (sig.sl - sig.entry), 4
                )
            elif self.bars_held >= self.cfg.expiry_bars:
                sig.status = "expired"
                sig.result_r = round((sig.entry - close) / (sig.sl - sig.entry), 4)
            else:
                return None
        bar_time = bar["time_utc"]
        sig.exit_ts = (
            bar_time.to_pydatetime() if hasattr(bar_time, "to_pydatetime") else bar_time
        )
        self.open_sig = None
        return sig


def run_backtest(
    m15: pd.DataFrame,
    cfg: EngineConfig | None = None,
    spread_points: float = 0.0,
    news: NewsState | None = None,
) -> BacktestResult:
    """Replay the M15 series through the live evaluate() pipeline."""
    cfg = cfg or EngineConfig()
    tf_min = TIMEFRAME_MINUTES[cfg.timeframe]
    if cfg.timeframe != "M15":
        raise ValueError("backtest base timeframe must be M15 (v1)")
    h1_full = resample_ohlc(m15, TIMEFRAME_MINUTES[cfg.trend_tf])

    warmup = max(cfg.sfp_lookback, cfg.trend_ema, cfg.rsi_period, cfg.atr_period) + 5
    result = BacktestResult(
        cfg=cfg,
        bars_tested=max(0, len(m15) - warmup),
        date_from=m15["time_utc"].iloc[warmup].to_pydatetime() if len(m15) > warmup else None,
        date_to=m15["time_utc"].iloc[-1].to_pydatetime() if len(m15) else None,
    )
    sim = _SimTracker(cfg)
    cooldown_until = -1

    for i in range(warmup, len(m15)):
        hist = m15.iloc[: i + 1].reset_index(drop=True)
        bar = m15.iloc[i]
        bar_close_time = bar["time_utc"] + timedelta(minutes=tf_min)

        # no evaluation while a simulated position is open (max_positions=1)
        # but the tracker steps on EVERY closed bar
        closed_sig = sim.step(bar)
        _ = closed_sig  # tracked in-place (already in result.signals)

        if sim.open_sig is not None or i < cooldown_until:
            continue

        h1 = closed_h1_asof(h1_full, bar_close_time)
        if len(h1) < cfg.trend_ema + 1:
            continue
        ev = evaluate(hist, h1, bar_close_time, cfg, spread_points, news=news)
        if ev.signal is None:
            continue
        p = ev.signal
        sig = BacktestSignal(
            ts=bar["time_utc"].to_pydatetime(),
            direction=p["direction"],
            entry=p["entry"],
            sl=p["sl"],
            tp=p["tp"],
            confidence=p["confidence"],
            session=p["session"],
        )
        result.signals.append(sig)
        sim.open_sig = sig
        sim.bars_held = 0
        cooldown_until = i + cfg.cooldown_bars + 1

    return result


# ------------------------------------------------------------------ data load


def load_mock_history(bars: int = 3000, seed: int = 42) -> pd.DataFrame:
    """Deterministic 30-day mock history pulled through MockDataSource."""
    from app.mt5.mock_source import MockDataSource

    src = MockDataSource(seed=seed, time_scale=0.0)
    # freeze clock at the end of the 30-day window
    src.advance_minutes(src.HISTORY_DAYS * 24 * 60 - 1)
    df = src._rates_sync("M15", bars)  # noqa: SLF001 — test harness access
    return df.reset_index(drop=True)


def inject_sweep_series(m15: pd.DataFrame, every_bars: int, seed: int = 42) -> pd.DataFrame:
    """Overlay crafted SFP sweeps onto a mock M15 series (engine sanity mode).

    Rather than using MockDataSource.inject_sweep (M1-level), this directly
    rewrites M15 bars so the sweep pattern is exact regardless of aggregation.
    """
    df = m15.copy()
    lookback = 20
    atr_proxy = (df["h"] - df["l"]).rolling(14).mean()
    for i in range(lookback + 50, len(df) - 1, every_bars):
        prior = df.iloc[i - lookback : i]
        atr = float(atr_proxy.iloc[i]) or 1.0
        if i % (2 * every_bars) == 0:
            swept = float(prior["l"].min())
            c = swept + 0.15 * atr
            o = c - 0.05 * atr
            low = swept - max(0.05 * atr, 0.5 * atr)
            high = max(o, c) + 0.10 * atr
        else:
            swept = float(prior["h"].max())
            c = swept - 0.15 * atr
            o = c + 0.05 * atr
            high = swept + max(0.05 * atr, 0.5 * atr)
            low = min(o, c) - 0.10 * atr
        df.iloc[i, df.columns.get_indexer(["o", "h", "l", "c"])] = [o, high, low, c]
    return df


def load_csv(path: str) -> pd.DataFrame:
    from app.mt5.base import RATES_COLUMNS

    df = pd.read_csv(path)
    if "time_utc" not in df.columns and "time" in df.columns:
        df = df.rename(columns={"time": "time_utc"})
    df["time_utc"] = pd.to_datetime(df["time_utc"], utc=True)
    for col in ("o", "h", "l", "c"):
        df[col] = df[col].astype(float)
    df["v"] = df.get("v", pd.Series([0] * len(df))).astype(int)
    df = df.sort_values("time_utc").reset_index(drop=True)
    return df[RATES_COLUMNS]


# ---------------------------------------------------------------------- report


def format_report(res: BacktestResult, label: str) -> str:
    s = res.stats()
    lines = [
        "=" * 64,
        f" SFP ENGINE BACKTEST — {label}",
        "=" * 64,
        f" period        : {s['date_from']} -> {s['date_to']} UTC",
        f" M15 bars     : {s['bars_tested']}",
        f" signals      : {s['total_signals']} (won {s['won']} / lost {s['lost']}"
        f" / expired {s['expired']} / active {s['active']})",
        f" win rate     : {s['win_rate']}",
        f" total R      : {s['total_r']}",
        f" expectancy   : {s['expectancy']} R per signal",
        f" profit factor: {s['profit_factor']}",
        f" max drawdown : {s['max_drawdown_r']} R",
        " by session   : " + json.dumps(s["by_session"]),
        "=" * 64,
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="SFP engine backtest")
    p.add_argument("--bars", type=int, default=2880, help="M15 bars from mock history")
    p.add_argument("--csv", type=str, default=None, help="OHLCV csv (time_utc,o,h,l,c,v)")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--inject-every", type=int, default=0,
                   help="overlay a crafted SFP sweep every N bars (engine sanity mode)")
    p.add_argument("--json", type=str, default=None, help="write stats+signals JSON here")
    p.add_argument("--csv-out", type=str, default=None, help="write signals CSV here")
    args = p.parse_args(argv)

    if args.csv:
        m15 = load_csv(args.csv)
        label = f"CSV {args.csv}"
    else:
        m15 = load_mock_history(args.bars, seed=args.seed)
        label = f"mock seed={args.seed} bars={len(m15)}"
    if args.inject_every:
        m15 = inject_sweep_series(m15, args.inject_every, seed=args.seed)
        label += f" +injected-sweep-every-{args.inject_every}"

    res = run_backtest(m15)
    print(format_report(res, label))

    if args.json:
        payload = {
            "label": label,
            "config": res.cfg.model_dump(),
            "stats": res.stats(),
            "signals": [
                {
                    "ts": s.ts.isoformat(),
                    "direction": s.direction,
                    "entry": s.entry,
                    "sl": s.sl,
                    "tp": s.tp,
                    "confidence": s.confidence,
                    "session": s.session,
                    "status": s.status,
                    "result_r": s.result_r,
                    "exit_ts": s.exit_ts.isoformat() if s.exit_ts else None,
                }
                for s in res.signals
            ],
        }
        Path(args.json).write_text(json.dumps(payload, indent=2))
        print(f"json report -> {args.json}")
    if args.csv_out:
        pd.DataFrame(
            [
                {
                    "ts": s.ts, "direction": s.direction, "entry": s.entry,
                    "sl": s.sl, "tp": s.tp, "confidence": s.confidence,
                    "session": s.session, "status": s.status, "result_r": s.result_r,
                }
                for s in res.signals
            ]
        ).to_csv(args.csv_out, index=False)
        print(f"signals csv -> {args.csv_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
