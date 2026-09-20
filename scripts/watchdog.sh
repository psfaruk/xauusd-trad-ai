#!/bin/bash
# Keep-alive watchdog for the dev stack (D-031 + D-034).
# Restarts any dead component every 15s:
#   mock-supabase :8090  (auth mock)
#   api           :8000  (FastAPI, DATA_SOURCE=live, MT5 MCP bridge)
#   vite          :3000  (frontend served through the sandbox preview gateway)
#   xvfb+openbox  :99    (display for the MT5 terminal GUI under wine)
#   mt5 terminal         (real MetaTrader 5 in user-space wine; MCP :22346)
# Started detached from .zscripts/dev.sh (container boot) or manually:
#   ( setsid nohup bash scripts/watchdog.sh >/dev/null 2>&1 & )
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RUN="$ROOT/run"
MT5STACK=/home/z/mt5stack
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
    MT5_MCP_KEY_FILE="$MT5STACK/mcp_key.txt" \
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

# ---- D-034: real MetaTrader 5 terminal under user-space wine ----------------
# Xvfb display + openbox WM + terminal64.exe /portable (auto-login Exness,
# MCP API on 127.0.0.1:22346). All binaries are user-space (no root needed):
#   wine      $MT5STACK/root        (debs extracted; winefix.so LD_PRELOAD shim)
#   x tools   $MT5STACK/xsroot      (xvfb is system-wide; openbox+xdotool user-space)
start_xvfb() {
  ( setsid nohup Xvfb :99 -screen 0 1600x1000x24 \
      >> "$MT5STACK/xvfb.log" 2>&1 < /dev/null & )
  echo "$(date '+%F %T') watchdog: started Xvfb :99" >> "$RUN/watchdog.log"
}

start_openbox() {
  ( LD_LIBRARY_PATH="$MT5STACK/xsroot/usr/lib/x86_64-linux-gnu" \
    PATH="$MT5STACK/xsroot/usr/bin:$PATH" DISPLAY=:99 XDG_DATA_DIRS="$MT5STACK/xsroot/usr/share:/usr/share" \
    setsid nohup "$MT5STACK/xsroot/usr/bin/openbox" \
      >> "$MT5STACK/openbox.log" 2>&1 < /dev/null & )
  echo "$(date '+%F %T') watchdog: started openbox :99" >> "$RUN/watchdog.log"
}

start_terminal() {
  ( LD_PRELOAD="$MT5STACK/winefix.so" \
    PATH="$MT5STACK/root/usr/local/bin:$PATH" \
    WINEPREFIX="$MT5STACK/prefix" WINEDLLOVERRIDES="mscoree,mshtml=" WINEDEBUG=-all DISPLAY=:99 \
    setsid nohup "$MT5STACK/root/usr/local/bin/wine64" \
      "C:\\Program Files\\MetaTrader 5\\terminal64.exe" /portable \
      >> "$MT5STACK/term.log" 2>&1 < /dev/null & )
  echo "$(date '+%F %T') watchdog: started MT5 terminal (MCP :22346)" >> "$RUN/watchdog.log"
}

terminal_alive() {
  pgrep -f "terminal64.exe" >/dev/null 2>&1
}

display_ok() {
  DISPLAY=:99 timeout 2 "$MT5STACK/xsroot/usr/bin/xdotool" getdisplaygeometry >/dev/null 2>&1
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
  sleep 2
  # D-034 wine stack (skip silently when the stack dir is absent, e.g. Railway)
  if [ -d "$MT5STACK/root" ]; then
    display_ok || start_xvfb
    sleep 1
    display_ok && { pgrep -x openbox >/dev/null 2>&1 || start_openbox; }
    sleep 1
    if ! terminal_alive; then
      if [ ! -f "$MT5STACK/.terminal_boot_grace" ] || \
         [ $(( $(date +%s) - $(stat -c %Y "$MT5STACK/.terminal_boot_grace" 2>/dev/null || echo 0) )) -gt 90 ]; then
        touch "$MT5STACK/.terminal_boot_grace"
        start_terminal
      fi
    else
      rm -f "$MT5STACK/.terminal_boot_grace"
    fi
  fi
  sleep 15
done
