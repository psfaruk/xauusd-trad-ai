"""D-073 A/B — the entry-proximity change (setup_near_atr 1.5 -> 0.75)
measured on the mock replay: signal flow, fill rate, ENTRY DISTANCE
(the user's complaint: "মার্কেট কোথায় আর এন্ট্রি সিগন্যাল কোথায় গিয়ে পড়ে"),
R/h, and the drawing-true vs legacy split.

Honesty note (same as D-070): the mock's random walk cannot measure
live ACCURACY — what is measurable here is the geometry: how far the
booked entries sit from the market, how many signals flow, how many
pendings actually fill, and the (mock) expectancy.

Run: python scripts/ab_d073.py   (from backend/)
"""

from __future__ import annotations

import statistics as stats

from app.engine.backtest import load_mock_history, run_backtest
from app.engine.config import EngineConfig

SEEDS = (42, 7, 99)
BARS = 6000
SPREAD = 20.0  # points, same as the D-070 A/B


def run_variant(label: str, cfg: EngineConfig) -> dict:
    flows: list[float] = []
    fill_rates: list[float] = []
    dists: list[float] = []
    rs: list[float] = []
    geo_split = {"drawing": 0, "legacy": 0}
    fired = filled_total = 0
    for seed in SEEDS:
        # engine base TF is M1 (D-051) — feed the mock M1 series as-is
        base = load_mock_history(BARS, seed=seed)
        res = run_backtest(base, cfg=cfg, spread_points=SPREAD)
        st = res.stats()
        hours = 1e-9
        if res.date_from and res.date_to:
            hours = max(
                (res.date_to - res.date_from).total_seconds() / 3600.0, 1e-9
            )
        flows.append(st.get("total_signals", 0) / hours)
        fr = st.get("fill_rate")
        if fr is not None:
            fill_rates.append(float(fr))
        fired += st.get("total_signals", 0)
        filled_total += st.get("filled", 0)
        for s in res.signals:
            if s.market_ref:
                dists.append(abs(s.entry - s.market_ref))
            geo_split[s.geometry if s.geometry in geo_split else "legacy"] += 1
        rs.append((st.get("total_r") or 0.0) / hours)
    dists.sort()
    n = len(dists)

    def pct(q: float) -> float:
        return dists[min(n - 1, int(q * n))] if n else float("nan")

    return {
        "label": label,
        "signals_per_hour": round(stats.mean(flows), 2),
        "fill_rate_limit": (
            round(stats.mean(fill_rates), 3) if fill_rates else None
        ),
        "fired_total": fired,
        "filled_total": filled_total,
        "entry_dist_usd": {
            "n": n,
            "p50": round(pct(0.50), 2),
            "p75": round(pct(0.75), 2),
            "p90": round(pct(0.90), 2),
            "over_2usd": round(
                (sum(1 for d in dists if d > 2.0) / n) if n else 0.0, 3
            ),
            "over_3usd": round(
                (sum(1 for d in dists if d > 3.0) / n) if n else 0.0, 3
            ),
        },
        "r_per_hour": round(stats.mean(rs), 3),
        "geometry_split": geo_split,
    }


def main() -> None:
    variants = [
        ("OLD reach 1.5", EngineConfig(setup_near_atr=1.5)),
        ("NEW reach 0.75 (shipped)", EngineConfig()),
        ("MID reach 1.0", EngineConfig(setup_near_atr=1.0)),
    ]
    for label, cfg in variants:
        r = run_variant(label, cfg)
        print(f"\n=== {r['label']} ===")
        for k, v in r.items():
            if k != "label":
                print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
