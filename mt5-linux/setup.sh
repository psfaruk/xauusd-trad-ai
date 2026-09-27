#!/usr/bin/env bash
# setup.sh — BEST-EFFORT from-scratch rebuild of the MT5-on-Linux stack.
#
# Builds, in order (skipping anything that already exists):
#   1. mt5.env                (credentials — you fill these in)
#   2. user-space wine-root   (amd64 + i386 Debian packages extracted locally,
#                              no root needed — vendor/fetch_pkgs.py +
#                              vendor/mt5-resolve-i386.py)
#   3. Xvfb :99               (virtual display)
#   4. MT5 install            (mt5setup.exe /auto silent install into the
#                              wine prefix)
#   5. login.ini + assistant.ini (portable-mode config: auto-login + MCP
#                              bridge on 127.0.0.1:22346)
#   6. hands over to ensure-stack.sh (the watchdog keeps it alive forever)
#
# Where full automation is impossible, this script prints the EXACT manual
# step. Read README.md for the full story and troubleshooting.
#
# Env overrides: MT5_RESTORE_DIR (default /home/z/mt5-restore), MT5_STACK_DIR,
# WINE_ROOT, MT5_DISPLAY, plus everything mt5-launch.sh honors.
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

MT5_RESTORE_DIR=${MT5_RESTORE_DIR:-/home/z/mt5-restore}
MT5_STACK_DIR=${MT5_STACK_DIR:-/home/z/mt5-stack}
WINE_ROOT=${WINE_ROOT:-$MT5_RESTORE_DIR/wine-root}
MT5_DISPLAY=${MT5_DISPLAY:-:99}
WINEPREFIX="$MT5_STACK_DIR/wine-prefix"
MT5_SETUP_EXE=${MT5_SETUP_EXE:-$MT5_RESTORE_DIR/mt5setup.exe}
MT5_SETUP_URL=${MT5_SETUP_URL:-https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe}
VENDOR="$SCRIPT_DIR/vendor"

step() { echo; echo -e "\033[1m== $* ==\033[0m"; }
have()  { command -v "$1" >/dev/null 2>&1; }

mkdir -p "$MT5_RESTORE_DIR" "$MT5_STACK_DIR"

# ---------------------------------------------------------------- 1. credentials
step "1/6 credentials (mt5.env)"
if [ ! -f "$SCRIPT_DIR/mt5.env" ]; then
  cp "$SCRIPT_DIR/mt5.env.example" "$SCRIPT_DIR/mt5.env"
  echo "created $SCRIPT_DIR/mt5.env from the example — EDIT IT NOW:"
  echo "  MT5_LOGIN / MT5_PASSWORD / MT5_SERVER"
  echo "then re-run this script."
  exit 1
fi
# shellcheck disable=SC1091
. "$SCRIPT_DIR/mt5.env"
if [ -z "${MT5_LOGIN:-}" ] || [ -z "${MT5_PASSWORD:-}" ] || [ -z "${MT5_SERVER:-}" ]; then
  echo "mt5.env has empty MT5_LOGIN/MT5_PASSWORD/MT5_SERVER — fill it in and re-run." >&2
  exit 1
fi
echo "ok: login=$MT5_LOGIN server=$MT5_SERVER"

# ---------------------------------------------------------------- 2. wine-root
step "2/6 user-space wine tree ($WINE_ROOT)"
if [ -x "$WINE_ROOT/usr/lib/wine/wine64" ]; then
  echo "ok: wine tree already present"
