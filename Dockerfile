# syntax=docker/dockerfile:1

# XAUUSD AI Trading Platform — single-service image (Railway, DECISIONS D-016).
# One uvicorn process serves: REST API (/api/*), the WebSocket (/ws, Phase 2)
# and the built SPA (STATIC_DIR) — same-origin, no CORS needed.
#
# SPEC C1/C7: the MetaTrader5 package is Windows-only; this Linux image runs
# DATA_SOURCE=live (D-030): REAL-TIME gold prices from free key-less public
# APIs (Binance PAXG -> gold-api -> Yahoo fallbacks), auto-degrading to mock
# when no provider is reachable. Real MT5 *execution* targets the Windows VPS
# deployment (SPEC §14).

# ---------- Stage 1: build the Vite SPA ----------
FROM node:20-alpine AS frontend
WORKDIR /build
# Layer-cache friendly: deps first, sources after.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
# VITE_ vars are baked into the SPA at build time. Railway injects service
# variables as Docker build args — declare them so a rebuild picks them up.
ARG VITE_SUPABASE_URL
ARG VITE_SUPABASE_ANON_KEY
ARG VITE_API_URL
ENV VITE_SUPABASE_URL=$VITE_SUPABASE_URL
ENV VITE_SUPABASE_ANON_KEY=$VITE_SUPABASE_ANON_KEY
ENV VITE_API_URL=$VITE_API_URL
RUN npm run build

# ---------- Stage 2: FastAPI + SPA static files ----------
FROM python:3.11-slim AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/app ./app
COPY --from=frontend /build/dist ./static

# FastAPI serves the SPA from STATIC_DIR (app/main.py, D-016).
# DATA_SOURCE=live (D-030): free real-time gold APIs, auto-degrade to mock.
ENV STATIC_DIR=/app/static \
    DATA_SOURCE=live

# Railway injects PORT; 8000 fallback for local docker runs.
EXPOSE 8000
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
