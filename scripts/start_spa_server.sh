#!/bin/bash
# Serve the built SPA + API from one uvicorn process (Railway-style, D-016)
# on :8010 — Phase 2/3 live verification. Mock source auto-connects (D-018).
set -euo pipefail
cd "$(dirname "$0")/../backend"
mkdir -p ../run
export DATA_SOURCE=mock
export STATIC_DIR="$(cd "$(dirname "$0")/../frontend/dist" && pwd)"
nohup .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8010 \
  > ../run/uvicorn-spa.log 2>&1 &
echo $! > ../run/uvicorn-spa.pid
echo "uvicorn SPA server started (pid $(cat ../run/uvicorn-spa.pid)) on :8010"
