#!/bin/bash
# D-053 single-call visual verification: servers + browser in ONE bash call
# (the sandbox reaps detached python processes between calls).
set -uo pipefail
ROOT=/home/z/my-project/xauusd-trad-ai
mkdir -p "$ROOT/run/shots"
# park the browser (its stale-token polls poison the backend auth cache
# for 60s — the deterministic mock token is the SAME string every run)
agent-browser open about:blank > /dev/null 2>&1 || true
fuser -k 8090/tcp 8000/tcp 2>/dev/null || true
sleep 1

# 1) start the stack (dies with this call — fine)
setsid /home/z/.venv/bin/python "$ROOT/scripts/mock_supabase.py" \
  > "$ROOT/run/mock-supabase.log" 2>&1 < /dev/null &
MOCK_PID=$!
sleep 2
setsid env DATA_SOURCE=mock SUPABASE_URL=http://127.0.0.1:8090 \
  SUPABASE_ANON_KEY=test ADMIN_EMAILS=trader@example.com ALLOW_DEMO=1 \
  /home/z/.venv/bin/python -m uvicorn app.main:app \
  --app-dir "$ROOT/backend" --host 127.0.0.1 --port 8000 \
  > "$ROOT/run/uvicorn.log" 2>&1 < /dev/null &
API_PID=$!
sleep 8
echo "health: $(curl -s -m 5 http://127.0.0.1:8000/api/health | head -c 80)"

# 2) login — wipe persisted supabase session FIRST (a stale token 401s
#    against the fresh in-memory mock)
agent-browser open http://127.0.0.1:3001/ > /dev/null 2>&1
sleep 2
agent-browser eval "localStorage.clear(); sessionStorage.clear()" > /dev/null 2>&1
agent-browser open http://127.0.0.1:3001/ > /dev/null 2>&1
sleep 3
agent-browser fill 'input[type="email"]' 'trader@example.com' > /dev/null 2>&1
agent-browser fill 'input[type="password"]' 'password123' > /dev/null 2>&1
agent-browser click 'button[type="submit"]' > /dev/null 2>&1
# 75s: auth-verdict cache (60s TTL) expires AND the mock engine closes
# enough M1 bars to mint real signals for the signal markers
sleep 75
echo "--- after login (token check) ---"
agent-browser eval "fetch('/api/health').then(r=>r.text()).catch(e=>'ERR')" 2>&1 | tail -1

# 3) home tab: full chart + legend — wait until candles AND analysis land
for i in $(seq 1 25); do
  STATE=$(agent-browser eval "(() => { const t = document.querySelector('main')?.innerText ?? ''; const m = t.match(/MARKS\\n(\\d+)/); const loading = t.includes('Loading real'); return (m && +m[1] > 0 && !loading) ? 'ready' : 'wait'; })()" 2>/dev/null | tail -1)
  [ "$STATE" = "ready" ] && break
  sleep 2
done
sleep 3
agent-browser screenshot "$ROOT/run/shots/d053-home.png" 2>&1
agent-browser eval "document.querySelector('main')?.innerText.match(/MARKS\\n\\d+/)?.[0]" 2>&1 | tail -1

# 4) fullscreen the home chart
agent-browser click 'button[aria-label="View chart fullscreen"]' > /dev/null 2>&1
sleep 3
agent-browser screenshot "$ROOT/run/shots/d053-home-fullscreen.png" 2>&1
agent-browser click 'button[aria-label="Exit fullscreen"]' > /dev/null 2>&1
sleep 2

# 5) chart tab: signals variant (JS click — :has-text() is unreliable)
agent-browser eval "document.querySelectorAll('nav[aria-label=\"Main navigation\"] button')[1].click()" > /dev/null 2>&1
# wait for the chart to settle (fresh mount of the signals-variant chart)
for i in $(seq 1 25); do
  LOADING=$(agent-browser eval "document.body.innerText.includes('Loading real') ? '1' : '0'" 2>/dev/null | tail -1)
  [ "$LOADING" = "0" ] && break
  sleep 2
done
sleep 4
agent-browser eval "window.scrollTo({top: 0})" > /dev/null 2>&1
sleep 1
agent-browser screenshot "$ROOT/run/shots/d053-charts.png" 2>&1
agent-browser eval "document.querySelector('main')?.innerText.slice(0, 260)" 2>&1 | head -4

echo "--- done ---"
ls -la "$ROOT/run/shots/"
kill $MOCK_PID $API_PID 2>/dev/null
