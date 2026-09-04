#!/usr/bin/env bash
set -euo pipefail

ROOT="${ODYSSEUS_ROOT:-$HOME/odysseus/odysseus}"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/odysseus-brain-shadow"
KEY_FILE="$STATE_DIR/api-key"
OVERLAY="$ROOT/docker-compose.brain-autonomous.yml"
ODYSSEUS_CONTAINER="odysseus-odysseus-1"
ANALYZER_CONTAINER="jarvis-qwen-analyzer"
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
    exit 1
  fi
}

wait_healthy() {
  local name="$1"
  local timeout="${2:-300}"
  local deadline=$((SECONDS + timeout))
  local status=""

  while (( SECONDS < deadline )); do
    status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$name" 2>/dev/null || true)"
    case "$status" in
      healthy)
        return 0
        ;;
      unhealthy|exited|dead)
        echo "ERROR: $name readiness failed: $status" >&2
        docker logs --tail 100 "$name" >&2 || true
        return 1
        ;;
    esac
    sleep 2
  done

  echo "ERROR: timeout waiting for $name readiness (last=$status)" >&2
  docker logs --tail 100 "$name" >&2 || true
  return 1
}

require_running "$CHROMA_CONTAINER"

compose=(
  docker compose
  -f docker-compose.yml
  -f docker/gpu.nvidia.yml
  -f "$OVERLAY"
)

# Brain owns its analyzer, API, and worker independently of Odysseus.
"${compose[@]}" up -d --no-deps qwen-analyzer
wait_healthy "$ANALYZER_CONTAINER" 300
"${compose[@]}" up -d --build --no-deps jarvis-brain
"${compose[@]}" up -d --build --no-deps jarvis-brain-worker

# This proxy is only a compatibility bridge for components inside Odysseus
# that still use 127.0.0.1:8000. It may be recreated without touching Odysseus.
if [[ "$(docker inspect -f '{{.State.Running}}' "$ODYSSEUS_CONTAINER" 2>/dev/null || true)" == "true" ]]; then
  "${compose[@]}" up -d --build --no-deps --force-recreate qwen-loopback-proxy
fi

echo
echo "Autonomous Brain services started independently of Odysseus."
echo "Analyzer: $ANALYZER_CONTAINER"
echo "LLM readiness:"
docker exec jarvis-brain-semantic-worker python -m jarvis_brain.worker_daemon --probe-llm 2>&1 || true
echo
echo "Worker logs:"
docker logs --tail 30 jarvis-brain-semantic-worker 2>&1 || true
