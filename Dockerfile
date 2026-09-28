# syntax=docker/dockerfile:1
# ============================================================================
# XAUUSD Trading AI — single-service cloud image (Next.js + market-service)
# ============================================================================
# Runtime shape (started by railway-start.sh as CMD):
#   - market-service  : bun + socket.io on :3003 (internal)
#     Data-source ladder (REAL data always, no Wine/MT5 needed on cloud):
#       local MT5 MCP → AURUM remote MT5 bridge (REMOTE_MT5_URL) → live web
#       exchanges (Binance PAXG/BTC WebSocket + NYMEX/CME futures via Yahoo)
#   - Next.js frontend: node .next/standalone/server.js on $PORT (Railway)
#
# Why a Dockerfile instead of Nixpacks: a deterministic environment (glibc
# bookworm + node 22 + bun) for the Prisma engines, the Next standalone
# server and the bun-run market-service — no builder-environment guesswork.
#
# NOTE ON BUILD MEMORY: `next build --webpack` (package.json) peaks at
# ~0.7–0.8GB RSS for this dependency graph (Turbopack ~1.1GB). Deploy this
# service with AT LEAST 1GB RAM — 2GB recommended. On a 512MB plan the build
# itself is OOM-killed and the deploy fails before the app ever boots.
# ============================================================================

# ---------- Stage 1: build --------------------------------------------------
FROM node:22-bookworm-slim AS build
WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates unzip \
 && rm -rf /var/lib/apt/lists/*

# bun — runs the installs and market-service at runtime
RUN curl -fsSL https://bun.sh/install | bash \
 && mv /root/.bun/bin/bun /usr/local/bin/bun \
 && bun --version

# 1) dependencies first (layer cache)
COPY package.json bun.lock ./
RUN bun install --frozen-lockfile

COPY mini-services/market-service/package.json ./mini-services/market-service/
RUN cd mini-services/market-service && bun install

# 2) prisma client generation (schema only — no DB needed at build time)
COPY prisma ./prisma
RUN bun run db:generate

# 3) the app itself + production build (next build --webpack + standalone cp steps)
COPY . .
ENV NEXT_TELEMETRY_DISABLED=1
RUN bun run build

# ---------- Stage 2: runtime -------------------------------------------------
FROM node:22-bookworm-slim AS runtime
WORKDIR /app

# curl: railway-start.sh health-polls market-service; bash is present in the
# Debian base image.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

# bun binary (market-service runtime — TS executed natively)
COPY --from=build /usr/local/bin/bun /usr/local/bin/bun

# Next standalone app (server.js + traced node_modules + .next/static + public,
# already assembled by the `bun run build` cp steps)
COPY --from=build /app/.next/standalone ./

# market-service (source + its own node_modules)
COPY --from=build /app/mini-services/market-service ./mini-services/market-service

# supervisor: starts market-service + Next, forwards signals, mirrors exit codes
COPY railway-start.sh ./railway-start.sh
RUN chmod +x railway-start.sh

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    MARKET_SERVICE_PORT=3003

EXPOSE 3000
CMD ["bash", "railway-start.sh"]

