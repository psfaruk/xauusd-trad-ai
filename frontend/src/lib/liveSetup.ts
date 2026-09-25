/**
 * D-074 — "THE SIGNAL IS THE DRAWING".
 *
 * User directive (Bengali, verbatim): "আমি চাই চার্ট এর ওপরে ক্যান্ডেল এর
 * সাথে যেই এন্ট্রি সিগন্যাল টি আসে, আমি চাই সেই ভাবে টিক এই ভাবে মিলিয়ে
 * এন্ট্রি সেটাপ করবেন … এই অঙ্কন এর মেয়াদ থাকবে নতুন যদি অন্য একটি
 * সিগন্যাল আসে, তখন আগের টি মুছে যাবে, যদি sl TP হিট হয়, তখনও সিগন্যাল
 * টি মুছে যাবে।"
 *
 * The chart's entry-setup drawing is the NEWEST *live* signal — nothing
 * else. One drawing at a time:
 *   - a NEW different signal replaces (deletes) the previous drawing;
 *   - SL/TP hit (status won/lost) deletes it — immediately, no fade;
 *   - expired / cancelled limit orders delete it too;
 *   - the stale analysis "forming" box never shows while a live signal
 *     exists (that box was the "পুরাতন এন্ট্রি সেটাপ" the market never
 *     reached).
 *
 * `pending` = limit order waiting at its level (max 30 M1 bars by the
 * engine's pending_expiry, +grace) → the chart draws "WAIT <level>".
 * `active`  = order filled, trade live (the engine's whole short-time
 *             horizon) → "ENTRY <level>".
 */

import type { Signal } from "../types";

/** pending limit ink lives as long as the order itself (+ small grace) */
export const PENDING_TTL_MS = 40 * 60 * 1000;
/** active (filled) trade ink horizon */
export const ACTIVE_TTL_MS = 120 * 60 * 1000;

/** finite epoch-ms of a signal's `ts`, or 0 when unparseable */
export function signalTsMs(s: Signal): number {
  const t = new Date(s.ts).getTime();
  return Number.isFinite(t) ? t : 0;
}

/**
 * D-074 — broker spellings and platform names must match as ONE
 * market: "XAUUSDm" / "XAUUSD.x" / "XAUUSD.pro" all == "XAUUSD" (the
 * backend normalizes at the signal identity + repo read; this is the
 * frontend's defensive mirror of the same rule — a suffixed row from
 * any legacy path must never hide a live signal from the chart again).
 */
export function marketKey(symbol: string | null | undefined): string {
  let s = (symbol ?? "").toString().trim().toUpperCase();
  if (!s) return "";
  s = s.split(".")[0];
  for (const suffix of ["MICRO", "PRO", "M"]) {
    if (s.endsWith(suffix) && s.length - suffix.length >= 6) {
      s = s.slice(0, s.length - suffix.length);
      break;
    }
  }
  return s;
}

/** do these two symbol spellings name the same market? */
export function sameMarket(a: string | null | undefined, b: string | null | undefined): boolean {
  const ka = marketKey(a);
  const kb = marketKey(b);
  return ka !== "" && ka === kb;
}

/** Is this signal still a LIVE order (pending limit / active trade)? */
export function isLiveStatus(s: Signal): boolean {
  return s.status === "pending" || s.status === "active";
}

/**
 * The ONE signal the chart draws as the live trade setup: the NEWEST
 * live signal for `symbol` whose ink has not expired.
 * `signals` may be any order (API is newest-first; this is defensive).
 */
export function pickLiveSetup(
  signals: Signal[] | null | undefined,
  symbol: string,
  now: number = Date.now(),
): Signal | null {
  if (!signals || !signals.length) return null;
  let best: Signal | null = null;
  let bestTs = -Infinity;
  for (const s of signals) {
    if (!s || !sameMarket(s.symbol, symbol)) continue;
    if (!isLiveStatus(s)) continue;
    const t = signalTsMs(s);
    if (t <= 0) continue;
    const ttl = s.status === "pending" ? PENDING_TTL_MS : ACTIVE_TTL_MS;
    if (now - t > ttl) continue;
    if (t > bestTs) {
      best = s;
      bestTs = t;
    }
  }
  return best;
}
