#!/usr/bin/env bash
set -euo pipefail
: "${EVENTS_API_KEY:?export EVENTS_API_KEY first}"
EVENTS_URL="${EVENTS_URL:-http://127.0.0.1:8780}"

curl --fail-with-body -sS \
  -H "Authorization: Bearer ${EVENTS_API_KEY}" \
  -H 'Content-Type: application/json' \
  -d '{
    "source": "manual-test",
    "event_type": "outbound_notification_test",
    "severity": "warning",
    "state": "active",
    "title": "Outbound notifications are working",
    "message": "Hello Jef. This is one active test condition. Repeating the same test should now be stored but not spam Telegram.",
    "target": "telegram",
    "actor_id": "system",
    "agent_id": "main",
    "fingerprint": "manual-test:telegram:v2",
    "notification_key": "active-v1"
  }' \
  "${EVENTS_URL}/events"
echo
