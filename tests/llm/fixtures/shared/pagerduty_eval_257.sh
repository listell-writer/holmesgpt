#!/bin/bash
# Shared setup for PagerDuty eval 257 (a + b).
#
# Idempotently seeds a single PagerDuty incident used by both matrix arms:
#   - A verification code in the incident *title*
#   - A verification token in an incident *note*
#
# The eval then asks Holmes to discover both. Static values (not random per
# run) so expected_output can be hardcoded.
#
# Required env vars:
#   PAGERDUTY_USER_API_KEY  — User API token with write permission.
# Optional overrides:
#   PAGERDUTY_EVAL_SERVICE_ID   (default: PUD1YCZ)
#   PAGERDUTY_EVAL_FROM_EMAIL   (default: natan@robusta.dev)
#
# Reentrant: safe to run in parallel with itself across PR runs.
# No after_test — the incident is reused across runs.

set -e

PD_API="https://api.pagerduty.com"
INCIDENT_KEY="HLMS257"
TITLE_CODE="7k9x2m4p"
NOTE_TOKEN="r8v3q1w5"
INCIDENT_TITLE="HOLMES-EVAL-257 verification-code=${TITLE_CODE}"
NOTE_CONTENT="holmes-eval-257 note-token=${NOTE_TOKEN}"
SERVICE_ID="${PAGERDUTY_EVAL_SERVICE_ID:-PUD1YCZ}"
FROM_EMAIL="${PAGERDUTY_EVAL_FROM_EMAIL:-natan@robusta.dev}"

if [ -z "$PAGERDUTY_USER_API_KEY" ]; then
  echo "❌ PAGERDUTY_USER_API_KEY is not set"
  exit 1
fi

AUTH_HEADER="Authorization: Token token=${PAGERDUTY_USER_API_KEY}"
ACCEPT_HEADER="Accept: application/vnd.pagerduty+json;version=2"

pd_get() {
  # pd_get <path> [extra curl args...]
  local path="$1"; shift
  curl -sf -G "${PD_API}${path}" \
    -H "${AUTH_HEADER}" \
    -H "${ACCEPT_HEADER}" \
    "$@"
}

# ── Step 1: look for an existing open incident with our key ──
echo "🔍 Looking for existing incident with incident_key=${INCIDENT_KEY}..."
EXISTING=$(pd_get "/incidents" \
  --data-urlencode "incident_key=${INCIDENT_KEY}" \
  --data-urlencode "statuses[]=triggered" \
  --data-urlencode "statuses[]=acknowledged" \
  --data-urlencode "limit=1" \
  --data-urlencode "service_ids[]=${SERVICE_ID}")

INC_ID=$(echo "$EXISTING" | python3 -c '
import sys, json
d = json.load(sys.stdin)
incs = d.get("incidents") or []
print(incs[0]["id"] if incs else "")
')

if [ -n "$INC_ID" ]; then
  echo "♻️  Reusing incident ${INC_ID}"

  # If an existing incident has a stale title from a previous design, update it.
  CURRENT_TITLE=$(echo "$EXISTING" | python3 -c '
import sys, json
d = json.load(sys.stdin)
print(d["incidents"][0].get("title", ""))
')
  if [ "$CURRENT_TITLE" != "$INCIDENT_TITLE" ]; then
    echo "✏️  Updating title (was: '${CURRENT_TITLE}')..."
    curl -sf -X PUT "${PD_API}/incidents/${INC_ID}" \
      -H "${AUTH_HEADER}" \
      -H "${ACCEPT_HEADER}" \
      -H "Content-Type: application/json" \
      -H "From: ${FROM_EMAIL}" \
      -d "$(python3 -c "
import json
print(json.dumps({'incident': {'type': 'incident_reference', 'title': '${INCIDENT_TITLE}'}}))
")" > /dev/null
  fi
else
  echo "➕ Creating new incident..."
  CREATE_BODY=$(python3 -c "
import json
print(json.dumps({
  'incident': {
    'type': 'incident',
    'title': '${INCIDENT_TITLE}',
    'service': {'id': '${SERVICE_ID}', 'type': 'service_reference'},
    'incident_key': '${INCIDENT_KEY}',
    'body': {
      'type': 'incident_body',
      'details': 'Seeded by HolmesGPT eval 257. Do not resolve — used to test read-path of the PagerDuty integration.',
    },
  }
}))
")

  CREATE_RESP=$(curl -s -w "\n%{http_code}" -X POST "${PD_API}/incidents" \
    -H "${AUTH_HEADER}" \
    -H "${ACCEPT_HEADER}" \
    -H "Content-Type: application/json" \
    -H "From: ${FROM_EMAIL}" \
    -d "${CREATE_BODY}")
  CREATE_HTTP=$(echo "${CREATE_RESP}" | tail -n1)
  CREATE_JSON=$(echo "${CREATE_RESP}" | sed '$d')

  if [ "$CREATE_HTTP" != "201" ] && [ "$CREATE_HTTP" != "202" ] && [ "$CREATE_HTTP" != "200" ]; then
    echo "❌ Failed to create incident (HTTP ${CREATE_HTTP}): ${CREATE_JSON}"
    exit 1
  fi
  INC_ID=$(echo "${CREATE_JSON}" | python3 -c 'import sys, json; print(json.load(sys.stdin)["incident"]["id"])')
  echo "✅ Created incident ${INC_ID}"
fi

# ── Step 2: ensure a note with our token exists ──
echo "🔍 Checking existing notes for token=${NOTE_TOKEN}..."
NOTES=$(pd_get "/incidents/${INC_ID}/notes")
if echo "$NOTES" | grep -q "${NOTE_TOKEN}"; then
  echo "♻️  Note already contains required token"
else
  echo "➕ Adding note..."
  NOTE_BODY=$(python3 -c "
import json
print(json.dumps({'note': {'content': '${NOTE_CONTENT}'}}))
")
  curl -sf -X POST "${PD_API}/incidents/${INC_ID}/notes" \
    -H "${AUTH_HEADER}" \
    -H "${ACCEPT_HEADER}" \
    -H "Content-Type: application/json" \
    -H "From: ${FROM_EMAIL}" \
    -d "${NOTE_BODY}" > /dev/null
  echo "✅ Note added"
fi

# ── Step 3: verify both markers are readable via the API ──
echo "🔎 Verifying setup..."
VERIFY_INC=$(pd_get "/incidents/${INC_ID}")
if ! echo "$VERIFY_INC" | grep -q "${TITLE_CODE}"; then
  echo "❌ Verification failed: title code '${TITLE_CODE}' not found on incident ${INC_ID}"
  echo "Response: ${VERIFY_INC}"
  exit 1
fi
VERIFY_NOTES=$(pd_get "/incidents/${INC_ID}/notes")
if ! echo "$VERIFY_NOTES" | grep -q "${NOTE_TOKEN}"; then
  echo "❌ Verification failed: note token '${NOTE_TOKEN}' not found on incident ${INC_ID}"
  echo "Response: ${VERIFY_NOTES}"
  exit 1
fi

echo "✅ Setup complete — incident=${INC_ID} title_code=${TITLE_CODE} note_token=${NOTE_TOKEN}"
