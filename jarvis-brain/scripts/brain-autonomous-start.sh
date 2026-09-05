#!/usr/bin/env bash
set -euo pipefail

ROOT="${ODYSSEUS_ROOT:-$HOME/odysseus/odysseus}"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/odysseus-brain-shadow"
KEY_FILE="$STATE_DIR/api-key"
OVERLAY="$ROOT/docker-compose.brain-autonomous.yml"
ODYSSEUS_CONTAINER="odysseus-odysseus-1"
CHROMA_CONTAINER="odysseus-chromadb-1"

cd "$ROOT"
[[ -r "$KEY_FILE" ]] || {
  echo "ERROR: missing Brain API key at $KEY_FILE" >&2
  exit 1
}
export JARVIS_BRAIN_API_KEY
JARVIS_BRAIN_API_KEY="$(cat "$KEY_FILE")"

require_running() {
  local name="$1"
  if [[ "$(docker inspect -f '{{.State.Running}}' "$name" 2>/dev/null || true)" != "true" ]]; then
    echo "ERROR: required existing service is not running: $name" >&2
    echo "Brain startup deliberately does not start/recreate Odysseus or Chroma." >&2
    exit 1
  fi
}

require_running "$ODYSSEUS_CONTAINER"
require_running "$CHROMA_CONTAINER"

compose=(
  docker compose
  -f docker-compose.yml
  -f docker/gpu.nvidia.yml
  -f "$OVERLAY"
)

# Brain owns only its own containers. Recreating Odysseus here would kill
# Cookbook model servers that run as processes inside the Odysseus container.
"${compose[@]}" up -d --build --no-deps jarvis-brain
"${compose[@]}" up -d --build --no-deps jarvis-brain-worker

echo
echo "Autonomous Brain services started without touching Odysseus."
echo "LLM readiness:"
docker exec jarvis-brain-semantic-worker \
  python -m jarvis_brain.worker_daemon --probe-llm 2>&1 || true
echo
echo "Worker logs:"
docker logs --tail 30 jarvis-brain-semantic-worker 2>&1 || true
