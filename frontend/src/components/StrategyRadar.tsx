/**
 * StrategyRadar (D-051) — the live strategy-state panel.
 *
 * User directive: "অ্যাপ এর প্রত্যেকটি স্টাডিজির ডাটা রিয়েল টাইমে সেকেন্ডের
 * মধ্যে দেখাতে হবে" — every strategy's input/state must be visible in the
 * app in real time, plus "প্রতি সেকেন্ডে ক্যান্ডেলস্টিকের reaction আর
 * ক্রেতা-বিক্রেতা কে dominate করছে" — the candle-by-candle buyer/seller
 * dominance.
 *
 * Data flow:
 *  - the engine broadcasts a `strategy_pulse` WS frame on EVERY M1 close
 *    (bias, RSI, ATR, session, triggers, whale pulse, POI zone map,
 *    near-miss reason, fired-signal summary, per-check states);
 *  - between closes the LIVE candle row recomputes the same dominance
 *    math from the FORMING M1 bar (bar_update frames arrive with every
 *    tick) — so the panel visibly moves second-by-second;
 *  - a 1s heartbeat timer re-renders the "updated Ns ago" stamp.
 */

import { useEffect, useState } from "react";
import { Badge, Card, SectionTitle } from "./ui";
import { useFormingBar, usePulse, useTick } from "../state/feed";
import type { Candle, WsStrategyPulseMsg } from "../types";

/* ------------------------------------------------------ live candle math */

interface LiveDominance {
  delta: number;
  buyPct: number;
  sellPct: number;
  dir: "bull" | "bear" | "flat";
}

/** Same proxy the backend's candle_pulse uses, run on the forming bar. */
function liveDominance(bar: Candle | null): LiveDominance | null {
  if (!bar) return null;
  const rng = bar.h - bar.l;
  if (rng <= 0) return null;
  const delta = ((bar.c - bar.l) / rng) * 2 - 1;
  return {
    delta,
    buyPct: Math.round(((delta + 1) / 2) * 100),
    sellPct: Math.round((1 - (delta + 1) / 2) * 100),
    dir: bar.c >= bar.o ? "bull" : "bear",
  };
}

/* ------------------------------------------------------------- sub-rows */

function DominanceBar({ buy, sell }: { buy: number; sell: number }) {
  return (
    <div className="flex min-w-0 flex-col gap-1">
      <div className="flex h-2.5 min-w-0 overflow-hidden rounded-full border border-zinc-800 bg-zinc-900">
        <div
          className="bg-emerald-500/70 transition-[width] duration-300"
          style={{ width: `${buy}%` }}
        />
        <div
          className="bg-red-500/70 transition-[width] duration-300"
          style={{ width: `${sell}%` }}
        />
      </div>
      <div className="flex min-w-0 items-center justify-between text-[10px] font-semibold tabular-nums">
        <span className="text-emerald-400">buy {buy}%</span>
        <span className="text-red-400">sell {sell}%</span>
      </div>
    </div>
  );
}

function ZoneRow({
  side, lo, hi, quality, source, dist,
}: {
  side: string; lo: number; hi: number; quality: number; source: string; dist: number;
}) {
  return (
    <li className="flex min-w-0 items-center gap-2 rounded-lg border border-zinc-800/70 bg-zinc-900/40 px-2.5 py-1.5">
      <Badge tone={side === "demand" ? "green" : "red"}>{side === "demand" ? "SUP" : "RES"}</Badge>
      <span className="min-w-0 flex-1 truncate font-mono text-[11px] tabular-nums text-zinc-300">
        {lo.toFixed(2)}–{hi.toFixed(2)}
      </span>
      <span className="shrink-0 text-[9px] text-zinc-500">
        q{quality.toFixed(2)} · {source}
      </span>
      <span className="shrink-0 rounded bg-zinc-800/80 px-1.5 py-0.5 font-mono text-[10px] font-bold tabular-nums text-gold">
        {dist.toFixed(2)}$
      </span>
    </li>
  );
}

