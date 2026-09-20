#!/bin/bash
# Full dev stack for local browser e2e (dev only):
#   mock-supabase :8090  <-  auth for BOTH frontend (supabase-js) and backend
#   api+LIVE      :8000  <-  DATA_SOURCE=live: free real-time gold APIs (D-030);
#                           auto-degrades to mock if providers are unreachable
#   vite dev      :3001  <-  VITE_SUPABASE_URL pointed at the mock
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$ROOT/run"

# 1) mock supabase
nohup "$ROOT/backend/.venv/bin/python" "$ROOT/scripts/mock_supabase.py" \
  > "$ROOT/run/mock-supabase.log" 2>&1 &
echo $! > "$ROOT/run/mock-supabase.pid"

# 2) backend api (LIVE source: real-time free APIs, mock fallback; mock supabase)
cd "$ROOT/backend"
DATA_SOURCE=${DATA_SOURCE:-live} SUPABASE_URL=http://127.0.0.1:8090 \
SUPABASE_ANON_KEY=test ADMIN_EMAILS=trader@example.com \
nohup .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 \
  > "$ROOT/run/uvicorn.log" 2>&1 &
echo $! > "$ROOT/run/uvicorn.pid"

# 3) vite dev server on 3001
cd "$ROOT/frontend"
VITE_SUPABASE_URL=http://127.0.0.1:8090 VITE_SUPABASE_ANON_KEY=test \
nohup node node_modules/vite/bin/vite.js --port 3001 --strictPort \
  > "$ROOT/run/vite.log" 2>&1 &
echo $! > "$ROOT/run/vite.pid"

echo "dev stack up: supabase-mock :8090, api :8000, vite :3001"
