# mt5-linux — the "Linux OS" for MetaTrader 5

Everything needed to run a real MetaTrader 5 terminal (and the live-data stack
behind it) on a headless Linux box, and to keep it running **forever** — the
sandbox resets periodically and kills processes; this stack brings itself
back.

```
                ┌─────────────────────────── one Linux box, no root ───────────────────────────┐
                │                                                                              │
  Xvfb :99  ──▶ terminal64.exe   (user-space Wine, /portable, CLI auto-login flags)            │
                │       │  built-in MCP server (JSON-RPC over HTTP)                            │
                │       ▼                                                                       │
                │  127.0.0.1:22346/mcp  ── Bearer key (mcp_key.txt) ──▶ market-service :3003    │
                │                                                        │                    │
                │                     bun --hot src/index.ts            ▼                    │
                │                                          Next.js app (bun run dev)        │
                │                                                                              │
                │  ensure-stack.sh  ← THE WATCHDOG: loops every 30s and repairs every layer    │
                └──────────────────────────────────────────────────────────────────────────────┘
```

## Files

| file | role |
|---|---|
| `mt5-launch.sh` | launches `terminal64.exe` under user-space wine on Xvfb with **CLI login flags** (`/portable /login=… /password=… /server=…`) — this is the permanent fix for Exness wiping saved passwords. Writes `terminal.pid` (the exec'd wine process *becomes* terminal64.exe, so the pidfile is always the real terminal PID). `MT5_DRY_RUN=1` prints the resolved command instead of executing. |
| `ensure-stack.sh` | **the watchdog** — infinite loop (default 30s): Xvfb → terminal → MCP :22346 (90s grace, hung-terminal recovery) → key sync → market-service :3003 (restart only if the HTTP call fails; `degraded` is fine, the engine self-heals via 60s retries). Appends one compact status line per tick to `stack.log`. |
| `sync-mcp-key.sh` | extracts the ApiKey from the terminal's `Config/assistant.ini` (UTF-16LE) and syncs it into `mcp_key.txt` — **validated**: it never clobbers a key that still authenticates (see "MCP key" below). market-service hot-reloads the key file within 60s, no restart. |
| `dev-stack.sh` | `dev` hook — starts the watchdog detached (only if not already running), runs one key sync, **returns instantly** (never blocks `next dev`). |
| `status.sh` | one-shot passive health report (processes, MCP port + live auth probe, market-service health, watchdog, last `stack.log` lines). `bun run stack:status`. |
| `setup.sh` | best-effort **from-scratch rebuild**: user-space wine tree from Debian debs (`vendor/fetch_pkgs.py`, `vendor/mt5-resolve-i386.py`), Xvfb, silent `mt5setup.exe /auto` install, `login.ini` + `assistant.ini`, then hands over to the watchdog. Prints exact manual steps where automation can't guarantee success. |
| `mt5.env` | credentials — **gitignored**. `mt5.env.example` is the committed template. |
| `vendor/` | copies of the two user-space package resolvers (env-overridable via `MT5_RESTORE_DIR`). |

## Quick start (already-built machine)

```bash
cp mt5-linux/mt5.env.example mt5-linux/mt5.env   # fill in LOGIN/PASSWORD/SERVER
bash mt5-linux/setup.sh                          # verifies each layer, fills gaps
bun run dev                                      # watchdog auto-starts with dev
```

Manual watchdog control:

```bash
bun run stack            # foreground watchdog (Ctrl-C to stop)
bun run stack:status     # one-shot health report
# or start it detached exactly like dev-stack.sh does:
bash -c 'setsid bash mt5-linux/ensure-stack.sh >> mt5-linux/stack.log 2>&1 < /dev/null &'
```

## How the pieces fit

1. **Xvfb :99** — MT5 is a GUI app; it needs *a* display even headless. Started
   as `Xvfb :99 -screen 0 1280x800x24 -nolisten tcp`, detached via setsid.
2. **User-space Wine** — no root, no system wine. A tree at
   `$WINE_ROOT` (`/home/z/mt5-restore/wine-root`) contains wine64 +
   wineserver64 + the full i386 dependency closure, extracted from Debian
   `.deb`s with `dpkg-deb -x`. The launcher exports exactly:
   `WINEPREFIX, WINEARCH=win64, WINELOADER, WINESERVER,
   LD_LIBRARY_PATH=<wine-root amd64>:<wine-root i386>:<wine-root wine>,
   WINEDEBUG=-all, XDG_RUNTIME_DIR=/tmp/xdg-run, HOME, DISPLAY`.
3. **Portable MT5** — `/portable` keeps all data inside
   `$MT5_STACK_DIR/wine-prefix/drive_c/Program Files/MetaTrader 5`, so the
   whole stack is one directory tree.
4. **CLI auto-login** — see troubleshooting #1.
5. **MCP bridge** — the terminal's built-in MCP server listens on
   `127.0.0.1:22346` (JSON-RPC over HTTP, `Authorization: Bearer <key>`).
   Enabled via `Config/assistant.ini`.
6. **market-service** (`mini-services/market-service`) — polls quotes/history
   from MCP, serves REST + socket.io on :3003. Started with
   `bun --hot src/index.ts` via the **intermediate-exit double-fork**
   (`bash -c 'cd … && setsid bun … >> service.log 2>&1 < /dev/null &'`)
   because plain nohup/setsid children die at tool-session cleanup.
7. **Watchdog** — `ensure-stack.sh`, same double-fork pattern, one compact
   audit line per tick in `stack.log`:
   `2026-09-27T23:12:00Z xvfb:ok term:ok(27128) mcp:ok svc:ok key:ok`

## From-scratch rebuild (what setup.sh automates)

1. `mkdir -p /home/z/mt5-restore/{debs,debs-i386} /home/z/mt5-stack`
2. amd64 wine: `python3 mt5-linux/vendor/fetch_pkgs.py wine wine64 libwine`
   (resolves + `apt-get download`s the closure into `debs/`)
3. i386 side (wine needs the 32-bit loader):
   `curl -fsSL http://deb.debian.org/debian/dists/stable/main/binary-i386/Packages.gz | gunzip > /tmp/Packages-i386`
   `python3 mt5-linux/vendor/mt5-resolve-i386.py libwine:i386 wine32:i386`
4. `for d in /home/z/mt5-restore/debs/*.deb /home/z/mt5-restore/debs-i386/*.deb; do dpkg-deb -x "$d" /home/z/mt5-restore/wine-root; done`
5. Download the terminal:
   `curl -fL -o /home/z/mt5-restore/mt5setup.exe https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe`
6. Silent install into the prefix (Xvfb :99 must be running):
   with the wine env above — `"$WINE_ROOT/usr/lib/wine/wine64" /home/z/mt5-restore/mt5setup.exe /auto`
7. `drive_c/login.ini` with `[StartUp] Login/Password/Server` (setup.sh writes
   it from `mt5.env`), `Config/assistant.ini` enabling the MCP bridge on
   `127.0.0.1:22346`.
8. `bash mt5-linux/setup.sh` again — it verifies every layer and starts the
   watchdog.

## Env vars

| var | default | used by |
|---|---|---|
| `MT5_LOGIN` / `MT5_PASSWORD` / `MT5_SERVER` | *(mt5.env)* | launcher (CLI flags), setup.sh (login.ini) |
| `MT5_STACK_DIR` | `/home/z/mt5-stack` | all (wine prefix, key file) |
| `WINE_ROOT` | `/home/z/mt5-restore/wine-root` | launcher, setup.sh |
| `MT5_DISPLAY` | `:99` | launcher, watchdog, setup.sh |
| `MT5_MCP_PORT` | `22346` | watchdog, status.sh |
| `MT5_MCP_URL` | `http://127.0.0.1:22346/mcp` | sync-mcp-key.sh, status.sh |
| `MT5_MCP_KEY_FILE` | `$MT5_STACK_DIR/mcp_key.txt` | sync-mcp-key.sh, status.sh (market-service reads it too) |
| `MT5_ASSISTANT_INI` | `…/Config/assistant.ini` | sync-mcp-key.sh |
| `STACK_WATCH_INTERVAL` | `30` (min 5) | watchdog |
| `MT5_MCP_GRACE` | `90` | watchdog (seconds before a fresh terminal must expose MCP) |
| `MARKET_SERVICE_URL` | `http://localhost:3003/api/health` | watchdog, status.sh |
| `MT5_RESTORE_DIR` | `/home/z/mt5-restore` | setup.sh + vendor resolvers |
| `MT5_DRY_RUN` | – | launcher (print, don't exec) |

## Troubleshooting

### 1. "Accounts deleted due security reason" (Exness wipes the saved password)
MT5 in portable mode periodically drops saved credentials for this broker.
**Fix (already built in):** the launcher passes CLI login flags on every start
— `/portable /login=$MT5_LOGIN /password=$MT5_PASSWORD /server=$MT5_SERVER` —
so every relaunch re-authenticates. `login.ini` is kept as a belt-and-braces
fallback. If login still fails, check `mt5-linux/launch.log` and that
`mt5.env` has current credentials.

### 2. MCP JSON-RPC quirk: notifications carry NO id and NO params
The strict MT5 MCP server **rejects** `notifications/initialized` if it
carries an `id` or `params` — the session then stays uninitialized and every
`tools/call` fails. Send exactly `{"jsonrpc":"2.0","method":"notifications/initialized"}`.

### 3. MCP key / 401s — and why sync-mcp-key.sh *validates*
`market-service` authenticates with `Authorization: Bearer <mcp_key.txt
content>`. Empirically (build 6231): the `ApiKey` hex inside
`Config/assistant.ini` (168 hex chars, UTF-16LE file — read it with
`iconv -f UTF-16LE -t UTF-8`) gets **HTTP 401**, while the 42-char token in
`mcp_key.txt` gets **200** — the ini hex is *not* the Bearer token. Therefore
`sync-mcp-key.sh` only writes an extracted candidate when the current key no
longer authenticates **and** the candidate does. If you ever see
`key:warn` in `stack.log` / `mcp-key auth FAILED` in `status.sh`:
read the token from the terminal UI (MCP/assistant settings) and write it to
`$MT5_MCP_KEY_FILE` — market-service picks it up within 60s, **no restart**.
The MCP server also caches its key in memory, so a briefly missing file is
harmless.

### 4. market-service `degraded:true` but answering
Normal during a terminal/MCP outage — the engine retries every 60s and
self-heals (symbols re-ensured, history reloaded). The watchdog deliberately
does **not** restart it (a restart would drop warm state). Only a failed
`curl /api/health` (connection refused) triggers a relaunch.

### 5. Terminal up, MCP :22346 missing after 90s
Watchdog detects a hung terminal: kills `terminal64.exe` + `wineserver64`,
relaunches with CLI auto-login. Expect one `mcp:recovering` line in
`stack.log`.

### 6. Everything died (sandbox reset)
Just run `bun run dev` (the hook restarts the watchdog) or
`bash mt5-linux/setup.sh`. Or nothing at all — if the watchdog itself
survived, it rebuilds the whole chain within one tick.

## Verification

```bash
bun run stack:status                                   # full report (pretty)
curl -s http://localhost:3003/api/health               # degraded:false, terminal_connected:true
ss -tlnp | grep 22346                                  # MCP bridge listening
tail -5 mt5-linux/stack.log                            # per-tick audit trail
pgrep -af 'ensure-stack|terminal64|wineserver|Xvfb :99'
```

`stack.log` line format:
`<utc-timestamp> xvfb:<ok|down> term:<ok(pid)|down> mcp:<ok|grace|down|recovering> svc:<ok|degraded|relaunched|down> key:<ok|warn>`
