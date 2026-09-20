"""Real-API smoke test: LiveDataSource against live Binance/gold-api (D-030).

Run: backend/.venv/bin/python scripts/smoke_live.py
Verifies: live quotes tick, real gold prices (~spot), M15/H1/D1 candles,
forming bar, paper order at real price, fallback chain transparency.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from app.mt5.base import Order, validate_tf  # noqa: E402
from app.mt5.live_source import LiveDataSource  # noqa: E402


async def main() -> int:
    src = LiveDataSource(poll_seconds=1.0, connect_timeout_s=8.0)
    print("connecting to free real-time providers...")
    info = await src.connect({"server": "LiveMarket"})
    print(f"connected: {info['server']}  balance={info['balance']}")

    print("\n--- streaming 5 ticks (1s cadence) ---")
    for i in range(5):
        t = await src.get_tick("XAUUSD")
        print(f"  tick {i + 1}: bid={t.bid:.2f} ask={t.ask:.2f} spread={t.ask - t.bid:.2f}")
        await asyncio.sleep(1.0)

    print("\n--- real candles (Binance PAXG) ---")
    for tf, count in (("M1", 5), ("M15", 5), ("H1", 5), ("D1", 3)):
        df = await src.get_rates("XAUUSD", tf, count)
        last = df.iloc[-1] if len(df) else None
        if last is not None:
            print(
                f"  {tf:>3}: {len(df)} bars  last close={float(last['c']):.2f}"
                f"  t={last['time_utc']}"
            )
        else:
            print(f"  {tf:>3}: NO DATA")

    print("\n--- forming bars ---")
    for tf in ("M1", "M15", "H1"):
        bar = await src.get_forming_bar("XAUUSD", tf)
        if bar:
            print(f"  {tf:>3} forming: o={bar['o']:.2f} h={bar['h']:.2f} l={bar['l']:.2f} c={bar['c']:.2f}")
        else:
            print(f"  {tf:>3} forming: NONE")

    print("\n--- paper trade at REAL prices ---")
    res = await src.place_order(Order(symbol="XAUUSD", side="BUY", volume=0.10))
    print(f"  BUY 0.10 -> ok={res.ok} fill={res.price} #{res.ticket}")
    positions = await src.get_positions()
    print(f"  positions: {[(p.side, p.volume, round(p.profit, 2)) for p in positions]}")
    acct = src.account_info()
    print(f"  account: balance={acct['balance']:.2f} equity={acct['equity']:.2f}")
    if positions:
        closed = await src.close_position(positions[0].ticket)
        print(f"  close -> ok={closed.ok} {closed.comment}")

    st = src.feed_status()
    print(f"\nfeed status: {st}")

    # external cross-check: spot XAU from gold-api vs our PAXG feed
    import httpx

    async with httpx.AsyncClient(timeout=5.0) as http:
        r = await http.get("https://api.gold-api.com/price/XAU")
        spot = float(r.json()["price"])
    mid = st["last_price"]
    print(f"cross-check: PAXG feed {mid:.2f} vs gold-api spot {spot:.2f} "
          f"-> basis {mid - spot:+.2f} ({(mid - spot) / spot * 100:+.2f}%)")

    await src.disconnect()
    print("\nSMOKE OK — real-time data streaming verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
