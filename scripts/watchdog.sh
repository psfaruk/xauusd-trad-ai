#!/bin/bash
# Keep-alive watchdog for the dev stack (D-031).
# Restarts any dead component every 15s:
#   mock-supabase :8090  (auth mock)
#   api           :8000  (FastAPI, DATA_SOURCE=live)
#   vite          :3000  (frontend served through the sandbox preview gateway)
# Started detached from .zscripts/dev.sh (container boot) or manually:
#   ( setsid nohup bash scripts/watchdog.sh >/dev/null 2>&1 & )
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RUN="$ROOT/run"
mkdir -p "$RUN"

port_open() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null && exec 3>&- && return 0 || return 1; }

# NOTE (D-031): every spawn is wrapped in `( ... & )` so the service is
# orphan-reparented to PID 1 IMMEDIATELY. The sandbox reaper kills the
# tool-command's descendant tree when the command ends; an already-reparented
# orphan (PPID=1, own session via setsid) escapes that walk. A direct
# `setsid nohup X &` is still a descendant until its parent exits -> killed.
start_supabase() {
  cd "$ROOT"
  ( setsid nohup "$ROOT/backend/.venv/bin/python" "$ROOT/scripts/mock_supabase.py" \
      >> "$RUN/mock-supabase.log" 2>&1 < /dev/null & )
  echo "$(date '+%F %T') watchdog: started mock-supabase :8090" >> "$RUN/watchdog.log"
}

start_api() {
  cd "$ROOT/backend"
  ( DATA_SOURCE=live SUPABASE_URL=http://127.0.0.1:8090 \
    SUPABASE_ANON_KEY=test ADMIN_EMAILS="trader@example.com,admin@dev.local,demo@xauusd.local" \
    setsid nohup .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 \
      >> "$RUN/uvicorn.log" 2>&1 < /dev/null & )
  echo "$(date '+%F %T') watchdog: started api :8000" >> "$RUN/watchdog.log"
}

start_vite() {
  cd "$ROOT/frontend"
  ( setsid nohup node node_modules/vite/bin/vite.js --port 3000 --strictPort \
      >> "$RUN/vite.log" 2>&1 < /dev/null & )
  echo "$(date '+%F %T') watchdog: started vite :3000" >> "$RUN/watchdog.log"
}

# Avoid duplicate watchdogs
LOCK="$RUN/watchdog.lock"
if [ -f "$LOCK" ] && kill -0 "$(cat "$LOCK")" 2>/dev/null; then exit 0; fi
echo $$ > "$LOCK"

echo "$(date '+%F %T') watchdog: loop started (pid $$)" >> "$RUN/watchdog.log"
while true; do
  port_open 8090 || start_supabase
  sleep 2
  port_open 8000 || start_api
  sleep 2
  port_open 3000 || start_vite
  sleep 15
done
