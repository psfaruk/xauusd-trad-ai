#!/usr/bin/env bash
# railway-start.sh — single-service supervisor for Railway (v3, Dockerfile/Nixpacks).
#
# One Railway service runs BOTH processes:
#   1. market-service  (bun + socket.io, REST /api/* on :3003 — data-source
#                       ladder: local MT5 MCP → AURUM remote bridge → live web
#                       exchanges, so REAL data streams even without Wine/MT5)
#   2. Next.js frontend (node .next/standalone/server.js on $PORT)
#
# The Next server runs as a *tracked child* (not via exec) so this supervisor
# can forward SIGTERM/SIGINT to BOTH processes and so a crash of either one
# exits the supervisor — Railway's ON_FAILURE policy then restarts the service.

set -uo pipefail

cd "$(dirname "$0")"

# bun is on PATH in the runtime image (Dockerfile installs it; Nixpacks
# detects bun.lock); fall back to the user install from the build phase.
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
# node is the reference runtime for the Next standalone server (bun fallback
# for images without node). NODE_OPTIONS caps the V8 heap so the frontend
# stays lean next to market-service on memory-capped plans.
echo "[railway-start] starting Next.js on :${PORT}"
if command -v node >/dev/null 2>&1; then
  ( exec env NODE_ENV=production NODE_OPTIONS="--max-old-space-size=512" \
      node .next/standalone/server.js ) &
else
  ( exec env NODE_ENV=production \
      bun .next/standalone/server.js ) &
fi
NEXT_PID=$!

# ---- 3) supervise --------------------------------------------------------------
# market-service is the app's data plane — give it up to MARKET_RESTARTS_MAX
# in-process restarts (hard crashes only; the engine self-heals transient
# failures internally via its retry ladder). The Next.js server is NOT
# restarted here: if the frontend dies, the supervisor exits non-zero and
# Railway's ON_FAILURE policy restarts the whole service.
MARKET_RESTARTS=0
MARKET_RESTARTS_MAX="${MARKET_RESTARTS_MAX:-5}"
while true; do
  sleep 2
  if ! kill -0 "$NEXT_PID" 2>/dev/null; then
    echo "[railway-start] Next.js exited — shutting down"
    kill -TERM "$MARKET_PID" 2>/dev/null || true
    pkill -TERM -f ".next/standalone/server.js" 2>/dev/null || true
    wait "$MARKET_PID" 2>/dev/null || true
    exit 1
  fi
  if ! kill -0 "$MARKET_PID" 2>/dev/null; then
    MARKET_RESTARTS=$((MARKET_RESTARTS + 1))
    if [ "$MARKET_RESTARTS" -gt "$MARKET_RESTARTS_MAX" ]; then
      echo "[railway-start] market-service exited ${MARKET_RESTARTS_MAX}+ times — exiting so Railway restarts the service"
      kill -TERM "$NEXT_PID" 2>/dev/null || true
      exit 1
    fi
    echo "[railway-start] market-service exited — restarting (attempt ${MARKET_RESTARTS}/${MARKET_RESTARTS_MAX})"
    ( cd mini-services/market-service && exec bun src/index.ts ) &
    MARKET_PID=$!
  fi
done
