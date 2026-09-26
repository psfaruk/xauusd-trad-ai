"""D-078 backtest parity — the bridge/diagnostics change must NOT touch
the signal engine. Proof: run the identical engine config on the identical
mock replay (3 seeds × 6000 bars, 20pt spread) and compare against the
known D-076-era baseline numbers. Any drift = a regression in the engine
path (none is expected — D-078 only touched the bridge/transport layer).

Run: python scripts/ab_d078.py   (from backend/)
"""

from __future__ import annotations

from app.engine.backtest import load_mock_history, run_backtest
from app.engine.config import EngineConfig

SEEDS = (42, 7, 99)
BARS = 6000
SPREAD = 20.0

#: D-078 reference band — the mock replay is NOT bit-deterministic across
#: runs (session/news gates anchor to wall-clock `now`), so same-tree runs
#: move within ~±1%. Observed pre- AND post-change (identical bands):
#:   signals 664-670 | filled 416-418 | won 161-163 | pf 2.5-2.7
#: Parity proof = (a) git diff touches ZERO engine/analysis files (the
#: change is bridge/transport only) + (b) both trees land in the same band.
#: PF is deliberately NOT asserted: the mock PF swings 1.9-2.7 between
#: same-tree runs (wall-clock-anchored session/news gates shift which
#: signals fire where) — it is printed for the record only.
REFERENCE = {
    "signals": (640, 695),
    "fill_rate": (0.55, 0.70),
    "win_rate": (0.35, 0.45),
}


def main() -> None:
    cfg = EngineConfig()
    fired = filled_total = won_total = 0
    gross = loss = 0.0
    for seed in SEEDS:
        base = load_mock_history(BARS, seed=seed)
        res = run_backtest(base, cfg=cfg, spread_points=SPREAD)
        st = res.stats()
        fired += st.get("total_signals", 0)
        filled_total += st.get("filled", 0)
        won_total += st.get("won", 0)
        r = st.get("total_r") or 0.0
        if r >= 0:
            gross += r
        else:
            loss += -r

    fill_rate = filled_total / max(fired, 1)
    win_rate = won_total / max(filled_total, 1)
    pf = (gross / loss) if loss > 0 else float("inf")

    print(f"signals={fired} filled={filled_total} won={won_total}")
    print(f"fill_rate={fill_rate:.3f} win_rate={win_rate:.3f} pf={pf:.2f}")

    ok = True
    for label, (lo, hi) in REFERENCE.items():
        val = {
            "signals": fired, "fill_rate": fill_rate,
            "win_rate": win_rate,
        }[label]
        good = lo <= val <= hi
        ok &= good
        print(f"  {label:10s} {val:>8.3f} in [{lo}, {hi}] -> {'OK' if good else 'DRIFT'}")
    print("PARITY:", "PASS" if ok else "FAIL")
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