/* ------------------------------------------------------------- the radar */

function MarketRadar({ symbol }: { symbol: string }) {
  const pulse = usePulse(symbol);
  const tick = useTick(symbol);
  const forming = useFormingBar(symbol, "M1");
  const [nowSec, setNowSec] = useState(() => Math.floor(Date.now() / 1000));

  // 1s heartbeat — the "updated Ns ago" stamp visibly ticks every second
  useEffect(() => {
    const t = window.setInterval(() => setNowSec(Math.floor(Date.now() / 1000)), 1000);
    return () => window.clearInterval(t);
  }, []);

  const live = liveDominance(forming);
  const ageSec = pulse ? Math.max(0, nowSec - Math.floor(Date.parse(pulse.ts) / 1000)) : null;

  return (
    <Card>
      <SectionTitle
        title={`Strategy Radar — ${symbol}`}
        right={
          pulse ? (
            <span className="flex shrink-0 items-center gap-2">
              <Badge tone={ageSec != null && ageSec <= 90 ? "green" : "amber"} pulse>
                live {pulse.tf}
              </Badge>
              <span className="font-mono text-[10px] tabular-nums text-zinc-500">
                {ageSec != null ? `${ageSec}s` : ""}
              </span>
            </span>
          ) : (
            <Badge tone="zinc">waiting…</Badge>
          )
        }
      />

      {!pulse ? (
        <p className="text-xs text-zinc-500">
          Waiting for the engine&apos;s first {symbol} M1 close — the radar lights up
          within a minute of the feed connecting.
        </p>
      ) : (
        <div className="flex min-w-0 flex-col gap-3">
          {/* strategy input states */}
          <div className="grid grid-cols-3 gap-2 sm:grid-cols-5">
            {[
              { label: "Bias", node: <BiasBadge bias={pulse.bias} /> },
              {
                label: "RSI",
                node: (
                  <span className="font-mono text-[15px] font-semibold tabular-nums text-zinc-100">
                    {pulse.rsi != null ? pulse.rsi.toFixed(1) : "—"}
                  </span>
                ),
              },
              {
                label: "ATR",
                node: (
                  <span className="font-mono text-[15px] font-semibold tabular-nums text-zinc-100">
                    {pulse.atr != null ? pulse.atr.toFixed(2) : "—"}
                  </span>
                ),
              },
              { label: "Session", node: <span className="truncate text-xs font-semibold text-zinc-200">{pulse.session ?? "—"}</span> },
              {
                label: "Price",
                node: (
                  <span className="font-mono text-[15px] font-semibold tabular-nums text-gold">
                    {(tick?.bid ?? pulse.price)?.toFixed?.(2) ?? "—"}
                  </span>
                ),
              },
            ].map((cell) => (
              <div key={cell.label} className="min-w-0 rounded-xl border border-zinc-800/70 bg-zinc-900/40 px-2.5 py-2">
                <p className="truncate text-[9px] font-medium uppercase tracking-wider text-zinc-500">
                  {cell.label}
                </p>
                <p className="mt-0.5 truncate">{cell.node}</p>
              </div>
            ))}
          </div>

          {/* live candle buyer/seller dominance (updates every tick) */}
          <div className="min-w-0 rounded-xl border border-zinc-800/70 bg-zinc-900/40 px-3 py-2.5">
            <p className="mb-2 text-[10px] font-semibold uppercase tracking-wider text-zinc-500">
              M1 candle — buyers vs sellers
            </p>
            {live ? (
              <DominanceBar buy={live.buyPct} sell={live.sellPct} />
            ) : (
              <p className="text-[11px] text-zinc-500">forming bar warming up…</p>
            )}
            {pulse.candle && (
              <p className="mt-1.5 truncate text-[11px] leading-relaxed text-zinc-400">
                <span
                  className={
                    pulse.candle.dir === "bull" ? "text-emerald-400" : pulse.candle.dir === "bear" ? "text-red-400" : "text-zinc-400"
                  }
                >
                  ▲ last close:
                </span>{" "}
                {pulse.candle.reaction}
              </p>
            )}
          </div>

          {/* trigger candidates + whale pulse */}
          <div className="flex min-w-0 flex-wrap items-center gap-1.5">
            <span className="mr-1 text-[10px] font-semibold uppercase tracking-wider text-zinc-500">
              triggers
            </span>
            <Badge tone={pulse.triggers?.zone ? "green" : "zinc"}>zone</Badge>
            <Badge tone={pulse.triggers?.sfp ? "green" : "zinc"}>sweep</Badge>
            <Badge tone={pulse.triggers?.pullback ? "green" : "zinc"}>pullback</Badge>
            {pulse.whale && (
              <>
                <span className="ml-2 mr-1 text-[10px] font-semibold uppercase tracking-wider text-zinc-500">
                  big players
                </span>
                <Badge tone={pulse.whale.bias === "buy" ? "green" : pulse.whale.bias === "sell" ? "red" : "zinc"}>
                  {pulse.whale.bias ?? "quiet"}
                </Badge>
                <span className="font-mono text-[10px] tabular-nums text-zinc-500">
                  ↑{pulse.whale.buy_events ?? 0} ↓{pulse.whale.sell_events ?? 0}
                </span>
              </>
            )}
          </div>
          {pulse.whale?.last && (
            <p className="truncate text-[11px] text-zinc-500">{pulse.whale.last}</p>
          )}

          {/* POI zone watchlist */}
          {pulse.zones?.length > 0 && (
            <div className="min-w-0">
              <p className="mb-1.5 text-[10px] font-semibold uppercase tracking-wider text-zinc-500">
                POI watchlist (USD from price)
              </p>
              <ul className="flex min-w-0 flex-col gap-1.5">
                {pulse.zones.slice(0, 3).map((z, i) => (
                  <ZoneRow
                    key={i}
                    side={z.side}
                    lo={z.lo}
                    hi={z.hi}
                    quality={z.quality}
                    source={z.source}
                    dist={z.dist_usd}
                  />
                ))}
              </ul>
            </div>
          )}

          {/* fired / near-miss */}
          <FiredOrMiss pulse={pulse} />

          {/* per-strategy checks */}
          {pulse.checks?.length > 0 && (
            <div className="min-w-0">
              <p className="mb-1.5 text-[10px] font-semibold uppercase tracking-wider text-zinc-500">
                checks this bar
              </p>
              <ul className="grid min-w-0 grid-cols-1 gap-1 sm:grid-cols-2">
                {pulse.checks.slice(-8).map((c, i) => (
                  <li
                    key={`${c.name}-${i}`}
                    className="flex min-w-0 items-center gap-2 rounded-lg border border-zinc-800/60 bg-zinc-900/40 px-2.5 py-1"
                  >
                    <span
                      className={`shrink-0 font-mono text-[10px] font-bold ${
                        c.ok ? "text-emerald-400" : "text-red-400"
                      }`}
                    >
                      {c.ok ? "PASS" : "FAIL"}
                    </span>
                    <span className="shrink-0 text-[10px] font-semibold text-zinc-300">
                      {c.name}
                    </span>
                    <span className="min-w-0 flex-1 truncate text-[10px] text-zinc-500">
                      {String(c.value ?? "")}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </Card>
  );
}

function BiasBadge({ bias }: { bias: WsStrategyPulseMsg["bias"] }) {
  if (bias === "BULL") return <Badge tone="green">BULL</Badge>;
  if (bias === "BEAR") return <Badge tone="red">BEAR</Badge>;
  if (bias === "NEUTRAL") return <Badge tone="zinc">NEUTRAL</Badge>;
  return <span className="text-xs text-zinc-500">—</span>;
}

function FiredOrMiss({ pulse }: { pulse: WsStrategyPulseMsg }) {
  if (pulse.fired) {
    const f = pulse.fired;
    const dist = Math.abs(f.entry - f.market_ref);
    return (
      <div className="min-w-0 rounded-xl border border-gold/30 bg-gold/5 px-3 py-2.5">
        <div className="mb-1 flex min-w-0 items-center gap-2">
          <Badge tone="gold" pulse>SIGNAL FIRED</Badge>
          <Badge tone={f.direction === "BUY" ? "green" : "red"}>{f.direction}</Badge>
          {f.whale && <Badge tone="blue">whale ✓</Badge>}
          <span className="ml-auto shrink-0 font-mono text-[10px] tabular-nums text-zinc-500">
            {f.trigger}
          </span>
        </div>
        <p className="truncate font-mono text-[11px] tabular-nums text-zinc-300">
          entry {f.entry.toFixed(2)} · {dist.toFixed(2)}$ {f.entry < f.market_ref ? "below" : "above"} market
          {" · SL "}{f.sl.toFixed(2)} · TP {f.tp.toFixed(2)} · conf {(f.confidence * 100).toFixed(0)}%
        </p>
        <p className="mt-0.5 truncate text-[10px] text-zinc-500">{f.entry_type}</p>
      </div>
    );
  }
  if (pulse.near_miss) {
    return (
      <div className="min-w-0 rounded-xl border border-zinc-800/70 bg-zinc-900/40 px-3 py-2.5">
        <p className="mb-0.5 text-[10px] font-semibold uppercase tracking-wider text-zinc-500">
          why no signal this bar
        </p>
        <p className="truncate text-[11px] text-amber-300/90">{pulse.near_miss}</p>
      </div>
    );
  }
  return null;
}

/* ------------------------------------------------------------ exported */

/**
 * One radar card per market the engines run on (XAUUSD + BTCUSD — the
 * user directive that BTC must show its strategy state too). Every card
 * renders immediately: a quiet market shows its "waiting for the first
 * M1 close" state instead of hiding (BTC visibility was the point).
 */
export default function StrategyRadar({ symbols }: { symbols: string[] }) {
  return (
    <>
      {symbols.map((s) => (
        <MarketRadar key={s} symbol={s} />
      ))}
    </>
  );
}

/**
 * CandlePulseCard — the HOME-tab live candle panel: the forming M1
 * candle's buyer/seller dominance recomputed on EVERY tick (the panel
 * visibly moves second-by-second) + the just-closed candle's verdict
 * from the engine's strategy_pulse.
 */
export function CandlePulseCard({ symbol }: { symbol: string }) {
  const pulse = usePulse(symbol);
  const forming = useFormingBar(symbol, "M1");
  const live = liveDominance(forming);
  const closed = pulse?.candle ?? null;

  return (
    <Card>
      <SectionTitle
        title="M1 Buyers vs Sellers"
        right={
          <Badge tone={live ? (live.dir === "bull" ? "green" : "red") : "zinc"} pulse>
            {live ? (live.dir === "bull" ? "buyers" : "sellers") : "warming up"}
          </Badge>
        }
      />
      {live ? (
        <DominanceBar buy={live.buyPct} sell={live.sellPct} />
      ) : (
        <p className="text-[11px] text-zinc-500">waiting for the first M1 tick…</p>
      )}
      <p className="mt-1.5 min-w-0 truncate text-[11px] leading-relaxed text-zinc-400">
        {closed ? (
          <>
            <span className={closed.dir === "bull" ? "text-emerald-400" : closed.dir === "bear" ? "text-red-400" : "text-zinc-400"}>
              last close:
            </span>{" "}
            {closed.reaction}
          </>
        ) : (
          "the closed-candle verdict appears here after the first M1 close"
        )}
      </p>
      <p className="mt-1 truncate text-[10px] text-zinc-600">
        {pulse ? `engine radar updated ${pulse.tf} close — open the AI tab for every strategy` : "engine pulse starts with the first M1 close"}
      </p>
    </Card>
  );
}
