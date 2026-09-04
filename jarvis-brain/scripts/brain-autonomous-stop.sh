#!/usr/bin/env bash
set -euo pipefail

ROOT="${ODYSSEUS_ROOT:-$HOME/odysseus/odysseus}"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/odysseus-brain-shadow"
KEY_FILE="$STATE_DIR/api-key"
OVERLAY="$ROOT/docker-compose.brain-autonomous.yml"

cd "$ROOT"
export JARVIS_BRAIN_API_KEY=""
if [[ -r "$KEY_FILE" ]]; then
  JARVIS_BRAIN_API_KEY="$(cat "$KEY_FILE")"
fi

docker compose \
  -f docker-compose.yml \
  -f docker/gpu.nvidia.yml \
  -f "$OVERLAY" \
  stop jarvis-brain-worker
