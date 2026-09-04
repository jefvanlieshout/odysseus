#!/usr/bin/env bash
set -euo pipefail

ROOT="${ODYSSEUS_ROOT:-$HOME/odysseus/odysseus}"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/odysseus-brain-shadow"
KEY_FILE="$STATE_DIR/api-key"
OVERLAY="$ROOT/docker-compose.brain-autonomous.yml"

cd "$ROOT"
[[ -r "$KEY_FILE" ]] || {
  echo "ERROR: missing Brain API key at $KEY_FILE" >&2
  exit 1
}
export JARVIS_BRAIN_API_KEY
JARVIS_BRAIN_API_KEY="$(cat "$KEY_FILE")"

docker compose \
  -f docker-compose.yml \
  -f docker/gpu.nvidia.yml \
  -f "$OVERLAY" \
  up -d --build jarvis-brain odysseus jarvis-brain-worker

echo
echo "Autonomous Brain services started."
echo "Worker logs:"
docker logs --tail 20 jarvis-brain-semantic-worker 2>&1 || true
