'use client';

/**
 * CandleScope (D-082) — the RUNNING CANDLE MICROSCOPE.
 *
 * User directive (Bengali, verbatim): "তারা রানিং ক্যান্ডেল টি দেখে বুঝতে
 * পারে, যে এই রানিং ক্যান্ডেল টি কোন দিকে যাবে, তারা এটা ধরতে পারে, যে
 * একটি ক্যান্ডেল এ বায়ার আছে নাকি সেলার আছে… এমন টা কি আমার অ্যাপ এ
 * এপ্লাই করা যাবে, যে একটি ক্যান্ডেল কি ঘটছে, মিলি সেকেন্ড এ আপডেট
 * হবে" — the prop-trader tape read, applied to this app:
 *
 *  - TICK RULE on every live tick (up/down vs the previous mid) — the
 *    classic "who is inside this candle" read: buyer vs seller aggression
 *    counts, pushed distance ($), consecutive runs, cumulative delta
 *    footprint;
 *  - the running candle REBUILT from the same tick stream (O/H/L/C live);
 *  - a transparent direction LEAN (weighted momentum score — displayed
 *    with its reasoning, never a fake certainty);
 *  - millisecond cadence: every number re-renders on each incoming tick
 *    frame + a 100ms heartbeat (countdown, tick age, velocity decay);
 *  - the closed-candle verdict + multi-candle battle carried over from
 *    the old M1 pulse card, so nothing was lost in the upgrade.
 */

import { useEffect, useState } from "react";
import { Badge, Card, SectionTitle } from "./ui";
import { usePulse, useTape, type TapePoint } from "../state/feed";
import { marketMeta } from "../lib/markets";
import type { WsStrategyPulseMsg } from "../types";

/* ------------------------------------------------------------- sub-visuals */

/** Cumulative tick-delta footprint of the running candle (SVG sparkline). */
function DeltaSparkline({ cum }: { cum: number[] }) {
  const W = 300;
  const H = 36;
  if (cum.length < 2) {
    return (
      <div className="flex h-[36px] items-center justify-center rounded-lg border border-zinc-800/70 bg-zinc-900/40 text-[10px] text-zinc-600">
        delta footprint builds with the first ticks…
      </div>
    );
  }
  const maxV = Math.max(...cum, 0);
  const minV = Math.min(...cum, 0);
  const range = maxV - minV || 1;
  const y = (v: number) => ((maxV - v) / range) * (H - 6) + 3;
  const pts = cum
    .map((v, i) => `${((i / (cum.length - 1)) * (W - 4) + 2).toFixed(1)},${y(v).toFixed(1)}`)
    .join(" ");
  const lastV = cum[cum.length - 1];
  const stroke = lastV > 0 ? "#34d399" : lastV < 0 ? "#f87171" : "#a1a1aa";
  const zeroY = y(0);
  return (
    <svg
      viewBox={`0 0 ${W} ${H}`}
      className="h-[36px] w-full"
      preserveAspectRatio="none"
      role="img"
      aria-label="Cumulative tick delta of the running candle"
    >
      <line x1="0" x2={W} y1={zeroY} y2={zeroY} stroke="#52525b" strokeWidth="1" strokeDasharray="3 3" />
      <polyline points={pts} fill="none" stroke={stroke} strokeWidth="1.8" strokeLinejoin="round" strokeLinecap="round" />
      <circle cx={W - 2} cy={y(lastV)} r="2.6" fill={stroke} />
    </svg>
  );
}

/** Buyer vs seller aggression bar (tick-rule counts + pushed distance). */
function AggroBar({
  up, dn, upDist, dnDist, digits,
}: { up: number; dn: number; upDist: number; dnDist: number; digits: number }) {
  const total = up + dn;
  const buyPct = total > 0 ? Math.round((up / total) * 100) : 50;
  return (
    <div className="flex min-w-0 flex-col gap-1.5">
      <div className="flex h-3 min-w-0 overflow-hidden rounded-full border border-zinc-800 bg-zinc-900">
        <div
          className="bg-emerald-500/75 transition-[width] duration-150"
          style={{ width: `${buyPct}%` }}
        />
        <div
          className="bg-red-500/75 transition-[width] duration-150"
          style={{ width: `${100 - buyPct}%` }}
        />
      </div>
      <div className="flex min-w-0 items-center justify-between gap-2 text-[10px] font-semibold tabular-nums">
        <span className="min-w-0 truncate text-emerald-400">
          buyers {up} ↑ · +{upDist.toFixed(digits)}$
        </span>
        <span className="min-w-0 truncate text-red-400 text-right">
          {dnDist.toFixed(digits)}$ ↓ · {dn} sellers
        </span>
      </div>
    </div>
  );
}

