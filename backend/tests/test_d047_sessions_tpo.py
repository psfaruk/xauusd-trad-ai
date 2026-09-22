"""D-047 — Tokyo (Asian) session upgrade + TPO time-at-price levels.

1. session defaults now cover Tokyo 00-07 UTC (the user's morning);
2. stored configs carrying the EXACT old default pair auto-upgrade;
3. customized session lists are preserved untouched;
4. TPO profile: price buckets where the market SPENT TIME become marked
   S/R levels (POC / value area / high-time nodes) with side + strength.
"""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pandas as pd

from app.engine.config import (
    DEFAULT_CONFIG,
    EngineConfig,
    upgrade_legacy_sessions,
)


def test_default_sessions_cover_asian_morning() -> None:
    """Dhaka 06:00-13:00 == UTC 00:00-07:00 must be IN-SESSION now."""
    for hour in range(0, 7):
        assert DEFAULT_CONFIG.session_for(hour) == "tokyo", f"hour {hour} blocked"
    assert DEFAULT_CONFIG.session_for(7) == "london"
    assert DEFAULT_CONFIG.session_for(15) == "london"
    assert DEFAULT_CONFIG.session_for(19) == "newyork"
    # only the true dead zone (NY close -> Tokyo open) stays off-session
    assert DEFAULT_CONFIG.session_for(21) is None
    assert DEFAULT_CONFIG.session_for(23) is None


def test_legacy_default_sessions_row_upgrades() -> None:
    """A stored row still on the old london+newyork default gains tokyo."""
    raw = {"sessions": [{"name": "london", "utc": [7, 16]},
                        {"name": "newyork", "utc": [13, 20]}],
           "min_confluence": 3}
    out, changed = upgrade_legacy_sessions(raw)
    assert changed is True
    names = [s["name"] for s in out["sessions"]]
    assert names == ["tokyo", "london", "newyork"]
    cfg = EngineConfig.model_validate(out)
    assert cfg.session_for(3) == "tokyo"


def test_customized_sessions_are_preserved() -> None:
    """User-edited session lists are NEVER touched by the upgrade."""
    custom = {"sessions": [{"name": "london", "utc": [8, 16]}],
              "min_confluence": 3}
    out, changed = upgrade_legacy_sessions(custom)
    assert changed is False
    assert out == custom


def test_missing_sessions_key_is_left_alone() -> None:
    """No sessions key (very old row) -> validator defaults apply later."""
    out, changed = upgrade_legacy_sessions({"min_confluence": 3})
    assert changed is False


# ------------------------------------------------------------- TPO levels

def _m1_df(minutes: int = 600, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    # price oscillates between two magnets: 4300 (15 min clusters) and 4320
    base = np.where(np.arange(minutes) % 120 < 25, 4320.0, 4300.0)
    close = base + rng.normal(0, 0.15, minutes)
    t0 = datetime(2026, 9, 22, 0, 0, tzinfo=UTC)
    return pd.DataFrame({
        "time_utc": pd.date_range(t0, periods=minutes, freq="1min", tz="UTC"),
        "o": close + 0.05, "h": close + 0.35, "l": close - 0.35,
        "c": close, "v": rng.integers(50, 500, minutes),
    })


def test_tpo_marks_levels_where_market_spent_time() -> None:
    """The two magnets (4300/4320) must surface as strong TPO levels."""
    from app.analysis.tpo import tpo_profile

    df = _m1_df(1200)
    prof = tpo_profile(df, lookback_minutes=1200, bucket=0.5)
    # POC must sit on one of the two magnets
    assert min(abs(prof["poc"] - 4300.0), abs(prof["poc"] - 4320.0)) <= 0.6
    # both magnets appear among the strong levels
    prices = [lv["price"] for lv in prof["levels"]]
    assert any(abs(p - 4300.0) <= 0.6 for p in prices)
    assert any(abs(p - 4320.0) <= 0.6 for p in prices)
    # levels carry side (vs current price) + minutes + strength
    for lv in prof["levels"]:
        assert lv["side"] in ("support", "resistance")
        assert lv["minutes"] > 0
        assert 0.0 <= lv["strength"] <= 1.0


def test_tpo_value_area_brackets_poc() -> None:
    from app.analysis.tpo import tpo_profile

    prof = tpo_profile(_m1_df(900), lookback_minutes=900, bucket=0.5)
    assert prof["va_lo"] <= prof["poc"] <= prof["va_hi"]
    assert prof["va_minutes"] > 0


def test_tpo_nearest_level_distance() -> None:
    """Engine helper: distance-to-nearest-strong-level in ATR units."""
    from app.analysis.tpo import nearest_level

    prof_levels = [
        {"price": 4300.0, "minutes": 60, "side": "support", "strength": 0.9},
        {"price": 4320.0, "minutes": 45, "side": "resistance", "strength": 0.7},
    ]
    near = nearest_level(prof_levels, 4300.4)
    assert near is not None and near["price"] == 4300.0
    assert nearest_level(prof_levels, 4310.0) is None  # far from both
    assert nearest_level([], 4300.0) is None


def test_tpo_handles_thin_data() -> None:
    """Degenerate inputs must return an empty-but-valid profile."""
    from app.analysis.tpo import tpo_profile

    empty = tpo_profile(pd.DataFrame(), lookback_minutes=100, bucket=0.5)
    assert empty["levels"] == []
    assert empty["poc"] is None
    tiny = tpo_profile(_m1_df(5), lookback_minutes=1440, bucket=0.5)
    assert tiny["poc"] is not None  # 5 bars (min) still profile
    below_min = tpo_profile(_m1_df(4), lookback_minutes=1440, bucket=0.5)
    assert below_min["levels"] == []  # under the 5-bar floor: empty-valid
