/**
 * chartZoom (D-073) — zoom-preservation math for the candle backfill.
 *
 * THE BUG (user report: "যখন কোন চার্ট জুম করে রাখি, হঠাৎ করে চার্ট
 * ভেঙে যায়, আগের অবস্থায় ফিরে আসে"): every candle refetch — the
 * 3-minute react-query poll, the 10-second warm-up poll, every WebSocket
 * (re)connect invalidation, every desync resync — ran a full
 * `setData()` + a fixed `setVisibleLogicalRange(last 95 bars)`. The
 * user's zoom was stomped back to the default window: the chart
 * "broke and returned to its previous state".
 *
 * THE FIX: when the data array is REPLACED for the same symbol+TF, the
 * visible logical range is re-anchored instead of reset:
 *   * FOLLOWING (the view pinned at the live right edge — the default
 *     after the first load): the right edge stays glued to the newest
 *     bar, keeping the user's exact zoom span and right whitespace.
 *   * HISTORY-ANCHORED (the user zoomed/panned into the past): the
 *     left edge re-anchors to the SAME wall-clock bar time in the new
 *     array and the zoom span is preserved — the candles under the
 *     cursor are the same candles, whatever the data window did.
 *
 * Pure math only (no chart imports) so it is unit-testable headless.
 */

export interface LogicalRange {
  from: number;
  to: number;
}

export interface RangeBar {
  t: number;
}

/** how many bars short of the last bar still counts as "at the edge" */
const FOLLOW_SLACK_BARS = 2;

/** nearest index in `bars` for epoch-seconds time `t` (binary search). */
export function nearestIndex(bars: RangeBar[], t: number): number {
  const n = bars.length;
  if (n === 0) return 0;
  if (!Number.isFinite(t)) return n - 1;
  if (t <= bars[0].t) return 0;
  if (t >= bars[n - 1].t) return n - 1;
  let lo = 0;
  let hi = n - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (bars[mid].t < t) lo = mid + 1;
    else hi = mid;
  }
  // lo = first bar with t >= target — pick the closer of lo-1 / lo
  if (lo > 0 && Math.abs(bars[lo - 1].t - t) <= Math.abs(bars[lo].t - t)) {
    return lo - 1;
  }
  return lo;
}

function clampIdx(n: number, i: number): number {
  return Math.max(0, Math.min(n - 1, Math.round(i)));
}

/** finite, sane logical range or null. */
export function sanitizeRange(
  r: LogicalRange | null | undefined,
): LogicalRange | null {
  if (!r) return null;
  const { from, to } = r;
  if (!Number.isFinite(from) || !Number.isFinite(to)) return null;
  const span = to - from;
  if (!(span > 0) || !Number.isFinite(span)) return null;
  return { from, to };
}

/**
 * The visible range to restore after `setData(next)` when the user was
 * looking at `applied` (the previously applied array) in `current`.
 * Returns null when there is nothing sane to preserve (first load,
 * garbage range) — the caller then applies its default window.
 */
export function preservedRange(
  applied: RangeBar[],
  next: RangeBar[],
  current: LogicalRange | null | undefined,
): LogicalRange | null {
  const cur = sanitizeRange(current);
  if (!cur) return null;
  if (applied.length === 0 || next.length === 0) return null;

  const span = cur.to - cur.from; // zoom scale — never changed
  const lastIdx = applied.length - 1;
  const rightPad = cur.to - lastIdx; // whitespace right of the last bar
  const following = cur.to >= lastIdx - FOLLOW_SLACK_BARS;

  if (following) {
    // FOLLOWING: glue the right edge to the new last bar, keep the
    // user's padding (or clamp back onto the edge if they had panned
    // left of it within the slack window)
    const pad = Math.max(rightPad, 0);
    const newTo = next.length - 1 + pad;
    return { from: newTo - span, to: newTo };
  }

  // HISTORY-ANCHORED: re-anchor the left edge to the same wall-clock
  // bar time in the new array; the span (zoom scale) is untouched.
  const fromT = applied[clampIdx(applied.length, cur.from)].t;
  const newFrom = nearestIndex(next, fromT);
  const newTo = newFrom + span;
  // a shrunken window may not have room for the full span — clamp the
  // right edge onto the new last bar (keeping whatever pad exists)
  const newLast = next.length - 1;
  const maxTo = newLast + Math.max(rightPad, 0);
  if (newTo > maxTo) {
    const clampedTo = Math.max(newFrom + 1, maxTo);
    return { from: clampedTo - span, to: clampedTo };
  }
  return { from: newFrom, to: newTo };
}
