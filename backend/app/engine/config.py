"""Engine configuration model (SPEC §8.6 defaults) + DB repository.

The single `engine_config` row (id=1) holds a JSONB payload of EngineConfig
fields plus the separate `auto_trade` flag (global kill switch, SPEC §0 —
defaults to false and is NEVER touched by the strategy engine itself).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

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
    """Strategy parameters (SPEC §8.6 defaults)."""

    model_config = ConfigDict(extra="forbid")

    timeframe: str = "M15"
    trend_tf: str = "H1"
    ema_fast: int = Field(20, ge=1)
    ema_slow: int = Field(50, ge=1)
    trend_ema: int = Field(50, ge=1)
    rsi_period: int = Field(14, ge=2)
    rsi_buy_min: float = Field(40.0, ge=0, le=100)
    rsi_buy_max: float = Field(65.0, ge=0, le=100)
    rsi_sell_min: float = Field(35.0, ge=0, le=100)
    rsi_sell_max: float = Field(60.0, ge=0, le=100)
    atr_period: int = Field(14, ge=1)
    min_atr: float = Field(0.8, ge=0)
    sfp_lookback: int = Field(20, ge=2)
    sfp_wick_atr_ratio: float = Field(0.3, gt=0)
    sl_buffer_atr: float = Field(0.2, ge=0)
    rr: float = Field(2.0, gt=0)
    expiry_bars: int = Field(20, ge=1)
    cooldown_bars: int = Field(3, ge=0)
    sessions: list[SessionRule] = Field(
        default_factory=lambda: [
            SessionRule(name="london", utc=(7, 16)),
            SessionRule(name="newyork", utc=(13, 20)),
        ]
    )
    news_blackout_min: int = Field(30, ge=0)
    max_spread_points: int = Field(35, ge=1)
    risk_mode: str = Field("percent", pattern="^(percent|fixed)$")
    risk_percent: float = Field(0.5, gt=0, le=100)
    fixed_lot: float = Field(0.01, gt=0)
    max_positions: int = Field(1, ge=1)
    daily_max_loss_pct: float = Field(3.0, gt=0)
    magic: int = Field(234000, ge=0)

    @field_validator("timeframe", "trend_tf")
    @classmethod
    def _tf(cls, v: str) -> str:
        validate_tf(v)
        return v

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
            cfg = EngineConfig.model_validate(json.loads(row[0]))
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
