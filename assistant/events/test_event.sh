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
    "severity": "info",
    "state": "info",
    "title": "Outbound notifications are working",
    "message": "Hello Jef. The event system delivered this without asking Qwen to do anything.",
    "target": "telegram",
    "actor_id": "system",
    "agent_id": "main",
    "fingerprint": "manual-test:telegram:v1",
    "cooldown_seconds": 0
  }' \
  "${EVENTS_URL}/events"

echo
