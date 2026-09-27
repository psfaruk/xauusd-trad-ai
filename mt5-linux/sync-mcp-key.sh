#!/usr/bin/env bash
# sync-mcp-key.sh — keep the MCP Bearer key file in sync with the terminal.
#
# Extracts the ApiKey from the terminal's Config/assistant.ini (a UTF-16LE
# Windows ini) and writes it to $MT5_MCP_KEY_FILE — but ONLY when it is SAFE:
#
#   * candidate == current file content          -> silent no-op
#   * cached "current key already validated"     -> silent no-op (no probing)
#   * MCP bridge up + current key authenticates  -> silent no-op (NEVER
#     clobber a working key)
#   * MCP bridge up + current key broken (401) + candidate authenticates
#                                               -> atomic write (tmp + mv)
#   * MCP bridge down + key file missing/empty   -> write candidate best-effort
#   * neither key authenticates                  -> warn on stderr, exit 1,
#     keep the existing file untouched
#
# WHY the validation gate: verified live on MT5 build 6231 — the assistant.ini
# ApiKey hex (168 chars) gets HTTP 401 from the MCP endpoint, while the
# 42-char Bearer token in mcp_key.txt gets HTTP 200. The ini hex is NOT the
# Bearer token on this build, so blind copying would break market-service
# (which hot-reloads the key file on mtime change — H1). If the terminal ever
# regenerates assistant.ini after a relaunch with a fresh, VALID key, this
# script picks it up automatically within one watchdog tick.
#
# market-service needs NO restart after a key change: it re-reads the file on
# mtime change (and on HTTP 401/403) within 60s (task H1).
#
# Env overrides:
#   MT5_STACK_DIR     (default /home/z/mt5-stack)
#   MT5_MCP_KEY_FILE  (default $MT5_STACK_DIR/mcp_key.txt)
#   MT5_MCP_URL       (default http://127.0.0.1:22346/mcp)
#   MT5_ASSISTANT_INI (default $MT5_STACK_DIR/wine-prefix/drive_c/Program
#                       Files/MetaTrader 5/Config/assistant.ini)
#   MT5_KEY_SYNC_VERBOSE=1  talk while working
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

MT5_STACK_DIR=${MT5_STACK_DIR:-/home/z/mt5-stack}
MT5_MCP_KEY_FILE=${MT5_MCP_KEY_FILE:-$MT5_STACK_DIR/mcp_key.txt}
MT5_MCP_URL=${MT5_MCP_URL:-http://127.0.0.1:22346/mcp}
ASSISTANT_INI=${MT5_ASSISTANT_INI:-$MT5_STACK_DIR/wine-prefix/drive_c/Program Files/MetaTrader 5/Config/assistant.ini}
STATE_FILE="$SCRIPT_DIR/.keycheck"
VERBOSE=${MT5_KEY_SYNC_VERBOSE:-0}

say() { [ "$VERBOSE" = "1" ] && echo "sync-mcp-key: $*"; }
warn() { echo "sync-mcp-key: $*" >&2; }

[ -f "$ASSISTANT_INI" ] || { warn "assistant.ini not found: $ASSISTANT_INI"; exit 1; }

# --- extract candidate key (UTF-16LE ini; iconv first, NUL-strip fallback) --
CAND=$(iconv -f UTF-16LE -t UTF-8 "$ASSISTANT_INI" 2>/dev/null \
       | grep -m1 -i '^ApiKey=' | cut -d= -f2- | tr -d '\r\n ')
if [ -z "$CAND" ]; then
  CAND=$(tr -d '\0' < "$ASSISTANT_INI" | grep -m1 -i 'ApiKey=' \
         | sed 's/^.*[Aa]pi[Kk]ey=//' | tr -d '\r\n ')
fi
[ -n "$CAND" ] || { warn "no ApiKey line found in $ASSISTANT_INI"; exit 1; }

CUR=""
[ -f "$MT5_MCP_KEY_FILE" ] && CUR=$(tr -d '\r\n ' < "$MT5_MCP_KEY_FILE")

# already in sync — silent
[ "$CAND" = "$CUR" ] && exit 0

# --- cache: skip re-probing while neither file changed since last "ok" -----
SIG="$(stat -c %Y "$MT5_MCP_KEY_FILE" 2>/dev/null || echo 0):$(stat -c %Y "$ASSISTANT_INI" 2>/dev/null || echo 0)"
if [ -f "$STATE_FILE" ] && [ "$(cat "$STATE_FILE" 2>/dev/null)" = "ok:$SIG" ]; then
  exit 0
fi

# --- probe helper: HTTP status of a JSON-RPC initialize with given Bearer ---
probe() {
  curl -s -o /dev/null -m 5 -w '%{http_code}' -X POST "$MT5_MCP_URL" \
    -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    -H "Authorization: Bearer $1" \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"stack-watchdog","version":"1.0"}}}' \
    2>/dev/null
}

write_key() { # atomic write so market-service never sees a partial key
  printf '%s' "$1" > "$MT5_MCP_KEY_FILE.tmp" && mv -f "$MT5_MCP_KEY_FILE.tmp" "$MT5_MCP_KEY_FILE"
}

CUR_CODE=$(probe "$CUR")
if [ "$CUR_CODE" = "200" ]; then
  # existing key works — keep it, cache the verdict, stay silent
  echo "ok:$SIG" > "$STATE_FILE"
  say "current key authenticates — keeping it (assistant.ini candidate differs but is not the live token)"
  exit 0
fi

CAND_CODE=$(probe "$CAND")
if [ "$CAND_CODE" = "200" ]; then
  write_key "$CAND"
  echo "sync-mcp-key: $(date -u +%FT%TZ) key updated from assistant.ini (old key stopped authenticating)"
  exit 0
fi

if [ -z "$CUR_CODE" ] && [ -z "$CAND_CODE" ]; then
  # MCP bridge is down entirely (terminal not running)
  if [ -z "$CUR" ]; then
    write_key "$CAND"
    say "MCP down + key file empty — wrote assistant.ini candidate as best effort"
    exit 0
  fi
  say "MCP bridge down — keeping existing key file untouched"
  exit 0
fi

warn "key problem: current key -> HTTP ${CUR_CODE:-none}, assistant.ini key -> HTTP ${CAND_CODE:-none}; manual recovery: read the token from the terminal UI (MCP/assistant settings) and write it to $MT5_MCP_KEY_FILE"
exit 1