else
  echo "building wine-root from Debian packages (no root required)..."
  if have python3 && have apt-get && have dpkg-deb; then
    export MT5_RESTORE_DIR   # vendored copies honor this (original paths default)
    python3 "$VENDOR/fetch_pkgs.py" wine wine64 libwine || true
    # i386 side: wine needs the 32-bit loader + its dependency closure.
    # The resolver walks a Debian i386 Packages file:
    if [ ! -f /tmp/Packages-i386 ]; then
      echo "downloading Debian i386 Packages index..."
      curl -fsSL http://deb.debian.org/debian/dists/stable/main/binary-i386/Packages.gz \
        | gunzip > /tmp/Packages-i386 || echo "  (download failed — MANUAL STEP below)"
    fi
    if [ -f /tmp/Packages-i386 ]; then
      python3 "$VENDOR/mt5-resolve-i386.py" libwine:i386 wine32:i386 || true
    fi
    echo "extracting packages into $WINE_ROOT ..."
    for d in "$MT5_RESTORE_DIR"/debs/*.deb "$MT5_RESTORE_DIR"/debs-i386/*.deb; do
      [ -f "$d" ] || continue
      dpkg-deb -x "$d" "$WINE_ROOT" 2>/dev/null || echo "  (extract failed: $d)"
    done
  else
    echo "MISSING python3 / apt tools — MANUAL STEP:"
    echo "  python3 $VENDOR/fetch_pkgs.py wine wine64 libwine"
    echo "  curl -fsSL http://deb.debian.org/debian/dists/stable/main/binary-i386/Packages.gz | gunzip > /tmp/Packages-i386"
    echo "  python3 $VENDOR/mt5-resolve-i386.py libwine:i386 wine32:i386"
    echo "  for d in $MT5_RESTORE_DIR/debs/*.deb $MT5_RESTORE_DIR/debs-i386/*.deb; do dpkg-deb -x \"\$d\" $WINE_ROOT; done"
  fi
  [ -x "$WINE_ROOT/usr/lib/wine/wine64" ] && echo "ok: wine tree built" \
    || echo "WARN: $WINE_ROOT/usr/lib/wine/wine64 still missing — see manual steps above"
fi

# wine environment used by every later step (mirrors mt5-launch.sh)
export WINEPREFIX
export WINEARCH=win64
export WINELOADER="$WINE_ROOT/usr/lib/wine/wine64"
export WINESERVER="$WINE_ROOT/usr/lib/wine/wineserver64"
export LD_LIBRARY_PATH="$WINE_ROOT/usr/lib/x86_64-linux-gnu:$WINE_ROOT/usr/lib/i386-linux-gnu:$WINE_ROOT/usr/lib/wine:${LD_LIBRARY_PATH:-}"
export PATH="$WINE_ROOT/usr/bin:$PATH"
export WINEDEBUG=${WINEDEBUG:--all}
export XDG_RUNTIME_DIR=/tmp/xdg-run
export HOME=${MT5_HOME:-/home/z}
export DISPLAY="$MT5_DISPLAY"
mkdir -p "$XDG_RUNTIME_DIR" "$WINEPREFIX"

# ---------------------------------------------------------------- 3. Xvfb
step "3/6 virtual display :$MT5_DISPLAY"
if pgrep -f "Xvfb $MT5_DISPLAY" >/dev/null 2>&1; then
  echo "ok: Xvfb already running"
elif have Xvfb; then
  bash -c "setsid Xvfb $MT5_DISPLAY -screen 0 1280x800x24 -nolisten tcp >/dev/null 2>&1 </dev/null &"
  sleep 1
  pgrep -f "Xvfb $MT5_DISPLAY" >/dev/null && echo "ok: Xvfb started" || echo "WARN: Xvfb did not start"
else
  echo "MISSING Xvfb — MANUAL STEP: install xvfb (apt-get install xvfb as root, or"
  echo "fetch the deb user-space like wine above) and re-run."
fi

# ---------------------------------------------------------------- 4. MT5 install
step "4/6 MetaTrader 5 install into wine prefix"
TERMINAL_EXE="$WINEPREFIX/drive_c/Program Files/MetaTrader 5/terminal64.exe"
if [ -f "$TERMINAL_EXE" ]; then
  echo "ok: terminal already installed"
else
  if [ ! -f "$MT5_SETUP_EXE" ]; then
    echo "downloading MT5 installer (~23 MB)..."
    curl -fL --retry 3 -o "$MT5_SETUP_EXE" "$MT5_SETUP_URL" \
      || echo "WARN: download failed — MANUAL STEP: fetch $MT5_SETUP_URL to $MT5_SETUP_EXE"
  fi
  if [ -f "$MT5_SETUP_EXE" ] && [ -x "$WINELOADER" ]; then
    echo "running silent install (mt5setup.exe /auto) — takes a few minutes..."
    "$WINELOADER" "$MT5_SETUP_EXE" /auto || echo "WARN: installer returned non-zero"
    sleep 5
    pkill -f 'mt5setup' 2>/dev/null
  fi
  [ -f "$TERMINAL_EXE" ] && echo "ok: terminal64.exe installed" || {
    echo "WARN: silent install did not produce terminal64.exe — MANUAL STEP:"
    echo "  run the installer GUI once: $WINELOADER \"$MT5_SETUP_EXE\""
    echo "  (needs Xvfb :$MT5_DISPLAY running), choose the default install dir,"
    echo "  or extract it manually like the original build did (see README.md)."
  }
fi

# ---------------------------------------------------------------- 5. portable config
step "5/6 portable-mode config (login.ini + assistant.ini)"
LOGIN_INI="$WINEPREFIX/drive_c/login.ini"
cat > "$LOGIN_INI" <<EOF
[StartUp]
Login=$MT5_LOGIN
Password=$MT5_PASSWORD
Server=$MT5_SERVER
EOF
echo "ok: $LOGIN_INI"

CONFIG_DIR="$WINEPREFIX/drive_c/Program Files/MetaTrader 5/Config"
ASSISTANT_INI="$CONFIG_DIR/assistant.ini"
if [ ! -f "$ASSISTANT_INI" ]; then
  mkdir -p "$CONFIG_DIR"
  # Enable the built-in MCP bridge on 127.0.0.1:22346. ApiKey: random hex
  # (MT5 accepts/replaces it; the REAL Bearer token must be read from the
  # terminal UI once and saved to mcp_key.txt — sync-mcp-key.sh validates).
  KEYHEX=$(openssl rand -hex 84 2>/dev/null || head -c 84 /dev/urandom | od -An -tx1 | tr -d ' \n')
  printf '\xff\xfe' > "$ASSISTANT_INI"   # UTF-16LE BOM
  printf '[MCP.MetaEditor]\r\nEnable=1\r\nEndpoint=http://127.0.0.1:22345/mcp\r\nApiKey=%s\r\n[MCP.MetaTrader]\r\nEnable=1\r\nEndpoint=http://127.0.0.1:22346/mcp\r\nApiKey=%s\r\n' \
    "$KEYHEX" "$KEYHEX" | iconv -f UTF-8 -t UTF-16LE >> "$ASSISTANT_INI" 2>/dev/null \
    || printf '[MCP.MetaEditor]\r\nEnable=1\r\nEndpoint=http://127.0.0.1:22345/mcp\r\nApiKey=%s\r\n[MCP.MetaTrader]\r\nEnable=1\r\nEndpoint=http://127.0.0.1:22346/mcp\r\nApiKey=%s\r\n' "$KEYHEX" "$KEYHEX" >> "$ASSISTANT_INI"
  echo "ok: created $ASSISTANT_INI (MCP bridge enabled on 127.0.0.1:22346)"
  echo "MANUAL STEP (once, after first launch): read the MCP Bearer token from the"
  echo "terminal UI (Tools -> Options -> MCP / assistant settings) and write it to"
  echo "  $MT5_STACK_DIR/mcp_key.txt"
  echo "sync-mcp-key.sh + status.sh will tell you when auth works."
else
  echo "ok: assistant.ini already present (MCP bridge configured)"
fi

# ---------------------------------------------------------------- 6. watchdog
step "6/6 start the watchdog (ensure-stack.sh)"
# liveness: pid recorded in .watchdog.pid, cross-checked via /proc argv shape
WATCHDOG_PID=""
if [ -f "$SCRIPT_DIR/.watchdog.pid" ]; then
  WDP=$(head -n 1 "$SCRIPT_DIR/.watchdog.pid" 2>/dev/null)
  if [ -n "$WDP" ] && [ -d "/proc/$WDP" ]; then
    WARGV=$(tr '\0' '\n' < "/proc/$WDP/cmdline" 2>/dev/null)
    W1=$(printf '%s\n' "$WARGV" | sed -n 1p)
    W2=$(printf '%s\n' "$WARGV" | sed -n 2p)
    W3=$(printf '%s\n' "$WARGV" | sed -n 3p)
    if [ -n "$W1" ] && [ -n "$W2" ] && [ -z "$W3" ]; then
      case "$W1" in *bash|bash|*sh|sh) case "$W2" in */ensure-stack.sh|ensure-stack.sh) WATCHDOG_PID=$WDP ;; esac ;; esac
    fi
  fi
fi
if [ -n "$WATCHDOG_PID" ]; then
  echo "ok: watchdog already running (pid $WATCHDOG_PID)"
else
  bash -c "setsid bash '$SCRIPT_DIR/ensure-stack.sh' >> '$SCRIPT_DIR/stack.log' 2>&1 </dev/null &"
  echo "ok: watchdog started (detached)"
fi

echo
echo "Done. Verify with:   bun run stack:status   (or bash $SCRIPT_DIR/status.sh)"
echo "Health endpoint:     curl -s ${MARKET_SERVICE_URL:-http://localhost:3003/api/health}"
echo "MCP bridge:          ss -tln | grep 22346"
