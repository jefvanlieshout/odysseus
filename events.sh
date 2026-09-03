#!/usr/bin/env bash
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [[ -z "$ROOT" || ! -f "$ROOT/ASSISTANT_VERSION" ]]; then
  echo "Run this from inside the consolidated assistant/Odysseus repository." >&2
  exit 2
fi

EVENTS_DIR="$ROOT/assistant/events"
TELEGRAM_DIR="$ROOT/assistant/telegram"
EVENTS_ENV="$EVENTS_DIR/.env"
TELEGRAM_ENV="$TELEGRAM_DIR/.env"
COMPOSE_FILE="$ROOT/docker-compose.assistant.yml"
EVENTS_URL="${EVENTS_URL:-http://127.0.0.1:8780}"

compose() {
  docker compose -f "$COMPOSE_FILE" "$@"
}

env_get() {
  local file="$1" key="$2"
  python3 - "$file" "$key" <<'PY'
from pathlib import Path
import sys
p = Path(sys.argv[1])
key = sys.argv[2]
if not p.is_file():
    raise SystemExit(0)
for raw in p.read_text(encoding="utf-8").splitlines():
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, v = line.split("=", 1)
    if k.strip() == key:
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in {'"', "'"}:
            v = v[1:-1]
        print(v)
        break
PY
}

ensure_config() {
  if [[ ! -f "$TELEGRAM_ENV" ]]; then
    echo "Missing $TELEGRAM_ENV" >&2
    echo "The Telegram bridge must be configured before notifications can start." >&2
    exit 1
  fi

  local token
  token="$(env_get "$TELEGRAM_ENV" TELEGRAM_BOT_TOKEN)"
  if [[ -z "$token" ]]; then
    echo "TELEGRAM_BOT_TOKEN is missing from $TELEGRAM_ENV" >&2
    exit 1
  fi

  if [[ ! -f "$EVENTS_ENV" ]]; then
    local key
    key="$(python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(36))
PY
)"
    cat > "$EVENTS_ENV" <<EOF_ENV
# Generated locally by events.sh. This file is intentionally ignored by Git.
EVENTS_API_KEY=$key
EVENTS_DATA_DIR=/data
EVENTS_DEFAULT_COOLDOWN_SECONDS=600
EVENTS_MAX_MESSAGE_CHARS=3500
LOG_LEVEL=INFO
TELEGRAM_TIMEOUT_SECONDS=20

# Optional override. Leave blank for one private allowlisted Telegram user.
TELEGRAM_CHAT_ID=
EOF_ENV
    chmod 600 "$EVENTS_ENV"
    echo "Created private event-service config: assistant/events/.env"
  fi

  local api_key
  api_key="$(env_get "$EVENTS_ENV" EVENTS_API_KEY)"
  if [[ -z "$api_key" || "$api_key" == replace-* ]]; then
    echo "EVENTS_API_KEY is missing/invalid in $EVENTS_ENV" >&2
    exit 1
  fi
}

health_json() {
  curl --fail --silent --show-error "$EVENTS_URL/health"
}

wait_healthy() {
  local i
  for i in $(seq 1 30); do
    if health_json >/tmp/assistant-events-health.$$ 2>/dev/null; then
      cat /tmp/assistant-events-health.$$
      rm -f /tmp/assistant-events-health.$$
      return 0
    fi
    sleep 1
  done
  rm -f /tmp/assistant-events-health.$$
  return 1
}

show_health() {
  local json
  json="$(health_json)"
  printf '%s\n' "$json" | python3 -m json.tool
  if ! printf '%s' "$json" | python3 -c 'import json,sys; d=json.load(sys.stdin); raise SystemExit(0 if "telegram" in d.get("enabled_channels", []) else 1)'; then
    echo
    echo "WARNING: event service is healthy, but Telegram notifications are not enabled." >&2
    echo "If you have multiple allowlisted users or want a group chat, set TELEGRAM_CHAT_ID in assistant/events/.env." >&2
    return 1
  fi
}

case "${1:-}" in
  setup)
    ensure_config
    echo "Event-service configuration is ready."
    ;;

  start)
    ensure_config
    echo "Building/starting assistant-events..."
    compose up -d --build assistant-events
    if ! wait_healthy >/tmp/assistant-events-ready.$$; then
      echo "Event service did not become healthy. Recent logs:" >&2
      compose logs --tail=100 assistant-events >&2 || true
      rm -f /tmp/assistant-events-ready.$$
      exit 1
    fi
    rm -f /tmp/assistant-events-ready.$$
    echo
    echo "assistant-events is healthy."
    show_health
    ;;

  stop)
    compose stop assistant-events
    ;;

  status)
    compose ps assistant-events
    echo
    show_health
    ;;

  test)
    ensure_config
    if ! health_json >/dev/null 2>&1; then
      echo "assistant-events is not running. Start it with: ./events.sh start" >&2
      exit 1
    fi
    api_key="$(env_get "$EVENTS_ENV" EVENTS_API_KEY)"
    EVENTS_API_KEY="$api_key" EVENTS_URL="$EVENTS_URL" "$EVENTS_DIR/test_event.sh"
    ;;

  logs)
    compose logs -f --tail=150 assistant-events
    ;;

  *)
    cat <<'EOF_HELP'
Usage: ./events.sh COMMAND

Commands:
  start   Create local config if needed, build/start service, verify health
  test    Send a real manual notification through Telegram
  status  Show container + event-service health
  logs    Follow recent event-service logs
  stop    Stop the event service
  setup   Only create/check local private config
EOF_HELP
    exit 2
    ;;
esac
