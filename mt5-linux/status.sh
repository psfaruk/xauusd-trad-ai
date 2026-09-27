#!/usr/bin/env bash
# status.sh — one-shot, PASSIVE stack health report (never restarts anything).
# Wired to `bun run stack:status`.
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

MT5_STACK_DIR=${MT5_STACK_DIR:-/home/z/mt5-stack}
MT5_MCP_PORT=${MT5_MCP_PORT:-22346}
MT5_MCP_URL=${MT5_MCP_URL:-http://127.0.0.1:$MT5_MCP_PORT/mcp}
MT5_MCP_KEY_FILE=${MT5_MCP_KEY_FILE:-$MT5_STACK_DIR/mcp_key.txt}
MARKET_SERVICE_URL=${MARKET_SERVICE_URL:-http://localhost:3003/api/health}

B="\033[1m"; D="\033[2m"; G="\033[32m"; R="\033[31m"; Y="\033[33m"; N="\033[0m"
ok()   { printf "${G}%-9s${N} %s\n" "$1" "$2"; }
bad()  { printf "${R}%-9s${N} %s\n" "$1" "$2"; }
warn() { printf "${Y}%-9s${N} %s\n" "$1" "$2"; }
info() { printf "${D}%-9s${N} %s\n" "$1" "$2"; }

echo -e "${B}MT5-on-Linux stack status — $(date -u +%FT%TZ)${N}"

# --- processes ------------------------------------------------------------------
XPID=$(pgrep -f "Xvfb ${MT5_DISPLAY:-:99}" 2>/dev/null | head -1)
[ -n "$XPID" ] && ok "xvfb" "running (pid $XPID, ${MT5_DISPLAY:-:99})" || bad "xvfb" "NOT RUNNING"

TPID=$(pgrep -f 'terminal64\.exe' 2>/dev/null | head -1)
[ -n "$TPID" ] && ok "terminal" "running (pid $TPID: $(ps -o cmd= -p "$TPID" 2>/dev/null | sed 's/^.*terminal64.exe //' | sed 's|/password=[^ ]*|/password=***|' | cut -c1-70))" \
                || bad "terminal" "NOT RUNNING"

WPID=$(pgrep -f 'wineserver64' 2>/dev/null | head -1)
[ -n "$WPID" ] && ok "winesrv" "running (pid $WPID)" || warn "winesrv" "not running (fine if terminal is down)"

# --- MCP bridge -----------------------------------------------------------------
if ss -tln 2>/dev/null | grep -q ":$MT5_MCP_PORT[[:space:]]"; then
  ok "mcp" "listening on 127.0.0.1:$MT5_MCP_PORT"
  KEY=""
  [ -f "$MT5_MCP_KEY_FILE" ] && KEY=$(tr -d '\r\n ' < "$MT5_MCP_KEY_FILE")
  if [ -n "$KEY" ]; then
    CODE=$(curl -s -o /dev/null -m 5 -w '%{http_code}' -X POST "$MT5_MCP_URL" \
      -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
      -H "Authorization: Bearer $KEY" \
      -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"stack-status","version":"1.0"}}}' 2>/dev/null)
    if [ "$CODE" = "200" ]; then
      ok "mcp-key" "authenticates (HTTP 200, ${#KEY} chars, ${MT5_MCP_KEY_FILE})"
    else
      bad "mcp-key" "auth FAILED (HTTP ${CODE:-none}) — ${#KEY} chars in $MT5_MCP_KEY_FILE"
    fi
  else
    bad "mcp-key" "key file missing/empty: $MT5_MCP_KEY_FILE"
  fi
else
  bad "mcp" "port $MT5_MCP_PORT NOT listening"
fi

# --- market-service ----------------------------------------------------------------
H=$(curl -s -m 5 "$MARKET_SERVICE_URL" 2>/dev/null)
if [ -n "$H" ]; then
  echo "$H" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print('  (unparsable health body)')
    raise SystemExit(1)
k = d.get('mcp_key') or {}
deg = d.get('degraded')
print('  status=%s  degraded=%s  tps=%s  ready=%s' % (d.get('status'), deg, d.get('tps'), d.get('ready')))
print('  terminal_connected=%s  db=%s  key_present=%s (len %s)' % (d.get('terminal_connected'), d.get('db'), k.get('present'), k.get('length')))
print('  detail: %s' % str(d.get('detail'))[:90])
" 2>/dev/null && ok "svc" "answered $MARKET_SERVICE_URL" || warn "svc" "answered but body unparsable: $(printf '%s' "$H" | cut -c1-120)"
else
  bad "svc" "no answer from $MARKET_SERVICE_URL (market-service down?)"
fi

# --- watchdog ----------------------------------------------------------------------
# liveness: pid recorded in .watchdog.pid, cross-checked via /proc argv shape
WD=""
if [ -f "$SCRIPT_DIR/.watchdog.pid" ]; then
  WDP=$(head -n 1 "$SCRIPT_DIR/.watchdog.pid" 2>/dev/null)
  if [ -n "$WDP" ] && [ -d "/proc/$WDP" ]; then
    WARGV=$(tr '\0' '\n' < "/proc/$WDP/cmdline" 2>/dev/null)
    W1=$(printf '%s\n' "$WARGV" | sed -n 1p)
    W2=$(printf '%s\n' "$WARGV" | sed -n 2p)
    W3=$(printf '%s\n' "$WARGV" | sed -n 3p)
    if [ -n "$W1" ] && [ -n "$W2" ] && [ -z "$W3" ]; then
      case "$W1" in *bash|bash|*sh|sh) case "$W2" in */ensure-stack.sh|ensure-stack.sh) WD=$WDP ;; esac ;; esac
    fi
  fi
fi
[ -n "$WD" ] && ok "watchdog" "running (pid $WD)" || bad "watchdog" "NOT RUNNING (bun run dev or bash mt5-linux/ensure-stack.sh)"

# --- recent audit trail ------------------------------------------------------------
if [ -f "$SCRIPT_DIR/stack.log" ]; then
  info "stack.log" "last 3 lines:"
  tail -n 3 "$SCRIPT_DIR/stack.log" | sed 's/^/    /'
else
  info "stack.log" "(no log yet)"
fi

exit 0
