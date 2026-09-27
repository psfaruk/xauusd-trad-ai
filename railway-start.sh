#!/usr/bin/env bash
# railway-start.sh — single-service supervisor for Railway (v2, Nixpacks).
#
# One Railway service runs BOTH processes:
#   1. market-service  (bun + socket.io, REST /api/* on :3003 — talks to the
#                       MetaTrader 5 MCP bridge at $MT5_MCP_URL)
#   2. Next.js frontend (bun run start → .next/standalone/server.js on $PORT)
#
# The Next server runs as a *tracked child* (not via exec) so this supervisor
# can forward SIGTERM/SIGINT to BOTH processes and so a crash of either one
# exits the supervisor — Railway's ON_FAILURE policy then restarts the service.

set -uo pipefail

cd "$(dirname "$0")"

# bun is on PATH in the Nixpacks runtime image (bun.lock at repo root);
# fall back to the user install from the build phase, just in case.
export PATH="$HOME/.bun/bin:$PATH"

# ---- environment defaults --------------------------------------------------
export MT5_MCP_URL="${MT5_MCP_URL:-http://127.0.0.1:22346/mcp}"
export MARKET_SERVICE_PORT="${MARKET_SERVICE_PORT:-3003}"  # market-service binds :3003 on all interfaces
export PORT="${PORT:-3000}"                                # Railway injects PORT; the Next standalone server reads it
export HOSTNAME="0.0.0.0"                                  # Next standalone must bind all interfaces, not the container hostname

NEXT_PID=""

cleanup() {
  trap - TERM INT
  echo "[railway-start] signal received — stopping market-service + next"
  kill -TERM "$MARKET_PID" ${NEXT_PID:+"$NEXT_PID"} 2>/dev/null || true
  # `bun run start` pipelines through a shell — sweep any orphaned standalone server
  pkill -TERM -f ".next/standalone/server.js" 2>/dev/null || true
  wait "$MARKET_PID" ${NEXT_PID:+"$NEXT_PID"} 2>/dev/null || true
  exit 0
}
trap cleanup TERM INT

# ---- 1) market-service -------------------------------------------------------
echo "[railway-start] starting market-service on :${MARKET_SERVICE_PORT} (MT5_MCP_URL=$MT5_MCP_URL)"
( cd mini-services/market-service && exec bun src/index.ts ) &
MARKET_PID=$!

# wait up to 15s for its REST health endpoint
READY=0
for _ in $(seq 1 15); do
  if curl -fsS "http://127.0.0.1:${MARKET_SERVICE_PORT}/api/health" >/dev/null 2>&1; then
    READY=1
    break
  fi
  sleep 1
done
if [ "$READY" = 1 ]; then
  echo "[railway-start] market-service healthy on :${MARKET_SERVICE_PORT}"
else
  echo "[railway-start] WARNING: market-service /api/health not ready after 15s — continuing; the app reports the bridge as unreachable honestly (no fake data)"
fi

# ---- 2) Next.js frontend ------------------------------------------------------
echo "[railway-start] starting Next.js on :${PORT}"
( exec bun run start ) &
NEXT_PID=$!

# ---- 3) supervise --------------------------------------------------------------
# Block until EITHER process exits, then stop the other and mirror the exit
# code so Railway's ON_FAILURE restart policy kicks in.
wait -n 2>/dev/null || wait
CODE=$?
echo "[railway-start] a process exited (code ${CODE}) — shutting down"
kill -TERM "$MARKET_PID" "$NEXT_PID" 2>/dev/null || true
pkill -TERM -f ".next/standalone/server.js" 2>/dev/null || true
wait "$MARKET_PID" "$NEXT_PID" 2>/dev/null || true
exit "$CODE"
