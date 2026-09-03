#!/usr/bin/env bash
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [[ -z "$ROOT" || ! -f "$ROOT/ASSISTANT_VERSION" ]]; then
  echo "Run this from inside the consolidated assistant/Odysseus repository." >&2
  exit 2
fi

SOURCE_SERVER="$ROOT/assistant/mcp/reminders_server.py"
EVENTS_ENV="$ROOT/assistant/events/.env"
ODYSSEUS_ENV="$ROOT/.env"
MCP_NAME="Assistant Reminders"
MCP_RUNTIME_DIR_REL="assistant-reminders-mcp"
MCP_RUNTIME_PATH="/app/data/$MCP_RUNTIME_DIR_REL/reminders_server.py"


env_get() {
  local file="$1" key="$2"
  python3 - "$file" "$key" <<'PY'
from pathlib import Path
import sys
p = Path(sys.argv[1]); key = sys.argv[2]
if not p.is_file(): raise SystemExit(0)
for raw in p.read_text(encoding="utf-8").splitlines():
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line: continue
    k, v = line.split("=", 1)
    if k.strip() == key:
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in {'"', "'"}: v = v[1:-1]
        print(v); break
PY
}

odysseus_container() {
  docker ps \
    --filter 'label=com.docker.compose.service=odysseus' \
    --format '{{.ID}}' | head -n1
}

odysseus_project() {
  local cid="$1"
  docker inspect "$cid" --format '{{ index .Config.Labels "com.docker.compose.project" }}'
}

odysseus_network() {
  local cid="$1" project candidate
  project="$(odysseus_project "$cid")"
  candidate="${project}_default"
  if docker network inspect "$candidate" >/dev/null 2>&1; then
    printf '%s\n' "$candidate"
    return 0
  fi
  docker inspect "$cid" --format '{{range $name, $cfg := .NetworkSettings.Networks}}{{println $name}}{{end}}' | head -n1
}

host_data_dir() {
  local configured
  configured="$(env_get "$ODYSSEUS_ENV" APP_DATA_DIR)"
  [[ -n "$configured" ]] || configured="./data"
  python3 - "$ROOT" "$configured" <<'PY'
from pathlib import Path
import os, sys
root = Path(sys.argv[1])
raw = os.path.expanduser(sys.argv[2])
p = Path(raw)
if not p.is_absolute(): p = root / p
print(p.resolve())
PY
}

mcp_cli() {
  local cid="$1"; shift
  docker exec "$cid" python /app/scripts/odysseus-mcp "$@"
}

existing_mcp_id() {
  local cid="$1" raw
  raw="$(mcp_cli "$cid" list)"
  python3 - "$MCP_NAME" "$raw" <<'PY'
import json, sys
name = sys.argv[1]
try:
    rows = json.loads(sys.argv[2])
except Exception:
    raise SystemExit(0)
for row in rows if isinstance(rows, list) else []:
    if str(row.get("name", "")) == name:
        print(row.get("id", ""))
        break
PY
}

wait_odysseus() {
  local cid="$1" i
  for i in $(seq 1 45); do
    if docker exec -i "$cid" python - <<'PY' >/dev/null 2>&1
import urllib.request
urllib.request.urlopen("http://127.0.0.1:7000/api/health", timeout=2).read(1)
PY
    then
      return 0
    fi
    sleep 1
  done
  return 1
}

