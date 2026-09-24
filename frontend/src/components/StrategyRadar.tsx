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
  /** D-067 — the running candle's full read: the wick war + who
   * controls the bar (same math as the backend's
   * running_candle_read, recomputed on every tick). */
  wick: "upper" | "lower" | "none";
  wickWar: string | null;
  control: "buyers" | "sellers" | "none";
}

/** Same proxy the backend's candle_pulse / running_candle_read use,
 * run on the forming bar — updated on every bar_update frame. */
function liveDominance(bar: Candle | null): LiveDominance | null {
  if (!bar) return null;
  const rng = bar.h - bar.l;
  if (rng <= 0) return null;
  const delta = ((bar.c - bar.l) / rng) * 2 - 1;
  const upperWick = bar.h - Math.max(bar.o, bar.c);
  const lowerWick = Math.min(bar.o, bar.c) - bar.l;
  let wick: "upper" | "lower" | "none" = "none";
  let wickWar: string | null = null;
  if (upperWick > 0.45 * rng) {
    wick = "upper";
    wickWar = "sellers rejecting the top";
  } else if (lowerWick > 0.45 * rng) {
    wick = "lower";
    wickWar = "buyers absorbing the dip";
  }
  const mid = (bar.h + bar.l) / 2;
  return {
    delta,
    buyPct: Math.round(((delta + 1) / 2) * 100),
    sellPct: Math.round((1 - (delta + 1) / 2) * 100),
    dir: bar.c >= bar.o ? "bull" : "bear",
    wick,
    wickWar,
    control: bar.c > mid ? "buyers" : bar.c < mid ? "sellers" : "none",
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
              <>
                <DominanceBar buy={live.buyPct} sell={live.sellPct} />
                <p className="mt-1.5 truncate text-[10px] leading-relaxed text-zinc-500">
                  <span
                    className={
                      live.control === "buyers"
                        ? "text-emerald-400"
                        : live.control === "sellers"
                          ? "text-red-400"
                          : "text-zinc-500"
                    }
                  >
                    running candle:
                  </span>{" "}
                  {live.control === "none"
                    ? "even fight inside the bar"
                    : `${live.control} control the bar`}
                  {live.wickWar ? ` — ${live.wickWar}` : ""}
                </p>
              </>
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

          {/* D-067 — the CANDLE BATTLE over the last closed candles: who
           * dominates whom, who won, what happened INSIDE them (user
           * directive: "কারা কাদের কে ডোমেনেট করছে, কারা জিতেছে, লাস্ট
           * কয়েক টি ক্যান্ডেল এর ভিতর কি ঘটেছে") */}
          {pulse.battle && pulse.battle.n ? (
            <div className="min-w-0 rounded-xl border border-zinc-800/70 bg-zinc-900/40 px-3 py-2.5">
              <div className="mb-2 flex min-w-0 items-center justify-between gap-2">
                <p className="text-[10px] font-semibold uppercase tracking-wider text-zinc-500">
                  Candle battle — last {pulse.battle.n}
                </p>
                {pulse.battle.state && pulse.battle.state !== "tug" && (
                  <Badge tone={pulse.battle.state === "buyers" ? "green" : "red"} pulse>
                    {pulse.battle.state === "buyers" ? "buyers" : "sellers"}
                  </Badge>
                )}
              </div>
              <DominanceBar
                buy={Math.round(pulse.battle.buy_pct ?? 50)}
                sell={Math.round(pulse.battle.sell_pct ?? 50)}
              />
              {pulse.battle.wins && (
                <p className="mt-1.5 truncate text-[10px] tabular-nums text-zinc-500">
                  won {pulse.battle.wins.buyers}/{pulse.battle.n} by buyers ·{" "}
                  {pulse.battle.wins.sellers}/{pulse.battle.n} by sellers
                  {pulse.battle.net_atr != null
                    ? ` · net ${pulse.battle.net_atr > 0 ? "+" : ""}${pulse.battle.net_atr} ATR`
                    : ""}
                  {pulse.battle.streak && pulse.battle.streak.len >= 2
                    ? ` · ${pulse.battle.streak.len} streak`
                    : ""}
                </p>
              )}
              <p className="mt-1 truncate text-[11px] leading-relaxed text-zinc-400">
                {pulse.battle.verdict}
              </p>
              {pulse.battle.events?.length ? (
                <p className="mt-1 truncate text-[10px] leading-relaxed text-zinc-500">
                  inside: {pulse.battle.events[pulse.battle.events.length - 1].note}
                </p>
              ) : null}
            </div>
          ) : null}

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

          {/* D-061 — the AMD cycle read + per-trade trap verdict: the radar
           * tells the user WHICH phase the market sits in every minute
           * (accumulation / manipulation / distribution) and whether the
           * institutional trap structure is stacked against new trades */}
          {(pulse.amd || pulse.trap) && (
            <div className="flex min-w-0 flex-wrap items-center gap-1.5">
              <span className="mr-1 text-[10px] font-semibold uppercase tracking-wider text-zinc-500">
                AMD cycle
              </span>
              {pulse.amd?.phase && (
                <Badge
                  tone={
                    pulse.amd.phase === "manipulation" ? "amber"
                    : pulse.amd.phase === "distribution" ? "green"
                    : "gold"
                  }
                >
                  {pulse.amd.phase.toUpperCase()}
                </Badge>
              )}
              {pulse.amd?.sweep && (
                <span className="font-mono text-[10px] tabular-nums text-zinc-500">
                  {pulse.amd.sweep.side} swept {pulse.amd.sweep.bars_ago}b ago · reclaimed
                </span>
              )}
              {pulse.trap && pulse.trap.risk > 0 && (
                <Badge tone={pulse.trap.risk >= 0.7 ? "red" : pulse.trap.risk >= 0.4 ? "amber" : "zinc"}>
                  trap {Math.round(pulse.trap.risk * 100)}%
                </Badge>
              )}
            </div>
          )}
          {pulse.trap && pulse.trap.risk >= 0.4 && pulse.trap.reasons?.[0] && (
            <p className="truncate text-[10px] leading-relaxed text-amber-300/80">
              {pulse.trap.reasons[0]}
            </p>
          )}
          {pulse.amd?.note && !pulse.trap?.reasons?.length && (
            <p className="truncate text-[11px] text-zinc-500">{pulse.amd.note}</p>
          )}

          {/* D-064 — the market-STRUCTURE ladder: the "কত বার LL/LH" count
           * the user asked about, live every minute — how many consecutive
           * same-direction breaks the run has, whether the market is
           * resting / extended / just shifted, the honest reversal odds,
           * and WHERE the pullback magnets sit */}
          {pulse.structure && (
            <div className="flex min-w-0 flex-wrap items-center gap-1.5">
              <span className="mr-1 text-[10px] font-semibold uppercase tracking-wider text-zinc-500">
                structure
              </span>
              {pulse.structure.run != null && pulse.structure.run_dir && (
                <Badge
                  tone={
                    pulse.structure.run_dir === "down" ? "red" : "green"
                  }
                >
                  LEG {pulse.structure.run} {pulse.structure.run_dir === "down" ? "↓" : "↑"}
                </Badge>
              )}
              {pulse.structure.phase && (
                <Badge
                  tone={
                    pulse.structure.phase === "reversal-confirmed"
                    ? pulse.structure.run_dir === "down" ? "red" : "green"
                    : pulse.structure.phase === "extended" ? "amber"
                    : pulse.structure.phase === "resting" ? "gold"
                    : "zinc"
                  }
                >
                  {pulse.structure.phase === "reversal-confirmed"
                    ? "SHIFTED"
                    : pulse.structure.phase.toUpperCase()}
                </Badge>
              )}
              {pulse.structure.p_reversal != null && (
                <span className="font-mono text-[10px] tabular-nums text-zinc-500">
                  p(rev) {Math.round(pulse.structure.p_reversal * 100)}%
                </span>
              )}
              {pulse.structure.choch && (
                <span className="font-mono text-[10px] tabular-nums text-zinc-500">
                  CHoCH {pulse.structure.choch.dir} {pulse.structure.choch.bars_ago}b ago
                </span>
              )}
            </div>
          )}
          {pulse.structure?.magnets?.length ? (
            <p className="truncate text-[10px] leading-relaxed text-amber-200/70">
              rest magnets:{" "}
              {pulse.structure.magnets
                .map((m) => `${m.kind} ${m.price?.toFixed(2)}`)
                .join(" · ")}
            </p>
          ) : null}
          {pulse.structure?.action && (
            <p className="truncate text-[11px] text-zinc-500">
              {pulse.structure.action}
            </p>
          )}

          {/* D-065 — the TIMEFRAME LADDER (user directive: "কত মিনিটের
           * টাইম ফ্রেম কত টি টাইম এনালাইসিস করে, কোন টাইম ফ্রেম এ
           * সিগন্যাল প্রধান করেন"): which TF plays which role + how much
           * history it reads — the short-time ladder, live in the app */}
          {pulse.tf_ladder?.length ? (
            <div className="min-w-0">
              <p className="mb-1.5 text-[10px] font-semibold uppercase tracking-wider text-zinc-500">
                TF ladder · who does what
              </p>
              <ul className="flex min-w-0 flex-col gap-1">
                {pulse.tf_ladder.map((r, i) => (
                  <li
                    key={i}
                    className="flex min-w-0 items-center justify-between gap-2 rounded-lg border border-zinc-800 bg-zinc-900/40 px-2.5 py-1.5"
                  >
                    <span className="flex min-w-0 items-center gap-1.5">
                      <span className="font-mono text-[10px] font-semibold text-zinc-200">
                        {r.tf}
                      </span>
                      <span className="text-[9px] uppercase tracking-wider text-zinc-500">
                        {r.role}
                      </span>
                    </span>
                    <span className="truncate text-[9px] text-zinc-500">
                      {r.bars != null ? `${r.bars} bars · ` : ""}
                      {r.note}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          ) : null}

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
          {/* D-067 — the fired trade's candle-battle verdict: did it fire
           * WITH the dominating flow or INTO it? */}
          {f.flow?.state && f.flow.state !== "tug" && (
            <Badge
              tone={
                f.flow.state === (f.direction === "BUY" ? "buyers" : "sellers")
                  ? "green"
                  : "amber"
              }
            >
              {f.flow.state} {Math.round(f.flow.buy_pct ?? f.flow.sell_pct ?? 0)}%
            </Badge>
          )}
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
  const battle = pulse?.battle ?? null;

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
        <>
          <DominanceBar buy={live.buyPct} sell={live.sellPct} />
          <p className="mt-1.5 min-w-0 truncate text-[10px] leading-relaxed text-zinc-500">
            <span
              className={
                live.control === "buyers"
                  ? "text-emerald-400"
                  : live.control === "sellers"
                    ? "text-red-400"
                    : "text-zinc-500"
              }
            >
              running candle:
            </span>{" "}
            {live.control === "none"
              ? "even fight inside the bar"
              : `${live.control} control the bar`}
            {live.wickWar ? ` — ${live.wickWar}` : ""}
          </p>
        </>
      ) : (
        <p className="text-[11px] text-zinc-500">waiting for the first M1 tick…</p>
      )}
      {/* D-067 — the multi-candle war under the running bar */}
      {battle && battle.n ? (
        <div className="mt-2 min-w-0 border-t border-zinc-800/70 pt-2">
          <div className="mb-1 flex min-w-0 items-center justify-between gap-2">
            <p className="text-[10px] font-semibold uppercase tracking-wider text-zinc-500">
              battle — last {battle.n} candles
            </p>
            {battle.state && battle.state !== "tug" && (
              <Badge tone={battle.state === "buyers" ? "green" : "red"}>
                {battle.state}
              </Badge>
            )}
          </div>
          <p className="min-w-0 truncate text-[11px] leading-relaxed text-zinc-400">
            {battle.verdict}
          </p>
        </div>
      ) : null}
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
