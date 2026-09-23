#!/bin/bash
# D-053 payload contract check: state fields + setup presence + signals
set -uo pipefail
ROOT=/home/z/my-project/xauusd-trad-ai
# stop the open browser page from polluting the auth cache with stale polls
agent-browser open about:blank > /dev/null 2>&1 || true
fuser -k 8090/tcp 8000/tcp 2>/dev/null || true
sleep 1
setsid /home/z/.venv/bin/python "$ROOT/scripts/mock_supabase.py" > "$ROOT/run/mock-supabase.log" 2>&1 < /dev/null &
sleep 2
setsid env DATA_SOURCE=mock SUPABASE_URL=http://127.0.0.1:8090 \
  SUPABASE_ANON_KEY=test ADMIN_EMAILS=trader@example.com ALLOW_DEMO=1 \
  /home/z/.venv/bin/python -m uvicorn app.main:app \
  --app-dir "$ROOT/backend" --host 127.0.0.1 --port 8000 \
  > "$ROOT/run/uvicorn.log" 2>&1 < /dev/null &
sleep 8
TOKEN=$(curl -s -m 5 -X POST "http://127.0.0.1:8090/auth/v1/token?grant_type=password" \
  -H "apikey: test" -H "Content-Type: application/json" \
  -d '{"email":"trader@example.com","password":"x"}' \
  | /home/z/.venv/bin/python -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
echo "token: ${TOKEN:0:24}..."
# let the mock engine warm up AND the 60s auth-verdict cache expire
# (a stale browser poll may have cached a rejection for this token-hash)
sleep 70
curl -s -m 15 -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:8000/api/analysis?symbol=XAUUSD" \
  -o "$ROOT/run/analysis.json" -w "analysis fetch: %{http_code}\n"
curl -s -m 10 -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:8000/api/signals?limit=10" \
  -o "$ROOT/run/signals.json" -w "signals fetch: %{http_code}\n"
/home/z/.venv/bin/python - <<'PY'
import json
d = json.load(open('/home/z/my-project/xauusd-trad-ai/run/analysis.json'))
total_state = 0
for tf, marks in (d.get('drawings_by_tf') or {}).items():
    kinds = {}
    for m in marks:
        kinds[m['kind']] = kinds.get(m['kind'], 0) + 1
    states = [(m['kind'], m.get('state')) for m in marks if m['kind'] in ('zone', 'trendline')]
    total_state += len(states)
    print(f"{tf}: {kinds}  setup={bool(kinds.get('setup'))}")
    if states:
        print("   fade-states:", states)
print("marks with explicit state field:", total_state)
sig = json.load(open('/home/z/my-project/xauusd-trad-ai/run/signals.json'))
sigs = sig.get('signals') or []
print("signals:", len(sigs), [f"{s['direction']}@{s['entry']}" for s in sigs[:5]])
PY
fuser -k 8090/tcp 8000/tcp 2>/dev/null || true
