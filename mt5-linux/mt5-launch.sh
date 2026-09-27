#!/usr/bin/env bash
# mt5-launch.sh — launch the MetaTrader 5 terminal under user-space Wine on Xvfb.
#
# Portable replica of the proven /home/z/mt5-stack/mt5-launch.sh, with:
#   * every path env-overridable (works from any clone / any stack dir)
#   * credentials loaded from mt5-linux/mt5.env (gitignored — NO secrets here)
#   * auto-login via CLI flags: /portable /login=... /password=... /server=...
#     (this is the fix for Exness wiping saved passwords: "Accounts deleted
#     due security reason" — the terminal re-authenticates on every start)
#   * writes mt5-linux/terminal.pid — the exec'd wine process BECOMES
#     terminal64.exe, so the pidfile always equals the live terminal PID
#
# Env overrides:
#   MT5_STACK_DIR  (default /home/z/mt5-stack)        wine prefix + key file live here
#   WINE_ROOT      (default /home/z/mt5-restore/wine-root)  user-space wine tree
#   MT5_DISPLAY    (default :99)                      Xvfb display
#   MT5_ENV_FILE   (default <script dir>/mt5.env)     credentials
#   MT5_DRY_RUN=1  print the resolved command, execute nothing (safe testing)
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

MT5_STACK_DIR=${MT5_STACK_DIR:-/home/z/mt5-stack}
WINE_ROOT=${WINE_ROOT:-/home/z/mt5-restore/wine-root}
DISPLAY=${MT5_DISPLAY:-:99}
MT5_ENV_FILE=${MT5_ENV_FILE:-$SCRIPT_DIR/mt5.env}
PID_FILE=${MT5_PID_FILE:-$SCRIPT_DIR/terminal.pid}
LAUNCH_LOG=${MT5_LAUNCH_LOG:-$SCRIPT_DIR/launch.log}

# --- credentials (mt5.env is gitignored; copy mt5.env.example to start) ------
MT5_LOGIN=${MT5_LOGIN:-}
MT5_PASSWORD=${MT5_PASSWORD:-}
MT5_SERVER=${MT5_SERVER:-}
if [ -f "$MT5_ENV_FILE" ]; then
  # shellcheck disable=SC1090
  . "$MT5_ENV_FILE"
fi
if [ -z "$MT5_LOGIN" ] || [ -z "$MT5_PASSWORD" ] || [ -z "$MT5_SERVER" ]; then
  echo "mt5-launch: missing MT5_LOGIN/MT5_PASSWORD/MT5_SERVER — create $MT5_ENV_FILE (see mt5.env.example)" >&2
  [ "${MT5_DRY_RUN:-0}" = "1" ] || exit 1
fi

# --- wine environment (replicates /home/z/mt5-stack/mt5-launch.sh exactly) --
export WINEPREFIX="$MT5_STACK_DIR/wine-prefix"
export WINEARCH=win64
export WINELOADER="$WINE_ROOT/usr/lib/wine/wine64"
export WINESERVER="$WINE_ROOT/usr/lib/wine/wineserver64"
export LD_LIBRARY_PATH="$WINE_ROOT/usr/lib/x86_64-linux-gnu:$WINE_ROOT/usr/lib/i386-linux-gnu:$WINE_ROOT/usr/lib/wine:${LD_LIBRARY_PATH:-}"
export PATH="$WINE_ROOT/usr/bin:$PATH"
export WINEDEBUG=${WINEDEBUG:--all}
export XDG_RUNTIME_DIR=/tmp/xdg-run
export HOME=${MT5_HOME:-/home/z}
export DISPLAY

mkdir -p "$XDG_RUNTIME_DIR" "$MT5_STACK_DIR" "$SCRIPT_DIR" "$(dirname "$PID_FILE")"

# --- virtual display (start detached; never blocks) --------------------------
if ! pgrep -f "Xvfb $DISPLAY" >/dev/null 2>&1; then
  if [ "${MT5_DRY_RUN:-0}" = "1" ]; then
    echo "dry-run: would start: Xvfb $DISPLAY -screen 0 1280x800x24 -nolisten tcp"
  else
    bash -c "setsid Xvfb $DISPLAY -screen 0 1280x800x24 -nolisten tcp >/dev/null 2>&1 </dev/null &"
    sleep 1
  fi
fi

TERMINAL_EXE="$WINEPREFIX/drive_c/Program Files/MetaTrader 5/terminal64.exe"
if [ ! -f "$TERMINAL_EXE" ]; then
  echo "mt5-launch: terminal64.exe not found at: $TERMINAL_EXE" >&2
  echo "mt5-launch: run mt5-linux/setup.sh (or see README.md rebuild steps)" >&2
  [ "${MT5_DRY_RUN:-0}" = "1" ] || exit 1
fi

if [ "${MT5_DRY_RUN:-0}" = "1" ]; then
  echo "dry-run: WINEPREFIX=$WINEPREFIX DISPLAY=$DISPLAY WINELOADER=$WINELOADER"
  echo "dry-run: exec \"$WINE_ROOT/usr/lib/wine/wine64\" \"$TERMINAL_EXE\" /portable /login=$MT5_LOGIN /password=*** /server=$MT5_SERVER"
  exit 0
fi

# --- go: this shell becomes terminal64.exe (pidfile stays accurate) ----------
echo $$ > "$PID_FILE"
{
  echo "$(date -u +%FT%TZ) mt5-launch: starting terminal64.exe pid=$$ login=$MT5_LOGIN server=$MT5_SERVER display=$DISPLAY"
} >> "$LAUNCH_LOG"
exec "$WINE_ROOT/usr/lib/wine/wine64" "$TERMINAL_EXE" \
  /portable "/login=$MT5_LOGIN" "/password=$MT5_PASSWORD" "/server=$MT5_SERVER"
