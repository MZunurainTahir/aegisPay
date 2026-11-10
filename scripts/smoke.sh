#!/usr/bin/env bash
# scripts/smoke.sh
# -----------------
# Smoke test: start gateway (mocked), run 3 refund scenarios, verify responses.
#
# Usage:
#   chmod +x scripts/smoke.sh
#   ./scripts/smoke.sh
#
# Requires: uvicorn, curl, jq
# Sets DB_PATH to a temp file so it doesn't touch aegis.db.

set -e

PASS=0
FAIL=0
PORT=18765
GW="http://localhost:${PORT}"
DB_FILE=$(mktemp /tmp/aegis_smoke_XXXXXX.db)

echo "── Aegis Gateway Smoke Test ─────────────────────────────────────"
echo "DB: ${DB_FILE}"

# Start gateway in background
export DB_PATH="${DB_FILE}"
export PAYPAL_CLIENT_ID="smoke_test_client"
export PAYPAL_SECRET="smoke_test_secret"
uvicorn gateway.main:app --port ${PORT} --log-level error &
GW_PID=$!
trap "kill ${GW_PID} 2>/dev/null; rm -f ${DB_FILE}" EXIT

# Wait for startup
sleep 3

check() {
  local name="$1"
  local expected="$2"
  local actual="$3"
  if echo "${actual}" | grep -q "\"${expected}\""; then
    echo "  ✅ PASS: ${name}"
    PASS=$((PASS+1))
  else
    echo "  ❌ FAIL: ${name}"
    echo "     Expected status: ${expected}"
    echo "     Got: ${actual}"
    FAIL=$((FAIL+1))
  fi
}

# Test 1: Health check
echo
echo "1. Health check…"
HEALTH=$(curl -s "${GW}/health")
check "health endpoint" "ok" "${HEALTH}"

# Test 2: $20 refund → EXECUTED (PayPal will fail in smoke test → FAILED status is OK,
#         the important thing is the gateway accepted the request and evaluated policy)
echo
echo "2. \$20 refund (should ALLOW via policy, may FAIL on PayPal call)…"
R20=$(curl -s -X POST "${GW}/act" \
  -H 'content-type: application/json' \
  -d '{"action":"refund","params":{"capture_id":"smoke_cap_20","amount":20},"amount":20,"customer":"alice"}')
# In smoke test without real PayPal, it will be FAILED (policy ALLOW, PayPal fails)
# We check that it's NOT BLOCKED (policy allowed it)
if echo "${R20}" | grep -q '"BLOCKED"'; then
  echo "  ❌ FAIL: \$20 refund was BLOCKED (should be ALLOW by policy)"
  FAIL=$((FAIL+1))
else
  echo "  ✅ PASS: \$20 refund was not blocked by policy (status: $(echo ${R20} | python3 -c 'import sys,json;print(json.load(sys.stdin).get(\"status\",\"?\"))' 2>/dev/null || echo '?'))"
  PASS=$((PASS+1))
fi

# Test 3: $60 refund → PENDING_APPROVAL
echo
echo "3. \$60 refund (should be PENDING_APPROVAL)…"
R60=$(curl -s -X POST "${GW}/act" \
  -H 'content-type: application/json' \
  -d '{"action":"refund","params":{"capture_id":"smoke_cap_60","amount":60},"amount":60,"customer":"bob"}')
check "\$60 refund pending" "PENDING_APPROVAL" "${R60}"

# Test 4: $500 refund → BLOCKED
echo
echo "4. \$500 refund (should be BLOCKED — over per-txn cap)…"
R500=$(curl -s -X POST "${GW}/act" \
  -H 'content-type: application/json' \
  -d '{"action":"refund","params":{"capture_id":"smoke_cap_500","amount":500},"amount":500,"customer":"carol"}')
check "\$500 refund blocked" "BLOCKED" "${R500}"

# Test 5: Kill switch
echo
echo "5. Kill switch (activate → verify $10 refund is BLOCKED)…"
curl -s -X POST "${GW}/kill?on=true" > /dev/null
RKILL=$(curl -s -X POST "${GW}/act" \
  -H 'content-type: application/json' \
  -d '{"action":"refund","params":{"capture_id":"smoke_cap_kill","amount":10},"amount":10,"customer":"dave"}')
check "kill switch blocks" "BLOCKED" "${RKILL}"
curl -s -X POST "${GW}/kill?on=false" > /dev/null

# Test 6: Audit chain verify
echo
echo "6. Audit chain verification…"
VERIFY=$(curl -s "${GW}/audit/verify")
check "audit chain valid" "true" "${VERIFY}"

# Summary
echo
echo "── Results ──────────────────────────────────────────────────────"
echo "  PASS: ${PASS}  FAIL: ${FAIL}"
if [ "${FAIL}" -eq 0 ]; then
  echo "  ✅ ALL TESTS PASSED"
  exit 0
else
  echo "  ❌ SOME TESTS FAILED"
  exit 1
fi

