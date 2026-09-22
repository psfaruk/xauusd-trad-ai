"""Engine configuration model (SPEC §8.6 defaults) + DB repository.

The single `engine_config` row (id=1) holds a JSONB payload of EngineConfig
fields plus the separate `auto_trade` flag (global kill switch, SPEC §0 —
defaults to false and is NEVER touched by the strategy engine itself).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.mt5.base import validate_tf

logger = logging.getLogger("xauusd.engine")

AUTO_TRADE_CONFIRM = "ENABLE"


class SessionRule(BaseModel):
    """UTC session window [start_hour, end_hour) — SPEC §8.2 rule 5."""

    model_config = ConfigDict(extra="forbid")
    name: str
    utc: tuple[int, int] = Field(min_length=2, max_length=2)

    @field_validator("utc")
    @classmethod
    def _hours(cls, v: tuple[int, int]) -> tuple[int, int]:
        start, end = v
        if not (0 <= start <= 24 and 0 <= end <= 24):
            raise ValueError("session hours must be within 0..24")
        if start == end:
            raise ValueError("session start == end (use 0,24 for 24h)")
        return (int(start), int(end))

    def contains(self, hour: int) -> bool:
        start, end = self.utc
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end  # wraps midnight


class EngineConfig(BaseModel):
    """Strategy parameters (SPEC §8.6 defaults, D-041 M1 rework, D-042 ICT,
    D-050 M5 + POI pending entries).

    D-041: the engine trades with multi-timeframe confirmation — the trend
    TF sets the bias, the confirm TFs must agree, and the trigger is a REAL
    base-TF pattern (liquidity-sweep SFP, EMA pullback rejection or a POI
    zone retest).
    D-042: ICT/SMC confluence — after the trigger, market structure
    (BOS/CHoCH), order blocks, fair value gaps, liquidity sweeps and
    supply/demand zones must confirm with >= min_confluence votes;
    SL/TP are zone-aware (app-controlled exits), and up to max_positions
    signals/entries can run CONCURRENTLY (multi-entry directive).
    D-050: the base timeframe moved to M5 (user directive: short-term
    trading off the 5-minute chart) and every signal becomes a PENDING
    LIMIT order placed at a structural POI level BEYOND the current
    market — BUY limits BELOW the nearest demand (support) zone, SELL
    limits ABOVE the nearest supply (resistance) zone (user directive:
    "মার্জিন" between market and entry so noise cannot stop out the
    trade straight away).
    """

    model_config = ConfigDict(extra="forbid")

    timeframe: str = "M5"     # trigger timeframe (D-050: M1 -> M5, user
    #                                  directive: short-term trading on the
    #                                  5-minute chart)
    trend_tf: str = "H1"      # major trend timeframe
    confirm_tfs: list[str] = Field(
        default_factory=lambda: ["M15"],
        description="MTF confirmation timeframes — each must be > timeframe",
    )
    min_tf_agree: int = Field(
        1, ge=0,
        description="how many confirm TFs must agree with the H1 trend (hard gate)",
    )
    ema_fast: int = Field(20, ge=1)
    ema_slow: int = Field(50, ge=1)
    trend_ema: int = Field(50, ge=1)
    rsi_period: int = Field(14, ge=2)
    rsi_buy_min: float = Field(40.0, ge=0, le=100)
    rsi_buy_max: float = Field(65.0, ge=0, le=100)
    rsi_sell_min: float = Field(35.0, ge=0, le=100)
    rsi_sell_max: float = Field(60.0, ge=0, le=100)
    atr_period: int = Field(14, ge=1)
    min_atr: float = Field(0.25, ge=0)   # M5 ATR floor (D-050: 0.15 M1 ->
    #                                    0.25 M5 recalibration)
    sfp_lookback: int = Field(20, ge=2)
    sfp_wick_atr_ratio: float = Field(0.35, gt=0)
    sl_buffer_atr: float = Field(0.2, ge=0)
    rr: float = Field(1.6, gt=0,         # D-049: fallback TP multiple when
        description="TP multiple used ONLY when no structural target "
                    "sits within tp_max_r — the primary TP is predicted "
                    "from the nearest opposing zone/liquidity/TPO level")
    expiry_bars: int = Field(36, ge=1)  # D-050: 3h on M5 (was 45 M1)
    #                                     # — structure TPs need room to be
    #                                     # HIT, not expire mid-flight
    cooldown_bars: int = Field(4, ge=0)
    # -------------------------------------------------- D-041 pullback trigger
    pullback_enabled: bool = True
    pullback_min_range_atr: float = Field(0.35, gt=0,
        description="min trigger-bar range as a fraction of ATR (doji filter)")
    pullback_wick_ratio: float = Field(0.45, gt=0,
        description="min rejection wick as a fraction of the bar range")
    # -------------------------------------------------- D-041 risk geometry
    min_sl_atr: float = Field(1.5, ge=0,
        description="SL at least this many ATRs from entry (spread/noise floor)")
    max_spread_to_risk: float = Field(0.30, gt=0,
        description="skip when spread > this fraction of the SL distance "
                    "(D-049: 0.5 -> 0.30 — at 0.5 the spread alone could "
                    "eat half the risk before the trade even started)")
    sessions: list[SessionRule] = Field(
        default_factory=lambda: [
            # D-047 — Tokyo added: the old london+newyork pair silently
            # blocked the ENTIRE Asian morning (00-07 UTC = 06:00-13:00
            # Dhaka), which is exactly when the user watches the chart.
            SessionRule(name="tokyo", utc=(0, 7)),
            SessionRule(name="london", utc=(7, 16)),
            SessionRule(name="newyork", utc=(13, 20)),
        ]
    )
    news_blackout_min: int = Field(30, ge=0)
    max_spread_points: int = Field(35, ge=1)
    #: D-047 — how deep the time-at-price (TPO) profile reaches (minutes of
    #: M1 history) for S/R level detection + SL anchoring
    tpo_lookback_min: int = Field(1440, ge=60, le=10080)
    risk_mode: str = Field("percent", pattern="^(percent|fixed)$")
    risk_percent: float = Field(0.5, gt=0, le=100)
    fixed_lot: float = Field(0.01, gt=0)
    max_positions: int = Field(3, ge=1)
    daily_max_loss_pct: float = Field(3.0, gt=0)
    max_trades_per_day: int = Field(
        6, ge=1, le=100,
        description="D-049 — user-directed daily auto-trade budget: how "
                    "many orders the executor may place per UTC day "
                    "(the user controls balance / risk / trade count; "
                    "the app controls entries, SL and TP)",
    )
    magic: int = Field(234000, ge=0)
    # -------------------------------------------------- D-042 ICT/SMC block
    smc_enabled: bool = Field(
        True,
        description="ICT/SMC confluence stage master switch",
    )
    bias_tfs: list[str] = Field(
        default_factory=lambda: ["H4"],
        description="extra HTF frames whose STRUCTURE must bias the trade",
    )
    min_confluence: int = Field(
        3, ge=0, le=6,
        description="how many of the 6 ICT gating factors must confirm "
        "(D-049: 2 -> 3 — on the honest 1500-bar window the premium gate "
        "measures BETTER (expR +0.023 vs +0.010, PF 1.039 vs 1.016) at "
        "nearly the same volume; the POI zone trigger ignores this gate "
        "entirely)",
    )
    max_zone_atr: float = Field(
        0.9, gt=0,
        description="entry-to-zone proximity tolerance in ATRs",
    )
    vol_z_min: float = Field(
        0.8, ge=0,
        description="trigger-bar volume z-score that counts as institutional",
    )
    max_sl_atr: float = Field(
        3.5, gt=0,
        description="SL at most this many ATRs from entry (risk cap)",
    )
    # -------------------------------------------------- D-049 target block
    tp_min_rr: float = Field(
        1.2, ge=0.5, le=5.0,
        description="minimum realized reward:risk — when the nearest "
                    "structural barrier stands closer than this, the "
                    "trade is SKIPPED (predicted to hit the barrier first)",
    )
    tp_max_r: float = Field(
        3.0, ge=1.0, le=10.0,
        description="targets further than this many R fall back to rr",
    )
    # -------------------------------------------------- D-048 POI zone block
    zone_trigger_enabled: bool = Field(
        True,
        description="POI zone-retest trigger (supply/demand/OB/FVG/TPO): "
        "price returning to a QUALITY zone and rejecting IS the entry — "
        "the ICT factor count does NOT gate this path (user directive: "
        "not every strategy must agree at once)",
    )
    min_zone_quality: float = Field(
        0.45, ge=0.0, le=1.0,
        description="POI quality gate for a WITH-TREND zone retest signal",
    )
    counter_trend_quality: float = Field(
        0.58, ge=0.0, le=1.0,
        description="higher POI quality a COUNTER-TREND zone reversal needs "
                    "(D-049: 0.70 -> 0.58 — the old gate was practically "
                    "unreachable, which froze BUY signals during H1 "
                    "downtrends: the engine could only sell)",
    )
    zone_retest_window: int = Field(
        2, ge=1, le=5,
        description="bars before the trigger that may have entered the zone",
    )
    # -------------------------------------------------- D-050 POI pending block
    entry_mode: str = Field(
        "poi_limit",
        pattern="^(market|poi_limit)$",
        description="D-050 — 'poi_limit' turns every signal into a PENDING "
                    "LIMIT order anchored at a POI zone level BEYOND the "
                    "market (BUY limit below the demand/support zone, SELL "
                    "limit above the supply/resistance zone — user "
                    "directive); 'market' keeps the legacy enter-at-close "
                    "behaviour",
    )
    entry_offset_atr: float = Field(
        0.35, gt=0,
        description="minimum distance (ATRs) between the current market "
                    "and a pending entry — the 'মার্জিন' that keeps noise "
                    "away from the fill (plus 2 spreads, whichever is "
                    "larger)",
    )
    pending_offset_atr: float = Field(
        0.8, gt=0,
        description="fallback pending distance (ATRs) when no same-side "
                    "POI zone sits within reach of the market",
    )
    pending_max_atr: float = Field(
        10.0, gt=0,
        description="maximum pending distance (ATRs) — a zone deeper than "
                    "this clamps the entry so fills stay realistic "
                    "(D-050: ~10 USD at M5 ATR 1.0 — the user's worked "
                    "example: market 4513, BUY at 4503)",
    )
    pending_expiry_bars: int = Field(
        24, ge=1,
        description="how many engine-TF bars a PENDING signal may wait for "
                    "its fill before expiring unfilled (2h on M5) — an "
                    "unfilled order is a MISSED trade, never a loss",
    )
    max_pending_signals: int = Field(
        6, ge=1,
        description="concurrent PENDING (unfilled) signal budget — kept "
                    "separate from max_positions so unfilled limits never "
                    "clog the signal flow (user directive: signals must "
                    "keep coming)",
    )

    @field_validator("timeframe", "trend_tf")
    @classmethod
    def _tf(cls, v: str) -> str:
        validate_tf(v)
        return v

    @field_validator("confirm_tfs")
    @classmethod
    def _confirm_tfs(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("confirm_tfs may not be empty (set min_tf_agree=0 to disable)")
        seen: list[str] = []
        for tf in v:
            validate_tf(tf)
            if tf in seen:
                raise ValueError(f"duplicate confirm tf: {tf}")
            seen.append(tf)
        return seen

    @field_validator("bias_tfs")
    @classmethod
    def _bias_tfs(cls, v: list[str]) -> list[str]:
        seen: set[str] = set()
        for tf in v:
            validate_tf(tf)
            if tf in seen:
                raise ValueError(f"duplicate bias tf: {tf}")
            seen.add(tf)
        return v

    @model_validator(mode="after")
    def _tf_order(self) -> EngineConfig:
        from app.mt5.base import TIMEFRAME_MINUTES

        base = TIMEFRAME_MINUTES[self.timeframe]
        if TIMEFRAME_MINUTES[self.trend_tf] <= base:
            raise ValueError("trend_tf must be greater than timeframe")
        for tf in self.confirm_tfs:
            if TIMEFRAME_MINUTES[tf] <= base:
                raise ValueError(f"confirm tf {tf} must be greater than timeframe")
            if TIMEFRAME_MINUTES[tf] >= TIMEFRAME_MINUTES[self.trend_tf]:
                raise ValueError(f"confirm tf {tf} must be below trend_tf {self.trend_tf}")
        for tf in self.bias_tfs:
            if TIMEFRAME_MINUTES[tf] <= base:
                raise ValueError(f"bias tf {tf} must be greater than timeframe")
        if self.min_tf_agree > len(self.confirm_tfs):
            raise ValueError("min_tf_agree can never be satisfied")
        return self

    @field_validator("sessions")
    @classmethod
    def _sessions(cls, v: list[SessionRule]) -> list[SessionRule]:
        if not v:
            raise ValueError("at least one session window required (use 0,24 for 24h)")
        return v

    def session_for(self, hour_utc: int) -> str | None:
        """First matching session name for a UTC hour, else None."""
        for s in self.sessions:
            if s.contains(hour_utc):
                return s.name
        return None


# D-042 — fields the auto-upgrade overrides when it meets a pre-D-042
# stored config (no `min_confluence` key). Risk/session fields the user
# may have customized are PRESERVED; only the strategy block moves to
# the new defaults.
_LEGACY_STRATEGY_DEFAULTS = {
    "timeframe": "M1",
    "confirm_tfs": ["M5", "M15"],
    "min_tf_agree": 1,
    "min_atr": 0.15,
    "sfp_wick_atr_ratio": 0.35,
    "rr": 1.6,
    "expiry_bars": 45,
    "cooldown_bars": 4,
    "pullback_enabled": True,
    "pullback_min_range_atr": 0.35,
    "pullback_wick_ratio": 0.45,
    "min_sl_atr": 1.5,
    "max_spread_to_risk": 0.30,
    # D-042 ICT block
    "smc_enabled": True,
    "bias_tfs": ["H4"],
    "min_confluence": 3,
    "max_zone_atr": 0.9,
    "vol_z_min": 0.8,
    "max_sl_atr": 3.5,
    "max_positions": 3,
}

#: D-050 — the exact pre-D-050 shipped default profile (M1 engine). Rows
#: still carrying it were NEVER customized by the user (the field is
#: admin-config only), so they move to the M5 short-term profile. Any
#: other value (a user's own TF choice, their own confirm list) stays put.
_LEGACY_D050_PROFILE = {
    "timeframe": "M1",
    "confirm_tfs": ["M5", "M15"],
    "min_atr": 0.15,
    "expiry_bars": 45,
}
_D050_PROFILE = {
    "timeframe": "M5",
    "confirm_tfs": ["M15"],
    "min_atr": 0.25,
    "expiry_bars": 36,
}


def upgrade_legacy_d050(raw: dict) -> tuple[dict, bool]:
    """D-050 — move untouched pre-D-050 rows (M1 shipped profile) to the
    M5 short-term profile (user directive: 5-minute timeframe trading).

    Returns (payload, moved). Only a row still carrying the EXACT old
    shipped profile AND no entry_mode key moves (same rule as the
    D-047/D-048/D-049 upgrades); user-customized values stay forever.
    """
    if not isinstance(raw, dict):
        return raw, False
    if "entry_mode" in raw:
        return raw, False  # already a D-050 row
    for key, want in _LEGACY_D050_PROFILE.items():
        if raw.get(key) != want:
            return raw, False
    out = dict(raw)
    out.update(_D050_PROFILE)
    return out, True


def upgrade_legacy_payload(raw: dict) -> tuple[dict, bool]:
    """Upgrade a pre-D-042 stored config to the ICT engine (one-time).

    Returns (upgraded_payload, changed). Unchanged when `min_confluence`
    is already present (current schema) — nothing to do.
    """
    if not isinstance(raw, dict) or "min_confluence" in raw:
        return raw, False
    out = dict(raw)
    out.update(_LEGACY_STRATEGY_DEFAULTS)
    return out, True


#: D-047 — the exact pre-D-047 default session pair. Rows still carrying
#: it were NEVER customized by the user, so they are auto-upgraded to the
#: Tokyo-inclusive default; anything else is a user choice and stays.
_LEGACY_SESSIONS = [{"name": "london", "utc": [7, 16]},
                    {"name": "newyork", "utc": [13, 20]}]
_D047_SESSIONS = [{"name": "tokyo", "utc": [0, 7]},
                  {"name": "london", "utc": [7, 16]},
                  {"name": "newyork", "utc": [13, 20]}]

#: D-048 shipped 3 -> 2; D-049 (honest 1500-bar window + structural TP)
#: measured 3 back on top (expR +0.023 vs +0.010) — rows still sitting on
#: the D-048-era default 2 were auto-set by the D-048 upgrade (the user
#: never chose them: the field is admin-config only), so they move to 3.
#: Rows on any OTHER value (a user's 1, 4, 5…) stay put.
_LEGACY_MIN_CONFLUENCE_D048 = 2
_LEGACY_MIN_CONFLUENCE_NOW = 3

#: D-049 — field-by-field one-time upgrades for rows still sitting on a
#: pre-D-049 shipped default. The rule is unchanged from D-047/D-048:
#: a row still carrying the EXACT old default was never customized by
#: the user, so it moves to the new default; anything else stays put.
_LEGACY_D049_FIELDS: dict[str, tuple[float, float]] = {
    # field: (old shipped default, new default)
    "rr": (1.1, 1.6),
    "expiry_bars": (20, 45),
    "max_spread_to_risk": (0.5, 0.30),
    "counter_trend_quality": (0.70, 0.58),
}


def upgrade_legacy_d049(raw: dict) -> tuple[dict, list[str]]:
    """D-049 — move untouched old-default rows to the recalibrated set.

    Returns (payload, moved_fields). Only fields still on the exact old
    shipped default move; user-customized values are preserved forever.
    """
    if not isinstance(raw, dict):
        return raw, []
    moved: list[str] = []
    out = dict(raw)
    for field, (old, new) in _LEGACY_D049_FIELDS.items():
        val = out.get(field)
        if val is not None and abs(float(val) - old) < 1e-9:
            out[field] = new
            moved.append(field)
    return out, moved


def upgrade_legacy_confluence(raw: dict) -> tuple[dict, bool]:
    """D-049 — move untouched D-048-era rows (min_confluence == 2) to 3."""
    if not isinstance(raw, dict):
        return raw, False
    if raw.get("min_confluence") != _LEGACY_MIN_CONFLUENCE_D048:
        return raw, False
    out = dict(raw)
    out["min_confluence"] = _LEGACY_MIN_CONFLUENCE_NOW
    return out, True


def upgrade_legacy_sessions(raw: dict) -> tuple[dict, bool]:
    """D-047 — add the Tokyo (Asian) session to untouched old-default rows.

    The pre-D-047 default (london+newyork only) made the engine refuse to
    fire ANY signal from 20:00 to 07:00 UTC — the entire Asian session,
    which is the platform's user morning (e.g. 06:00-13:00 in Dhaka).
    Rows still carrying that exact pair are upgraded; customized session
    lists are preserved untouched.
    """
    if not isinstance(raw, dict):
        return raw, False
    if raw.get("sessions") != _LEGACY_SESSIONS:
        return raw, False
    out = dict(raw)
    out["sessions"] = [dict(s) for s in _D047_SESSIONS]
    return out, True


DEFAULT_CONFIG = EngineConfig()


class ConfigRepo:
    """engine_config row (id=1) with in-memory fallback when DB is down (C6)."""

    def __init__(self) -> None:
        self._mem_config: EngineConfig = DEFAULT_CONFIG.model_copy(deep=True)
        self._mem_auto_trade: bool = False
        # D-036 — AI-signal -> auto-order on the REAL MT5 terminal (live arm)
        self._mem_auto_live: bool = False
        self._mem_auto_live_by: str | None = None

    async def load(self, db_engine: Any) -> tuple[EngineConfig, bool]:
        if db_engine is None:
            return self._mem_config, self._mem_auto_trade
        try:
            from sqlalchemy import text

            async with db_engine.connect() as conn:
                row = (
                    await conn.execute(
                        text("select config, auto_trade from engine_config where id = 1")
                    )
                ).first()
            if row is None:
                # schema.sql seeds it; if missing (fresh DB without seed) insert defaults
                await self.save(db_engine, self._mem_config, self._mem_auto_trade)
                return self._mem_config, self._mem_auto_trade
            # D-043: asyncpg/SQLAlchemy returns jsonb columns as dict
            # already — json.loads() only for string payloads (sqlite/mem)
            raw = row[0]
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except ValueError:
                    logger.warning("engine_config.config is not valid JSON — defaults")
                    return self._mem_config, self._mem_auto_trade
            if not isinstance(raw, dict):
                logger.warning("engine_config.config has unexpected type %s — defaults",
                               type(raw).__name__)
                return self._mem_config, self._mem_auto_trade
            raw, upgraded_strategy = upgrade_legacy_payload(raw)
            raw, upgraded_sessions = upgrade_legacy_sessions(raw)
            raw, upgraded_confluence = upgrade_legacy_confluence(raw)
            raw, moved_d049 = upgrade_legacy_d049(raw)
            raw, upgraded_d050 = upgrade_legacy_d050(raw)
            upgraded = (
                upgraded_strategy or upgraded_sessions
                or upgraded_confluence or bool(moved_d049) or upgraded_d050
            )
            cfg = EngineConfig.model_validate(raw)
            if upgraded:
                why = []
                if upgraded_strategy:  # pre-D-042 row: strategy block moved
                    why.append("strategy")
                if upgraded_sessions:
                    why.append("sessions")
                if upgraded_confluence:
                    why.append("confluence")
                if moved_d049:
                    why.append("d049:" + "+".join(moved_d049))
                if upgraded_d050:
                    why.append("d050:M5-profile")
                logger.info(
                    "engine config upgraded (%s) — persisting",
                    "+".join(why) or "strategy",
                )
                await self.save(db_engine, cfg, bool(row[1]))
            self._mem_config, self._mem_auto_trade = cfg, bool(row[1])
            return cfg, bool(row[1])
        except Exception as exc:  # noqa: BLE001 — degrade, never crash (C6)
            logger.warning("config load failed, using cached defaults: %s", exc)
            return self._mem_config, self._mem_auto_trade

    async def save(
        self, db_engine: Any, cfg: EngineConfig, auto_trade: bool
    ) -> None:
        self._mem_config, self._mem_auto_trade = cfg, auto_trade
        if db_engine is None:
            return
        try:
            from sqlalchemy import text

            async with db_engine.begin() as conn:
                await conn.execute(
                    text(
                        """
                        insert into engine_config (id, config, auto_trade, updated_at)
                        values (1, :cfg, :auto, now())
                        on conflict (id) do update
                          set config = excluded.config,
                              auto_trade = excluded.auto_trade,
                              updated_at = now()
                        """
                    ),
                    {"cfg": cfg.model_dump_json(), "auto": auto_trade},
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("config persist failed (kept in memory): %s", exc)

    # ------------------------------------------------- D-036 live MT5 auto arm

    async def load_auto_live(self, db_engine: Any) -> tuple[bool, str | None]:
        """(armed, armed_by) of the REAL-terminal auto-execution arm."""
        if db_engine is None:
            return self._mem_auto_live, self._mem_auto_live_by
        try:
            from sqlalchemy import text

            async with db_engine.connect() as conn:
                row = (
                    await conn.execute(
                        text(
                            "select auto_trade_live, auto_trade_live_by"
                            " from engine_config where id = 1"
                        )
                    )
                ).first()
            if row is None:
                return self._mem_auto_live, self._mem_auto_live_by
            self._mem_auto_live = bool(row[0])
            self._mem_auto_live_by = str(row[1]) if row[1] else None
            return self._mem_auto_live, self._mem_auto_live_by
        except Exception as exc:  # noqa: BLE001 — degrade, never crash (C6)
            logger.warning("auto_live load failed, using cached state: %s", exc)
            return self._mem_auto_live, self._mem_auto_live_by

    async def save_auto_live(
        self, db_engine: Any, enabled: bool, armed_by: str | None = None
    ) -> None:
        self._mem_auto_live, self._mem_auto_live_by = enabled, armed_by
        if db_engine is None:
            return
        try:
            from sqlalchemy import text

            async with db_engine.begin() as conn:
                await conn.execute(
                    text(
                        """
                        update engine_config
                           set auto_trade_live = :enabled,
                               auto_trade_live_by = :by,
                               updated_at = now()
                         where id = 1
                        """
                    ),
                    {"enabled": enabled, "by": armed_by},
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("auto_live persist failed (kept in memory): %s", exc)
