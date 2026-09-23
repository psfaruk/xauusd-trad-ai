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

from app.mt5.base import market_key, validate_tf

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
    D-050: every signal becomes a PENDING LIMIT order placed at a
    structural POI level BEYOND the current market — BUY limits BELOW the
    nearest demand (support) zone, SELL limits ABOVE the nearest supply
    (resistance) zone (user directive: "মার্জিন" between market and entry
    so noise cannot stop out the trade straight away).
    D-051 (user directives, Bengali): the base timeframe is BACK on M1
    ("আগে অ্যাপ এ এক মিনিটের টাইম ফ্রেম এর উপর ভিত্তি করে সিগন্যাল আসতো.
    ওটাই টিক টিক ছিলো" — the M1 profile is the one that produced
    signals, M5 starved the flow); pending entries clamp to a 4-6 USD
    distance from the market ("সর্বোচ্চ 4 থেকে 6 usd উপরে অথবা নিচে
    পেন্ডিং অর্ডার বসাবেন ... অবশ্যই এক মিনিটের ক্যান্ডেল দেখে এন্ট্রি
    বসাবেন"); a few TRUSTED strategies voting together is enough
    ("বিশ্বাসযোগ্য কয়েকটি স্ট্র্যাটেজি" + real-time big buyer/seller
    weighting); BTCUSD signals + per-market auto-trade control; every
    strategy's state streams to the UI in real time (strategy_pulse WS
    frame each bar close).
    """

    model_config = ConfigDict(extra="forbid")

    timeframe: str = "M1"     # trigger timeframe (D-051: back to M1 — the
    #                                  user's signals came on the 1-minute
    #                                  chart; every pending entry is placed
    #                                  off the M1 candle)
    trend_tf: str = "H1"      # major trend timeframe
    confirm_tfs: list[str] = Field(
        default_factory=lambda: ["M5", "M15"],
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
    min_atr: float = Field(0.15, ge=0)   # M1 ATR floor (D-051: back to the
    #                                    M1 calibration that produced the
    #                                    user's original signal flow)
    sfp_lookback: int = Field(20, ge=2)
    sfp_wick_atr_ratio: float = Field(0.35, gt=0)
    sl_buffer_atr: float = Field(0.2, ge=0)
    rr: float = Field(1.6, gt=0,         # D-049: fallback TP multiple when
        description="TP multiple used ONLY when no structural target "
                    "sits within tp_max_r — the primary TP is predicted "
                    "from the nearest opposing zone/liquidity/TPO level")
    expiry_bars: int = Field(45, ge=1)  # D-051: 45 min on M1 (the profile
    #                                     # that produced the user's original
    #                                     # signal flow) — structure TPs get
    #                                     # room to be HIT, not expire mid-flight
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
    # -------------------------------------------------- D-052 money window
    daily_loss_usd: float = Field(
        0.0, ge=0,
        description="D-052 — user-directed DAILY STOP LOSS in USD (0 = off): "
                    "when realized+floating loss from the day-start balance "
                    "reaches this, auto-trade turns OFF (button auto-off, "
                    "user directive: 'স্টপ লস কত টার্গেট প্রফিট কত usd')",
    )
    daily_profit_usd: float = Field(
        0.0, ge=0,
        description="D-052 — user-directed DAILY TARGET PROFIT in USD "
                    "(0 = off): when reached, profit is locked, all "
                    "positions close and auto-trade turns OFF",
    )
    day_start_balance: float = Field(
        0.0, ge=0,
        description="D-052 — 'আজকের ট্রেডিং ব্যালেন্স' the user enters in "
                    "the money-management window: the anchor the USD "
                    "loss/profit checks measure from (0 = use account "
                    "equity at arm time)",
    )
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
                    "away from the fill (plus 2 spreads and entry_min_usd, "
                    "whichever is larger)",
    )
    entry_min_usd: float = Field(
        1.0, gt=0,
        description="D-051 — absolute USD floor for the pending-entry "
                    "distance (the ATR floor collapses on quiet M1 bars; "
                    "an entry closer than this fills on pure spread noise)",
    )
    pending_offset_atr: float = Field(
        0.8, gt=0,
        description="fallback pending distance (ATRs) when no same-side "
                    "POI zone sits within reach of the market",
    )
    pending_target_usd: float = Field(
        4.5, gt=0,
        description="D-051 — preferred pending distance in USD when no "
                    "same-side POI zone anchors the entry (user directive: "
                    "'সর্বোচ্চ 4 থেকে 6 usd উপরে অথবা নিচে পেন্ডিং অর্ডার "
                    "বসাবেন' — 4-6 USD away so the order actually books)",
    )
    pending_max_atr: float = Field(
        15.0, gt=0,
        description="maximum pending distance (ATRs) — secondary cap; "
                        "the USD cap below normally binds first (D-051)",
    )
    pending_max_usd: float = Field(
        6.0, gt=0,
        description="D-051 — HARD USD cap on the pending-entry distance "
                    "from the market (user directive: at most 4-6 USD — "
                    "orders further than this simply never fill; the M1 "
                    "candle decides the exact anchor inside the cap)",
    )
    pending_expiry_bars: int = Field(
        60, ge=1,
        description="how many engine-TF bars a PENDING signal may wait for "
                    "its fill before expiring unfilled (1h on M1 — a 4-6 "
                    "USD retrace takes its time; an unfilled order is a "
                    "MISSED trade, never a loss)",
    )
    max_pending_signals: int = Field(
        6, ge=1,
        description="concurrent PENDING (unfilled) signal budget — kept "
                    "separate from max_positions so unfilled limits never "
                    "clog the signal flow (user directive: signals must "
                    "keep coming)",
    )
    # -------------------------------------------------- D-057 drawing-true block
    drawing_true: bool = Field(
        True,
        description="D-057 (user directive: 'SL TP ENTRY সব কিছু এই চার্ট "
                    "ফলো করে হবে') — when a zone supports the trade, the "
                    "signal's ENTRY/SL/TP come from the SAME shared "
                    "geometry the chart's entry-setup box draws "
                    "(app.analysis.setup_geometry): entry = zone midpoint, "
                    "SL beyond the zone protected past liquidity, TP at "
                    "the drawn liquidity target. False keeps the legacy "
                    "poi_pending_entry + smart_targets chain",
    )
    setup_near_atr: float = Field(
        1.5, gt=0,
        description="D-057 — how close (in M5-ATR units) a DRAWN zone "
                    "(the zone boxes the chart renders) must be to the "
                    "market for the drawing-true geometry to anchor the "
                    "order there; the setup BOX itself still uses the "
                    "tighter 0.75 (setup_geometry.SETUP_NEAR_ATR) — the "
                    "box is the 'trade is live here' card, this window is "
                    "the 'orders sit at drawn zone boxes' reach",
    )
    setup_entry_anchor: str = Field(
        "near", pattern="^(near|mid)$",
        description="D-057 — where on the DRAWN zone the entry anchors: "
                    "'near' = the zone's near edge (the first-touch "
                    "boundary line — a pending there fills on the first "
                    "retest; a mid-zone pending only fills when price "
                    "trades deep, which is adverse selection); 'mid' = "
                    "the classic D-052 box midpoint",
    )
    setup_max_rr: float = Field(
        1.8, gt=0,
        description="D-057 — drawn targets beyond this RR are swing-scale "
                    "trades, not short-time trades: the geometry falls "
                    "back to the legacy ladder instead ('TP যেনো হিট "
                    "বেশি হয়' — a TP the market cannot reach in the "
                    "engine's horizon is a missed TP, not a win)",
    )
    setup_max_risk_atr: float = Field(
        1.6, gt=0,
        description="D-057 — sanity cap on the drawn SL distance in M5-ATR "
                    "units (ATR-relative so the same gate scales across "
                    "mock / gold / BTC): a zone whose structural stop is "
                    "wider than this is a swing trade, not the user's "
                    "short-time profile — the engine falls back to the "
                    "legacy chain",
    )
    counter_needs_sweep: bool = Field(
        False,
        description="D-056 — when TRUE, counter-trend zone reversals must "
                    "show the stop-hunt PROOF (the bar wicked through the "
                    "zone's far edge and closed back inside: sweep + "
                    "reclaim) before firing. Default FALSE: the D-049 "
                    "user directive ('Best POI ZONE... সিগন্যাল দিতে হবে, "
                    "মিস করা যাবে না') requires zone signals to keep "
                    "flowing against the bias; the A/B showed the gate "
                    "is seed-mixed (protects the worst day, costs the "
                    "good ones) — enable it for a quieter signal flow",
    )
    # -------------------------------------------------- D-061 trap/AMD block
    trap_filter: bool = Field(
        True,
        description="D-061 (user directive: 'কখন রিটেইলার ট্রেডার "
                    "ইন্সট্রিটিউনাল ট্রেডার এর কাছে ফেইল হয়েছো... "
                    "ইকমেলিউশন ও মেনোপোলেশন কোনো লজিক এড করতে পারবেন?') — "
                    "detect the institutional AMD trap (liquidity sweep "
                    "reclaimed against the trade, unswept pool inside the "
                    "risk window, opposing displacement run, Judas timing) "
                    "and REFUSE the retail side above trap_block_risk. "
                    "The AMD phase + trap reasons ride every signal "
                    "payload + strategy pulse regardless of this flag",
    )
    trap_block_risk: float = Field(
        0.7, ge=0.0, le=1.0,
        description="D-061 — trap risk at/above this blocks the signal "
                    "(near-miss: 'institutional trap'): the trade IS the "
                    "liquidity the institutions are hunting",
    )
    trap_warn_risk: float = Field(
        0.4, ge=0.0, le=1.0,
        description="D-061 — trap risk at/above this tags the signal "
                    "(context.trap + trace line) and cuts confidence by "
                    "trap_conf_penalty, but the signal still fires — the "
                    "user SEES the warning on the chart and the signal "
                    "panel",
    )
    trap_conf_penalty: float = Field(
        0.12, ge=0.0, le=0.5,
        description="D-061 — confidence penalty applied when trap risk is "
                    "in the warn band (the trade still fires, visibly "
                    "flagged)",
    )
    # -------------------------------------------------- D-064 structure block
    structure_guard: bool = Field(
        True,
        description="D-064 (user directive: 'মার্কেট কোথায় গিয়ে রেস্ট করে... "
                    "কি এমন লজিক আছে যে মার্কেট এখন রিভার্স করবে? কত বার "
                    "HL LL LH HH হলে রিভার্স বা কনটিনিউ করে?') — the "
                    "market-structure ladder (consecutive same-direction "
                    "breaks) + REST zones + pullback magnets ride every "
                    "pulse and signal context; fading a MATURE run "
                    "(structure_counter_legs+ legs) without structural "
                    "proof (fresh CHoCH or swept-and-reclaimed pool) is "
                    "REFUSED; chasing an extended run costs confidence",
    )
    structure_counter_legs: int = Field(
        3, ge=1, le=10,
        description="D-064 — a counter-run signal is blocked as a "
                    "falling-knife fade only when the live run has THIS "
                    "many consecutive same-direction breaks AND no CHoCH / "
                    "sweep proof (measured: 87% of runs end at leg 3 — "
                    "below this the run is not yet mature, and the D-049 "
                    "'সিগনাল মিস করা যাবে না' directive keeps zone flow)",
    )
    structure_exhaust_legs: int = Field(
        3, ge=2, le=10,
        description="D-064 — legs at/above this mark the run EXTENDED "
                    "(overdue for the measured ~2 ATR rest): with-trend "
                    "signals pay structure_chase_penalty, the read says "
                    "'do not chase, expect a rest at the magnets'",
    )
    structure_chase_penalty: float = Field(
        0.08, ge=0.0, le=0.5,
        description="D-064 — confidence penalty on a WITH-trend signal "
                    "fired while the run is extended (buying the tail of a "
                    "mature run: the measured rest eats the entry before "
                    "the next leg)",
    )
    structure_rest_bonus: float = Field(
        0.06, ge=0.0, le=0.5,
        description="D-064 — confidence bonus on a WITH-trend signal fired "
                    "right after / inside a REST zone (the user's exact "
                    "pattern: down, rest, small counter-move, continue — "
                    "the entry at the rest rejection is the good one)",
    )
    # -------------------------------------------------- D-051 trusted-vote block
    trusted_min_votes: float = Field(
        2.0, ge=1.0, le=6.0,
        description="D-051 — a few TRUSTED strategies voting together is "
                    "enough (user directive: 'যদি কয়েকটি স্ট্যাটাজি মিলে "
                    "ভোট দেয়, বিশ্বাসযোগ্য কয়েকটি স্ট্রাটেজি হতে হবে'): "
                    "when the trusted core (real-time whale pulse counts "
                    "DOUBLE, M1 structure, liquidity sweep, POI zone) "
                    "reaches this score the signal fires even if the full "
                    "ICT panel is short of min_confluence",
    )
    whale_pulse_bars: int = Field(
        3, ge=1, le=10,
        description="D-051 — how many recent M1 bars the REAL-TIME whale "
                    "pulse scans (user directive: 'কখন বড় ভাইয়ার এন্ট্রি "
                    "নিলো এই বিষয়টি রিয়েল টাইমে ধরা লাগবে' — big "
                    "buyer/seller entries must be caught as they happen)",
    )
    whale_confidence_boost: float = Field(
        0.05, ge=0.0, le=0.2,
        description="confidence bonus when the real-time whale pulse agrees "
                    "with the trade direction (big-player entries matter "
                    "MORE — user directive)",
    )
    # -------------------------------------------------- D-051 multi-market block
    signal_symbols: list[str] = Field(
        default_factory=lambda: ["XAUUSD", "BTCUSD"],
        description="D-051 — markets the SIGNAL ENGINES run on (user "
                    "directive: 'বিটকয়েনের উপর কোন সিগন্যাল ... হচ্ছে না'): "
                    "one engine per market; signals are ALWAYS generated "
                    "for every listed market regardless of auto-trade",
    )
    auto_trade_symbols: list[str] = Field(
        default_factory=lambda: ["XAUUSD"],
        description="D-051 — markets where AUTO-TRADE may EXECUTE orders "
                    "(user directive: users activate which markets "
                    "auto-trading executes on). Signals still generate for "
                    "every signal_symbols market — only execution is gated",
    )

    @field_validator("signal_symbols", "auto_trade_symbols")
    @classmethod
    def _symbol_lists(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("symbol list may not be empty")
        seen: set[str] = set()
        out: list[str] = []
        for s in v:
            clean = s.strip().upper()
            if not clean or not clean.isalnum():
                raise ValueError(f"invalid market symbol: {s!r}")
            if clean in seen:
                continue  # tolerate duplicates, keep one
            seen.add(clean)
            out.append(clean)
        return out

    @model_validator(mode="after")
    def _auto_trade_subset(self) -> EngineConfig:
        # execution markets must reference markets we actually signal on
        # (normalize broker suffixes: XAUUSDm -> XAUUSD)
        signal_keys = {market_key(s) for s in self.signal_symbols}
        for m in self.auto_trade_symbols:
            if market_key(m) not in signal_keys:
                raise ValueError(
                    f"auto_trade_symbols entry {m!r} is not in signal_symbols"
                )
        return self

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

#: D-051 — the exact D-050 shipped profile (M5 + 24-bar pendings). Rows
#: still carrying it were set by the D-050 AUTO-UPGRADE (never hand-
#: edited by the user), so they move BACK to M1 (user directive:
#: "আগে অ্যাপ এ এক মিনিটের টাইম ফ্রেম এর উপর ভিত্তি করে সিগন্যাল আসতো.
#: ওটাই টিক টিক ছিলো" — M5 starved the signal flow). Any user-
#: customized value stays put, same rule as every legacy upgrade.
_LEGACY_D051_PROFILE = {
    "timeframe": "M5",
    "confirm_tfs": ["M15"],
    "min_atr": 0.25,
    "expiry_bars": 36,
    "pending_expiry_bars": 24,
    "pending_max_atr": 10.0,
}
_D051_PROFILE = {
    "timeframe": "M1",
    "confirm_tfs": ["M5", "M15"],
    "min_atr": 0.15,
    "expiry_bars": 45,
    "pending_expiry_bars": 60,
    "pending_max_atr": 15.0,
}


def upgrade_legacy_d051(raw: dict) -> tuple[dict, bool]:
    """D-051 — move rows still on the D-050 auto-set M5 profile BACK to
    the M1 profile the user's signal flow came from (user directive).

    Returns (payload, moved). Only a row with `entry_mode` (a D-050 row)
    still carrying the EXACT D-050 shipped values AND no `signal_symbols`
    key (pre-D-051) moves; anything user-customized stays forever.
    """
    if not isinstance(raw, dict):
        return raw, False
    if "signal_symbols" in raw:
        return raw, False  # already a D-051 row
    if "entry_mode" not in raw:
        return raw, False  # pre-D-050 row: the d050 upgrade handles it
    for key, want in _LEGACY_D051_PROFILE.items():
        if raw.get(key) != want:
            return raw, False
    out = dict(raw)
    out.update(_D051_PROFILE)
    return out, True


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
            raw, upgraded_d051 = upgrade_legacy_d051(raw)
            upgraded = (
                upgraded_strategy or upgraded_sessions
                or upgraded_confluence or bool(moved_d049) or upgraded_d050
                or upgraded_d051
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
                if upgraded_d051:
                    why.append("d051:M1-back")
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
