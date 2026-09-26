"""Backtest runner (verification harness for the D-041/D-050 engine).

Runs the EXACT same `engine.evaluate()` pipeline as the live engine over a
historical base-TF series (M5 by default; bars replayed one-by-one, higher
TF context derived with `closed_asof` to prevent lookahead), then simulates
the SPEC §8.4 tracker:

- D-050 PENDING stage: limit-entry signals start as PENDING orders; a
  BUY limit fills when a later bar's LOW trades down to the entry, a SELL
  limit when a bar's HIGH trades up to it (fill at the limit price);
  `pending_expiry_bars` bars without a fill -> expired UNFILLED (result_r
  None — a missed trade, never a loss);
- SL/TP from bar highs/lows; when a bar touches BOTH, SL wins (pessimistic);
- expiry after `expiry_bars` base closes -> result_r from the closing price;
- spread applied as an entry cost via `spread_cost_r` (optional, default 0).

Sources of data:
- `--source mock`  deterministic MockDataSource history (default seed),
  auto-resampled to the engine timeframe (M1 history -> M5 bars),
- `--csv file.csv` OHLCV bars ALREADY on the engine timeframe
  (time_utc, o, h, l, c, v) e.g. MT5 export.

CLI: python -m app.engine.backtest [--bars N] [--csv path] [--tf M5]
                                    [--entry-mode poi_limit|market]
                                    [--inject-every N] [--json out.json]
                                    [--csv-out out.csv]
Prints a human-readable report and writes machine-readable artifacts.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from app.engine.config import EngineConfig
from app.engine.engine import NewsState, evaluate
from app.engine.executor import size_lot
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
    trigger: str = "sfp"
    status: str = "active"
    result_r: float | None = None
    exit_ts: datetime | None = None
    # D-050 — pending-limit order fields
    entry_type: str = "market"  # "market" | "limit"
    market_ref: float | None = None  # market price at signal time
    filled: bool = True  # market signals are born filled
    fill_ts: datetime | None = None
    geometry: str = "legacy"  # D-057 — "drawing" | "legacy"
    # D-061 — pre-fill displacement guard (mirrors the live tracker)
    atr_ref: float | None = None  # ATR at signal time
    close_reason: str | None = None

    @property
    def is_pending(self) -> bool:
        return self.entry_type == "limit" and not self.filled


@dataclass
class BacktestResult:
    cfg: EngineConfig
    bars_tested: int
    date_from: datetime | None
    date_to: datetime | None
    signals: list[BacktestSignal] = field(default_factory=list)

    def stats(self) -> dict:
        closed = [s for s in self.signals if s.status != "active" and s.status != "pending"]
        won = [s for s in closed if s.status == "won"]
        lost = [s for s in closed if s.status == "lost"]
        expired = [s for s in closed if s.status == "expired"]
        # D-050 — expired pendings that never filled: missed trades, not losses
        # D-061 — cancelled pendings (AMD displacement guard) are missed
        # trades too: the order never filled, no trade, no R
        unfilled = [s for s in closed
                    if s.entry_type == "limit" and not s.filled
                    and s.status in ("expired", "cancelled")]
        limit_signals = [s for s in self.signals if s.entry_type == "limit"]
        filled_sigs = [s for s in self.signals if s.filled]
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
        by_trigger: dict[str, dict] = {}
        for s in self.signals:
            slot = by_trigger.setdefault(
                s.trigger, {"signals": 0, "won": 0, "lost": 0, "expired": 0}
            )
            slot["signals"] += 1
            if s.status in slot:
                slot[s.status] += 1
        hours = None
        if self.date_from and self.date_to:
            hours = max((self.date_to - self.date_from).total_seconds() / 3600.0, 1e-9)
        return {
            "bars_tested": self.bars_tested,
            "date_from": self.date_from.isoformat() if self.date_from else None,
            "date_to": self.date_to.isoformat() if self.date_to else None,
            "total_signals": len(self.signals),
            "signals_per_hour": round(len(self.signals) / hours, 2) if hours else None,
            "won": len(won),
            "lost": len(lost),
            "expired": len(expired),
            "unfilled": len(unfilled),  # D-050 — missed fills (no trade taken)
            "filled": len(filled_sigs),
            "fill_rate": (
                round(len(filled_sigs) / len(limit_signals), 4)
                if limit_signals else 1.0
            ),
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
            "by_trigger": by_trigger,
        }


def resample_ohlc(base: pd.DataFrame, dst_min: int, src_min: int = 1) -> pd.DataFrame:
    """Aggregate the base frame into bigger buckets (dst_min % src_min == 0).

    The trailing bucket is dropped when incomplete (fewer than dst_min/src_min
    bars) so backtests never see a half-formed higher-timeframe bar.
    """
    from app.mt5.base import RATES_COLUMNS

    if dst_min % src_min != 0:
        raise ValueError("resample target must be a multiple of the source tf")
    df = base.set_index("time_utc")
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
    """Bar-based §8.4 simulation (SL-first pessimism, D-042 multi-entry,
    D-050 pending limit orders).

    Holds up to cfg.max_positions FILLED signals plus a separate budget of
    cfg.max_pending_signals PENDING limit orders — mirroring the live
    engine's split budget exactly (unfilled pendings must never block new
    signals).
    """

    def __init__(self, cfg: EngineConfig, spread_price: float = 0.0) -> None:
        self.cfg = cfg
        # D-VERIFY (external report §13/§15) — bid/ask-exact fill model:
        # the replayed bars are BID-based (MT5 copy_rates convention), so
        # BUY limits fill on the ASK touch (bid low <= entry - spread)
        # while SELL limits fill on the BID touch (exact); SELL exits
        # trigger on the ask (high >= sl - spread, low <= tp - spread).
        # The old low<=entry/high>=sl symmetry was one-spread optimistic
        # on BUY fills, SELL SLs and SELL TPs.
        self.spread_price = float(spread_price)
        self.open_sigs: list[BacktestSignal] = []
        self._held: dict[int, int] = {}  # id(sig) -> bars held (after fill)
        self._pbars: dict[int, int] = {}  # id(sig) -> bars pending (D-050)
        self._drun: dict[int, int] = {}  # id(sig) -> opposing displacement run (D-061)

    @property
    def full(self) -> bool:
        """D-050 — BOTH budgets checked: filled positions AND pendings."""
        filled = [s for s in self.open_sigs if s.filled]
        pending = [s for s in self.open_sigs if not s.filled and s.status == "pending"]
        return (
            len(filled) >= max(1, self.cfg.max_positions)
            or len(pending) >= max(1, self.cfg.max_pending_signals)
        )

    def add(self, sig: BacktestSignal) -> None:
        sig.status = "pending" if sig.is_pending else "active"
        self.open_sigs.append(sig)
        self._held[id(sig)] = 0
        self._pbars[id(sig)] = 0
        self._drun[id(sig)] = 0

    def step(self, bar: pd.Series) -> list[BacktestSignal]:
        """Advance one base-TF bar; return signals that closed this bar."""
        if not self.open_sigs:
            return []
        closed: list[BacktestSignal] = []
        for sig in list(self.open_sigs):
            if self._step_one(sig, bar):
                self.open_sigs.remove(sig)
                self._held.pop(id(sig), None)
                self._pbars.pop(id(sig), None)
                self._drun.pop(id(sig), None)
                closed.append(sig)
        return closed

    def _step_one(self, sig: BacktestSignal, bar: pd.Series) -> bool:
        """True when the signal resolved on this bar (SL-first pessimism)."""
        high, low, close = float(bar["h"]), float(bar["l"]), float(bar["c"])

        # D-050 — PENDING stage: wait for the market to retrace to the limit
        # D-VERIFY — bid/ask-exact: BUY fills on the ASK touch (bid bars
        # need low <= entry - spread); SELL fills on the BID touch (exact)
        if sig.status == "pending":
            self._pbars[id(sig)] += 1
            buy_touch = (
                sig.direction == "BUY"
                and low <= sig.entry - self.spread_price
            )
            sell_touch = sig.direction == "SELL" and high >= sig.entry
            if buy_touch or sell_touch:
                sig.filled = True
                sig.fill_ts = bar["time_utc"].to_pydatetime() \
                    if hasattr(bar["time_utc"], "to_pydatetime") else bar["time_utc"]
                sig.status = "active"
                # same-bar pessimism: the fill happened somewhere in the
                # bar — if the bar also swept the SL, SL wins
                # (BUY SL is a bid-side stop: exact; SELL SL is an
                # ask-side stop: bid high >= sl - spread)
                if sig.direction == "BUY" and low <= sig.sl:
                    sig.status, sig.result_r = "lost", -1.0
                    return self._exit(sig, bar)
                if sig.direction == "SELL" \
                        and high >= sig.sl - self.spread_price:
                    sig.status, sig.result_r = "lost", -1.0
                    return self._exit(sig, bar)
                return False
            # D-061 — PRE-FILL DISPLACEMENT GUARD (mirrors the live
            # tracker): 2+ consecutive institutional bodies AGAINST a
            # waiting limit = the AMD distribution already began —
            # cancel before the trap fills
            if sig.atr_ref is not None and sig.atr_ref > 0:
                body = close - float(bar["o"])
                opposing = (
                    (body > 0 and sig.direction == "SELL")
                    or (body < 0 and sig.direction == "BUY")
                )
                if opposing and abs(body) >= 1.1 * sig.atr_ref:
                    self._drun[id(sig)] += 1
                else:
                    self._drun[id(sig)] = 0
                if self._drun[id(sig)] >= 2:
                    sig.status = "cancelled"
                    sig.result_r = None  # never filled — missed trade, no R
                    sig.close_reason = (
                        "pre-fill displacement: 2+ institutional bodies "
                        "against the trade (AMD guard)"
                    )
                    return self._exit(sig, bar)
            if self._pbars[id(sig)] >= self.cfg.pending_expiry_bars:
                sig.status = "expired"
                sig.result_r = None  # never filled — missed trade, no R
                return self._exit(sig, bar)
            return False

        # ACTIVE stage (market-born signals start here; limit signals after fill)
        # D-VERIFY — exits are side-exact: BUY exits on the BID (exact on
        # bid bars); SELL exits on the ASK (sl: high >= sl - spread,
        # tp: low <= tp - spread); a SELL expiry buys back at the ask
        # (close + spread) — the spread rides the exit, not a post-hoc tax
        self._held[id(sig)] += 1
        if sig.direction == "BUY":
            if low <= sig.sl:
                sig.status, sig.result_r = "lost", -1.0
            elif high >= sig.tp:
                sig.status = "won"
                sig.result_r = round(
                    (sig.tp - sig.entry) / (sig.entry - sig.sl), 4
                )
            elif self._held[id(sig)] >= self.cfg.expiry_bars:
                sig.status = "expired"
                sig.result_r = round((close - sig.entry) / (sig.entry - sig.sl), 4)
            else:
                return False
        else:
            if high >= sig.sl - self.spread_price:
                sig.status, sig.result_r = "lost", -1.0
            elif low <= sig.tp - self.spread_price:
                sig.status = "won"
                sig.result_r = round(
                    (sig.entry - sig.tp) / (sig.sl - sig.entry), 4
                )
            elif self._held[id(sig)] >= self.cfg.expiry_bars:
                sig.status = "expired"
                sig.result_r = round(
                    (sig.entry - close - self.spread_price)
                    / (sig.sl - sig.entry), 4
                )
            else:
                return False
        return self._exit(sig, bar)

    @staticmethod
    def _exit(sig: BacktestSignal, bar: pd.Series) -> bool:
        bar_time = bar["time_utc"]
        sig.exit_ts = (
            bar_time.to_pydatetime() if hasattr(bar_time, "to_pydatetime") else bar_time
        )
        return True


def run_backtest(
    base: pd.DataFrame,
    cfg: EngineConfig | None = None,
    spread_points: float = 0.0,
    news: NewsState | None = None,
    point_size: float = 0.01,  # D-076 — the market's point (spread cost)
) -> BacktestResult:
    """Replay the base-TF series through the live evaluate() pipeline.

    The base TF is cfg.timeframe (M5 by default, D-050); every confirm/trend
    TF frame is resampled from the SAME series and sliced with closed_asof
    so no evaluation ever sees a higher-TF bar that had not closed yet.
    Pass an ALREADY-base-TF series (mock history is resampled by the CLI
    loader; CSVs are expected on the engine TF).
    """
    cfg = cfg or EngineConfig()
    tf_min = TIMEFRAME_MINUTES[cfg.timeframe]

    # Pre-resample every higher TF once (trend + confirms + D-042 bias TFs)
    # + their bar-close timestamp arrays (searchsorted asof slicing).
    htf_frames: dict[str, pd.DataFrame] = {}
    htf_close_ts: dict[str, Any] = {}
    fetch_tfs = [cfg.trend_tf, *cfg.confirm_tfs]
    if cfg.smc_enabled:
        fetch_tfs += [tf for tf in cfg.bias_tfs if tf not in fetch_tfs]
    for tf in dict.fromkeys(fetch_tfs):
        dst_min = TIMEFRAME_MINUTES[tf]
        if dst_min % tf_min != 0:
            raise ValueError(f"confirm tf {tf} must be a multiple of {cfg.timeframe}")
        frame = resample_ohlc(base, dst_min, src_min=tf_min)
        htf_frames[tf] = frame
        htf_close_ts[tf] = (frame["time_utc"] + pd.Timedelta(minutes=dst_min)).values

    # Windowed history depth — mirrors what the LIVE engine fetches through
    # get_rates (D-049: max(1500, lookback+5, tpo+10) bars — 24h of M1).
    # The old 260-bar mirror starved the TPO profile (3.3h instead of
    # 24h) and left PDH/PDL liquidity undefined, so backtest TP behavior
    # degenerated to the fixed rr multiple exactly like live did.
    hist_window = max(1500, cfg.sfp_lookback + 10, cfg.tpo_lookback_min + 60)

    # Warmup: enough base bars for the trend EMA on the LARGEST TF to be
    # defined, plus the base-level indicator needs.
    trend_min = TIMEFRAME_MINUTES[cfg.trend_tf]
    warmup = max(
        int((cfg.trend_ema + 5) * trend_min / tf_min),
        cfg.sfp_lookback,
        max(cfg.ema_fast, cfg.atr_period, cfg.rsi_period) + 5,
    )
    result = BacktestResult(
        cfg=cfg,
        bars_tested=max(0, len(base) - warmup),
        date_from=base["time_utc"].iloc[warmup].to_pydatetime() if len(base) > warmup else None,
        date_to=base["time_utc"].iloc[-1].to_pydatetime() if len(base) else None,
    )
    sim = _SimTracker(cfg, spread_price=spread_points * 0.01)
    cooldown_until = -1

    for i in range(warmup, len(base)):
        hist = base.iloc[max(0, i - hist_window) : i + 1].reset_index(drop=True)
        bar = base.iloc[i]
        bar_close_time = bar["time_utc"] + timedelta(minutes=tf_min)
        close_np = np.datetime64(bar_close_time.tz_localize(None)) if bar_close_time.tzinfo \
            else np.datetime64(bar_close_time)

        # multi-entry (D-042): evaluate while the concurrent budget has room;
        # the tracker steps every closed bar
        sim.step(bar)

        if sim.full or i < cooldown_until:
            continue

        htf = {}
        # D-056 — mirror the LIVE fetch window: the engine's _htf_frame()
        # asks get_rates for ~100 bars per confirm/trend TF, so the replay
        # must not feed evaluate() the WHOLE growing history (quadratic
        # iloc copies + more context than live ever sees). Cap each frame
        # at the last 300 closed bars — every consumer slices .iloc[-160:]
        # or needs <= slow+2 EMA bars, 300 gives warmup margin.
        for tf, frame in htf_frames.items():
            idx = int(np.searchsorted(htf_close_ts[tf], close_np, side="right"))
            htf[tf] = frame.iloc[max(0, idx - 300) : idx]
        if len(htf.get(cfg.trend_tf, pd.DataFrame())) < cfg.trend_ema + 1:
            continue
        ev = evaluate(hist, htf, bar_close_time, cfg, spread_points, news=news)
        if ev.signal is None:
            continue
        p = ev.signal
        entry_type = p.get("entry_type", "market")
        # D-061 — ATR scale at signal time for the pre-fill displacement
        # guard (same math the live tracker receives via make_tracked)
        try:
            _o = hist["o"].to_numpy(dtype=float)[-14:]
            _h = hist["h"].to_numpy(dtype=float)[-14:]
            _l = hist["l"].to_numpy(dtype=float)[-14:]
            sig_atr = float(np.mean(_h - _l)) or None
        except Exception:  # noqa: BLE001 — guard anchor is best-effort
            sig_atr = None
        sig = BacktestSignal(
            ts=bar["time_utc"].to_pydatetime(),
            direction=p["direction"],
            entry=p["entry"],
            sl=p["sl"],
            tp=p["tp"],
            confidence=p["confidence"],
            session=p["session"],
            trigger=p.get("trigger", "sfp"),
            entry_type=entry_type,
            market_ref=p.get("market_ref"),
            filled=entry_type != "limit",  # D-050 — pendings wait for a fill
            geometry=p.get("geometry", "legacy"),  # D-057
            atr_ref=sig_atr,  # D-061
        )
        result.signals.append(sig)
        sim.add(sig)
        cooldown_until = i + cfg.cooldown_bars + 1

    # D-VERIFY (external report §13/§15) — the bid/ask-exact levels above
    # now EMBED the spread for LIMIT trades (BUY fills at the ask touch,
    # SELL exits at the ask side, SELL expiry buys back at close+spread);
    # the post-hoc tax would double-count them. Only MARKET-born trades
    # still owe one spread: the engine records their entry at the trigger
    # bar's close (bid), but a BUY market fill pays the ask and exits at
    # the bid — that cost is not in the levels. Unfilled pendings never
    # traded — no cost (XAUUSD point = 0.01).
    if spread_points and spread_points > 0:
        cost = spread_points * point_size  # D-076 — market point, not 0.01
        for s in result.signals:
            risk = abs(s.entry - s.sl)
            if (
                risk > 0 and s.filled and s.result_r is not None
                and s.entry_type == "market"
            ):
                s.result_r = round(s.result_r - cost / risk, 4)

    return result


# ------------------------------------------------------------------ data load


def load_mock_history(
    bars: int = 5000, seed: int = 42, symbol: str = "XAUUSD"
) -> pd.DataFrame:
    """Deterministic mock history pulled through MockDataSource (M1 base).

    D-076 — `symbol` routes to the market's tape (XAUUSD/BTCUSD/USOIL/
    USTEC) so every market can be backtested on its own price level and
    volatility.
    """

    from app.mt5.mock_source import MockDataSource

    src = MockDataSource(seed=seed, time_scale=0.0)
    tape = src._tape_for(symbol)  # noqa: SLF001 — test harness access
    # freeze clock at the end of the 30-day window
    src.advance_minutes(src.HISTORY_DAYS * 24 * 60 - 1)
    df = src._rates_sync("M1", bars, tape=tape)  # noqa: SLF001 — harness
    return df.reset_index(drop=True)


def inject_sweep_series(base: pd.DataFrame, every_bars: int, seed: int = 42) -> pd.DataFrame:
    """Overlay crafted SFP sweeps onto a mock base series (engine sanity mode).

    Rather than using MockDataSource.inject_sweep (M1-level), this directly
    rewrites base bars so the sweep pattern is exact regardless of aggregation.
    """
    df = base.copy()
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


# ---------------------------------------------------------------------- risk


@dataclass
class RiskSimResult:
    """Phase 4 §9 simulation: lot sizing + realized P/L + kill switches."""

    start_equity: float
    final_equity: float
    net_pl: float
    max_drawdown_usd: float
    total_r: float
    lots_min: float | None
    lots_max: float | None
    lots_avg: float | None
    kill_switch_events: int
    trades_taken: int
    signals_skipped_after_kill: int
    per_trade: list[dict] = field(default_factory=list)

    def stats(self) -> dict:
        return {
            "start_equity": round(self.start_equity, 2),
            "final_equity": round(self.final_equity, 2),
            "net_pl": round(self.net_pl, 2),
            "net_pl_pct": round(self.net_pl / self.start_equity * 100, 3)
            if self.start_equity
            else None,
            "max_drawdown_usd": round(self.max_drawdown_usd, 2),
            "total_r": round(self.total_r, 4),
            "lots": {
                "min": self.lots_min,
                "max": self.lots_max,
                "avg": round(self.lots_avg, 4) if self.lots_avg is not None else None,
            },
            "kill_switch_events": self.kill_switch_events,
            "trades_taken": self.trades_taken,
            "signals_skipped_after_kill": self.signals_skipped_after_kill,
        }


def simulate_risk(
    res: BacktestResult,
    start_equity: float = 10_000.0,
    contract_size: float = 100.0,
    volume_min: float = 0.01,
    volume_max: float = 100.0,
    volume_step: float = 0.01,
) -> RiskSimResult:
    """Replay closed signals through the SAME §9 sizing + kill-switch math.

    Mirrors OrderExecutor semantics:
    - lots per trade from `size_lot()` (risk_percent mode, compounding equity),
    - P/L = result_r x sl_distance x contract x lots,
    - daily loss >= daily_max_loss_pct -> auto_trade disarmed for the REST of
      the run (kill switch), remaining signals counted as skipped.
    """
    from app.mt5.base import SymbolInfo

    cfg = res.cfg
    info = SymbolInfo(
        name="XAUUSD", contract_size=contract_size,
        volume_min=volume_min, volume_max=volume_max, volume_step=volume_step,
    )
    equity = start_equity
    peak = start_equity
    day_peak = start_equity  # D-056 — the DAILY drawdown anchor
    max_dd = 0.0
    armed = True
    kills = 0
    skipped = 0
    taken = 0
    lots_seen: list[float] = []
    total_r = 0.0
    per_trade: list[dict] = []
    day: Any = None

    for sig in res.signals:
        if sig.status == "active" or sig.result_r is None:
            continue  # never counted in a realized simulation
        # D-056 — the LIVE money window re-arms every UTC day (the daily
        # drawdown anchor resets to the new day's start equity). The old
        # replay set armed=False FOREVER after one trip, silently skipped
        # every later signal and misreported a recovering run as a
        # terminal "net loss" — a sim artifact, not the live behaviour.
        d = sig.ts.date() if hasattr(sig.ts, "date") else None
        if d is not None and d != day:
            if day is not None and not armed:
                per_trade.append({
                    "ts": sig.ts.isoformat(),
                    "event": "RE_ARM (new day)",
                    "equity": round(equity, 2),
                })
            armed = True
            day = d
            day_peak = equity  # today's anchor: day-start equity
        if not armed:
            skipped += 1
            continue
        sl_distance = abs(sig.entry - sig.sl)
        if sl_distance <= 0:
            continue
        # daily-loss check BEFORE every order (SPEC §9 order) — against
        # the DAY's anchor, exactly like the live money window
        if day_peak and (day_peak - equity) / day_peak * 100.0 >= cfg.daily_max_loss_pct:
            armed = False
            kills += 1
            skipped += 1
            per_trade.append(
                {
                    "ts": sig.ts.isoformat(),
                    "event": "KILL_SWITCH",
                    "equity": round(equity, 2),
                    "loss_pct": round((day_peak - equity) / day_peak * 100.0, 3),
                }
            )
            continue
        lot = size_lot(cfg, equity, sl_distance, info)
        pnl = sig.result_r * sl_distance * contract_size * lot.lots
        equity += pnl
        total_r += sig.result_r
        peak = max(peak, equity)
        day_peak = max(day_peak, equity)  # D-056 — intraday high-water
        max_dd = max(max_dd, peak - equity)
        taken += 1
        lots_seen.append(lot.lots)
        per_trade.append(
            {
                "ts": sig.ts.isoformat(),
                "direction": sig.direction,
                "lots": lot.lots,
                "result_r": sig.result_r,
                "pnl_usd": round(pnl, 2),
                "equity_after": round(equity, 2),
            }
        )

    return RiskSimResult(
        start_equity=start_equity,
        final_equity=equity,
        net_pl=equity - start_equity,
        max_drawdown_usd=max_dd,
        total_r=total_r,
        lots_min=min(lots_seen) if lots_seen else None,
        lots_max=max(lots_seen) if lots_seen else None,
        lots_avg=sum(lots_seen) / len(lots_seen) if lots_seen else None,
        kill_switch_events=kills,
        trades_taken=taken,
        signals_skipped_after_kill=skipped,
        per_trade=per_trade,
    )


def format_risk_report(risk: RiskSimResult) -> str:
    s = risk.stats()
    lines = [
        "-" * 64,
        " RISK / EXECUTOR SIMULATION (SPEC §9)",
        "-" * 64,
        f" start equity : {s['start_equity']:.2f} USD",
        f" final equity : {s['final_equity']:.2f} USD",
        f" net P/L      : {s['net_pl']:+.2f} USD ({s['net_pl_pct']:+.3f}%)",
        f" max DD (USD) : {s['max_drawdown_usd']:.2f}",
        f" total R      : {s['total_r']}",
        f" lots min/avg/max : {s['lots']['min']} / {s['lots']['avg']} / {s['lots']['max']}",
        f" trades taken : {s['trades_taken']}",
        f" kill switches : {s['kill_switch_events']}"
        f" (skipped {s['signals_skipped_after_kill']} later signals)",
        "-" * 64,
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------- report


def format_report(res: BacktestResult, label: str) -> str:
    s = res.stats()
    lines = [
        "=" * 64,
        f" M5 MTF ENGINE BACKTEST — {label}",
        "=" * 64,
        f" period        : {s['date_from']} -> {s['date_to']} UTC",
        f" base bars     : {s['bars_tested']} ({res.cfg.timeframe})",
        f" signals       : {s['total_signals']} ({s['signals_per_hour']}/h)"
        f" (won {s['won']} / lost {s['lost']} / expired {s['expired']}",
        f"                 unfilled {s['unfilled']} / active {s['active']})"
        f" — fill_rate {s['fill_rate']}",
        f" win rate      : {s['win_rate']}",
        f" total R       : {s['total_r']}",
        f" expectancy    : {s['expectancy']} R per signal",
        f" profit factor : {s['profit_factor']}",
        f" max drawdown  : {s['max_drawdown_r']} R",
        " by session    : " + json.dumps(s["by_session"]),
        " by trigger    : " + json.dumps(s["by_trigger"]),
        "=" * 64,
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="M5 MTF engine backtest (D-050)")
    p.add_argument("--bars", type=int, default=5000, help="M1 bars from mock history")
    p.add_argument(
        "--csv", type=str, default=None,
        help="OHLCV csv on the engine TF (time_utc,o,h,l,c,v)",
    )
    p.add_argument("--tf", type=str, default=None,
                   help="override the engine timeframe (default: the config's, M5)")
    p.add_argument("--entry-mode", type=str, default=None,
                   choices=("poi_limit", "market"),
                   help="override the entry mode (default: the config's)")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--inject-every", type=int, default=0,
                   help="overlay a crafted SFP sweep every N bars (engine sanity mode)")
    p.add_argument("--start-equity", type=float, default=10_000.0,
                   help="risk simulation starting equity (USD)")
    p.add_argument("--spread", type=float, default=0.0,
                   help="spread in points charged as entry cost (e.g. 20)")
    p.add_argument("--symbol", type=str, default="XAUUSD",
                   help="D-076 — the market to backtest (XAUUSD/BTCUSD/USOIL/"
                        "USTEC): its own mock tape, scaled engine windows, "
                        "real spread and contract size)")
    p.add_argument("--json", type=str, default=None, help="write stats+signals JSON here")
    p.add_argument("--csv-out", type=str, default=None, help="write signals CSV here")
    args = p.parse_args(argv)

    # D-076 — market specs (per-symbol backtests)
    from app.mt5.base import MARKETS, market_key, market_scale, market_spread_scale

    cfg = EngineConfig()
    if args.tf:
        cfg = cfg.model_copy(update={"timeframe": args.tf})
    if args.entry_mode:
        cfg = cfg.model_copy(update={"entry_mode": args.entry_mode})

    if args.csv:
        base = load_csv(args.csv)
        label = f"CSV {args.csv}"
    else:
        base = load_mock_history(args.bars, seed=args.seed, symbol=args.symbol)
        label = f"mock {args.symbol} seed={args.seed} bars={len(base)}"
        # D-076 — the per-market config view (exactly the live engine's
        # _market_cfg math): USD windows scaled to the price level, spread
        # budget to the market's spread width; XAUUSD = 1.0x (unchanged).
        mk = market_key(args.symbol)
        spec = MARKETS.get(mk, MARKETS["XAUUSD"])
        if mk != "XAUUSD":
            cfg = cfg.model_copy(
                update={
                    "entry_min_usd": cfg.entry_min_usd * market_scale(mk),
                    "pending_target_usd": cfg.pending_target_usd * market_scale(mk),
                    "pending_max_usd": cfg.pending_max_usd * market_scale(mk),
                    "max_spread_points": cfg.max_spread_points * market_spread_scale(mk),
                }
            )
        # the market's real mock spread in POINTS when --spread is unset
        if args.spread == 0.0 and spec.mock_spread > 0:
            args.spread = spec.mock_spread / spec.point
        label += f" spread={args.spread:.0f}pts contract={spec.contract_size:g}"
        # D-050 — the mock generates M1; the engine TF may be M5/M15 —
        # resample the base series up-front so the replay is honest.
        tf_min = TIMEFRAME_MINUTES[cfg.timeframe]
        if tf_min > 1:
            base = resample_ohlc(base, tf_min, src_min=1)
            label += f" -> {cfg.timeframe}"
    if args.inject_every:
        base = inject_sweep_series(base, args.inject_every, seed=args.seed)
        label += f" +injected-sweep-every-{args.inject_every}"

    label += f" entry={cfg.entry_mode}"
    res = run_backtest(
        base, cfg=cfg, spread_points=args.spread,
        point_size=(MARKETS.get(market_key(args.symbol), MARKETS["XAUUSD"]).point),
    )
    print(format_report(res, label))
    spec = MARKETS.get(market_key(args.symbol), MARKETS["XAUUSD"])
    risk = simulate_risk(
        res, start_equity=args.start_equity,
        contract_size=spec.contract_size,
        volume_min=spec.volume_min, volume_max=spec.volume_max,
        volume_step=spec.volume_step,
    )
    print(format_risk_report(risk))

    if args.json:
        payload = {
            "label": label,
            "config": res.cfg.model_dump(),
            "stats": res.stats(),
            "risk_sim": risk.stats(),
            "risk_sim_per_trade": risk.per_trade,
            "signals": [
                {
                    "ts": s.ts.isoformat(),
                    "direction": s.direction,
                    "entry": s.entry,
                    "sl": s.sl,
                    "tp": s.tp,
                    "confidence": s.confidence,
                    "session": s.session,
                    "trigger": s.trigger,
                    "entry_type": s.entry_type,
                    "market_ref": s.market_ref,
                    "filled": s.filled,
                    "fill_ts": s.fill_ts.isoformat() if s.fill_ts else None,
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
                    "session": s.session, "trigger": s.trigger,
                    "entry_type": s.entry_type, "market_ref": s.market_ref,
                    "filled": s.filled, "status": s.status, "result_r": s.result_r,
                }
                for s in res.signals
            ]
        ).to_csv(args.csv_out, index=False)
        print(f"signals csv -> {args.csv_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
