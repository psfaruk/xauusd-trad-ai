#!/usr/bin/env bash
# ensure-stack.sh — THE WATCHDOG. Keeps the whole MT5-on-Linux stack alive:
#
#   Xvfb :99  ->  terminal64.exe (user-space wine, /portable, CLI auto-login)
#             ->  MCP bridge 127.0.0.1:22346  ->  key sync  ->  market-service :3003
#
# Every tick (default 30s):
#   1. Xvfb up?                  start if missing
#   2. terminal64.exe alive?     (re)launch via mt5-launch.sh (auto-login)
#   3. MCP :22346 listening?     90s grace after a fresh launch; if still dead
#                                 the terminal is hung -> kill terminal+wineserver,
#                                 relaunch
#   4. sync-mcp-key.sh           picks up NEW keys after terminal relaunches
#                                 (validated — never clobbers a working key)
#   5. market-service :3003?     curl /api/health; connection failure -> relaunch
#                                 (double-fork, survives session cleanup);
#                                 degraded:true is OK — the engine self-heals
#                                 via 60s retries, we only log it
#   6. one compact status line appended to mt5-linux/stack.log (uptime audit)
#
# The loop itself must survive sandbox resets: start it detached via the
# intermediate-exit double-fork (see dev-stack.sh / README.md), never plain
# nohup — plain children die at tool-session cleanup (worklog task 6/7).
#
# Env overrides:
#   STACK_WATCH_INTERVAL (default 30, min 5)  tick seconds
#   MT5_STACK_DIR        (default /home/z/mt5-stack)
#   WINE_ROOT            (default /home/z/mt5-restore/wine-root)
#   MT5_DISPLAY          (default :99)
#   MT5_MCP_PORT         (default 22346)
#   MARKET_SERVICE_URL   (default http://localhost:3003/api/health)
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

MT5_STACK_DIR=${MT5_STACK_DIR:-/home/z/mt5-stack}
WINE_ROOT=${WINE_ROOT:-/home/z/mt5-restore/wine-root}
DISPLAY=${MT5_DISPLAY:-:99}
INTERVAL=${STACK_WATCH_INTERVAL:-30}
MT5_MCP_PORT=${MT5_MCP_PORT:-22346}
MARKET_SERVICE_URL=${MARKET_SERVICE_URL:-http://localhost:3003/api/health}
LAUNCHER="$SCRIPT_DIR/mt5-launch.sh"
KEY_SYNC="$SCRIPT_DIR/sync-mcp-key.sh"
STACK_LOG="$SCRIPT_DIR/stack.log"
SERVICE_DIR="$REPO_ROOT/mini-services/market-service"
GRACE=${MT5_MCP_GRACE:-90}   # seconds a fresh terminal gets before MCP must listen

case "$INTERVAL" in ''|*[!0-9]*) INTERVAL=30 ;; esac
[ "$INTERVAL" -lt 5 ] && INTERVAL=30
case "$GRACE" in ''|*[!0-9]*) GRACE=90 ;; esac

log()  { echo "$(date -u +%FT%TZ) $*" >> "$STACK_LOG"; }
wlog() { echo "$(date -u +%FT%TZ) [watchdog] $*" >> "$STACK_LOG"; }

LOCK_FILE="$SCRIPT_DIR/.watchdog.lock"
WD_PID_FILE="$SCRIPT_DIR/.watchdog.pid"

# --- duplicate instance guard (exactly one watchdog, ever) --------------------
# flock is authoritative. A plain `pgrep -f ensure-stack` check is NOT: session
# wrappers (`bash -c "... ensure-stack.sh ..."`) and even this script's own
# $() subshell forks carry the same cmdline string and phantom-match.
if command -v flock >/dev/null 2>&1; then
  exec 200<>"$LOCK_FILE" 2>/dev/null || true
  if [ -e "/proc/self/fd/200" ] && ! flock -n 200; then
    echo "ensure-stack: already running (pid $(cat "$WD_PID_FILE" 2>/dev/null || echo '?')) — exiting" >&2
    exit 0
  fi
else
  # fallback (no flock): two-token argv shape via /proc; self, parent and our
  # own fork-children excluded
  is_watchdog_pid() {
    local argv p1 p2 p3
    argv=$(tr '\0' '\n' < "/proc/$1/cmdline" 2>/dev/null) || return 1
    p1=$(printf '%s\n' "$argv" | sed -n 1p)
    p2=$(printf '%s\n' "$argv" | sed -n 2p)
    p3=$(printf '%s\n' "$argv" | sed -n 3p)
    [ -n "$p1" ] && [ -n "$p2" ] && [ -z "$p3" ] || return 1
    case "$p1" in *bash|bash|*sh|sh) ;; *) return 1 ;; esac
    case "$p2" in */ensure-stack.sh|ensure-stack.sh) return 0 ;; *) return 1 ;; esac
  }
  DUP=""
  for p in $(pgrep -f 'ensure-stack\.sh' 2>/dev/null); do
    [ "$p" = "$$" ] && continue
    [ "$p" = "$PPID" ] && continue
    # exclude our own command-substitution forks (ppid == $$)
    ppid=$(awk '{print $4}' "/proc/$p/stat" 2>/dev/null)
    [ "$ppid" = "$$" ] && continue
    is_watchdog_pid "$p" && DUP="$DUP $p"
  done
  if [ -n "$DUP" ]; then
    echo "ensure-stack: already running (pid$DUP) — exiting" >&2
    exit 0
  fi
