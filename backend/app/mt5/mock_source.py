"""Deterministic mock market data source (SPEC §8.1 / C7, DECISIONS.md D-007).

Design:
- Base M1 candles are generated from a seeded random walk. Each bar's randomness
  derives from `np.random.default_rng([seed, minute_index])`, so bar N is
  identical regardless of the order/generation history -> full determinism.
- Higher timeframes (M5..D1) aggregate the M1 series, so all TFs stay consistent.
- Virtual clock: starts at `start_time` and advances `time_scale` virtual
  seconds per real second (default 60 -> a fresh M1 bar every real second).
  `time_scale=0` freezes the clock; tests then drive time with
  `advance_minutes()` for fully deterministic, sleep-free tests.
- Volatility regimes: list of (start_minute, sigma) applied from that minute on.
- Scenario injection: `inject_sweep()` crafts an SFP sweep candle at M15 (or any
  TF) granularity, then distributes it into M1 overrides so every timeframe
  sees a consistent bar.
"""

from __future__ import annotations

import asyncio
import time as time_mod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from dataclasses import replace as dataclasses_replace
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd

from app.mt5.base import (
    RATES_COLUMNS,
    DataSource,
    Order,
    OrderResult,
    Position,
    SymbolInfo,
    Tick,
    empty_rates,
    validate_tf,
)

TRADE_RETCODE_DONE = 10009

# Mock lot-sizing metadata mirrors a typical Exness XAUUSD standard account.
MOCK_SYMBOL_INFO = SymbolInfo(
    name="XAUUSDm", point=0.01, contract_size=100.0,
    volume_min=0.01, volume_max=100.0, volume_step=0.01,
)


@dataclass(frozen=True)
class VolRegime:
    """Per-M1-bar return sigma (USD) active from `start_minute` (origin-based)."""

    start_minute: int
    sigma: float


@dataclass
class SweepScenario:
    """Craft an SFP sweep at `tf` granularity (SPEC §8.2 rule 2 test fixture)."""

    direction: str  # "BUY" sweeps lows, "SELL" sweeps highs
    tf: str = "M15"
    offset_bars: int = 1  # 1 = the next bucket after the last closed one
    wick_atr_mult: float = 0.5  # crafted wick = this * ATR(14) (spec min 0.3)
    lookback: int = 20

    def __post_init__(self) -> None:
        if self.direction not in ("BUY", "SELL"):
            raise ValueError("direction must be BUY or SELL")
        if self.offset_bars < 1:
            raise ValueError("offset_bars must be >= 1 (target bucket must not be closed yet)")
        validate_tf(self.tf)


@dataclass
class InjectedScenario:
    scenario: SweepScenario
    bucket_time: datetime  # UTC open time of the crafted bucket
    candle: dict  # {"o","h","l","c","v"} of the crafted tf-level candle


@dataclass
class _Account:
    balance: float = 10_000.0
    equity: float = 10_000.0
    currency: str = "USD"
    leverage: int = 500