/** The running candle's anatomy: live mini-candle + micro numbers. */
function CandleAnatomy({
  o, h, l, c, digits,
}: { o: number; h: number; l: number; c: number; digits: number }) {
  const HGT = 78;
  const rng = h - l;
  const top = (v: number) => (rng > 0 ? ((h - v) / rng) * (HGT - 10) + 5 : HGT / 2);
  const bull = c >= o;
  const bodyTop = top(Math.max(o, c));
  const bodyH = Math.max(3, Math.abs(top(o) - top(c)));
  const upperWick = h - Math.max(o, c);
  const lowerWick = Math.min(o, c) - l;
  return (
    <div className="flex min-w-0 items-stretch gap-3">
      {/* live mini-candle */}
      <div className="relative w-6 shrink-0" style={{ height: HGT }} aria-hidden>
        <div
          className="absolute left-1/2 w-px -translate-x-1/2 bg-zinc-600"
          style={{ top: top(h), height: Math.max(1, top(l) - top(h)) }}
        />
        <div
          className={`absolute left-1/2 w-3.5 -translate-x-1/2 rounded-sm ${
            bull ? "bg-emerald-500/80" : "bg-red-500/80"
          }`}
          style={{ top: bodyTop, height: bodyH }}
        />
        {/* current price marker — rides every tick */}
        <div
          className="absolute left-0 h-0.5 w-6 rounded-full bg-gold shadow-[0_0_6px_#d4af37]"
          style={{ top: top(c) }}
        />
      </div>
      {/* micro numbers */}
      <dl className="grid min-w-0 flex-1 grid-cols-2 content-center gap-x-3 gap-y-0.5 font-mono text-[10px] tabular-nums leading-tight">
        <div className="flex justify-between gap-1">
          <dt className="text-zinc-600">O</dt>
          <dd className="text-zinc-300">{o.toFixed(digits)}</dd>
        </div>
        <div className="flex justify-between gap-1">
          <dt className="text-zinc-600">C</dt>
          <dd className={bull ? "text-emerald-400" : "text-red-400"}>{c.toFixed(digits)}</dd>
        </div>
        <div className="flex justify-between gap-1">
          <dt className="text-zinc-600">H</dt>
          <dd className="text-zinc-300">{h.toFixed(digits)}</dd>
        </div>
        <div className="flex justify-between gap-1">
          <dt className="text-zinc-600">L</dt>
          <dd className="text-zinc-300">{l.toFixed(digits)}</dd>
        </div>
        <div className="flex justify-between gap-1">
          <dt className="text-zinc-600">Δ</dt>
          <dd className={c >= o ? "text-emerald-400" : "text-red-400"}>
            {c >= o ? "+" : ""}{(c - o).toFixed(digits)}
          </dd>
        </div>
        <div className="flex justify-between gap-1">
          <dt className="text-zinc-600">rng</dt>
          <dd className="text-zinc-300">{rng.toFixed(digits)}</dd>
        </div>
        <div className="col-span-2 flex justify-between gap-1">
          <dt className="text-zinc-600">wicks</dt>
          <dd className="text-zinc-400">
            ▲{((upperWick / (rng || 1)) * 100).toFixed(0)}% / ▼{((lowerWick / (rng || 1)) * 100).toFixed(0)}%
          </dd>
        </div>
      </dl>
    </div>
  );
}

