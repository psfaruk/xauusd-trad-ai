#!/usr/bin/env bash
# dev-stack.sh — the `bun run dev` integration hook.
#
# Starts ensure-stack.sh DETACHED (intermediate-exit double-fork: the watchdog
# is init-parented and survives tool-session/sandbox cleanup) ONLY if it is
# not already running, then runs ONE immediate key sync so a fresh boot has
# the MCP key file ASAP. Exits immediately — it MUST NEVER block `next dev`.
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

# Is the watchdog alive? Authoritative answer: the pid ensure-stack.sh records
# in .watchdog.pid, cross-checked against /proc (guards stale pid reuse).
# (A plain `pgrep -f ensure-stack` would phantom-match this very wrapper's
# cmdline, since the dev script string mentions the script name.)
watchdog_running() {
  local pid argv p1 p2 p3
  [ -f "$SCRIPT_DIR/.watchdog.pid" ] || return 1
  pid=$(head -n 1 "$SCRIPT_DIR/.watchdog.pid" 2>/dev/null)
  [ -n "$pid" ] && [ -d "/proc/$pid" ] || return 1
  argv=$(tr '\0' '\n' < "/proc/$pid/cmdline" 2>/dev/null) || return 1
  p1=$(printf '%s\n' "$argv" | sed -n 1p)
  p2=$(printf '%s\n' "$argv" | sed -n 2p)
  p3=$(printf '%s\n' "$argv" | sed -n 3p)
  [ -n "$p1" ] && [ -n "$p2" ] && [ -z "$p3" ] || return 1
  case "$p1" in *bash|bash|*sh|sh) ;; *) return 1 ;; esac
  case "$p2" in */ensure-stack.sh|ensure-stack.sh) return 0 ;; *) return 1 ;; esac
}

if ! watchdog_running; then
  bash -c "setsid bash '$SCRIPT_DIR/ensure-stack.sh' >> '$SCRIPT_DIR/stack.log' 2>&1 </dev/null &"
fi

# one immediate key sync (silent when already in sync / healthy)
bash "$SCRIPT_DIR/sync-mcp-key.sh" >/dev/null 2>&1 || true

exit 0
