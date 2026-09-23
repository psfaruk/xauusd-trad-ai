#!/bin/bash
# D-054 single-call API e2e: prove the auto-trade pipeline actually places
# pending orders and they are VISIBLE, and the money window anchors on real
# equity (typed balance deliberately WRONG vs plane equity — old code would
# disarm on the first signal with zero orders).
set -uo pipefail
ROOT=/home/z/my-project/xauusd-trad-ai
API=http://127.0.0.1:8000
pkill -9 -f "mock_supabase.py" 2>/dev/null; pkill -9 -f "uvicorn app.main" 2>/dev/null; sleep 1
sleep 1

# 1) stack: mock supabase + backend (DATA_SOURCE=mock, deterministic market)
setsid /home/z/.venv/bin/python "$ROOT/scripts/mock_supabase.py" \
  > "$ROOT/run/mock-supabase.log" 2>&1 < /dev/null &
sleep 2
cd "$ROOT/backend"
setsid env DATA_SOURCE=mock SUPABASE_URL=http://127.0.0.1:8090 \
  SUPABASE_ANON_KEY=test ADMIN_EMAILS=trader@example.com ALLOW_DEMO=1 \
  /home/z/.venv/bin/python -m uvicorn app.main:app \
  --host 127.0.0.1 --port 8000 \
  > "$ROOT/run/uvicorn.log" 2>&1 < /dev/null &
sleep 8
echo "health: $(curl -s -m 5 $API/api/health | head -c 100)"

# 2) login (admin) — fresh token every run
TOKEN=$(curl -s -m 10 -X POST "http://127.0.0.1:8090/auth/v1/token?grant_type=password" \
  -H "apikey: test" -H "Content-Type: application/json" \
  -d '{"email":"trader@example.com","password":"password123"}' \
  | /home/z/.venv/bin/python -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
echo "token: ${TOKEN:0:25}..."

# 3) money window with a DELIBERATELY WRONG typed balance (old-bug repro):
#    plane equity is 10 000 (fresh practice plane); typed balance 8 000 +
#    target +50 USD would instantly "complete" (equity - anchor = +2000)
#    under the OLD anchor logic -> disarm with zero orders.
curl -s -m 10 -X PUT "$API/api/trading/settings" -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -d '{
    "day_start_balance": 8000, "daily_loss_usd": 40, "daily_profit_usd": 50,
    "fixed_lot": 0.02, "max_trades_per_day": 6, "max_positions": 3,
    "risk_mode": "fixed", "daily_max_loss_pct": 100
  }' | head -c 220; echo

# 4) arm auto-trade (admin -> institution scope in mock = McpAutoTrader off,
#    but the route also arms the caller's practice plane path; non-admin
#    arm goes to the practice plane. We arm as the admin's own account via
#    the trading route to exercise set_auto_trade + the equity anchor.)
ARM=$(curl -s -m 10 -X POST "$API/api/trading/auto-trade" -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -d '{"enabled": true}')
echo "arm: $ARM" | head -c 220; echo

# 5) wait for the mock engine to mint signals and relay them to the armed
#    plane (mock market: M1 bars close every few seconds)
PEND=0; POS=0; SIGS=0
for i in $(seq 1 60); do
  sleep 5
  RESP=$(curl -s -m 10 "$API/api/trading/positions" -H "Authorization: Bearer $TOKEN")
  PEND=$(echo "$RESP" | /home/z/.venv/bin/python -c "import sys,json;d=json.load(sys.stdin);print(len(d.get('pending',[])))" 2>/dev/null || echo 0)
  POS=$(echo "$RESP" | /home/z/.venv/bin/python -c "import sys,json;d=json.load(sys.stdin);print(len(d.get('positions',[])))" 2>/dev/null || echo 0)
  SIGS=$(curl -s -m 10 "$API/api/signals?limit=20" -H "Authorization: Bearer $TOKEN" \
    | /home/z/.venv/bin/python -c "import sys,json;print(json.load(sys.stdin)['count'])" 2>/dev/null || echo 0)
  echo "t=$((i*5))s signals=$SIGS pending=$PEND positions=$POS"
  if [ "$PEND" -gt 0 ] || [ "$POS" -gt 0 ]; then break; fi
done

# 6) verdicts
echo "=== verdicts ==="
ST=$(curl -s -m 10 "$API/api/mt5/auto-trade" -H "Authorization: Bearer $TOKEN")
echo "auto-trade status: $(echo "$ST" | /home/z/.venv/bin/python -c "
import sys, json
d = json.load(sys.stdin)
print('armed=', d.get('armed'), '| why=', (d.get('why') or {}).get('code'), '| skip=', d.get('last_skip_reason'))")"
echo "$ST" | /home/z/.venv/bin/python -c "
import sys, json
d = json.load(sys.stdin)
acc = d.get('account') or {}
print('plane: connected=', acc.get('connected'), 'balance=', acc.get('balance'), 'equity=', acc.get('equity'))"
RESP=$(curl -s -m 10 "$API/api/trading/positions" -H "Authorization: Bearer $TOKEN")
echo "$RESP" | /home/z/.venv/bin/python -c "
import sys, json
d = json.load(sys.stdin)
print('pending orders:', json.dumps(d.get('pending', []), indent=1)[:400])
print('positions:', len(d.get('positions', [])))"
curl -s -m 10 "$API/api/signals?limit=5" -H "Authorization: Bearer $TOKEN" | /home/z/.venv/bin/python -c "
import sys, json
rows = json.load(sys.stdin)['signals']
print('recent signals:', [(r['direction'], r.get('entry_type'), r['status']) for r in rows[:5]])"

# 7) cleanup
pkill -9 -f "mock_supabase.py" 2>/dev/null; pkill -9 -f "uvicorn app.main" 2>/dev/null; sleep 1
echo "e2e done"