/** The flowing tick tape — price chips colored by tick-rule direction. */
function TapeRow({ points, digits }: { points: TapePoint[]; digits: number }) {
  const chips = points.slice(-16);
  if (!chips.length) return null;
  return (
    <div className="flex min-w-0 flex-wrap gap-1">
      {chips.map((p, i) => {
        const newest = i === chips.length - 1;
        const cls =
          p.dir === 1
            ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-300"
            : p.dir === -1
              ? "border-red-500/40 bg-red-500/10 text-red-300"
              : "border-zinc-700 bg-zinc-800/40 text-zinc-400";
        return (
          <span
            key={`${p.at}-${i}`}
            title={`${p.dir === 1 ? "up-tick" : p.dir === -1 ? "down-tick" : "unchanged"} · jump ${p.jump.toFixed(digits)}$ · ${new Date(p.at).toLocaleTimeString()}.${String(p.at % 1000).padStart(3, "0")}`}
            className={`rounded border px-1.5 py-0.5 font-mono text-[10px] font-semibold tabular-nums transition-colors ${cls} ${
              newest ? "ring-1 ring-gold/60" : ""
            }`}
          >
            {p.mid.toFixed(digits)}
            {p.dir === 1 ? "↑" : p.dir === -1 ? "↓" : "·"}
          </span>
        );
      })}
    </div>
  );
}

/* ------------------------------------------------------------ closed candle */

function ClosedVerdict({ pulse }: { pulse: WsStrategyPulseMsg | null }) {
  const closed = pulse?.candle ?? null;
  const battle = pulse?.battle ?? null;
  if (!closed && !battle) return null;
  return (
    <div className="mt-2 min-w-0 border-t border-zinc-800/70 pt-2">
      {closed && (
        <p className="min-w-0 truncate text-[11px] leading-relaxed text-zinc-400">
          <span
            className={
              closed.dir === "bull"
                ? "text-emerald-400"
                : closed.dir === "bear"
                  ? "text-red-400"
                  : "text-zinc-400"
            }
          >
            last close:
          </span>{" "}
          {closed.reaction}
          {closed.buy_pct != null ? ` — buyers ${closed.buy_pct}%` : ""}
        </p>
      )}
      {battle && battle.n ? (
        <p className="mt-1 min-w-0 truncate text-[11px] leading-relaxed text-zinc-400">
          <span className="text-zinc-500">battle last {battle.n}:</span>{" "}
          {battle.verdict}
        </p>
      ) : null}
    </div>
  );
}

/* ------------------------------------------------------------ the microscope */