install_bridge() {
  [[ -f "$SOURCE_SERVER" ]] || { echo "Missing $SOURCE_SERVER" >&2; exit 1; }

  # Starting events here is intentional: it refreshes the event container onto
  # the same Docker network as Odysseus before the MCP bridge is registered.
  "$ROOT/events.sh" start

  local cid network data_dir runtime_dir api_key env_json old_id
  cid="$(odysseus_container)"
  [[ -n "$cid" ]] || { echo "Could not find a running Odysseus Docker container." >&2; exit 1; }
  network="$(odysseus_network "$cid")"
  [[ -n "$network" ]] || { echo "Could not determine the Odysseus Docker network." >&2; exit 1; }

  data_dir="$(host_data_dir)"
  runtime_dir="$data_dir/$MCP_RUNTIME_DIR_REL"
  mkdir -p "$runtime_dir"
  install -m 0644 "$SOURCE_SERVER" "$runtime_dir/reminders_server.py"

  api_key="$(env_get "$EVENTS_ENV" EVENTS_API_KEY)"
  [[ -n "$api_key" ]] || { echo "EVENTS_API_KEY is missing. Run ./events.sh setup." >&2; exit 1; }

  echo "Checking Odysseus -> assistant-events connectivity on Docker network: $network"
  docker exec -i "$cid" python - <<'PY'
import urllib.request
with urllib.request.urlopen("http://assistant-events:8780/health", timeout=5) as r:
    if r.status >= 400:
        raise SystemExit(f"assistant-events health returned HTTP {r.status}")
PY

  old_id="$(existing_mcp_id "$cid")"
  if [[ -n "$old_id" ]]; then
    echo "Refreshing existing '$MCP_NAME' MCP registration..."
    mcp_cli "$cid" delete "$old_id" >/dev/null
  fi

  env_json="$(python3 - "$api_key" <<'PY'
import json, sys
print(json.dumps({
    "ASSISTANT_EVENTS_URL": "http://assistant-events:8780",
    "ASSISTANT_EVENTS_API_KEY": sys.argv[1],
    "ASSISTANT_AGENT_ID": "main",
}))
PY
)"

  args_json='["/app/data/assistant-reminders-mcp/reminders_server.py"]'
  mcp_cli "$cid" add \
    --name "$MCP_NAME" \
    --transport stdio \
    --command python \
    --args "$args_json" \
    --env "$env_json" >/dev/null

  echo "Restarting Odysseus so its MCP manager loads the refreshed tool server..."
  docker restart "$cid" >/dev/null
  if ! wait_odysseus "$cid"; then
    echo "Odysseus did not become healthy after restart. Recent logs:" >&2
    docker logs --tail=120 "$cid" >&2 || true
    exit 1
  fi

  echo
  echo "✓ Assistant reminder MCP bridge installed."
  echo "✓ Runtime MCP source: $runtime_dir/reminders_server.py"
  echo "✓ Event service remains authoritative for scheduling/delivery."
  echo
  echo 'Test it through Qwen/Telegram with: Remind me in 10 seconds that the reminder tool works.'
}

show_status() {
  local cid
  cid="$(odysseus_container)"
  [[ -n "$cid" ]] || { echo "Odysseus container is not running." >&2; exit 1; }
  echo "Configured MCP entry:"
  mcp_cli "$cid" list | python3 -m json.tool
  echo
  echo "Recent Odysseus MCP log lines:"
  docker logs --tail=250 "$cid" 2>&1 | grep -Ei 'assistant reminders|assistant-reminders|mcp.*reminder' | tail -n20 || true
}

uninstall_bridge() {
  local cid old_id data_dir
  cid="$(odysseus_container)"
  [[ -n "$cid" ]] || { echo "Odysseus container is not running." >&2; exit 1; }
  old_id="$(existing_mcp_id "$cid")"
  if [[ -n "$old_id" ]]; then
    mcp_cli "$cid" delete "$old_id" >/dev/null
  fi
  data_dir="$(host_data_dir)"
  rm -rf "$data_dir/$MCP_RUNTIME_DIR_REL"
  docker restart "$cid" >/dev/null
  wait_odysseus "$cid" || true
  echo "Assistant reminder MCP bridge removed."
}

case "${1:-}" in
  install|start|connect) install_bridge ;;
  status) show_status ;;
  uninstall|remove) uninstall_bridge ;;
  *)
    cat <<'EOF_HELP'
Usage: ./reminders.sh COMMAND

Commands:
  install     Start/refresh events, install MCP bridge, register it, restart Odysseus
  status      Show configured MCP servers and recent reminder-MCP logs
  uninstall   Remove only the Assistant Reminders MCP registration/runtime copy
EOF_HELP
    exit 2 ;;
esac