class MockDataSource(DataSource):
    """Deterministic synthetic market for tests and demo (DATA_SOURCE=mock)."""

    SYMBOL = "XAUUSDm"  # Exness-style symbol name (SPEC C3)
    HISTORY_DAYS = 30

    def __init__(
        self,
        seed: int = 42,
        base_price: float = 2650.0,
        start_time: datetime | None = None,
        time_scale: float = 60.0,
        base_vol: float = 0.35,
        vol_regimes: list[VolRegime] | None = None,
        spread: float = 0.20,
        tick_interval: float = 0.25,
        origin: datetime | None = None,
    ) -> None:
        self._seed = int(seed)
        self._base_price = float(base_price)
        self._anchor = (start_time or datetime.now(UTC)).replace(
            second=0, microsecond=0
        )
        # Grid-align the origin to UTC midnight so M15/H1/D1 buckets share the
        # SAME grid as epoch-aligned consumers (MarketStream, lightweight-charts)
        # — otherwise buckets are offset by the anchor's minute-of-hour.
        # An explicit `origin` (market sibling) pins this to the PUBLIC source's
        # grid so per-user demo accounts price off the identical market.
        if origin is not None:
            self._origin = origin
        else:
            origin_raw = self._anchor - timedelta(days=self.HISTORY_DAYS)
            self._origin = origin_raw.replace(hour=0, minute=0, second=0, microsecond=0)
        self._time_scale = float(time_scale)
        self._spread = float(spread)
        self._tick_interval = float(tick_interval)
        regimes = vol_regimes or [VolRegime(0, base_vol)]
        self._regimes = sorted(regimes, key=lambda r: r.start_minute)

        # M1 cache (index 0 == origin minute)
        self._cache_minute = -1
        self._o: list[float] = []
        self._h: list[float] = []
        self._l: list[float] = []
        self._c: list[float] = []
        self._v: list[int] = []

        self._m1_overrides: dict[int, tuple[float, float, float, float, int]] = {}
        self.injected: list[InjectedScenario] = []

        self._connected = False
        self._account = _Account()
        self._positions: list[Position] = []
        self._next_ticket = 100_000

        self._t0 = time_mod.monotonic()
        self._manual_seconds = 0.0

    # ------------------------------------------------------------------ clock

    def market_params(self) -> dict:
        """Clock/market identity — consumed by `sibling()` so a per-user demo
        account prices positions off the SAME market as the public chart."""
        return {
            "seed": self._seed,
            "base_price": self._base_price,
            "origin": self._origin,
            "time_scale": self._time_scale,
            "spread": self._spread,
        }

    @classmethod
    def sibling(cls, public: MockDataSource) -> MockDataSource:
        """A new source sharing the public market (same seed/origin/clock).

        The anchor is rebased to the public source's CURRENT virtual now, so
        both clocks advance in lockstep and every price lookup returns the
        same value as the public chart at the same wall-clock moment.
        """
        p = public.market_params()
        return cls(
            seed=p["seed"],
            base_price=p["base_price"],
            start_time=public._vnow(),
            time_scale=p["time_scale"],
            spread=p["spread"],
            origin=p["origin"],
        )

    def _vnow(self) -> datetime:
        elapsed = (time_mod.monotonic() - self._t0) * self._time_scale
        return self._anchor + timedelta(seconds=elapsed + self._manual_seconds)

    def advance_minutes(self, n: float) -> None:
        """Manually push the virtual clock forward (tests use time_scale=0)."""
        self._manual_seconds += n * 60.0

    def set_time_scale(self, scale: float) -> None:
        """Change the real->virtual speed, rebasing so virtual time is continuous."""
        now = self._vnow()
        self._anchor = now
        self._t0 = time_mod.monotonic()
        self._manual_seconds = 0.0
        self._time_scale = float(scale)

    # ------------------------------------------------------------- generation

    def _sigma_for(self, minute_index: int) -> float:
        sigma = self._regimes[0].sigma
        for regime in self._regimes:
            if regime.start_minute <= minute_index:
                sigma = regime.sigma
            else:
                break
        return sigma

    def _ensure_m1(self, upto_minute: int) -> None:
        """Generate M1 bars [cache_minute+1 .. upto_minute] (deterministic)."""
        for m in range(self._cache_minute + 1, upto_minute + 1):
            rng = np.random.default_rng([self._seed, m])
            z = rng.standard_normal(4)
            sigma = self._sigma_for(m)
            o = self._c[m - 1] if m > 0 else self._base_price
            c = o + sigma * float(z[0])
            h = max(o, c) + abs(sigma * float(z[1]))
            low = min(o, c) - abs(sigma * float(z[2]))
            v = int(50 + 200 * abs(float(z[3])))
            if m in self._m1_overrides:
                o, h, low, c, v = self._m1_overrides[m]
            self._o.append(o)
            self._h.append(h)
            self._l.append(low)
            self._c.append(c)
            self._v.append(v)
            self._cache_minute = m

    def _last_closed_minute(self, vnow: datetime) -> int:
        cur = int((vnow - self._origin).total_seconds() // 60)
        return cur - 1

    def _last_closed_bucket(self, tf_min: int, vnow: datetime) -> int:
        return (self._last_closed_minute(vnow) + 1) // tf_min - 1

    def _rates_sync(self, tf: str, count: int, vnow: datetime | None = None) -> pd.DataFrame:
        tf_min = validate_tf(tf)
        now = vnow or self._vnow()
        last_bucket = self._last_closed_bucket(tf_min, now)
        if last_bucket < 0:
            return empty_rates()
        first_bucket = max(0, last_bucket - count + 1)
        self._ensure_m1((last_bucket + 1) * tf_min - 1)
        rows = []
        for b in range(first_bucket, last_bucket + 1):
            s, e = b * tf_min, (b + 1) * tf_min
            rows.append(
                {
                    "time_utc": self._origin + timedelta(minutes=b * tf_min),
                    "o": self._o[s],
                    "h": max(self._h[s:e]),
                    "l": min(self._l[s:e]),
                    "c": self._c[e - 1],
                    "v": int(sum(self._v[s:e])),
                }
            )
        return pd.DataFrame(rows, columns=RATES_COLUMNS)

    @staticmethod
    def _atr(df: pd.DataFrame, period: int = 14) -> float:
        h, low, c = df["h"], df["l"], df["c"]
        prev_c = c.shift(1)
        tr = pd.concat([h - low, (h - prev_c).abs(), (low - prev_c).abs()], axis=1).max(axis=1)
        return float(tr.tail(period).mean())

    # ------------------------------------------------------ scenario crafting

    def inject_sweep(self, scenario: SweepScenario) -> InjectedScenario:
        """Craft an SFP sweep candle for the upcoming `tf` bucket (SPEC §8.2).

        BUY: low sweeps below min(low of `lookback` prior bars), close pulls back
        above it, lower wick >= wick_atr_mult * ATR(14). SELL mirrors it.
        """
        tf_min = validate_tf(scenario.tf)
        now = self._vnow()
        target_bucket = self._last_closed_bucket(tf_min, now) + scenario.offset_bars
        target_start = self._origin + timedelta(minutes=target_bucket * tf_min)

        # History of the same tf strictly before the target bucket.
        hist = self._rates_sync(
            scenario.tf, scenario.lookback, vnow=target_start
        )
        if len(hist) < scenario.lookback:
            raise ValueError("not enough closed history to craft a sweep scenario")
        prior = hist.tail(scenario.lookback)
        atr = self._atr(prior)
        if atr <= 0:
            raise ValueError("ATR must be positive to craft a sweep scenario")

        if scenario.direction == "BUY":
            swept = float(prior["l"].min())
            c = swept + 0.15 * atr
            o = c - 0.05 * atr
            low = min(swept - 0.05 * atr, o - scenario.wick_atr_mult * atr)
            high = max(o, c) + 0.10 * atr
        else:
            swept = float(prior["h"].max())
            c = swept - 0.15 * atr
            o = c + 0.05 * atr
            high = max(swept + 0.05 * atr, o + scenario.wick_atr_mult * atr)
            low = min(o, c) - 0.10 * atr
        volume = 250

        self._distribute_override(target_bucket, tf_min, o, high, low, c, volume)
        record = InjectedScenario(
            scenario=scenario,
            bucket_time=target_start,
            candle={"o": o, "h": high, "l": low, "c": c, "v": volume},
        )
        self.injected.append(record)
        return record

    def _distribute_override(
        self, bucket: int, tf_min: int, o: float, h: float, low: float, c: float, v: int
    ) -> None:
        """Spread a crafted tf-level candle across its M1 minutes so every
        timeframe aggregates to the same values (D-007)."""
        n = tf_min
        # numpy SeedSequence only accepts ints — use a fixed salt for scenarios.
        scen_salt = 0x5CE9  # "SCEN"
        rng = np.random.default_rng([self._seed, scen_salt, bucket, tf_min])
        body = abs(c - o)
        closes = (
            o + (c - o) * np.linspace(1, n, n) / n + rng.normal(0, body * 0.02 + 0.01, n)
        )
        closes = np.clip(closes, min(low, min(o, c)) + 1e-9, max(h, max(o, c)) - 1e-9)
        closes[0], closes[-1] = o + (c - o) / n, c
        opens = np.concatenate([[o], closes[:-1]])
        li = int(rng.integers(0, n))
        hi = int(rng.integers(0, n))
        lows = np.minimum(opens, closes) - np.abs(rng.normal(0, 0.01, n))
        lows = np.clip(lows, low, None)
        lows[li] = low
        highs = np.maximum(opens, closes) + np.abs(rng.normal(0, 0.01, n))
        highs = np.clip(highs, None, h)
        highs[hi] = h
        vols = rng.integers(1, 6, n).astype(float)
        vols = (vols * v / vols.sum()).astype(int)
        vols[-1] += v - int(vols.sum())
        start = bucket * tf_min
        for i in range(n):
            self._m1_overrides[start + i] = (
                float(opens[i]),
                float(highs[i]),
                float(lows[i]),
                float(closes[i]),
                int(vols[i]),
            )

    # ---------------------------------------------------- DataSource interface

    async def connect(self, creds: dict) -> dict:
        self._connected = True
        return {
            "login": str(creds.get("login", "10000000")),
            "server": creds.get("server", "MockServer"),
            "balance": self._account.balance,
            "equity": self._account.equity,
            "currency": self._account.currency,
            "leverage": self._account.leverage,
        }

    async def disconnect(self) -> None:
        self._connected = False

    async def is_connected(self) -> bool:
        return self._connected

    async def get_rates(self, symbol: str, tf: str, count: int) -> pd.DataFrame:
        return self._rates_sync(tf, count)

    def get_forming_bar(self, symbol: str, tf: str) -> dict | None:
        """Current forming tf-bar built from closed M1 minutes + live tick."""
        tf_min = validate_tf(tf)
        now = self._vnow()
        cur_minute = int((now - self._origin).total_seconds() // 60)
        bucket_start = cur_minute // tf_min * tf_min
        self._ensure_m1(cur_minute - 1)
        if cur_minute <= bucket_start:  # empty bucket edge
            return None
        o = self._o[bucket_start]
        h = max(self._h[bucket_start:cur_minute])
        low = min(self._l[bucket_start:cur_minute])
        v = int(sum(self._v[bucket_start:cur_minute]))
        # live forming minute: interpolate toward its target close
        progress = min(max((now - self._origin).total_seconds() % 60.0 / 60.0, 0.0), 0.999)
        rng = np.random.default_rng([self._seed, cur_minute])
        z = rng.standard_normal(4)
        sigma = self._sigma_for(cur_minute)
        target = (
            self._m1_overrides[cur_minute][3]
            if cur_minute in self._m1_overrides
            else self._c[cur_minute - 1] + sigma * float(z[0])
        )
        price = self._c[cur_minute - 1] + (target - self._c[cur_minute - 1]) * progress
        return {
            "t": int((self._origin + timedelta(minutes=bucket_start)).timestamp()),
            "o": o,
            "h": max(h, price),
            "l": min(low, price),
            "c": price,
            "v": v,
        }

    async def get_tick(self, symbol: str) -> Tick:
        return self._tick_from_now(symbol)

    def subscribe_ticks(self, symbol: str) -> AsyncIterator[Tick]:
        return self._tick_stream(symbol)

    async def _tick_stream(self, symbol: str) -> AsyncIterator[Tick]:
        try:
            while True:
                yield await self.get_tick(symbol)
                await asyncio.sleep(self._tick_interval)
        except asyncio.CancelledError:
            return

    def discover_symbols(self, pattern: str = "*XAUUSD*") -> list[str]:
        return [self.SYMBOL]

    async def place_order(self, order: Order) -> OrderResult:
        if not self._connected:
            return OrderResult(ok=False, retcode=0, comment="mock: not connected")
        tick = await self.get_tick(order.symbol)
        price = tick.ask if order.side == "BUY" else tick.bid
        self._next_ticket += 1
        self._positions.append(
            Position(
                ticket=self._next_ticket,
                symbol=order.symbol,
                side=order.side,
                volume=order.volume,
                price_open=price,
                sl=order.sl,
                tp=order.tp,
                profit=0.0,
                time=self._vnow(),
            )
        )
        return OrderResult(
            ok=True, ticket=self._next_ticket, price=price,
            retcode=TRADE_RETCODE_DONE, comment="mock fill",
        )

    async def get_positions(self) -> list[Position]:
        """Open positions with LIVE floating P/L priced off the current tick."""
        if not self._positions:
            return []
        out: list[Position] = []
        for p in self._positions:
            tick = await self.get_tick(p.symbol)
            out.append(
                dataclasses_replace(
                    p, profit=self._floating_profit(p, tick.bid, tick.ask)
                )
            )
        return out

    def _floating_profit(self, p: Position, bid: float, ask: float) -> float:
        """P/L in account currency: (exit - entry) x contract x lots (USD for XAUUSD)."""
        exit_price = bid if p.side == "BUY" else ask  # close BUY at bid, SELL at ask
        direction = 1.0 if p.side == "BUY" else -1.0
        info = self.symbol_info(p.symbol) or SymbolInfo(name=p.symbol)
        return (exit_price - p.price_open) * direction * info.contract_size * p.volume

    async def close_position(self, ticket: int, deviation: int = 30) -> OrderResult:
        """Close by ticket: realize floating P/L into the balance."""
        if not self._connected:
            return OrderResult(ok=False, retcode=0, comment="mock: not connected")
        pos = next((p for p in self._positions if p.ticket == ticket), None)
        if pos is None:
            return OrderResult(ok=False, retcode=10036, comment="mock: position not found")
        tick = await self.get_tick(pos.symbol)
        exit_price = tick.bid if pos.side == "BUY" else tick.ask
        profit = self._floating_profit(pos, tick.bid, tick.ask)
        self._account.balance += profit
        self._positions = [p for p in self._positions if p.ticket != ticket]
        return OrderResult(
            ok=True, ticket=ticket, price=exit_price,
            retcode=TRADE_RETCODE_DONE,
            comment=f"mock close profit={profit:.2f}",
        )

    def symbol_info(self, symbol: str) -> SymbolInfo:
        return MOCK_SYMBOL_INFO

    # ------------------------------------------------------- account simulation

    def account_info(self) -> dict | None:  # type: ignore[override]
        """Balance + floating equity (called by the 5s account poller)."""
        if not self._connected:
            return None
        balance = self._account.balance
        floating = 0.0
        for p in self._positions:
            tick = self.get_tick_sync(p.symbol)
            floating += self._floating_profit(p, tick.bid, tick.ask)
        self._account.equity = balance + floating
        return {
            "login": "10000000",
            "server": "MockServer",
            "balance": self._account.balance,
            "equity": self._account.equity,
            "currency": self._account.currency,
            "leverage": self._account.leverage,
        }

    def get_tick_sync(self, symbol: str) -> Tick:
        """Sync tick for internal simulation loops (account_info is sync)."""
        return self._tick_from_now(symbol)

    def _tick_from_now(self, symbol: str) -> Tick:
        """Pure-sync tick computation (no awaits) reusing get_tick's math."""
        now = self._vnow()
        m = int((now - self._origin).total_seconds() // 60)
        self._ensure_m1(m - 1)
        o_prev = self._c[m - 1]
        rng = np.random.default_rng([self._seed, m])
        z = rng.standard_normal(4)
        sigma = self._sigma_for(m)
        if m in self._m1_overrides:
            target_c = self._m1_overrides[m][3]
        else:
            target_c = o_prev + sigma * float(z[0])
        bar_start = self._origin + timedelta(minutes=m)
        progress = min(max((now - bar_start).total_seconds() / 60.0, 0.0), 0.999)
        price = o_prev + (target_c - o_prev) * progress
        return Tick(bid=price - self._spread / 2, ask=price + self._spread / 2, time=now)

    def set_starting_balance(self, balance: float) -> None:
        """Test helper — deterministic account seeding."""
        self._account.balance = float(balance)
        self._account.equity = float(balance)