export function CandleScopeCard({ symbol }: { symbol: string }) {
  const tape = useTape(symbol);
  const pulse = usePulse(symbol);
  const digits = marketMeta(symbol).digits;

  /* 100ms heartbeat — countdown, tick age, velocity stay alive between
   * ticks (the "মিলি সেকেন্ড এ আপডেট" feel). */
  const [nowMs, setNowMs] = useState(() => Date.now());
  useEffect(() => {
    const t = window.setInterval(() => setNowMs(Date.now()), 100);
    return () => window.clearInterval(t);
  }, []);

  if (!tape) {
    return (
      <Card>
        <SectionTitle
          title="Running Candle · M1"
          right={<Badge tone="zinc">waiting…</Badge>}
        />
        <p className="text-[11px] text-zinc-500">
          The microscope lights up with the first live tick of{" "}
          <span className="font-semibold text-zinc-300">{symbol}</span> — every
          tick inside the running candle is then classified buyer/seller in
          real time.
        </p>
      </Card>
    );
  }

  const points = tape.points;
  const last = points.length ? points[points.length - 1] : null;
  const up = tape.up;
  const dn = tape.dn;
  const total = up + dn;

  /* tick age + cadence */
  const ageMs = last ? nowMs - last.at : null;
  const ageText =
    ageMs == null
      ? "—"
      : ageMs < 1000
        ? `${ageMs}ms`
        : `${(ageMs / 1000).toFixed(1)}s`;
  const gapBase = points.length >= 3 ? points.slice(-12) : points;
  const avgGap =
    gapBase.length >= 2
      ? (gapBase[gapBase.length - 1].at - gapBase[0].at) / (gapBase.length - 1)
      : null;

  /* candle countdown */
  const elapsedSec = (nowMs - tape.bucketStart) / 1000;
  const remainSec = Math.max(0, 60 - elapsedSec);
  const progress = Math.min(100, (elapsedSec / 60) * 100);
  const remainText = `${Math.floor(remainSec / 60)}:${String(Math.floor(remainSec % 60)).padStart(2, "0")}.${Math.floor((remainSec % 1) * 10)}`;

  /* velocity over ~5s (price units / second) */
  let vel: number | null = null;
  if (points.length >= 3 && last) {
    const cutoff = last.at - 5000;
    const ref = points.find((p) => p.at >= cutoff) ?? points[0];
    const dt = (last.at - ref.at) / 1000;
    if (dt >= 0.5) vel = (last.mid - ref.mid) / dt;
  }

  /* the running candle (tick-derived, always fresh) */
  const { o, h, l, c } = tape;
  const rng = h - l;
  const upperWick = h - Math.max(o, c);
  const lowerWick = Math.min(o, c) - l;

  /* ------- the transparent direction lean (weighted momentum score) ----- */
  const tickDelta = total > 0 ? (up - dn) / total : 0;
  const closePos = rng > 0 ? (2 * (c - l)) / rng - 1 : 0;
  const velNorm =
    vel != null && rng > 0
      ? Math.max(-1, Math.min(1, (vel * 8) / rng))
      : 0;
  const wickSig = rng > 0 ? (lowerWick - upperWick) / rng : 0;
  const score = 0.42 * tickDelta + 0.22 * closePos + 0.2 * velNorm + 0.16 * wickSig;
  const lean: "buyers" | "sellers" | "tug" =
    score > 0.12 ? "buyers" : score < -0.12 ? "sellers" : "tug";
  const conf = Math.min(96, Math.round(50 + Math.abs(score) * 46));

  const streakAbs = Math.abs(tape.streak);
  const streakText =
    streakAbs >= 3
      ? `${streakAbs} straight ${tape.streak > 0 ? "BUY" : "SELL"} ticks`
      : null;
  const wickWar =
    rng > 0 && lowerWick > 0.45 * rng
      ? "buyers absorbing the dip (lower wick)"
      : rng > 0 && upperWick > 0.45 * rng
        ? "sellers rejecting the top (upper wick)"
        : null;

  const leanBox =
    lean === "buyers"
      ? "border-emerald-500/40 bg-emerald-500/10"
      : lean === "sellers"
        ? "border-red-500/40 bg-red-500/10"
        : "border-zinc-700 bg-zinc-900/50";
  const leanIcon = lean === "buyers" ? "▲" : lean === "sellers" ? "▼" : "⚔";
  const leanTitle =
    lean === "buyers"
      ? "BUYERS PUSHING UP"
      : lean === "sellers"
        ? "SELLERS PRESSING DOWN"
        : "TUG OF WAR — NO EDGE";
  const leanReason = `${total > 0 ? Math.round((up / total) * 100) : 50}% buy ticks · ${
    vel != null ? `${vel >= 0 ? "+" : ""}${vel.toFixed(2)}$/s` : "vel —"
  } · price ${(closePos >= 0 ? "top " : "bottom ")}${Math.round((Math.abs(closePos) * 50) + 50)}% of range`;

  const stale = ageMs != null && ageMs > 15_000;

  return (
    <Card>
      <SectionTitle
        title="Running Candle · M1"
        right={
          <span className="flex shrink-0 items-center gap-2">
            <span
              className={`font-mono text-[10px] font-semibold tabular-nums ${
                stale ? "text-amber-400" : ageMs != null && ageMs < 1200 ? "text-emerald-400" : "text-zinc-500"
              }`}
              title="time since the last live tick arrived"
            >
              {stale ? "feed quiet" : `${ageText} ago`}
            </span>
            <Badge
              tone={lean === "buyers" ? "green" : lean === "sellers" ? "red" : "zinc"}
              pulse={!stale}
            >
              {lean === "buyers" ? "buyers" : lean === "sellers" ? "sellers" : "tug"}
            </Badge>
          </span>
        }
      />

      {/* candle countdown — rides the 100ms heartbeat */}
      <div className="mb-2.5 flex min-w-0 items-center gap-2">
        <div className="h-1.5 min-w-0 flex-1 overflow-hidden rounded-full bg-zinc-800">
          <div
            className="h-full rounded-full bg-gold/70"
            style={{ width: `${progress}%` }}
          />
        </div>
        <span className="shrink-0 font-mono text-[10px] font-semibold tabular-nums text-zinc-400">
          {remainText}
        </span>
      </div>

      {/* the direction lean — the tape-read verdict */}
      <div className={`mb-2.5 flex min-w-0 items-center justify-between gap-2 rounded-xl border px-3 py-2 ${leanBox}`}>
        <div className="flex min-w-0 items-center gap-2.5">
          <span
            aria-hidden
            className={`text-lg leading-none ${
              lean === "buyers" ? "text-emerald-400" : lean === "sellers" ? "text-red-400" : "text-zinc-400"
            }`}
          >
            {leanIcon}
          </span>
          <div className="min-w-0">
            <p
              className={`text-xs font-bold ${
                lean === "buyers" ? "text-emerald-300" : lean === "sellers" ? "text-red-300" : "text-zinc-300"
              }`}
            >
              {leanTitle}
            </p>
            <p className="text-[10px] leading-snug text-zinc-500">{leanReason}</p>
          </div>
        </div>
        <div className="shrink-0 text-right">
          <p className="font-mono text-sm font-bold tabular-nums text-zinc-100">{conf}%</p>
          <p className="text-[9px] uppercase tracking-wide text-zinc-600">lean conf</p>
        </div>
      </div>

      {/* buyer vs seller aggression — the tick-rule read */}
      <AggroBar up={up} dn={dn} upDist={tape.upDist} dnDist={tape.dnDist} digits={digits} />

      {/* cumulative delta footprint */}
      <div className="mt-2.5 min-w-0">
        <p className="mb-1 flex items-center justify-between text-[9px] font-semibold uppercase tracking-wider text-zinc-500">
          <span>cumulative tick delta</span>
          <span
            className={`font-mono tabular-nums ${
              (tape.cum[tape.cum.length - 1] ?? 0) > 0
                ? "text-emerald-400"
                : (tape.cum[tape.cum.length - 1] ?? 0) < 0
                  ? "text-red-400"
                  : "text-zinc-500"
            }`}
          >
            {tape.cum.length ? (tape.cum[tape.cum.length - 1] > 0 ? "+" : "") + tape.cum[tape.cum.length - 1] : "—"}
          </span>
        </p>
        <DeltaSparkline cum={tape.cum} />
      </div>

      {/* the running candle's anatomy */}
      <div className="mt-2.5 min-w-0 rounded-xl border border-zinc-800/70 bg-zinc-900/40 px-2.5 py-2">
        <CandleAnatomy o={o} h={h} l={l} c={c} digits={digits} />
        <div className="mt-1.5 flex min-w-0 flex-wrap items-center gap-x-3 gap-y-0.5 text-[10px] tabular-nums text-zinc-500">
          <span>{(h - c).toFixed(digits)}$ below high</span>
          <span>{(c - l).toFixed(digits)}$ above low</span>
          <span>{tape.candleTicks} ticks in</span>
          {wickWar && <span className="text-amber-300/80">{wickWar}</span>}
          {streakText && (
            <span
              className={
                tape.streak > 0 ? "text-emerald-400/90" : "text-red-400/90"
              }
            >
              {streakText}
            </span>
          )}
        </div>
      </div>

      {/* the flowing tape — every chip is one live tick */}
      <div className="mt-2.5 min-w-0">
        <p className="mb-1 flex items-center justify-between text-[9px] font-semibold uppercase tracking-wider text-zinc-500">
          <span>live tape — {points.slice(-16).length} ticks</span>
          <span className="font-mono tabular-nums">
            {tape.tps != null ? `${tape.tps.toFixed(1)} tps` : ""}
            {avgGap != null ? ` · ø${avgGap < 1000 ? `${Math.round(avgGap)}ms` : `${(avgGap / 1000).toFixed(1)}s`}` : ""}
          </span>
        </p>
        <TapeRow points={points} digits={digits} />
      </div>

      {/* engine's closed-candle verdict (carried from the old pulse card) */}
      <ClosedVerdict pulse={pulse} />

      <p className="mt-2 text-[10px] leading-relaxed text-zinc-600">
        Tape-read lean = live buyer/seller momentum inside this candle — a
        probability hint, never a promise. {pulse ? "Closed-candle verdict updates on every M1 close." : "Engine verdict starts after the first M1 close."}
      </p>
    </Card>
  );
}

export default CandleScopeCard;
