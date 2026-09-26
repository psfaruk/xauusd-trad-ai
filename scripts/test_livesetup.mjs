/**
 * D-074/D-075 — headless unit tests for frontend/src/lib/liveSetup.ts
 * (THE SIGNAL IS THE DRAWING pick, event-only lifecycle). Transpiled
 * with esbuild, run with node.
 * Usage: node scripts/test_livesetup.mjs   (from the repo root)
 */
import { execSync } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const FRONTEND = join(process.cwd(), "frontend");
const tmp = mkdtempSync(join(tmpdir(), "livesetup-"));
const out = join(tmp, "liveSetup.mjs");

// D-076 — liveSetup re-exports marketKey from ./markets (the shared
// market-key rule); bundle BOTH modules so the temp-dir import resolves.
execSync(
  `npx esbuild src/lib/liveSetup.ts --format=esm --outfile=${out} --bundle`,
  { cwd: FRONTEND, stdio: "pipe" },
);

const { pickLiveSetup, marketKey, sameMarket } = await import(`file://${out}`);

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
eq("null when no signals", pickLiveSetup([], "XAUUSD"), null);
eq("null when undefined", pickLiveSetup(undefined, "XAUUSD"), null);
eq(
  "picks a fresh pending signal",
  pickLiveSetup([sig("a")], "XAUUSD")?.id,
  "a",
);
eq(
  "picks a fresh active signal",
  pickLiveSetup([sig("a", { status: "active" })], "XAUUSD")?.id,
  "a",
);
eq(
  "filters by symbol",
  pickLiveSetup([sig("a", { symbol: "BTCUSD" })], "XAUUSD"),
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
  )?.id,
  "newer",
);

// ----------------------------------- D-075: NO time-based deletion EVER
// user directive (verbatim): "আর আপনি বলেছেন এটি মুছে যাবে 6 মিনিটে আমি
// এটা বলি নি, আমি বলেছি, নতুন কোনো এন্ট্রি সিগন্যাল আসলে তখন মুছে যাবে।
// আর যদি সে সেটাপ এর sl TP হিট হয়, তখন মুছে যাবে।"
// A live order stays drawn for as long as it LIVES — age alone never
// deletes ink (the engine's tracker flips the status, the WS
// signal_update event refetches, the drawing follows the ORDER).
eq(
  "pending ink 3 hours old STILL draws (no TTL)",
  pickLiveSetup([sig("old", { ts: iso(3 * 3_600_000) })], "XAUUSD")?.id,
  "old",
);
eq(
  "pending ink 24 hours old STILL draws (no TTL)",
  pickLiveSetup([sig("day", { ts: iso(24 * 3_600_000) })], "XAUUSD")?.id,
  "day",
);
eq(
  "active ink 6 hours old STILL draws (no TTL)",
  pickLiveSetup(
    [sig("long", { ts: iso(6 * 3_600_000), status: "active" })],
    "XAUUSD",
  )?.id,
  "long",
);
eq(
  "the CLOCK never deletes: same list, same pick an hour later",
  pickLiveSetup([sig("a")], "XAUUSD")?.id,
  "a",
);
eq(
  "the age cut is STATUS, not minutes: dead after 1 minute is gone",
  pickLiveSetup([sig("dead", { ts: iso(60_000), status: "lost" })], "XAUUSD"),
  null,
);

// ------------------------------------------------------------- garbage
eq(
  "unparseable ts never picks",
  pickLiveSetup([sig("bad", { ts: "not-a-date" })], "XAUUSD"),
  null,
);
eq(
  "null/undefined entries skipped",
  pickLiveSetup([null, undefined, sig("ok")], "XAUUSD")?.id,
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
// D-076 — the 5-char bases: the legacy >=6-char suffix rule broke these
eq("marketKey USOILm prefix rule", marketKey("USOILm"), "USOIL");
eq("marketKey USTECmicro prefix rule", marketKey("USTECmicro"), "USTEC");
eq("marketKey USOIL plain", marketKey("usoil"), "USOIL");
eq("marketKey USTEC dot broker", marketKey("USTEC.x"), "USTEC");
eq("marketKey XAUUSDm never collapses to USOIL", marketKey("XAUUSDm"), "XAUUSD");
eq("sameMarket suffix match", sameMarket("XAUUSDm", "XAUUSD"), true);
eq("sameMarket exact match", sameMarket("XAUUSD", "XAUUSD"), true);
eq("sameMarket different markets", sameMarket("BTCUSDm", "XAUUSD"), false);
eq(
  "live setup matches a suffixed signal row (defensive)",
  pickLiveSetup(
    [sig("a", { symbol: "XAUUSDm", status: "active" })],
    "XAUUSD",
  )?.id,
  "a",
);

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
