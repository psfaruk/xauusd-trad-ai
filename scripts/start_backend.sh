#!/bin/bash
# Start the FastAPI backend (Phase 0, mock data source) in the background.
set -euo pipefail
cd "$(dirname "$0")/../backend"
mkdir -p ../run
nohup .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 \
  > ../run/uvicorn.log 2>&1 &
echo $! > ../run/uvicorn.pid
echo "uvicorn started (pid $(cat ../run/uvicorn.pid))"
