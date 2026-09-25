/**
 * D-073 — headless unit tests for frontend/src/lib/chartZoom.ts
 * (the zoom-preservation math). Transpiled with esbuild, run with node.
 *
 * Usage: node scripts/test_chartzoom.mjs   (from the repo root)
 */
import { execSync } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const FRONTEND = join(process.cwd(), "frontend");
const tmp = mkdtempSync(join(tmpdir(), "chartzoom-"));
const out = join(tmp, "chartZoom.mjs");

execSync(
  `npx esbuild src/lib/chartZoom.ts --format=esm --outfile=${out}`,
  { cwd: FRONTEND, stdio: "pipe" },
);

const { nearestIndex, sanitizeRange, preservedRange } = await import(
  `file://${out}`
);

let pass = 0;
let fail = 0;
const eq = (name, actual, expected) => {
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  if (a === e) {
    pass++;
  } else {
    fail++;
    console.error(`✗ ${name}: got ${a}, expected ${e}`);
  }
};

const bars = (n, t0 = 0, step = 60) =>
  Array.from({ length: n }, (_, i) => ({ t: t0 + i * step }));

// ------------------------------------------------------------- nearestIndex
eq("nearestIndex exact hit", nearestIndex(bars(10), 5 * 60), 5);
eq("nearestIndex between -> nearer lower", nearestIndex(bars(10), 4.4 * 60), 4);
eq("nearestIndex between -> nearer upper", nearestIndex(bars(10), 4.6 * 60), 5);
eq("nearestIndex below window clamps to 0", nearestIndex(bars(10), -999), 0);
eq("nearestIndex above window clamps to last", nearestIndex(bars(10), 999), 9);
eq("nearestIndex empty", nearestIndex([], 0), 0);
eq("nearestIndex non-finite -> last", nearestIndex(bars(10), NaN), 9);

// ------------------------------------------------------------ sanitizeRange
eq("sanitizeRange null", sanitizeRange(null), null);
eq("sanitizeRange undefined", sanitizeRange(undefined), null);
eq("sanitizeRange NaN from", sanitizeRange({ from: NaN, to: 10 }), null);
eq("sanitizeRange zero span", sanitizeRange({ from: 5, to: 5 }), null);
eq("sanitizeRange negative span", sanitizeRange({ from: 5, to: 1 }), null);
eq("sanitizeRange sane", sanitizeRange({ from: 10, to: 40 }), { from: 10, to: 40 });

// ------------------------------------------------------- following the edge
// default view after first load: {from: len-95, to: len+5} -> rightPad 5
{
  const applied = bars(100);
  const next = bars(102, 2 * 60); // window slid: same tail, 2 new bars
  const r = preservedRange(applied, next, { from: 30, to: 105 });
  eq("following: glued to new edge, span+pad kept", r, { from: 32, to: 107 });
}
{
  const applied = bars(100);
  const next = bars(100, 60); // same length, window slid by 1 bar
  const r = preservedRange(applied, next, { from: 30, to: 104 });
  eq("following: pad 4 kept", r, { from: 30, to: 104 });
}
{
  // user panned slightly back from the edge but within the slack window
  const applied = bars(100);
  const next = bars(103, 3 * 60);
  const r = preservedRange(applied, next, { from: 40, to: 99 });
  eq("following: to within slack clamps onto edge", r, { from: 43, to: 102 });
}

// --------------------------------------------------------- history anchored
{
  // identical arrays -> identical range restored (no-op, no reset)
  const applied = bars(100);
  const r = preservedRange(applied, applied, { from: 10, to: 40 });
  eq("identical data: range unchanged", r, { from: 10, to: 40 });
}
{
  // window EXTENDED backwards (more history prepended): the same
  // wall-clock bars must stay under the cursor
  const applied = bars(100, 20 * 60);
  const next = bars(120, 0); // next[i+20].t == applied[i].t
  const r = preservedRange(applied, next, { from: 10, to: 40 });
  eq("backfill prepends: left edge follows the same time", r, { from: 30, to: 60 });
}
{
  // window slid forward (old history dropped): from-time no longer in
  // the array -> nearest index 0; span preserved
  const applied = bars(100, 0);
  const next = bars(100, 20 * 60); // next[i].t == applied[i+20].t
  const r = preservedRange(applied, next, { from: 10, to: 40 });
  eq("window slid: clamps to 0, span kept", r, { from: 0, to: 30 });
}
{
  // shrunken window: right edge clamps onto the new last bar
  const applied = bars(100, 0);
  const next = bars(60, 0); // first 60 times identical
  const r = preservedRange(applied, next, { from: 50, to: 80 });
  eq("shrunken: clamped to the new edge, span kept", r, { from: 29, to: 59 });
}

// ---------------------------------------------------------------- garbage in
eq("garbage range -> null", preservedRange(bars(10), bars(12), { from: NaN, to: 5 }), null);
eq("empty applied -> null", preservedRange([], bars(12), { from: 0, to: 5 }), null);
eq("empty next -> null", preservedRange(bars(12), [], { from: 0, to: 5 }), null);
eq("null current -> null", preservedRange(bars(10), bars(12), null), null);

console.log(`\nchartZoom tests: ${pass} passed, ${fail} failed`);
process.exit(fail === 0 ? 0 : 1);
