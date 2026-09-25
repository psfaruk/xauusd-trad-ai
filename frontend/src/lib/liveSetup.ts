/**
 * D-074/D-075 — "THE SIGNAL IS THE DRAWING", event-only lifecycle.
 *
 * User directive (Bengali, verbatim, restated in D-075): "আর আপনি বলেছেন
 * এটি মুছে যাবে 6 মিনিটে আমি এটা বলি নি, আমি বলেছি, নতুন কোনো এন্ট্রি
 * সিগন্যাল আসলে তখন মুছে যাবে। আর যদি সে সেটাপ এর sl TP হিট হয়, তখন
 * মুছে যাবে।"
 *
 * NO TIME-BASED DELETION. The chart's entry-setup drawing is the NEWEST
 * *live* signal — nothing else — and it lives exactly as long as the
 * ORDER lives:
 *   - a NEW different signal replaces (deletes) the previous drawing;
 *   - SL/TP hit (status won/lost) deletes it — immediately, no fade;
 *   - expired / cancelled limit orders delete it too (order death,
 *     reported by the engine's tracker, not by a frontend timer);
 *   - there is NO TTL: a pending limit drawn for 3 hours stays drawn
 *     until the engine fills it, expires it, or a newer signal replaces
 *     it. Age alone NEVER removes ink.
 *
 * `pending` = limit order waiting at its level → the chart draws
 * "WAIT <level>". `active` = order filled, trade live → "ENTRY <level>".
 * The engine's tracker flips statuses and the WS `signal_update` event
 * refetches the list — the drawing follows the ORDER, not the clock.
 */

import type { Signal } from "../types";

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
 * live signal for `symbol`. Pure status logic — no clock, no TTL (the
 * D-075 directive). `signals` may be any order (API is newest-first;
 * this is defensive).
 */
export function pickLiveSetup(
  signals: Signal[] | null | undefined,
  symbol: string,
): Signal | null {
  if (!signals || !signals.length) return null;
  let best: Signal | null = null;
  let bestTs = -Infinity;
  for (const s of signals) {
    if (!s || !sameMarket(s.symbol, symbol)) continue;
    if (!isLiveStatus(s)) continue;
    const t = signalTsMs(s);
    if (t <= 0) continue;
    if (t > bestTs) {
      best = s;
      bestTs = t;
    }
  }
  return best;
}