fi
echo $$ > "$WD_PID_FILE"   # liveness marker for dev-stack.sh / status.sh / setup.sh

mkdir -p "$SCRIPT_DIR" /tmp/xdg-run "$MT5_STACK_DIR"

# --- predicates ---------------------------------------------------------------
xvfb_up()  { pgrep -f "Xvfb $DISPLAY" >/dev/null 2>&1; }
term_pid() { pgrep -f 'terminal64\.exe' 2>/dev/null | head -1; }
mcp_up()   { ss -tln 2>/dev/null | grep -q ":$MT5_MCP_PORT[[:space:]]"; }
svc_health() { curl -s -m 5 "$MARKET_SERVICE_URL" 2>/dev/null; }

ensure_xvfb() {
  if xvfb_up; then return 0; fi
  bash -c "setsid Xvfb $DISPLAY -screen 0 1280x800x24 -nolisten tcp >/dev/null 2>&1 </dev/null &"
  sleep 1
  if xvfb_up; then wlog "xvfb: started :$DISPLAY"; else wlog "xvfb: FAILED to start :$DISPLAY"; fi
}

launch_terminal() { # double-fork: terminal survives this watchdog AND session cleanup
  bash -c "setsid bash '$LAUNCHER' >> '$SCRIPT_DIR/launch.log' 2>&1 </dev/null &"
  sleep 2
}

recover_terminal() { # terminal hung: full kill + fresh launch (auto-login)
  wlog "recovery: MCP dead after grace — killing terminal64.exe + wineserver, relaunching"
  pkill -f 'terminal64\.exe' 2>/dev/null
  sleep 2
  pkill -f 'wineserver64' 2>/dev/null
  sleep 2
  launch_terminal
  LAST_LAUNCH=$(date +%s)
}

relaunch_service() { # known-good pattern from worklog (task 6/7): intermediate-exit double-fork
  bash -c "cd '$SERVICE_DIR' && setsid bun --hot src/index.ts >> service.log 2>&1 </dev/null &"
}

wlog "watchdog started (pid $$, interval ${INTERVAL}s, repo $REPO_ROOT)"
LAST_LAUNCH=0

while true; do
  now=$(date +%s)

  # 1. Xvfb -------------------------------------------------------------------
  ensure_xvfb
  if xvfb_up; then XV="xvfb:ok"; else XV="xvfb:down"; fi

  # 2. terminal (auto-login launcher) ------------------------------------------
  TPID=$(term_pid)
  if [ -z "$TPID" ] && [ $((now - LAST_LAUNCH)) -ge "$GRACE" ]; then
    if mcp_up; then
      # terminal process gone but something (stale wineserver) still holds the
      # MCP port -> clean it so the fresh terminal can bind cleanly
      wlog "term:down but :$MT5_MCP_PORT still held — killing stale wineserver"
      pkill -f 'wineserver64' 2>/dev/null
      sleep 2
    fi
    launch_terminal
    LAST_LAUNCH=$(date +%s)
    wlog "term: launched via mt5-launch.sh (CLI auto-login)"
    TPID=$(term_pid)
  fi
  if [ -n "$TPID" ]; then T="term:ok($TPID)"; else T="term:down"; fi

  # 3. MCP bridge (grace after fresh launch; hung terminal -> recover) --------
  if mcp_up; then
    M="mcp:ok"
  elif [ "$LAST_LAUNCH" -gt 0 ] && [ $((now - LAST_LAUNCH)) -lt "$GRACE" ]; then
    M="mcp:grace"
  else
    M="mcp:down"
    if [ -n "$TPID" ]; then
      recover_terminal
      M="mcp:recovering"
    fi
  fi

  # 4. MCP key sync (idempotent, silent when healthy) --------------------------
  if bash "$KEY_SYNC" >/dev/null 2>&1; then K="key:ok"; else K="key:warn"; fi

  # 5. market-service ------------------------------------------------------------
  H=$(svc_health)
  if [ -z "$H" ]; then
    if [ -d "$SERVICE_DIR" ]; then
      relaunch_service
      wlog "svc: was down — relaunched market-service (bun --hot, double-fork)"
      S="svc:relaunched"
    else
      wlog "svc: down and $SERVICE_DIR missing — nothing to relaunch"
      S="svc:missing"
    fi
  elif printf '%s' "$H" | grep -q '"degraded"[[:space:]]*:[[:space:]]*true'; then
    # engine self-heals via 60s retries (H1) — restart would lose warm state
    S="svc:degraded"
  else
    S="svc:ok"
  fi

  # 6. compact audit line ---------------------------------------------------------
  echo "$(date -u +%FT%TZ) $XV $T $M $S $K" >> "$STACK_LOG"

  # keep stack.log bounded (rotate at ~5 MB)
  if [ "$(stat -c %s "$STACK_LOG" 2>/dev/null || echo 0)" -gt 5000000 ]; then
    tail -n 5000 "$STACK_LOG" > "$STACK_LOG.tmp" && mv -f "$STACK_LOG.tmp" "$STACK_LOG"
  fi

  sleep "$INTERVAL"
done
