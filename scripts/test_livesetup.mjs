/**
 * D-074 — headless unit tests for frontend/src/lib/liveSetup.ts
 * (THE SIGNAL IS THE DRAWING pick). Transpiled with esbuild, run with
 * node. Usage: node scripts/test_livesetup.mjs   (from the repo root)
 */
import { execSync } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const FRONTEND = join(process.cwd(), "frontend");
const tmp = mkdtempSync(join(tmpdir(), "livesetup-"));
const out = join(tmp, "liveSetup.mjs");

execSync(
  `npx esbuild src/lib/liveSetup.ts --format=esm --outfile=${out}`,
  { cwd: FRONTEND, stdio: "pipe" },
);

const { PENDING_TTL_MS, ACTIVE_TTL_MS, pickLiveSetup, marketKey, sameMarket } =
  await import(`file://${out}`);

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

const NOW = 1_800_000_000_000; // fixed epoch-ms for determinism
const iso = (ageMs) => new Date(NOW - ageMs).toISOString();
const sig = (id, over = {}) => ({
  id,
  ts: iso(60_000),
  symbol: "XAUUSD",
  tf: "M1",
  direction: "BUY",
  entry: 2700,
  sl: 2699,
  tp: 2702,
  confidence: 0.7,
  status: "pending",
  ...over,
});

// ------------------------------------------------------- the basic pick
eq("null when no signals", pickLiveSetup([], "XAUUSD", NOW), null);
eq("null when undefined", pickLiveSetup(undefined, "XAUUSD", NOW), null);
eq(
  "picks a fresh pending signal",
  pickLiveSetup([sig("a")], "XAUUSD", NOW)?.id,
  "a",
);
eq(
  "picks a fresh active signal",
  pickLiveSetup([sig("a", { status: "active" })], "XAUUSD", NOW)?.id,
  "a",
);
eq(
  "filters by symbol",
  pickLiveSetup([sig("a", { symbol: "BTCUSD" })], "XAUUSD", NOW),
  null,
);

// ------------------------------------------- the lifecycle directives
eq(
  "DEAD (won/lost/expired/cancelled) ink deleted immediately",
  pickLiveSetup(
    [
      sig("won", { status: "won" }),
      sig("lost", { status: "lost" }),
      sig("exp", { status: "expired" }),
      sig("can", { status: "cancelled" }),
    ],
    "XAUUSD",
    NOW,
  ),
  null,
);
eq(
  "NEW signal replaces the previous drawing (newest wins)",
  pickLiveSetup(
    [
      sig("old", { ts: iso(10 * 60_000), status: "active" }),
      sig("new", { ts: iso(30_000), direction: "SELL", status: "pending" }),
    ],
    "XAUUSD",
    NOW,
  )?.id,
  "new",
);
eq(
  "newest DEAD is skipped — the newest LIVE still draws",
  pickLiveSetup(
    [
      sig("live", { ts: iso(5 * 60_000), status: "active" }),
      sig("dead", { ts: iso(30_000), status: "won" }),
    ],
    "XAUUSD",
    NOW,
  )?.id,
  "live",
);
eq(
  "list order does not matter (defensive: ts decides)",
  pickLiveSetup(
    [
      sig("newer", { ts: iso(30_000), status: "pending" }),
      sig("older", { ts: iso(9 * 60_000), status: "pending" }),
    ],
    "XAUUSD",
    NOW,
  )?.id,
  "newer",
);

// --------------------------------------------------------- TTL windows
eq(
  "pending ink dies with the order window",
  pickLiveSetup(
    [sig("stale", { ts: iso(PENDING_TTL_MS + 1) })],
    "XAUUSD",
    NOW,
  ),
  null,
);
eq(
  "pending ink alive at the order window edge",
  pickLiveSetup(
    [sig("edge", { ts: iso(PENDING_TTL_MS - 1) })],
    "XAUUSD",
    NOW,
  )?.id,
  "edge",
);
eq(
  "active ink carries the trade horizon",
  pickLiveSetup(
    [sig("long", { ts: iso(ACTIVE_TTL_MS - 1), status: "active" })],
    "XAUUSD",
    NOW,
  )?.id,
  "long",
);
eq(
  "active ink expires past the horizon",
  pickLiveSetup(
    [sig("gone", { ts: iso(ACTIVE_TTL_MS + 1), status: "active" })],
    "XAUUSD",
    NOW,
  ),
  null,
);

// ------------------------------------------------------------- garbage
eq(
  "unparseable ts never picks",
  pickLiveSetup([sig("bad", { ts: "not-a-date" })], "XAUUSD", NOW),
  null,
);
eq(
  "null/undefined entries skipped",
  pickLiveSetup([null, undefined, sig("ok")], "XAUUSD", NOW)?.id,
  "ok",
);

// ------------------------------------------------------- symbol identity
// D-074 — broker spellings and platform names are ONE market (the
// exact mismatch that made the newest signal invisible on the chart)
eq("marketKey strips broker suffix", marketKey("XAUUSDm"), "XAUUSD");
eq("marketKey strips dot suffix", marketKey("XAUUSD.x"), "XAUUSD");
eq("marketKey strips pro suffix", marketKey("XAUUSD.PRO"), "XAUUSD");
eq("marketKey keeps platform name", marketKey("xauusd"), "XAUUSD");
eq("marketKey empty", marketKey(null), "");
eq("sameMarket suffix match", sameMarket("XAUUSDm", "XAUUSD"), true);
eq("sameMarket exact match", sameMarket("XAUUSD", "XAUUSD"), true);
eq("sameMarket different markets", sameMarket("BTCUSDm", "XAUUSD"), false);
eq(
  "live setup matches a suffixed signal row (defensive)",
  pickLiveSetup(
    [sig("a", { symbol: "XAUUSDm", status: "active" })],
    "XAUUSD",
    NOW,
  )?.id,
  "a",
);

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
